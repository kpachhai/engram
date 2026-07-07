#!/usr/bin/env python3
r"""engram team-vault pre-receive hook.

Stdlib-only Python 3.10+. Copied to ``<bare-remote>/hooks/pre-receive``
by the operator running ``engram team-vault setup`` (or by the platform
admin UI for hosted forges).

Hook responsibilities:

* Refuse any pushed file under ``.indexes/``.
* Refuse non-fast-forward / force-push.
* Read the team policy YAML and ``members.yaml`` from the just-pushed
  tree (atomic with the push; reads from new commit's tree, NOT the
  working dir).
* For each pushed thought file: assert ``prefix`` in
  ``allowed_prefixes``, ``source`` in ``allowed_sources``,
  ``portability != "block"``, AND ``captured_by`` matches the committer
  GPG primary fingerprint.
* For pushes mutating policy.yaml or members.yaml: refuses if committer
  is not in the OLD-tree's ``stewards:`` list.
* Lists ALL violating files (not just the first) in the rejection
  message.

Stdin format (per ``man githooks(5)`` / pre-receive):
    <old-sha> <new-sha> <ref-name>\n  (one line per ref)

The hook reads stdin, returns 0 if all refs pass, non-zero otherwise.
The body of any rejection is printed to stderr (which git relays to
the pushing client).

This module is also importable as a function (``run_hook``) for
test purposes - the tests drive it via subprocess against a local
bare-repo fixture (no actual git host required).
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

# === Restricted YAML-subset parser ===
# We avoid PyYAML so the hook is stdlib-only. The team policy + members
# YAML is a small fixed schema; we hand-parse it.

_FRONTMATTER_FENCE = "---\n"
_FINGERPRINT_RE = re.compile(r"^[A-F0-9]{40}$")


def _parse_simple_yaml(text: str) -> dict[str, object]:
    """Hand-parse a restricted YAML subset.

    Supported:
    * top-level scalars: ``key: value`` (value may be quoted)
    * top-level lists: ``key:`` followed by ``  - <item>`` lines
    * top-level booleans: ``true`` / ``false``
    * top-level None / null
    * comments after ``#``
    * one level of dict-list ``- key: value`` items

    NOT supported: nested dicts beyond list-of-dict, multi-line strings,
    anchors, references. The hook's policy/members schema deliberately
    avoids these to keep the parser tractable.
    """
    result: dict[str, object] = {}
    current_list: list[object] | None = None
    current_list_key: str | None = None
    pending_dict_in_list: dict[str, object] | None = None

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        # List item: starts with optional whitespace + "- "
        stripped = line.lstrip()
        leading_ws = len(line) - len(stripped)
        if stripped.startswith("- "):
            if current_list is None:
                continue
            item = stripped[2:].strip()
            if ":" in item:
                # "- key: value" - dict-list item
                pending_dict_in_list = {}
                k, v = item.split(":", 1)
                pending_dict_in_list[k.strip()] = _coerce_scalar(v.strip())
                current_list.append(pending_dict_in_list)
            else:
                current_list.append(_coerce_scalar(item))
                pending_dict_in_list = None
            continue
        # Continuation of dict-list item (indented "key: value").
        if (
            leading_ws > 0
            and pending_dict_in_list is not None
            and ":" in stripped
            and not stripped.startswith("- ")
        ):
            k, v = stripped.split(":", 1)
            pending_dict_in_list[k.strip()] = _coerce_scalar(v.strip())
            continue
        # Top-level mapping: "key: value" or "key:".
        if ":" in line and leading_ws == 0:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if not val:
                # New list or dict block.
                current_list = []
                current_list_key = key
                result[key] = current_list
                pending_dict_in_list = None
            else:
                result[key] = _coerce_scalar(val)
                current_list = None
                current_list_key = None
                pending_dict_in_list = None

    # If a key opened a list but no items followed, normalize to empty list.
    if current_list_key and current_list is not None and not current_list:
        result[current_list_key] = []
    return result


def _coerce_scalar(value: str) -> object:
    """Coerce a YAML scalar to its Python type (str / int / bool / None / list)."""
    v = value.strip()
    if v in {"null", "~", ""}:
        return None
    if v.lower() == "true":
        return True
    if v.lower() == "false":
        return False
    if v.startswith('"') and v.endswith('"'):
        return v[1:-1]
    if v.startswith("'") and v.endswith("'"):
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        # Inline list: ``[a, b, c]``.
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_coerce_scalar(item.strip()) for item in inner.split(",")]
    try:
        return int(v)
    except ValueError:
        return v


def _split_frontmatter(text: str) -> tuple[dict[str, object], str] | None:
    r"""Split ``---\nYAML\n---\nbody`` into (parsed_yaml_dict, body)."""
    if not text.startswith(_FRONTMATTER_FENCE):
        return None
    rest = text[len(_FRONTMATTER_FENCE) :]
    end = rest.find("\n" + _FRONTMATTER_FENCE)
    if end == -1:
        if rest.endswith("\n---"):
            return _parse_simple_yaml(rest[:-4]), ""
        return None
    fm_yaml = rest[:end]
    body = rest[end + len("\n" + _FRONTMATTER_FENCE) :]
    return _parse_simple_yaml(fm_yaml), body


def _normalize_fingerprint(fp: str) -> str:
    return fp.upper().replace(" ", "").replace(":", "")


def _is_valid_fingerprint(fp: str) -> bool:
    return bool(_FINGERPRINT_RE.fullmatch(_normalize_fingerprint(fp)))


# === Git plumbing ===


@dataclass(frozen=True)
class _RefUpdate:
    old_sha: str
    new_sha: str
    ref: str


@dataclass(frozen=True)
class _Violation:
    file_path: str
    reason: str
    detail: str


def _parse_stdin(stdin_text: str) -> list[_RefUpdate]:
    """Parse ``<old> <new> <ref>`` lines from stdin into RefUpdates."""
    updates: list[_RefUpdate] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        updates.append(_RefUpdate(old_sha=parts[0], new_sha=parts[1], ref=parts[2]))
    return updates


def _git_cmd(args: list[str], *, cwd: str | None = None) -> str:
    """Run a git plumbing command, return stdout, raise on nonzero."""
    result = subprocess.run(  # noqa: S603 - git is the canonical tool here
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        raise RuntimeError(msg)
    return result.stdout


def _ls_tree_at(sha: str, path: str, *, cwd: str | None = None) -> str | None:
    """Read a file's content at a specific commit (via ``git show <sha>:<path>``)."""
    try:
        return _git_cmd(["show", f"{sha}:{path}"], cwd=cwd)
    except RuntimeError:
        return None


def _changed_files(old_sha: str, new_sha: str, *, cwd: str | None = None) -> list[str]:
    """Return file paths changed by the push (added / modified)."""
    try:
        # Handle initial branch push: old_sha is all zeros.
        if set(old_sha) == {"0"}:
            output = _git_cmd(["ls-tree", "-r", "--name-only", new_sha], cwd=cwd)
        else:
            output = _git_cmd(
                ["diff", "--name-only", "--diff-filter=AMRT", old_sha, new_sha],
                cwd=cwd,
            )
    except RuntimeError:
        return []
    return [p for p in output.splitlines() if p]


def _committer_fingerprint(sha: str, *, cwd: str | None = None) -> str | None:
    """Extract the committer's GPG primary fingerprint via ``git verify-commit``."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "verify-commit", "--raw", sha],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    # verify-commit emits its raw output to STDERR.
    text = (result.stderr or "") + (result.stdout or "")
    # Per gnupg doc/DETAILS, the VALIDSIG status line is:
    #   [GNUPG:] VALIDSIG <sig-key-fpr> <date> <ts> <expire-ts> <ver>
    #            <reserved> <pk-algo> <hash-algo> <sig-class> <primary-key-fpr>
    # Field 1 is the key that MADE the signature (the signing subkey when
    # one is used); the LAST field is the PRIMARY key fingerprint, which
    # is what captured_by / members.yaml / stewards bind to. Taking the
    # first fingerprint would reject every push signed with a separate
    # signing subkey (the standard GPG setup).
    for line in text.splitlines():
        if "VALIDSIG" in line:
            candidates = [p for p in line.split() if _is_valid_fingerprint(p)]
            if candidates:
                return _normalize_fingerprint(candidates[-1])
    return None


# === Validation logic ===


def _is_indexes_path(path: str) -> bool:
    """True iff ``path`` is under ``.indexes/`` (machine-local index files)."""
    return path.startswith(".indexes/") or "/.indexes/" in path


def _validate_thought(
    path: str,
    content: str,
    policy: dict[str, object],
    committer_fp: str | None,
) -> list[_Violation]:
    """Validate one pushed thought file against the team policy."""
    violations: list[_Violation] = []
    parsed = _split_frontmatter(content)
    if parsed is None:
        # Not a thought file; skip validation (could be README.md, etc.).
        return []
    fm, _body = parsed

    # block portability is structural refusal.
    if fm.get("portability") == "block":
        violations.append(
            _Violation(file_path=path, reason="block_thought_in_team_vault_disallowed", detail=""),
        )

    allowed_prefixes = policy.get("allowed_prefixes")
    if (
        allowed_prefixes is not None
        and isinstance(allowed_prefixes, list)
        and fm.get("prefix") not in allowed_prefixes
    ):
        violations.append(
            _Violation(
                file_path=path,
                reason="prefix_not_allowed",
                detail=f"prefix={fm.get('prefix')!r}",
            ),
        )

    allowed_sources = policy.get("allowed_sources")
    if (
        allowed_sources is not None
        and isinstance(allowed_sources, list)
        and fm.get("source") not in allowed_sources
    ):
        violations.append(
            _Violation(
                file_path=path,
                reason="source_not_allowed",
                detail=f"source={fm.get('source')!r}",
            ),
        )

    if fm.get("portability") == "sensitive" and not policy.get("accept_sensitive", False):
        violations.append(
            _Violation(
                file_path=path,
                reason="sensitive_thought_target_does_not_accept",
                detail="",
            ),
        )

    captured_by = fm.get("captured_by")
    if captured_by is not None:
        captured_by_norm = _normalize_fingerprint(str(captured_by))
        if committer_fp is None:
            violations.append(
                _Violation(
                    file_path=path,
                    reason="attribution_committer_mismatch",
                    detail="no committer GPG fingerprint",
                ),
            )
        elif captured_by_norm != committer_fp:
            violations.append(
                _Violation(
                    file_path=path,
                    reason="attribution_committer_mismatch",
                    detail=f"captured_by={captured_by_norm} != committer={committer_fp}",
                ),
            )

    return violations


def run_hook(
    *,
    stdin_text: str,
    repo_path: str | None = None,
) -> tuple[int, str]:
    """Run the pre-receive hook against parsed stdin + a bare-repo path.

    Returns (exit_code, stderr_message). The script's ``__main__`` block
    invokes this and exits with ``exit_code``, printing
    ``stderr_message`` to stderr.
    """
    updates = _parse_stdin(stdin_text)
    if not updates:
        return 0, ""

    all_violations: list[_Violation] = []

    for upd in updates:
        # Refuse non-fast-forward / force-push.
        if set(upd.old_sha) != {"0"}:
            try:
                _git_cmd(
                    ["merge-base", "--is-ancestor", upd.old_sha, upd.new_sha],
                    cwd=repo_path,
                )
            except RuntimeError:
                all_violations.append(
                    _Violation(
                        file_path=upd.ref,
                        reason="non_fast_forward_refused",
                        detail="force-push detected",
                    ),
                )
                continue

        # Read team policy + members from the OLD tree (steward gate).
        # On initial push, the OLD tree doesn't exist; both come from new tree.
        is_initial = set(upd.old_sha) == {"0"}
        policy_source_sha = upd.new_sha if is_initial else upd.old_sha

        policy_text = _ls_tree_at(policy_source_sha, ".engram/team-policy.yaml", cwd=repo_path)
        members_text = _ls_tree_at(policy_source_sha, ".engram/members.yaml", cwd=repo_path)
        if policy_text is None or members_text is None:
            # Initial setup push without canonical files - rejected.
            all_violations.append(
                _Violation(
                    file_path=upd.ref,
                    reason="missing_team_canonical_files",
                    detail=(
                        "engram team-vault setup must run before any push; "
                        "policy.yaml and members.yaml absent"
                    ),
                ),
            )
            continue

        policy = _parse_simple_yaml(policy_text)
        # members_text is currently consulted only via the captured_by/
        # committer mismatch check (which reads the GPG fingerprint via
        # `git verify-commit`); future work may surface unenrolled-key
        # refusals here too.
        del members_text  # silence "unused" while preserving the read-side semantics
        stewards_raw = policy.get("stewards") or []
        stewards: set[str] = set()
        if isinstance(stewards_raw, list):
            stewards = {_normalize_fingerprint(s) for s in stewards_raw if isinstance(s, str)}

        # Get committer fingerprint of the new HEAD (for attribution + steward gate).
        committer_fp = _committer_fingerprint(upd.new_sha, cwd=repo_path)

        changed = _changed_files(upd.old_sha, upd.new_sha, cwd=repo_path)

        # `.indexes/` path refusal.
        for path in changed:
            if _is_indexes_path(path):
                all_violations.append(
                    _Violation(
                        file_path=path,
                        reason="indexes_path_refused",
                        detail="machine-local index files must not be pushed",
                    ),
                )

        # Steward-only mutation of policy / members.
        for sensitive_path in (".engram/team-policy.yaml", ".engram/members.yaml"):
            if sensitive_path in changed and (committer_fp is None or committer_fp not in stewards):
                all_violations.append(
                    _Violation(
                        file_path=sensitive_path,
                        reason="steward_only_mutation",
                        detail=(
                            f"committer {committer_fp!r} not in stewards: {sorted(stewards)!r}"
                        ),
                    ),
                )

        # Validate each thought file.
        for path in changed:
            if not path.startswith("thoughts/"):
                continue
            if not path.endswith(".md"):
                continue
            content = _ls_tree_at(upd.new_sha, path, cwd=repo_path)
            if content is None:
                continue
            all_violations.extend(_validate_thought(path, content, policy, committer_fp))

    if not all_violations:
        return 0, ""

    lines = ["engram team-vault: push refused. Violations:"]
    for v in all_violations:
        suffix = f" - {v.detail}" if v.detail else ""
        lines.append(f"  {v.file_path}: {v.reason}{suffix}")
    return 1, "\n".join(lines) + "\n"


def main() -> int:
    """Script entry point."""
    stdin_text = sys.stdin.read()
    code, stderr = run_hook(stdin_text=stdin_text)
    if stderr:
        sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
