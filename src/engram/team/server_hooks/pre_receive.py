#!/usr/bin/env python3
r"""engram team-vault pre-receive hook.

Stdlib-only Python 3.10+. Copied to ``<bare-remote>/hooks/pre-receive``
by the operator running ``engram team-vault setup`` (or by the platform
admin UI for hosted forges).

Hook responsibilities:

* Refuse any pushed file under ``.indexes/``.
* Refuse non-fast-forward / force-push.
* Read the team policy YAML and ``members.yaml`` from the canonical state
  already in the repository (``HEAD``, i.e. the remote's default branch),
  never from the tree being pushed - otherwise the pusher supplies the
  rules they are judged against. Only a repository with no commits at all
  may seed policy from the push itself.
* For each pushed thought file: assert ``prefix`` in
  ``allowed_prefixes``, ``source`` in ``allowed_sources``,
  ``portability != "block"``, AND ``captured_by`` is PRESENT and matches
  the committer GPG primary fingerprint.
* Enforce membership per commit: every commit the push introduces must be
  signed by an enrolled, non-revoked member - including commits that touch
  no thought file, and commits between the base and the tip.
* Validate every commit in the pushed range, not just its endpoints, so
  content added and deleted within one push is still gated (its blobs stay
  reachable in the shared remote either way).
* For pushes mutating OR deleting policy.yaml / members.yaml: refuses if
  the committer is not in the canonical ``stewards:`` list.
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
_FINGERPRINT_RE = re.compile(r"^[A-F0-9]{40}$")  # vocab-allow: hex char class


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


#: YAML 1.1 boolean spellings. PyYAML (used by the client-side policy model)
#: reads all of these as booleans, so the hook must agree: ``accept_sensitive: no``
#: coerced to the truthy string ``'no'`` disables the sensitive-thought gate on the
#: server while the client believes it is switched off.
_YAML_TRUE = frozenset({"true", "yes", "on", "y"})
_YAML_FALSE = frozenset({"false", "no", "off", "n"})


def _coerce_scalar(value: str) -> object:
    """Coerce a YAML scalar to its Python type (str / int / bool / None / list)."""
    v = value.strip()
    if v in {"null", "~", ""}:
        return None
    if v.lower() in _YAML_TRUE:
        return True
    if v.lower() in _YAML_FALSE:
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
    """Parse ``<old> <new> <ref>`` lines from stdin into RefUpdates.

    Splits on ASCII space only. git forbids space in a ref name but permits
    non-ASCII bytes, and ``str.split()`` also separates on Unicode whitespace
    (U+00A0, U+0085, ...), so a ref name containing U+00A0 would yield four
    fields and be dropped - skipping every check for that ref.

    Raises:
        ValueError: a non-empty line that is not three space-separated fields.
            Silently ignoring it is the same bypass by another route.
    """
    updates: list[_RefUpdate] = []
    for raw_line in stdin_text.split("\n"):
        line = raw_line.rstrip("\r")
        if not line:
            continue
        parts = line.split(" ")
        if len(parts) != 3 or not all(parts):
            msg = f"malformed pre-receive stdin line: {line!r}"
            raise ValueError(msg)
        updates.append(_RefUpdate(old_sha=parts[0], new_sha=parts[1], ref=parts[2]))
    return updates


#: Wall-clock cap on every git call this hook makes. Same value the rest of
#: the repo puts on local git operations (``engram.sync.gitops``). A hook that
#: hangs holds the push open indefinitely for every member of the vault, and a
#: hang is indistinguishable from a slow network from the pusher's side; a cap
#: turns it into a refusal, which is the safe direction for a gate.
_GIT_TIMEOUT_SECONDS = 30.0


def _git_cmd(args: list[str], *, cwd: str | None = None) -> str:
    """Run a git plumbing command, return stdout, raise on nonzero or on timeout."""
    try:
        result = subprocess.run(  # noqa: S603 - git is the canonical tool here
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        raise RuntimeError(msg) from exc
    if result.returncode != 0:
        msg = f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        raise RuntimeError(msg)
    return result.stdout


def _ls_tree_at(sha: str, path: str, *, cwd: str | None = None) -> str | None:
    """Read a file's content at a commit. ``None`` only when the path is absent there.

    Absence is established with ``ls-tree``, which exits 0 and prints nothing for
    a path that is not in the tree, and nonzero for a sha it cannot read. That
    separation is the point: this used to wrap ``git show`` in a bare
    ``except RuntimeError`` and return ``None`` for every failure alike, and two
    callers read ``None`` as "nothing here to validate" and moved on. A timeout
    or an unreadable object therefore let a thought file through the gate
    unchecked. Everything that is not a genuine absence now raises, so it
    reaches a caller that can refuse the push instead of skipping it.
    """
    listing = _git_cmd(["ls-tree", "--name-only", sha, "--", path], cwd=cwd)
    if not listing.strip():
        return None
    return _git_cmd(["show", f"{sha}:{path}"], cwd=cwd)


def _changed_files(
    old_sha: str,
    new_sha: str,
    *,
    cwd: str | None = None,
    include_deletions: bool = False,
) -> list[str]:
    r"""Return file paths changed by the push.

    Uses ``-z`` (NUL-delimited) output. Without it git applies ``core.quotePath``
    and renders a non-ASCII path as ``"thoughts/caf\303\251.md"`` - quotes and
    all - which then fails every ``startswith("thoughts/")`` check and skips
    validation for that file entirely.

    ``include_deletions`` adds removed paths, which the steward gate needs:
    deleting ``members.yaml`` must be as gated as modifying it.
    """
    diff_filter = "AMRTD" if include_deletions else "AMRT"
    try:
        # Handle initial branch push: old_sha is all zeros.
        if set(old_sha) == {"0"}:
            output = _git_cmd(["ls-tree", "-r", "-z", "--name-only", new_sha], cwd=cwd)
        else:
            output = _git_cmd(
                ["diff", "-z", "--name-only", f"--diff-filter={diff_filter}", old_sha, new_sha],
                cwd=cwd,
            )
    except RuntimeError:
        return []
    return [p for p in output.split("\0") if p]


#: gpg's machine-readable status lines are prefixed with this marker.
_GNUPG_STATUS_PREFIX = "[GNUPG:] "


def _status_payload(line: str) -> str | None:
    """Return the status keyword + args of a gpg status line, else None.

    Anchoring on the prefix is what separates a real status line from free-form
    text (notably a key UID) that merely contains a status keyword.
    """
    stripped = line.strip()
    if not stripped.startswith(_GNUPG_STATUS_PREFIX):
        return None
    return stripped[len(_GNUPG_STATUS_PREFIX) :].strip()


def _commits_in_range(old_sha: str, new_sha: str, *, cwd: str | None = None) -> list[str]:
    """Return the commits this push actually introduces.

    For a newly created ref, "new" means not reachable from any existing ref,
    so an existing branch's history is not re-validated. Endpoint diffing alone
    cannot see content that a later commit in the same push removes.
    """
    args = (
        ["rev-list", new_sha, "--not", "--all"]
        if set(old_sha) == {"0"}
        else ["rev-list", f"{old_sha}..{new_sha}"]
    )
    try:
        output = _git_cmd(args, cwd=cwd)
    except RuntimeError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _commit_changed_files(
    commit: str,
    *,
    cwd: str | None = None,
    include_deletions: bool = False,
) -> list[str]:
    """Return paths touched by a single commit (NUL-delimited, unquoted)."""
    diff_filter = "AMRTD" if include_deletions else "AMRT"
    try:
        output = _git_cmd(
            [
                "diff-tree",
                "-z",
                "--no-commit-id",
                "--name-only",
                "-r",
                "--root",
                f"--diff-filter={diff_filter}",
                commit,
            ],
            cwd=cwd,
        )
    except RuntimeError:
        return []
    return [p for p in output.split("\0") if p]


def _existing_head_sha(*, cwd: str | None = None) -> str | None:
    """Return the commit holding the repository's canonical team state.

    Prefers ``HEAD`` (on a bare remote this tracks the default branch). Falls
    back to the first existing branch so a repository that has branches but an
    unresolvable HEAD still has a trust root rather than deferring to the tree
    being pushed. None means a genuinely empty repository (bootstrap).
    """
    try:
        output = _git_cmd(["rev-parse", "--verify", "HEAD"], cwd=cwd)
    except RuntimeError:
        output = ""
    if output.strip():
        return output.strip()
    try:
        refs = _git_cmd(
            ["for-each-ref", "--format=%(objectname)", "--count=1", "refs/heads/"],
            cwd=cwd,
        )
    except RuntimeError:
        return None
    return refs.strip() or None


def _committer_fingerprint(sha: str, *, cwd: str | None = None) -> str | None:
    """Extract the committer's GPG primary fingerprint via ``git verify-commit``."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "verify-commit", "--raw", sha],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        # verify-commit shells out to gpg, which blocks indefinitely on an
        # unreachable agent. No answer is not an authorizing answer: returning
        # None makes the caller record "no verifiable GPG signature".
        return None
    # A failed verification must never yield an authorizing identity.
    if result.returncode != 0:
        return None
    # verify-commit emits its raw output to STDERR.
    text = (result.stderr or "") + (result.stdout or "")
    lines = text.splitlines()
    # gpg emits VALIDSIG alongside REVKEYSIG / EXPKEYSIG, so the presence of
    # VALIDSIG alone does not mean the key is currently usable.
    for line in lines:
        status = _status_payload(line)
        if status is not None and status.split(" ", 1)[0] in {"REVKEYSIG", "EXPKEYSIG"}:
            return None
    # Per gnupg doc/DETAILS, the VALIDSIG status line is:
    #   [GNUPG:] VALIDSIG <sig-key-fpr> <date> <ts> <expire-ts> <ver>
    #            <reserved> <pk-algo> <hash-algo> <sig-class> <primary-key-fpr>
    # Field 1 is the key that MADE the signature (the signing subkey when
    # one is used); the LAST field is the PRIMARY key fingerprint, which
    # is what captured_by / members.yaml / stewards bind to. Taking the
    # first fingerprint would reject every push signed with a separate
    # signing subkey (the standard GPG setup).
    #
    # The match is anchored to the status-line prefix: a bare ``"VALIDSIG" in
    # line`` also matches the GOODSIG line, whose trailing field is the
    # attacker-controlled key UID, letting a crafted UID supply the principal.
    for line in lines:
        status = _status_payload(line)
        if status is None or not status.startswith("VALIDSIG "):
            continue
        candidates = [p for p in status.split() if _is_valid_fingerprint(p)]
        if candidates:
            return _normalize_fingerprint(candidates[-1])
    return None


# === Validation logic ===


def _extract_membership(members: dict[str, object]) -> tuple[set[str], set[str]]:
    """Return (enrolled, revoked) fingerprint sets from parsed members.yaml.

    Enrolled excludes revoked, mirroring ``MembersList.is_enrolled`` on the
    client side - the server layer is canonical at push time (invariant 4).
    """
    revoked: set[str] = set()
    raw_revoked = members.get("revoked")
    if isinstance(raw_revoked, list):
        revoked = {
            _normalize_fingerprint(fp)
            for fp in raw_revoked
            if isinstance(fp, str) and _is_valid_fingerprint(fp)
        }
    enrolled: set[str] = set()
    raw_members = members.get("members")
    if isinstance(raw_members, list):
        for entry in raw_members:
            fp: object = entry.get("fingerprint") if isinstance(entry, dict) else entry
            if isinstance(fp, str) and _is_valid_fingerprint(fp):
                enrolled.add(_normalize_fingerprint(fp))
    return enrolled - revoked, revoked


def _is_indexes_path(path: str) -> bool:
    """True iff ``path`` is under ``.indexes/`` (machine-local index files)."""
    return path.startswith(".indexes/") or "/.indexes/" in path


def _validate_thought(
    path: str,
    content: str,
    policy: dict[str, object],
    committer_fp: str | None,
    enrolled: set[str],
    revoked: set[str],
) -> list[_Violation]:
    """Validate one pushed thought file against the team policy."""
    violations: list[_Violation] = []
    parsed = _split_frontmatter(content)
    if parsed is None:
        # Not a thought file; skip validation (could be README.md, etc.).
        return []
    fm, _body = parsed
    # Compare portability case-folded: an exact match lets `portability: BLOCK`
    # through a gate whose whole job is to keep block content out.
    raw_portability = fm.get("portability")
    portability = raw_portability.strip().lower() if isinstance(raw_portability, str) else None

    # block portability is structural refusal.
    if portability == "block":
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

    if portability == "sensitive" and policy.get("accept_sensitive") is not True:
        violations.append(
            _Violation(
                file_path=path,
                reason="sensitive_thought_target_does_not_accept",
                detail="",
            ),
        )

    # Server-canonical identity gate (invariants 4+5): every team-vault
    # thought must be signed by an enrolled, non-revoked member.
    if committer_fp is None:
        violations.append(
            _Violation(
                file_path=path,
                reason="attribution_committer_mismatch",
                detail="no committer GPG fingerprint",
            ),
        )
    elif committer_fp in revoked:
        violations.append(
            _Violation(
                file_path=path,
                reason="team_membership_revoked",
                detail=f"committer={committer_fp}",
            ),
        )
    elif committer_fp not in enrolled:
        violations.append(
            _Violation(
                file_path=path,
                reason="team_member_not_enrolled",
                detail=f"committer={committer_fp}",
            ),
        )

    # captured_by is REQUIRED: a missing field is the hand-edited /
    # pre-team-client bypass this hook exists to reject, not a pass.
    captured_by = fm.get("captured_by")
    if captured_by is None:
        violations.append(
            _Violation(
                file_path=path,
                reason="attribution_committer_mismatch",
                detail="captured_by missing; team-vault thoughts require GPG attribution",
            ),
        )
    elif committer_fp is not None:
        captured_by_norm = _normalize_fingerprint(str(captured_by))
        if captured_by_norm != committer_fp:
            violations.append(
                _Violation(
                    file_path=path,
                    reason="attribution_committer_mismatch",
                    detail=f"captured_by={captured_by_norm} != committer={committer_fp}",
                ),
            )

    return violations


def _validate_range(
    upd: _RefUpdate,
    *,
    repo_path: str | None,
    policy: dict[str, object],
    enrolled: set[str],
    revoked: set[str],
    stewards: set[str],
) -> list[_Violation]:
    """Validate every commit the push introduces, not just the range endpoints.

    Covers three gaps in endpoint diffing: content added and deleted within the
    same push (whose blobs still reach the shared remote), commits between the
    base and the tip whose signatures were never checked, and pushes that touch
    no thought file at all and so met no identity requirement.
    """
    violations: list[_Violation] = []
    commits = _commits_in_range(upd.old_sha, upd.new_sha, cwd=repo_path)
    if not commits:
        return violations

    for commit in commits:
        commit_fp = _committer_fingerprint(commit, cwd=repo_path)
        # Identity is required per commit, independent of what it touches.
        if commit_fp is None:
            violations.append(
                _Violation(
                    file_path=commit,
                    reason="attribution_committer_mismatch",
                    detail="commit carries no verifiable GPG signature",
                ),
            )
        elif commit_fp in revoked:
            violations.append(
                _Violation(
                    file_path=commit,
                    reason="team_membership_revoked",
                    detail=f"committer={commit_fp}",
                ),
            )
        elif commit_fp not in enrolled:
            violations.append(
                _Violation(
                    file_path=commit,
                    reason="team_member_not_enrolled",
                    detail=f"committer={commit_fp}",
                ),
            )

        touched = _commit_changed_files(commit, cwd=repo_path)
        touched_with_deletions = _commit_changed_files(
            commit, cwd=repo_path, include_deletions=True
        )

        for path in touched_with_deletions:
            if _is_indexes_path(path):
                violations.append(
                    _Violation(
                        file_path=path,
                        reason="indexes_path_refused",
                        detail="machine-local index files must not be pushed",
                    ),
                )
            if path in (".engram/team-policy.yaml", ".engram/members.yaml") and (
                commit_fp is None or commit_fp not in stewards
            ):
                violations.append(
                    _Violation(
                        file_path=path,
                        reason="steward_only_mutation",
                        detail=f"committer {commit_fp!r} not in stewards: {sorted(stewards)!r}",
                    ),
                )

        for path in touched:
            if not path.startswith("thoughts/") or not path.endswith(".md"):
                continue
            try:
                content = _ls_tree_at(commit, path, cwd=repo_path)
            except RuntimeError as exc:
                # Unreadable is not absent. Skipping here would pass the file
                # through unvalidated, so it is refused with the git error.
                violations.append(
                    _Violation(
                        file_path=path,
                        reason="thought_content_unreadable",
                        detail=f"could not read {path} at {commit}: {exc}",
                    ),
                )
                continue
            if content is None:
                continue
            violations.extend(
                _validate_thought(path, content, policy, commit_fp, enrolled, revoked)
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
    try:
        updates = _parse_stdin(stdin_text)
    except ValueError as exc:
        # Unparseable input is refused rather than skipped: a line the parser
        # cannot read is a ref update that would otherwise go unchecked.
        return 1, f"engram team-vault: push refused. {exc}\n"
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

        # Read team policy + members from the canonical state already in the
        # repository, never from the tree being pushed - otherwise the pusher
        # supplies the rules they are judged against. ``old_sha`` is all zeros
        # for ANY newly created ref, so falling back to the new tree there would
        # let a throwaway branch reset the trust root; only a repository with no
        # commits at all (genuine bootstrap) may seed policy from the push.
        policy_source_sha = upd.old_sha
        if set(upd.old_sha) == {"0"}:
            policy_source_sha = _existing_head_sha(cwd=repo_path) or upd.new_sha

        try:
            policy_text = _ls_tree_at(policy_source_sha, ".engram/team-policy.yaml", cwd=repo_path)
            members_text = _ls_tree_at(policy_source_sha, ".engram/members.yaml", cwd=repo_path)
        except RuntimeError as exc:
            # Unreadable canonical state is not absent canonical state. Both
            # refuse, but say which: a push refused for a git failure should not
            # send the operator looking for a setup step they already ran.
            all_violations.append(
                _Violation(
                    file_path=upd.ref,
                    reason="team_canonical_files_unreadable",
                    detail=f"could not read policy at {policy_source_sha}: {exc}",
                ),
            )
            continue
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
        enrolled, revoked = _extract_membership(_parse_simple_yaml(members_text))
        stewards_raw = policy.get("stewards") or []
        stewards: set[str] = set()
        if isinstance(stewards_raw, list):
            stewards = {_normalize_fingerprint(s) for s in stewards_raw if isinstance(s, str)}

        # Get committer fingerprint of the new HEAD (for attribution + steward gate).
        committer_fp = _committer_fingerprint(upd.new_sha, cwd=repo_path)

        changed = _changed_files(upd.old_sha, upd.new_sha, cwd=repo_path)
        # Deletions are excluded from `changed` because the thought validator
        # needs file content, but removing a canonical file is a mutation the
        # steward gate must still see.
        changed_with_deletions = _changed_files(
            upd.old_sha,
            upd.new_sha,
            cwd=repo_path,
            include_deletions=True,
        )

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
            if sensitive_path in changed_with_deletions and (
                committer_fp is None or committer_fp not in stewards
            ):
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
            try:
                content = _ls_tree_at(upd.new_sha, path, cwd=repo_path)
            except RuntimeError as exc:
                all_violations.append(
                    _Violation(
                        file_path=path,
                        reason="thought_content_unreadable",
                        detail=f"could not read {path} at {upd.new_sha}: {exc}",
                    ),
                )
                continue
            if content is None:
                continue
            all_violations.extend(
                _validate_thought(path, content, policy, committer_fp, enrolled, revoked)
            )

        # Per-commit pass over the range. The endpoint diff above cannot see
        # content a later commit in the same push removes, and it attributes the
        # whole range to the tip signer.
        all_violations.extend(
            _validate_range(
                upd,
                repo_path=repo_path,
                policy=policy,
                enrolled=enrolled,
                revoked=revoked,
                stewards=stewards,
            )
        )

    if not all_violations:
        return 0, ""

    # The endpoint pass and the per-commit pass overlap on files present at both;
    # report each distinct problem once.
    seen: set[tuple[str, str]] = set()
    unique: list[_Violation] = []
    for violation in all_violations:
        key = (violation.file_path, violation.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(violation)

    lines = ["engram team-vault: push refused. Violations:"]
    for v in unique:
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
