"""Pre-serve safety probes covering R-H/M risks from PHASE_2_PLAN.

Phase 2 Step 11 deliverable. Each probe maps 1:1 to a doctor check code
from :mod:`engram.diagnostics.check_codes`. The same probe logic is also
re-used by :mod:`engram.diagnostics.doctor` (Step 13) so a single set of
checks surfaces both at startup (FAIL refuses to serve) and under
``engram doctor`` (operator inspection).

The :func:`run_startup_probes` aggregate runs every probe in a
predictable order and returns a :class:`ProbeReport`. Per-cycle re-checks
of probes 7 (``branch_alignment``) and 11 (``vault_identity_remote_match``)
also run before every push so mid-session admin changes do not leak.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from engram.config.models import SyncConfig
from engram.diagnostics import check_codes
from engram.errors import ConfigError
from engram.sync import gitops
from engram.sync.identity import (
    IDENTITY_FILE_RELATIVE,
    Mismatch,
    MissingIdentity,
    check_identity,
)
from engram.utils.run_command import run_git

_log = logging.getLogger("engram.sync.startup_probes")

#: Floor for the git binary (per R-M11). Older releases lack
#: --force-with-lease semantics + symbolic-ref refresh contracts that
#: the coordinator depends on.
GIT_VERSION_FLOOR: tuple[int, int, int] = (2, 40, 0)

#: Cloud-sync directory hints (case-insensitive substring match).
_CLOUD_SYNC_HINTS: tuple[str, ...] = (
    "Dropbox",
    "iCloud Drive",
    "Library/CloudStorage",
    "OneDrive",
    "Google Drive",
    "Box Sync",
    "pCloud",
    "MEGA",
)

#: Required entries (substrings) in ``.gitignore``.
_REQUIRED_GITIGNORE_PATTERNS: tuple[str, ...] = (
    ".indexes/",
    "*.sqlite",
)


@dataclass(frozen=True, slots=True)
class ProbeFailure:
    """A FAIL-level probe outcome."""

    code: str
    message: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeWarning:
    """A WARN-level probe outcome."""

    code: str
    message: str
    detail: str | None = None


@dataclass(slots=True)
class ProbeReport:
    """Aggregated startup-probe result.

    Construction is mutable (probes append as they run); after
    :func:`run_startup_probes` returns, treat as immutable.
    """

    failures: list[ProbeFailure] = field(default_factory=list)
    warnings: list[ProbeWarning] = field(default_factory=list)

    def add_fail(self, code: str, message: str, detail: str | None = None) -> None:
        """Append a FAIL outcome."""
        self.failures.append(ProbeFailure(code=code, message=message, detail=detail))

    def add_warn(self, code: str, message: str, detail: str | None = None) -> None:
        """Append a WARN outcome."""
        self.warnings.append(ProbeWarning(code=code, message=message, detail=detail))

    @property
    def has_failures(self) -> bool:
        """True iff any probe FAILed."""
        return bool(self.failures)


# === individual probes ===


async def probe_git_version(vault_dir: Path, report: ProbeReport) -> None:
    """Verify the git binary is on PATH and meets the version floor."""
    if shutil.which("git") is None:
        report.add_fail(
            check_codes.GIT_VERSION_FLOOR,
            "git binary not found in PATH",
        )
        return
    version = await gitops.git_version(vault_dir)
    if version < GIT_VERSION_FLOOR:
        actual = ".".join(map(str, version))
        floor = ".".join(map(str, GIT_VERSION_FLOOR))
        report.add_fail(
            check_codes.GIT_VERSION_FLOOR,
            f"git version {actual} below floor {floor}",
        )


def _read_gitattributes(vault_dir: Path) -> str:
    path = vault_dir / ".gitattributes"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


async def probe_autocrlf(vault_dir: Path, report: ProbeReport) -> None:
    """Refuse vaults with autocrlf=true unless ``.gitattributes`` pins LF."""
    cp = await asyncio.to_thread(
        run_git, ["config", "--get", "core.autocrlf"], cwd=vault_dir, check=False
    )
    autocrlf = cp.stdout.strip().lower() if cp.returncode == 0 else "false"
    gitattrs = _read_gitattributes(vault_dir)
    pinned = ("*.md text eol=lf" in gitattrs) or ("*.md eol=lf" in gitattrs)
    if autocrlf == "true" and not pinned:
        report.add_fail(
            check_codes.AUTOCRLF_DRIFT,
            "core.autocrlf=true AND .gitattributes does not pin '*.md text eol=lf'",
        )


async def probe_lfs_drift(vault_dir: Path, report: ProbeReport) -> None:
    """Warn when git LFS filter rules apply to ``*.md``."""
    gitattrs = _read_gitattributes(vault_dir)
    lfs_md = False
    for line in gitattrs.splitlines():
        if "*.md" in line and "filter=lfs" in line:
            lfs_md = True
            break
    if lfs_md:
        report.add_warn(
            check_codes.LFS_DRIFT,
            "git LFS filter applies to *.md; engram thoughts must remain non-LFS",
        )


async def probe_branch_alignment(
    vault_dir: Path,
    sync_config: SyncConfig,
    report: ProbeReport,
) -> None:
    """Refuse detached HEAD; warn when local branch != sync.git_branch."""
    inside = await gitops.is_inside_work_tree(vault_dir)
    if not inside:
        report.add_fail(
            check_codes.BRANCH_ALIGNMENT,
            f"vault_dir {vault_dir} is not a git working tree",
        )
        return
    current = await gitops.current_branch(vault_dir)
    if current is None:
        report.add_fail(
            check_codes.BRANCH_ALIGNMENT,
            "HEAD is detached; coordinator refuses to start",
        )
        return
    if current != sync_config.git_branch:
        report.add_warn(
            check_codes.BRANCH_ALIGNMENT,
            f"local branch {current!r} != sync.git_branch {sync_config.git_branch!r}",
        )
    # Worktree split detection: if .git is a file (gitlink), the repo is in a
    # worktree; the parent must point to a real .git directory we can write to.
    git_dir = vault_dir / ".git"
    if git_dir.exists() and git_dir.is_file():
        report.add_warn(
            check_codes.BRANCH_ALIGNMENT,
            "vault is a git worktree (linked); engram supports the primary worktree only",
        )


async def probe_submodules(vault_dir: Path, thoughts_dir: Path, report: ProbeReport) -> None:
    """Refuse submodules under ``thoughts_dir`` (R-H/edge 47)."""
    cp = await asyncio.to_thread(run_git, ["submodule", "status"], cwd=vault_dir, check=False)
    if cp.returncode != 0:
        return
    if not cp.stdout.strip():
        return
    base = thoughts_dir.resolve()
    for line in cp.stdout.splitlines():
        # Format: " <sha> <path> [(<branch>)]"; path is the second whitespace-separated token.
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        sub_path = (vault_dir / parts[1]).resolve()
        try:
            sub_path.relative_to(base)
        except ValueError:
            continue
        report.add_fail(
            check_codes.SUBMODULE_UNDER_VAULT,
            (
                f"submodule {parts[1]} resides under thoughts_dir; "
                "engram does not support nested submodules"
            ),
        )
        return


async def probe_remote_default_branch(
    vault_dir: Path,
    sync_config: SyncConfig,
    report: ProbeReport,
) -> None:
    """When a remote is configured, refs/remotes/<remote>/HEAD must align with sync.git_branch."""
    url = await gitops.remote_url(vault_dir, sync_config.git_remote)
    if url is None:
        return
    default = await gitops.default_remote_branch(vault_dir, sync_config.git_remote)
    if default is None:
        report.add_warn(
            check_codes.BRANCH_ALIGNMENT,
            f"refs/remotes/{sync_config.git_remote}/HEAD not set; "
            "run `git remote set-head` after first push",
        )
        return
    if default != sync_config.git_branch:
        report.add_fail(
            check_codes.BRANCH_ALIGNMENT,
            f"remote default branch {default!r} != sync.git_branch {sync_config.git_branch!r}",
        )


def probe_gitignore_indexes(vault_dir: Path, report: ProbeReport) -> None:
    """Refuse vaults that do not gitignore the index files (L6)."""
    gitignore = vault_dir / ".gitignore"
    if not gitignore.exists():
        report.add_fail(
            check_codes.GITIGNORE_INDEXES,
            ".gitignore missing; index files will be committed",
        )
        return
    try:
        text = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.add_fail(
            check_codes.GITIGNORE_INDEXES,
            f".gitignore unreadable: {exc}",
        )
        return
    missing: list[str] = [p for p in _REQUIRED_GITIGNORE_PATTERNS if p not in text]
    if missing:
        report.add_fail(
            check_codes.GITIGNORE_INDEXES,
            f"required .gitignore patterns missing: {missing}",
        )


def probe_cloud_sync(vault_dir: Path, report: ProbeReport) -> None:
    """Refuse ``.git`` paths under known cloud-sync providers (R-H7)."""
    git_dir = vault_dir / ".git"
    target = git_dir.resolve() if git_dir.exists() else vault_dir.resolve()
    parts_lower = [p.lower() for p in target.parts]
    for hint in _CLOUD_SYNC_HINTS:
        if hint.lower() in parts_lower:
            report.add_fail(
                check_codes.CLOUD_SYNC_UNDER_DOTGIT,
                f".git resides under cloud-sync root {hint!r}; corruption risk",
            )
            return


async def probe_gpg_agent(
    vault_dir: Path,
    sync_config: SyncConfig,
    report: ProbeReport,
) -> None:
    """Warn when commit.gpgsign=true but the gpg agent is unreachable."""
    cp = await asyncio.to_thread(
        run_git, ["config", "--get", "commit.gpgsign"], cwd=vault_dir, check=False
    )
    if cp.returncode != 0 or cp.stdout.strip().lower() != "true":
        return
    if sync_config.allow_unsigned:
        return
    if shutil.which("gpg-agent") is None and shutil.which("gpg") is None:
        report.add_warn(
            check_codes.GPG_AGENT_REACHABLE,
            "commit.gpgsign=true but no gpg/gpg-agent on PATH; signing will fail",
        )
        return
    # Best-effort agent reachability test: `gpg-connect-agent` returns 0 when alive.
    gpg_connect = shutil.which("gpg-connect-agent")
    if gpg_connect is not None:
        try:
            cp_agent = subprocess.run(  # noqa: S603 - resolved absolute path; static args
                [gpg_connect, "/bye"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            report.add_warn(
                check_codes.GPG_AGENT_REACHABLE,
                "gpg-connect-agent did not respond; commits will fail to sign",
            )
            return
        if cp_agent.returncode != 0:
            report.add_warn(
                check_codes.GPG_AGENT_REACHABLE,
                f"gpg-connect-agent exited {cp_agent.returncode}",
            )


async def probe_vault_identity(
    vault_dir: Path,
    sync_config: SyncConfig,
    report: ProbeReport,
) -> None:
    """Cross-vault contamination guard (R-H3) via .engram/identity.local."""
    actual_url = await gitops.remote_url(vault_dir, sync_config.git_remote)
    try:
        result = check_identity(vault_dir, actual_url)
    except ConfigError as exc:
        report.add_fail(
            check_codes.VAULT_IDENTITY_REMOTE_MATCH,
            "identity file invalid",
            detail=str(exc),
        )
        return
    if isinstance(result, MissingIdentity):
        # Not a hard failure - many vaults have not been formally identified.
        # Surface as WARN so the operator knows to add one before sharing the vault.
        report.add_warn(
            check_codes.VAULT_IDENTITY_REMOTE_MATCH,
            f"{IDENTITY_FILE_RELATIVE} missing; cannot enforce cross-vault contamination check",
        )
        return
    if isinstance(result, Mismatch):
        report.add_fail(
            check_codes.VAULT_IDENTITY_REMOTE_MATCH,
            (
                f"vault identity {result.identity.vault_id!r} does not match remote URL "
                f"{result.actual_url!r}; refusing to start to prevent cross-vault contamination"
            ),
        )
        return
    # Match - silent OK.


async def probe_user_identity(vault_dir: Path, report: ProbeReport) -> None:
    """Warn when per-vault git identity is not set.

    Inheriting from global config silently leaks the user's default
    identity into vault commits (R-M14).
    """
    cp_email = await asyncio.to_thread(
        run_git,
        ["config", "--local", "--get", "user.email"],
        cwd=vault_dir,
        check=False,
    )
    cp_name = await asyncio.to_thread(
        run_git,
        ["config", "--local", "--get", "user.name"],
        cwd=vault_dir,
        check=False,
    )
    email = cp_email.stdout.strip() if cp_email.returncode == 0 else ""
    name = cp_name.stdout.strip() if cp_name.returncode == 0 else ""
    if not email or not name:
        report.add_warn(
            check_codes.SYNC_USER_IDENTITY_SET,
            "git user.email or user.name not set per-vault; commit author may leak global identity",
        )


async def probe_working_tree_dirty(vault_dir: Path, report: ProbeReport) -> None:
    """Refuse startup when the working tree has uncommitted changes (R-M12)."""
    entries = await gitops.status_porcelain(vault_dir)
    if entries:
        sample = ", ".join(e.path for e in entries[:5])
        report.add_fail(
            check_codes.WORKING_TREE_DIRTY_AT_STARTUP,
            (
                "uncommitted changes in working tree at startup; "
                "commit/stash them or run `engram sync --resume` after manual review"
            ),
            detail=sample + ("..." if len(entries) > 5 else ""),
        )


def probe_read_only_role_consistency(sync_config: SyncConfig, report: ProbeReport) -> None:
    """Refuse ``role=read-only`` AND ``auto_push_on_capture=true`` (config contradiction)."""
    if sync_config.role == "read-only" and sync_config.auto_push_on_capture:
        report.add_fail(
            check_codes.READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH,
            (
                "sync.role=read-only AND sync.auto_push_on_capture=true is a config "
                "contradiction; reconcile config (defense-in-depth refuses to override silently)"
            ),
        )


def probe_signed_commits_required(sync_config: SyncConfig, report: ProbeReport) -> None:
    """Warn when signed_pull_required=true but trusted-keys file is missing."""
    if not sync_config.signed_pull_required:
        return
    trusted_keys = Path.home() / ".config" / "engram" / "trusted-keys.yaml"
    if not trusted_keys.exists():
        report.add_warn(
            check_codes.SIGNED_COMMITS_REQUIRED,
            (
                f"sync.signed_pull_required=true but {trusted_keys} is missing; "
                "verify-commit gate cannot succeed"
            ),
        )


# === aggregator ===


async def run_startup_probes(
    sync_config: SyncConfig,
    vault_dir: Path,
    *,
    thoughts_dir: Path | None = None,
) -> ProbeReport:
    """Run every probe and return the aggregated :class:`ProbeReport`.

    ``thoughts_dir`` defaults to ``vault_dir / 'thoughts'``; the storage
    facade resolves it from :class:`engram.config.EffectiveConfig` so the
    caller normally passes the resolved value.
    """
    report = ProbeReport()
    if sync_config.disabled:
        return report
    if thoughts_dir is None:
        thoughts_dir = vault_dir / "thoughts"

    # Static / config-only probes first (cheap, no fork/exec).
    probe_read_only_role_consistency(sync_config, report)
    probe_signed_commits_required(sync_config, report)
    probe_gitignore_indexes(vault_dir, report)
    probe_cloud_sync(vault_dir, report)

    # Now the git-touching probes. branch_alignment must run before remote
    # default-branch check so detached-HEAD is reported once, not twice.
    await probe_git_version(vault_dir, report)
    await probe_branch_alignment(vault_dir, sync_config, report)
    await probe_remote_default_branch(vault_dir, sync_config, report)
    await probe_autocrlf(vault_dir, report)
    await probe_lfs_drift(vault_dir, report)
    await probe_submodules(vault_dir, thoughts_dir, report)
    await probe_gpg_agent(vault_dir, sync_config, report)
    await probe_vault_identity(vault_dir, sync_config, report)
    await probe_user_identity(vault_dir, report)
    await probe_working_tree_dirty(vault_dir, report)
    return report


async def per_cycle_recheck(
    sync_config: SyncConfig,
    vault_dir: Path,
) -> ProbeReport:
    """Cheap re-run of probes 7 + 11 before each push.

    Catches mid-session admin changes (re-pointed origin/HEAD, swapped
    remote URL) without re-running the full 14-probe suite.
    """
    report = ProbeReport()
    if sync_config.disabled:
        return report
    await probe_remote_default_branch(vault_dir, sync_config, report)
    await probe_vault_identity(vault_dir, sync_config, report)
    return report


def serialize_failures(failures: Iterable[ProbeFailure]) -> str:
    """Render a list of failures as a human-readable multi-line string."""
    lines: list[str] = []
    for failure in failures:
        lines.append(f"  [{failure.code}] {failure.message}")
        if failure.detail:
            lines.append(f"      {failure.detail}")
    return "\n".join(lines)


__all__ = [
    "GIT_VERSION_FLOOR",
    "ProbeFailure",
    "ProbeReport",
    "ProbeWarning",
    "per_cycle_recheck",
    "probe_autocrlf",
    "probe_branch_alignment",
    "probe_cloud_sync",
    "probe_git_version",
    "probe_gitignore_indexes",
    "probe_gpg_agent",
    "probe_lfs_drift",
    "probe_read_only_role_consistency",
    "probe_remote_default_branch",
    "probe_signed_commits_required",
    "probe_submodules",
    "probe_user_identity",
    "probe_vault_identity",
    "probe_working_tree_dirty",
    "run_startup_probes",
    "serialize_failures",
]
