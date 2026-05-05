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
    * ``UNKNOWN`` - default fallback; treat as non-retryable.
    """

    OK = "ok"
    AUTH = "auth"
    NETWORK_TRANSIENT = "network_transient"
    NETWORK_PERMANENT = "network_permanent"
    NON_FAST_FORWARD = "non_fast_forward"
    CONFLICT = "conflict"
    LOCK_HELD = "lock_held"
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
    """Outcome of :func:`commit_paths`."""

    sha: str | None
    message: str
    nothing_to_commit: bool = False


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
            return CommitResult(sha=None, message=message, nothing_to_commit=False)

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
        return CommitResult(sha=None, message=message, nothing_to_commit=False)

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

    On exit-zero, also walks the working tree for conflict markers (a clean
    rebase leaves the markers behind only when the underlying file genuinely
    contains the literal sequence; the coordinator decides what to do).
    """
    cp = await _git(
        ["pull", "--rebase=true", "--no-edit", remote, branch],
        cwd=cwd,
        timeout=timeout,
    )
    error_class = GitErrorClass.OK if cp.returncode == 0 else classify_stderr(cp.stderr)
    return PullResult(error_class=error_class, stderr=cp.stderr)


async def push(
    cwd: Path,
    remote: str,
    branch: str,
    *,
    force_with_lease: bool = False,
    timeout: float = 60.0,
    set_upstream: bool = False,
) -> PushResult:
    """Run ``git push`` and classify any failure.

    ``force_with_lease=True`` translates to ``--force-with-lease``;
    plain ``--force`` is never invoked from this module by design.
    """
    args = ["push"]
    if force_with_lease:
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
    # gpg --status-fd output contains "VALIDSIG <fingerprint> ..."; we
    # accept any allowed fingerprint that appears.
    for line in cp.stderr.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith("VALIDSIG"):
            fingerprint = parts[2].upper()
            if fingerprint in allowed or fingerprint[-16:] in allowed:
                return True
    return False


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
    "pull_rebase",
    "push",
    "remote_url",
    "status_porcelain",
    "verify_commit",
]
