"""Stable string identifiers for engram doctor checks.

Each constant names exactly one row in ``engram doctor`` output AND one
probe in :mod:`engram.sync.startup_probes`. The 1:1 mapping is deliberate:
the same logic surfaces at startup (where a FAIL refuses to serve) and
under ``engram doctor`` (where an operator can inspect issues outside
the serve loop).

Constants are plain strings - they appear in user-facing CLI output and in
test assertions, so changing one is a user-visible breaking change. Do not
re-letter or rename without bumping a CHANGELOG entry.

The ``ALL_PHASE_N_CHECK_CODES`` constants are part of the public API; the
naming reflects how the check set was rolled out historically and is
preserved for compatibility.
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


# Multi-vault check codes ---------------------------------------------------

#: At most one vault may declare role=primary. FAIL when count > 1.
MULTIPLE_PRIMARY_VAULTS: Final[str] = "multiple_primary_vaults"
#: Two vaults resolve to the same realpath. FAIL.
VAULT_PATH_COLLISION: Final[str] = "vault_path_collision"
#: At least two configured vaults declare different embedding models. FAIL
#: (cross-vault similarity scores would not be comparable).
EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS: Final[str] = "embedding_model_mismatch_across_vaults"
#: INFO-only row reporting the active aggregator mode (ATTACH or SEQUENTIAL).
AGGREGATOR_MODE: Final[str] = "aggregator_mode"
#: WARN if LLM is configured but ``provider.health_check()`` does not respond.
LLM_PROVIDER_REACHABLE: Final[str] = "llm_provider_reachable"
#: WARN at >=80% of the daily cost cap, per vault that has consumed budget.
LLM_DAILY_COST_CAP_APPROACHED: Final[str] = "llm_daily_cost_cap_approached"
#: WARN when a read-only-role vault declares a per-vault LLM block; the
#: resolver ignores it but operator should know the config is dead.
READ_ONLY_VAULT_DECLARES_LLM: Final[str] = "read_only_vault_declares_llm"
#: FAIL when a friend-imported (read-only) vault carries a portability=block
#: thought; refuse to mount that vault.
FRIEND_VAULT_BLOCK_THOUGHT_PRESENT: Final[str] = "friend_vault_block_thought_present"
#: WARN when a user-config vault ``name:`` differs from the vault's own
#: ``vault_name:`` in its ``engram.config.yaml``. The mismatch causes
#: ``engram serve`` to attempt a second primary mount and log a VaultError.
USER_CONFIG_VAULT_NAME_MISMATCH: Final[str] = "user_config_vault_name_mismatch"

#: Canonical multi-vault superset; tests iterate this to assert all 23
#: codes are unique non-empty snake_case strings.
ALL_PHASE_3_CHECK_CODES: Final[tuple[str, ...]] = (
    *ALL_PHASE_2_CHECK_CODES,
    MULTIPLE_PRIMARY_VAULTS,
    VAULT_PATH_COLLISION,
    EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
    AGGREGATOR_MODE,
    LLM_PROVIDER_REACHABLE,
    LLM_DAILY_COST_CAP_APPROACHED,
    READ_ONLY_VAULT_DECLARES_LLM,
    FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
    USER_CONFIG_VAULT_NAME_MISMATCH,
)


# Team-vault check codes ----------------------------------------------------

#: INFO row counting team-write mounts. Always present; informational.
MULTIPLE_TEAM_WRITE_VAULTS_OK: Final[str] = "multiple_team_write_vaults_ok"
#: FAIL when the local GPG primary fingerprint is missing from any
#: team-vault's members.yaml.
TEAM_MEMBER_NOT_ENROLLED: Final[str] = "team_member_not_enrolled"
#: INFO row reporting persistent push queue depth + last-attempt timestamp.
TEAM_PENDING_PUSHES: Final[str] = "team_pending_pushes"
#: FAIL when ``git ls-remote`` returns 403 / permission-denied for a
#: team-vault remote (revoked membership).
TEAM_MEMBERSHIP_REVOKED: Final[str] = "team_membership_revoked"
#: WARN listing orphan tarballs under ``.engram/orphans/`` awaiting
#: operator triage via ``engram orphan-recover``.
TEAM_POLICY_VIOLATION_QUARANTINED: Final[str] = "team_policy_violation_quarantined"
#: WARN when serve's loaded config is older than ``~/.config/engram/config.yaml``
#: file mtime (newly-joined team-vault won't surface until restart).
SERVE_CONFIG_STALE: Final[str] = "serve_config_stale"
#: WARN when two routing rules tie on prefix-length + priority.
ROUTING_RULE_PRIORITY_COLLISION: Final[str] = "routing_rule_priority_collision"
#: WARN when a team-write vault's ``required_embedding_model`` differs
#: from the local engram's configured model.
TEAM_VAULT_EMBEDDING_MISMATCH: Final[str] = "team_vault_embedding_mismatch"
#: WARN when a vault's git branch HEAD has changed since mount time
#: (someone ran ``git checkout`` outside engram's awareness).
GIT_BRANCH_DRIFTED: Final[str] = "git_branch_drifted"

#: Canonical team-vault superset; tests iterate this to assert all 32
#: codes are unique non-empty snake_case strings.
ALL_PHASE_4_CHECK_CODES: Final[tuple[str, ...]] = (
    *ALL_PHASE_3_CHECK_CODES,
    MULTIPLE_TEAM_WRITE_VAULTS_OK,
    TEAM_MEMBER_NOT_ENROLLED,
    TEAM_PENDING_PUSHES,
    TEAM_MEMBERSHIP_REVOKED,
    TEAM_POLICY_VIOLATION_QUARANTINED,
    SERVE_CONFIG_STALE,
    ROUTING_RULE_PRIORITY_COLLISION,
    TEAM_VAULT_EMBEDDING_MISMATCH,
    GIT_BRANCH_DRIFTED,
)


# Daemon-mode check codes. Check implementations live in
# `daemon_checks.py`.
DAEMON_RUNNING: Final[str] = "daemon_running"
DAEMON_SOCKET_PERMISSIONS: Final[str] = "daemon_socket_permissions"
DAEMON_SOCKET_STALE: Final[str] = "daemon_socket_stale"
DAEMON_LOG_ROTATION_HEALTHY: Final[str] = "daemon_log_rotation_healthy"
DAEMON_UPTIME_EXCESSIVE: Final[str] = "daemon_uptime_excessive"
DAEMON_SOCKET_PATH_TOO_LONG: Final[str] = "daemon_socket_path_too_long"

#: Canonical daemon-mode check tuple. Tests iterate this to assert the six
#: codes are unique snake_case strings.
ALL_DAEMON_CHECK_CODES: Final[tuple[str, ...]] = (
    DAEMON_RUNNING,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_UPTIME_EXCESSIVE,
    DAEMON_SOCKET_PATH_TOO_LONG,
)


__all__ = [
    "AGGREGATOR_MODE",
    "ALL_DAEMON_CHECK_CODES",
    "ALL_PHASE_2_CHECK_CODES",
    "ALL_PHASE_3_CHECK_CODES",
    "ALL_PHASE_4_CHECK_CODES",
    "AUTOCRLF_DRIFT",
    "BRANCH_ALIGNMENT",
    "CLOUD_SYNC_UNDER_DOTGIT",
    "CONFLICT_MARKERS_PRESENT",
    "DAEMON_LOG_ROTATION_HEALTHY",
    "DAEMON_RUNNING",
    "DAEMON_SOCKET_PATH_TOO_LONG",
    "DAEMON_SOCKET_PERMISSIONS",
    "DAEMON_SOCKET_STALE",
    "DAEMON_UPTIME_EXCESSIVE",
    "EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS",
    "FRIEND_VAULT_BLOCK_THOUGHT_PRESENT",
    "GITIGNORE_INDEXES",
    "GIT_BRANCH_DRIFTED",
    "GIT_VERSION_FLOOR",
    "GPG_AGENT_REACHABLE",
    "LFS_DRIFT",
    "LLM_DAILY_COST_CAP_APPROACHED",
    "LLM_PROVIDER_REACHABLE",
    "MULTIPLE_PRIMARY_VAULTS",
    "MULTIPLE_TEAM_WRITE_VAULTS_OK",
    "READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH",
    "READ_ONLY_VAULT_DECLARES_LLM",
    "ROUTING_RULE_PRIORITY_COLLISION",
    "SERVE_CONFIG_STALE",
    "SIGNED_COMMITS_REQUIRED",
    "SUBMODULE_UNDER_VAULT",
    "SYNC_USER_IDENTITY_SET",
    "TEAM_MEMBERSHIP_REVOKED",
    "TEAM_MEMBER_NOT_ENROLLED",
    "TEAM_PENDING_PUSHES",
    "TEAM_POLICY_VIOLATION_QUARANTINED",
    "TEAM_VAULT_EMBEDDING_MISMATCH",
    "USER_CONFIG_VAULT_NAME_MISMATCH",
    "VAULT_IDENTITY_REMOTE_MATCH",
    "VAULT_PATH_COLLISION",
    "WORKING_TREE_DIRTY_AT_STARTUP",
]
