"""Stable string identifiers for Phase 2 doctor checks.

Each constant names exactly one row in ``engram doctor`` output AND one
probe in :mod:`engram.sync.startup_probes`. The 1:1 mapping is deliberate:
the same logic surfaces at startup (where a FAIL refuses to serve) and
under ``engram doctor`` (where an operator can inspect issues outside
the serve loop).

Constants are plain strings - they appear in user-facing CLI output and in
test assertions, so changing one is a user-visible breaking change. Do not
re-letter or rename without bumping a CHANGELOG entry.
"""

from __future__ import annotations

from typing import Final

GIT_VERSION_FLOOR: Final[str] = "git_version_floor"
BRANCH_ALIGNMENT: Final[str] = "branch_alignment"
CONFLICT_MARKERS_PRESENT: Final[str] = "conflict_markers_present"
CLOUD_SYNC_UNDER_DOTGIT: Final[str] = "cloud_sync_under_dotgit"
GITIGNORE_INDEXES: Final[str] = "gitignore_indexes"
SIGNED_COMMITS_REQUIRED: Final[str] = "signed_commits_required"
LFS_DRIFT: Final[str] = "lfs_drift"
AUTOCRLF_DRIFT: Final[str] = "autocrlf_drift"
SUBMODULE_UNDER_VAULT: Final[str] = "submodule_under_vault"
GPG_AGENT_REACHABLE: Final[str] = "gpg_agent_reachable"
VAULT_IDENTITY_REMOTE_MATCH: Final[str] = "vault_identity_remote_match"
SYNC_USER_IDENTITY_SET: Final[str] = "sync_user_identity_set"
WORKING_TREE_DIRTY_AT_STARTUP: Final[str] = "working_tree_dirty_at_startup"
READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH: Final[str] = "read_only_role_contradicts_auto_push"

#: Canonical list in the order they appear at startup; tests iterate this
#: to assert all 14 codes exist + are unique strings.
ALL_PHASE_2_CHECK_CODES: Final[tuple[str, ...]] = (
    GIT_VERSION_FLOOR,
    BRANCH_ALIGNMENT,
    CONFLICT_MARKERS_PRESENT,
    CLOUD_SYNC_UNDER_DOTGIT,
    GITIGNORE_INDEXES,
    SIGNED_COMMITS_REQUIRED,
    LFS_DRIFT,
    AUTOCRLF_DRIFT,
    SUBMODULE_UNDER_VAULT,
    GPG_AGENT_REACHABLE,
    VAULT_IDENTITY_REMOTE_MATCH,
    SYNC_USER_IDENTITY_SET,
    WORKING_TREE_DIRTY_AT_STARTUP,
    READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH,
)


__all__ = [
    "ALL_PHASE_2_CHECK_CODES",
    "AUTOCRLF_DRIFT",
    "BRANCH_ALIGNMENT",
    "CLOUD_SYNC_UNDER_DOTGIT",
    "CONFLICT_MARKERS_PRESENT",
    "GITIGNORE_INDEXES",
    "GIT_VERSION_FLOOR",
    "GPG_AGENT_REACHABLE",
    "LFS_DRIFT",
    "READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH",
    "SIGNED_COMMITS_REQUIRED",
    "SUBMODULE_UNDER_VAULT",
    "SYNC_USER_IDENTITY_SET",
    "VAULT_IDENTITY_REMOTE_MATCH",
    "WORKING_TREE_DIRTY_AT_STARTUP",
]
