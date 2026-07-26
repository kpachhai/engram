"""Typed async wrapper around :func:`engram.utils.run_command.run_git`.

Every function in this module:

* Returns a typed dataclass (never a raw :class:`subprocess.CompletedProcess`).
* Wraps the sync :func:`run_git` call with :func:`asyncio.to_thread` so the
  coordinator's event loop is never blocked.
* Parses stderr against well-known patterns and classifies failures via
  :class:`GitErrorClass` so the state machine can decide between retry,
  permanent failure, or manual-resolution.

The classification table is deliberately conservative: anything that does
not match a known pattern is :data:`GitErrorClass.UNKNOWN`, and the state
machine treats UNKNOWN as a non-retryable failure (better to surface than
to silently retry and lose data).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from engram.utils.run_command import run_git

_log = logging.getLogger("engram.sync.gitops")


_FP40_RE = re.compile(r"^[A-Fa-f0-9]{40}$")

#: Default allow-list consulted by :func:`signed_pull_gate`.
DEFAULT_TRUSTED_KEYS_PATH = Path.home() / ".config" / "engram" / "trusted-keys.yaml"


class GitErrorClass(enum.StrEnum):
    """Classified git failure modes used by the sync coordinator.

    Categories chosen to drive specific recovery paths:

    * ``AUTH`` - never retry (creds will not improve on retry).
    * ``NETWORK_TRANSIENT`` - retry with backoff up to ``push_retry_count``.
    * ``NETWORK_PERMANENT`` - never retry; surface to the operator.
    * ``NON_FAST_FORWARD`` - run the reflog gate then attempt rebase + retry.
    * ``CONFLICT`` - transition to ``manual-resolution-required``.
    * ``LOCK_HELD`` - wait briefly, then retry once (another git invocation
      raced us; engram's coordinator owns its own asyncio.Lock so this
      should only happen if a human is running git concurrently).
    * ``SIGNATURE_UNVERIFIED`` - signed-pull gate refused; never retry
      (the remote head must be re-signed by a trusted key).
    * ``UNKNOWN`` - default fallback; treat as non-retryable.
    """

    OK = "ok"
    AUTH = "auth"
    NETWORK_TRANSIENT = "network_transient"
    NETWORK_PERMANENT = "network_permanent"
    NON_FAST_FORWARD = "non_fast_forward"
    CONFLICT = "conflict"
    LOCK_HELD = "lock_held"
    SIGNATURE_UNVERIFIED = "signature_unverified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StatusEntry:
    """One row of ``git status --porcelain=v1``.

    The XY two-character status code is split into ``index_status`` and
    ``worktree_status`` per the porcelain v1 grammar; ``path`` is the file
    relative to the repo root. Renames are not tracked (engram only ever
    writes new markdown files, never renames).
    """

    index_status: str
    worktree_status: str
    path: str


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Outcome of :func:`commit_paths`.

    ``failed`` is the explicit failure discriminator: ``sha`` alone cannot
    distinguish a failed commit from a successful one whose ``rev-parse``
    lookup failed.
    """

    sha: str | None
    message: str
    nothing_to_commit: bool = False
    failed: bool = False
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of :func:`fetch`."""

    error_class: GitErrorClass = GitErrorClass.OK
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class PullResult:
    """Outcome of :func:`pull_rebase`.

    ``conflicts`` is populated when the merge / rebase produced files with
    conflict markers; the coordinator inspects this list to decide whether
    to enter degraded mode.
    """

    error_class: GitErrorClass = GitErrorClass.OK
    stderr: str = ""
    conflicts: list[Path] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PushResult:
    """Outcome of :func:`push`."""

    error_class: GitErrorClass = GitErrorClass.OK
    stderr: str = ""


# Stderr fragment patterns (case-insensitive) drive classification.
_AUTH_PATTERNS = (
    re.compile(r"permission denied \(publickey", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"could not read username", re.IGNORECASE),
    re.compile(r"\b(401|403)\b"),
    re.compile(r"fatal: unable to access.*the requested url returned error: 40[13]", re.IGNORECASE),
)
_NETWORK_PERMANENT_PATTERNS = (
    re.compile(r"\b404\b"),
    re.compile(r"repository not found", re.IGNORECASE),
    re.compile(r"does not appear to be a git repository", re.IGNORECASE),
)
_NETWORK_TRANSIENT_PATTERNS = (
    re.compile(r"could not resolve host", re.IGNORECASE),
    re.compile(r"connection timed out", re.IGNORECASE),
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"network is unreachable", re.IGNORECASE),
    re.compile(r"\b5\d{2}\b"),
    re.compile(r"failed to send request", re.IGNORECASE),
)
_NON_FAST_FORWARD_PATTERNS = (
    re.compile(r"non[-\s]fast[-\s]forward", re.IGNORECASE),
    re.compile(r"rejected.*non-fast-forward", re.IGNORECASE),
    re.compile(r"updates were rejected", re.IGNORECASE),
    re.compile(r"failed to push some refs", re.IGNORECASE),
)
_CONFLICT_PATTERNS = (
    re.compile(r"\bCONFLICT \(", re.IGNORECASE),
    re.compile(r"merge conflict", re.IGNORECASE),
    re.compile(r"automatic merge failed", re.IGNORECASE),
    re.compile(r"could not apply", re.IGNORECASE),
)
_LOCK_HELD_PATTERNS = (
    re.compile(r"unable to create.*\.git/index\.lock", re.IGNORECASE),
    re.compile(r"another git process seems to be running", re.IGNORECASE),
)


def classify_stderr(stderr: str) -> GitErrorClass:
    """Map a git stderr blob to a :class:`GitErrorClass` value.

    Order matters: AUTH and NETWORK_PERMANENT win over NETWORK_TRANSIENT
    (a 404 inside an HTTPS clone surfaces both "404" and "failed to send
    request"; the permanent answer is the actionable one).
    """
    if not stderr:
        return GitErrorClass.OK
    for pat in _AUTH_PATTERNS:
        if pat.search(stderr):
            return GitErrorClass.AUTH
    for pat in _NETWORK_PERMANENT_PATTERNS:
        if pat.search(stderr):
            return GitErrorClass.NETWORK_PERMANENT
    for pat in _NON_FAST_FORWARD_PATTERNS:
        if pat.search(stderr):
            return GitErrorClass.NON_FAST_FORWARD
    for pat in _CONFLICT_PATTERNS:
        if pat.search(stderr):
            return GitErrorClass.CONFLICT
    for pat in _LOCK_HELD_PATTERNS:
        if pat.search(stderr):
            return GitErrorClass.LOCK_HELD
    for pat in _NETWORK_TRANSIENT_PATTERNS:
        if pat.search(stderr):
            return GitErrorClass.NETWORK_TRANSIENT
    return GitErrorClass.UNKNOWN


async def _git(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = 30.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run git in a worker thread; never raises by default."""
    return await asyncio.to_thread(
        run_git,
        args,
        cwd=cwd,
        timeout=timeout,
        check=check,
    )


async def is_inside_work_tree(cwd: Path) -> bool:
    """Return True when ``cwd`` is inside a non-bare git working tree."""
    cp = await _git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    return cp.returncode == 0 and cp.stdout.strip() == "true"


async def current_branch(cwd: Path) -> str | None:
    """Return the current branch name, or ``None`` if HEAD is detached."""
    cp = await _git(["symbolic-ref", "--short", "HEAD"], cwd=cwd)
    if cp.returncode != 0:
        return None
    name = cp.stdout.strip()
    return name or None


async def remote_url(cwd: Path, remote: str = "origin") -> str | None:
    """Return the configured URL for ``remote`` or ``None`` if unknown."""
    cp = await _git(["remote", "get-url", remote], cwd=cwd)
    if cp.returncode != 0:
        return None
    url = cp.stdout.strip()
    return url or None


async def default_remote_branch(cwd: Path, remote: str = "origin") -> str | None:
    """Return the branch ``refs/remotes/<remote>/HEAD`` points at, if any."""
    cp = await _git(
        ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"],
        cwd=cwd,
    )
    if cp.returncode != 0:
        return None
    raw = cp.stdout.strip()  # "<remote>/<branch>"
    prefix = f"{remote}/"
    if raw.startswith(prefix):
        return raw[len(prefix) :]
    return raw or None


async def git_version(cwd: Path) -> tuple[int, int, int]:
    """Return ``(major, minor, patch)`` for the resolved git binary.

    Falls back to ``(0, 0, 0)`` when parsing fails so callers compare with
    the ``>= (2, 40, 0)`` floor without raising.
    """
    cp = await _git(["--version"], cwd=cwd)
    if cp.returncode != 0:
        return (0, 0, 0)
    match = re.search(r"git version (\d+)\.(\d+)\.(\d+)", cp.stdout)
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


async def status_porcelain(cwd: Path) -> list[StatusEntry]:
    """Return parsed ``git status --porcelain=v1 -z`` rows.

    Uses the NUL-separated ``-z`` form so paths containing whitespace are
    handled correctly. Renames (``R<scor>``) split into source + dest; we
    return only the destination path (engram never renames so the case is
    unreachable in normal operation).
    """
    cp = await _git(["status", "--porcelain=v1", "-z"], cwd=cwd)
    if cp.returncode != 0:
        return []
    entries: list[StatusEntry] = []
    chunks = cp.stdout.split("\x00")
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        if not chunk:
            i += 1
            continue
        # XY status (2 chars) + space + path.
        if len(chunk) < 3:
            i += 1
            continue
        index_status = chunk[0]
        worktree_status = chunk[1]
        path = chunk[3:]
        entries.append(
            StatusEntry(
                index_status=index_status,
                worktree_status=worktree_status,
                path=path,
            )
        )
        # Rename rows are followed by their source path; skip it.
        if index_status in {"R", "C"} or worktree_status in {"R", "C"}:
            i += 2
        else:
            i += 1
    return entries


async def ahead_behind_count(
    cwd: Path,
    branch: str,
    remote: str = "origin",
) -> tuple[int, int]:
    """Return ``(ahead, behind)`` counts of ``branch`` vs ``<remote>/<branch>``.

    Returns ``(0, 0)`` when the upstream ref does not exist (e.g. brand-new
    branch with no upstream tracking).
    """
    cp = await _git(
        ["rev-list", "--left-right", "--count", f"{remote}/{branch}...{branch}"],
        cwd=cwd,
    )
    if cp.returncode != 0:
        return (0, 0)
    parts = cp.stdout.strip().split()
    if len(parts) != 2:
        return (0, 0)
    behind = int(parts[0])
    ahead = int(parts[1])
    return (ahead, behind)


async def commit_paths(
    cwd: Path,
    paths: Iterable[Path | str],
    *,
    message: str,
    user_email: str | None = None,
    user_name: str | None = None,
    no_verify: bool = True,
    allow_empty: bool = False,
) -> CommitResult:
    """Stage ``paths`` then commit; returns a :class:`CommitResult`.

    The resulting commit author/committer identity comes from the
    optional ``user_email`` + ``user_name`` overrides (passed via
    ``-c user.email=...``) so the per-vault identity per
    ``.engram/identity.local`` is honored even when global git config
    differs.

    When ``git status --porcelain`` shows no staged changes, returns
    ``CommitResult(sha=None, nothing_to_commit=True)`` without invoking
    the commit (defends against empty-commit pollution from rapid
    successive enqueues that all targeted the same file).
    """
    paths_str = [str(p) for p in paths]
    if not paths_str and not allow_empty:
        return CommitResult(sha=None, message=message, nothing_to_commit=True)

    if paths_str:
        add_cp = await _git(["add", "--", *paths_str], cwd=cwd)
        if add_cp.returncode != 0:
            _log.warning("git add failed: %s", add_cp.stderr.strip())
            return CommitResult(
                sha=None,
                message=message,
                nothing_to_commit=False,
                failed=True,
                stderr=add_cp.stderr,
            )

    if not allow_empty:
        # Bail before invoking git commit if nothing actually changed.
        diff_cp = await _git(["diff", "--cached", "--name-only"], cwd=cwd)
        if diff_cp.returncode == 0 and not diff_cp.stdout.strip():
            return CommitResult(sha=None, message=message, nothing_to_commit=True)

    args: list[str] = []
    if user_email is not None:
        args += ["-c", f"user.email={user_email}"]
    if user_name is not None:
        args += ["-c", f"user.name={user_name}"]
    args += ["commit", "-m", message]
    if no_verify:
        args.append("--no-verify")
    if allow_empty:
        args.append("--allow-empty")

    cp = await _git(args, cwd=cwd)
    if cp.returncode != 0:
        _log.warning("git commit failed: %s", cp.stderr.strip())
        return CommitResult(
            sha=None,
            message=message,
            nothing_to_commit=False,
            failed=True,
            stderr=cp.stderr,
        )

    sha_cp = await _git(["rev-parse", "HEAD"], cwd=cwd)
    sha = sha_cp.stdout.strip() if sha_cp.returncode == 0 else None
    return CommitResult(sha=sha, message=message, nothing_to_commit=False)


async def fetch(cwd: Path, remote: str = "origin", *, timeout: float = 60.0) -> FetchResult:
    """Run ``git fetch <remote>`` and classify any failure."""
    cp = await _git(["fetch", remote], cwd=cwd, timeout=timeout)
    if cp.returncode == 0:
        return FetchResult()
    return FetchResult(error_class=classify_stderr(cp.stderr), stderr=cp.stderr)


async def pull_rebase(
    cwd: Path,
    remote: str,
    branch: str,
    *,
    timeout: float = 60.0,
) -> PullResult:
    """Run ``git pull --rebase=true <remote> <branch>`` and classify failure.

    This re-fetches as part of the pull. Callers that have already fetched and
    verified a specific remote SHA should use :func:`rebase_onto` instead, so
    the commit that passed the gates is the commit they rebase onto.
    """
    cp = await _git(
        ["pull", "--rebase=true", "--no-edit", remote, branch],
        cwd=cwd,
        timeout=timeout,
    )
    error_class = GitErrorClass.OK if cp.returncode == 0 else classify_stderr(cp.stderr)
    return PullResult(error_class=error_class, stderr=cp.stderr)


async def rebase_onto(cwd: Path, target_sha: str, *, timeout: float = 60.0) -> PullResult:
    """Rebase the current branch onto ``target_sha`` without re-fetching.

    ``git pull --rebase`` performs its own fetch, so the commit that passed the
    ancestor and signature gates is not necessarily the commit being rebased
    onto. Rebasing a known SHA closes that window.

    On failure the in-progress rebase is aborted, so a conflicted rebase never
    leaves a detached HEAD and conflict markers in the markdown source of truth
    while the daemon keeps serving the vault.
    """
    cp = await _git(["rebase", target_sha], cwd=cwd, timeout=timeout)
    if cp.returncode == 0:
        return PullResult(error_class=GitErrorClass.OK, stderr=cp.stderr)
    abort = await _git(["rebase", "--abort"], cwd=cwd, timeout=timeout)
    if abort.returncode != 0:
        _log.warning(
            "rebase onto %s failed and `git rebase --abort` also failed: %s",
            target_sha,
            abort.stderr.strip(),
        )
    return PullResult(error_class=classify_stderr(cp.stderr), stderr=cp.stderr)


async def push(
    cwd: Path,
    remote: str,
    branch: str,
    *,
    force_with_lease: bool = False,
    lease_expect: str | None = None,
    timeout: float = 60.0,
    set_upstream: bool = False,
) -> PushResult:
    """Run ``git push`` and classify any failure.

    ``force_with_lease=True`` translates to ``--force-with-lease``;
    plain ``--force`` is never invoked from this module by design.

    Pass ``lease_expect`` with the remote SHA this machine actually verified.
    The bare lease form leases against whatever the remote-tracking ref happens
    to say at push time, so any unrelated ``git fetch`` in the vault (a terminal,
    an IDE autofetch, another engram process) silently re-arms it and the push
    can overwrite commits this machine never saw. Pinning the lease to the
    verified SHA makes the remote reject exactly that case.
    """
    args = ["push"]
    if force_with_lease:
        if lease_expect is not None:
            args.append(f"--force-with-lease=refs/heads/{branch}:{lease_expect}")
        else:
            args.append("--force-with-lease")
    if set_upstream:
        args.append("--set-upstream")
    args += [remote, branch]
    cp = await _git(args, cwd=cwd, timeout=timeout)
    if cp.returncode == 0:
        return PushResult()
    return PushResult(error_class=classify_stderr(cp.stderr), stderr=cp.stderr)


async def verify_commit(
    cwd: Path,
    ref: str,
    allowed_keys: Iterable[str],
) -> bool:
    """Return True iff the commit signature verifies against an allow-list.

    ``git verify-commit ref`` must succeed AND the signing key fingerprint
    must be in ``allowed_keys``. Falls back to False on any error (missing
    key, unknown key, gpg agent unreachable). Trusted-key verification
    is opt-in (off by default); callers that opt in must pass a
    non-empty allow-list.
    """
    allowed = {k.strip().upper() for k in allowed_keys if k.strip()}
    if not allowed:
        return False
    cp = await _git(["verify-commit", "--raw", ref], cwd=cwd)
    if cp.returncode != 0:
        return False
    # Real status lines look like "[GNUPG:] VALIDSIG <sig-key-fpr> ...
    # <primary-key-fpr>". Accept when EITHER 40-hex field (signing subkey
    # or its primary key) is on the allow-list; operators normally list
    # primary fingerprints, but a pinned subkey also verifies.
    for line in cp.stderr.splitlines():
        if "VALIDSIG" not in line:
            continue
        for token in line.split():
            if not _FP40_RE.fullmatch(token):
                continue
            fingerprint = token.upper()
            if fingerprint in allowed or fingerprint[-16:] in allowed:
                return True
    return False


def load_trusted_keys(path: Path | None = None) -> list[str]:
    """Load the fingerprint allow-list from ``trusted-keys.yaml``.

    Accepts either a top-level YAML list of fingerprints or a mapping
    with a ``trusted_keys:`` list. Returns ``[]`` when the file is
    missing or unparseable - callers treat an empty allow-list as a
    refusal when ``signed_pull_required`` is on (fail closed).
    """
    resolved = path or DEFAULT_TRUSTED_KEYS_PATH
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        from ruamel.yaml import YAML

        data = YAML(typ="safe", pure=True).load(text)
    except Exception:
        _log.warning("could not parse trusted-keys file at %s", resolved)
        return []
    if isinstance(data, dict):
        data = data.get("trusted_keys")
    if not isinstance(data, list):
        return []
    return [str(fp).strip() for fp in data if isinstance(fp, str) and str(fp).strip()]


async def signed_pull_gate(
    cwd: Path,
    *,
    remote: str,
    branch: str,
    signed_pull_required: bool,
    trusted_keys_path: Path | None = None,
    timeout: float = 60.0,
) -> str | None:
    """Return a refusal reason when signed-pull verification blocks the pull.

    Fetches the remote head and verifies its GPG signature against the
    trusted-keys allow-list BEFORE any rebase touches the working tree.
    Returns ``None`` when the pull may proceed: gate off, verification
    passed, or no remote branch exists yet.
    """
    if not signed_pull_required:
        return None
    allowed = load_trusted_keys(trusted_keys_path)
    if not allowed:
        return (
            "signed_pull_required=true but the trusted-keys allow-list is "
            "missing or empty; refusing pull"
        )
    fetch_result = await fetch(cwd, remote, timeout=timeout)
    if fetch_result.error_class is not GitErrorClass.OK:
        return f"fetch for signature verification failed: {fetch_result.stderr.strip()[:200]}"
    cp = await _git(["rev-parse", "--verify", f"refs/remotes/{remote}/{branch}"], cwd=cwd)
    if cp.returncode != 0:
        return None
    remote_head = cp.stdout.strip()
    if await verify_commit(cwd, remote_head, allowed):
        return None
    return (
        f"remote head {remote_head[:8]} is not signed by a key on the "
        "trusted-keys allow-list; refusing pull"
    )


# === Conflict marker scanner (Step 5) ===

_CONFLICT_MARKER_RE = re.compile(
    r"(^<<<<<<< )|(^>>>>>>> )",
    re.MULTILINE,
)


def conflict_marker_scan(thoughts_dir: Path) -> list[Path]:
    """Return markdown files that contain conflict markers anywhere.

    A file is flagged when it contains BOTH a ``<<<<<<<`` line AND a
    ``>>>>>>>`` line (paired, matching git's own merge output). The bare
    ``=======`` line alone is NOT a flag - it appears in legitimate
    markdown horizontal-rule contexts and would produce false positives.

    Whole-file scan is intentional: markdown frontmatter, body, and trailing
    appendices can all hold a marker, and the security cost of skipping a
    range outweighs the IO savings on engram's typical <2 KB thoughts.
    """
    base = Path(thoughts_dir)
    if not base.exists():
        return []
    found: list[Path] = []
    for path in base.rglob("*.md"):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        has_open = False
        has_close = False
        for match in _CONFLICT_MARKER_RE.finditer(content):
            if match.group(1):
                has_open = True
            elif match.group(2):
                has_close = True
        if has_open and has_close:
            found.append(path)
    return sorted(found)


__all__ = [
    "CommitResult",
    "FetchResult",
    "GitErrorClass",
    "PullResult",
    "PushResult",
    "StatusEntry",
    "ahead_behind_count",
    "classify_stderr",
    "commit_paths",
    "conflict_marker_scan",
    "current_branch",
    "default_remote_branch",
    "fetch",
    "git_version",
    "is_inside_work_tree",
    "load_trusted_keys",
    "pull_rebase",
    "push",
    "rebase_onto",
    "remote_url",
    "signed_pull_gate",
    "status_porcelain",
    "verify_commit",
]
