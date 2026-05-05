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


# Phase 3 additions ---------------------------------------------------------

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
#: resolver ignores it (R-M2) but operator should know the config is dead.
READ_ONLY_VAULT_DECLARES_LLM: Final[str] = "read_only_vault_declares_llm"
#: FAIL when a friend-imported (read-only) vault carries a portability=block
#: thought; refuse to mount that vault.
FRIEND_VAULT_BLOCK_THOUGHT_PRESENT: Final[str] = "friend_vault_block_thought_present"

#: Canonical Phase 3 superset; tests iterate this to assert all 22 codes
#: are unique non-empty snake_case strings.
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
)


# Phase 4 additions ---------------------------------------------------------

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

#: Canonical Phase 4 superset; tests iterate this to assert all 30 codes
#: are unique non-empty snake_case strings.
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
)


__all__ = [
    "AGGREGATOR_MODE",
    "ALL_PHASE_2_CHECK_CODES",
    "ALL_PHASE_3_CHECK_CODES",
    "ALL_PHASE_4_CHECK_CODES",
    "AUTOCRLF_DRIFT",
    "BRANCH_ALIGNMENT",
    "CLOUD_SYNC_UNDER_DOTGIT",
    "CONFLICT_MARKERS_PRESENT",
    "EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS",
    "FRIEND_VAULT_BLOCK_THOUGHT_PRESENT",
    "GITIGNORE_INDEXES",
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
    "VAULT_IDENTITY_REMOTE_MATCH",
    "VAULT_PATH_COLLISION",
    "WORKING_TREE_DIRTY_AT_STARTUP",
]
