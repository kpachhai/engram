"""Custom exception hierarchy for engram.

Every engram-raised error inherits from :class:`EngramError`, allowing a single
``except EngramError`` block to catch any application-level failure. Each class
carries a stable ``error_code`` attribute used by :mod:`engram.mcp.tools` to map
exceptions onto MCP JSON-RPC error responses with reproducible codes across
versions.

Catch only what you can handle. Re-raise with context using
``raise FooError(...) from e`` so the cause chain is preserved.
"""

from __future__ import annotations


class EngramError(Exception):
    """Base class for all engram errors."""

    error_code: str = "engram_error"


class ConfigError(EngramError):
    """Configuration is missing, malformed, or contains invalid values."""

    error_code: str = "config_error"


class VaultError(EngramError):
    """Vault structure, permissions, or initialization issue."""

    error_code: str = "vault_error"


class LockError(EngramError):
    """Per-vault advisory lock could not be acquired or was lost."""

    error_code: str = "lock_error"


class SyncError(EngramError):
    """Git sync issue (conflict, push rejected, network failure).

    Raised by :mod:`engram.sync` for transitions the coordinator cannot
    automatically reconcile (e.g. attempting an undocumented state
    transition, working-tree contamination at startup, or ``--force``-style
    history rewrites detected via the reflog gate).
    """

    error_code: str = "sync_error"


class IndexError(EngramError):
    """SQLite or vector index issue.

    Note: this name shadows the builtin ``IndexError``. Callers that work with
    both should import this class via ``from engram.errors import IndexError as
    EngramIndexError`` or qualify usage as ``engram.errors.IndexError``.
    """

    error_code: str = "index_error"


class EmbeddingError(EngramError):
    """Embedding generation, model load, or model verification issue."""

    error_code: str = "embedding_error"


class MigrationError(EngramError):
    """One-time migration script issue (Open Brain or future sources)."""

    error_code: str = "migration_error"


# Multi-vault errors --------------------------------------------------------


class VaultReadOnlyError(VaultError):
    """A write was attempted against a vault mounted with ``role: read-only``.

    Raised at the storage-layer write boundary (``update_metadata``,
    ``update_body``, ``delete``, ``write_thought``, ``_q_upsert_embedding``,
    ``_q_mark_embedding_status``, ``reindex_vault``,
    ``_repair_pending_embeddings``) as a hard refusal rather than a soft
    skip.
    """

    error_code: str = "vault_read_only"


class VaultPathCollision(VaultError):
    """Two configured vaults resolve to the same on-disk path after ``realpath``.

    Canonical enforcement point: raised by ``VaultRegistry.__init__``
    after every storage's ``vault_path`` is resolved through ``realpath``;
    advisory enforcement also exists at config-load time but symlinks may
    change between load and registry init.
    """

    error_code: str = "vault_path_collision"


class DuplicateVaultName(VaultError):
    """Two configured vaults share the same logical ``name``.

    Distinct from path collision: the same vault may be reachable under
    multiple names by mistake. Raised by ``VaultRegistry.mount`` when a
    second mount is attempted with a name that already exists.
    """

    error_code: str = "duplicate_vault_name"


class EmbeddingModelMismatch(EmbeddingError):
    """Two vaults declare different embedding models (or dimensions).

    Cross-vault similarity scores are not comparable across embedding
    models, so the multi-vault aggregator refuses cross-vault search rather
    than returning rankings the user cannot reason about.
    """

    error_code: str = "embedding_model_mismatch"


class BundleImportError(EngramError):
    """Bundle import refused (path traversal, oversize, format, or staging).

    The reason is carried in the message string. Common reasons:
    ``path_traversal``, ``oversized_file``, ``oversized_bundle``,
    ``bad_yaml``, ``id_collision``, ``schema_version_unsupported``,
    ``staging_failure``, ``bundle_export_lock_held``.
    """

    error_code: str = "bundle_import_error"


class BundleCycleDetected(BundleImportError):
    """A bundle's ``bundle_id`` already appears in this vault's source chain.

    Walks every existing thought's ``source: bundle:<id> <- ...`` chain
    looking for the candidate ``manifest.bundle_id``; if found, the bundle
    is refused. Multi-machine same-user imports are not cycles because
    each export gets a distinct ``bundle_id`` UUID-v7.
    """

    error_code: str = "bundle_cycle_detected"


class BlockThoughtLLMDisallowed(EngramError):
    """A thought with ``portability: block`` reached an LLM call site.

    The absolute floor of the LLM portability gate: no flag, config, or
    provider locality overrides this refusal. Raised by the resolver and
    re-asserted as defense-in-depth at the portability gate and at every
    LLM tool entry point.
    """

    error_code: str = "block_thought_llm_disallowed"


class LLMProviderError(EngramError):
    """LLM provider configuration, validation, or runtime issue.

    Covers reasoned refusals from the resolver (e.g.
    ``sensitive_thought_remote_provider_disallowed``,
    ``cross_provider_synthesis_disallowed``,
    ``daily_cost_cap_exceeded``, ``prompt_too_large``,
    ``prompt_too_large_even_at_floor``, ``base_url_not_trusted``,
    ``provider_unreachable``) as well as transient runtime errors (5xx,
    timeout, malformed response). The reason is the message string.
    """

    error_code: str = "llm_provider_error"


# Team-vault errors ---------------------------------------------------------


class TeamMemberNotEnrolled(VaultError):
    """Local GPG primary-fingerprint absent from a team-vault ``members.yaml``.

    Refusal at capture time (NOT at push time) so the user fails fast with
    a clear remediation hint rather than a delayed push reject. Resolution:
    a steward runs ``engram team-vault add-member <fingerprint>`` and the
    affected member re-pulls.
    """

    error_code: str = "team_member_not_enrolled"


class TeamPolicyViolation(VaultError):
    """Team-vault policy allowlist or sensitive-acceptance gate refused a capture.

    Reason carried in the message string. Common reasons:
    ``prefix_not_allowed``, ``source_not_allowed``,
    ``sensitive_thought_target_does_not_accept``.
    """

    error_code: str = "team_policy_violation"


class RoutingRuleAmbiguous(ConfigError):
    """Two routing rules match the same first-prefix with no tie-breaker.

    Per-prefix tie-break order: longest-pattern-match wins; ties broken by
    user-config declaration order; remaining ties refuse with this error.
    """

    error_code: str = "routing_rule_ambiguous"


class RoutingTargetNotMounted(ConfigError):
    """A routing rule or explicit ``vault:`` arg named an unmounted vault alias.

    The user must either mount the vault (via ``engram team-vault join``)
    or correct the rule / argument.
    """

    error_code: str = "routing_target_not_mounted"


class BlockThoughtInTeamVaultDisallowed(VaultError):
    """A ``portability: block`` thought reached a team-write vault.

    Refusal is structural: ``block`` portability ALWAYS lands in
    personal-primary. Defense-in-depth: the routing dispatcher catches
    this upstream; this error fires only if a future code path bypasses
    routing.
    """

    error_code: str = "block_thought_in_team_vault_disallowed"


class TeamVaultEmbeddingMismatch(EmbeddingModelMismatch):
    """A team-vault policy pins an embedding model the local machine does not match.

    Refines the cross-vault ``EmbeddingModelMismatch`` to surface the
    team-vault context (the team's policy is the source of truth; the
    local machine must conform or capture personal-only).
    """

    error_code: str = "team_vault_embedding_mismatch"


class TeamMembershipRevoked(VaultError):
    """Team-vault remote no longer accepts pushes from this machine's credentials.

    Detected via TTL'd ``git ls-remote`` probe. The mount auto-degrades to
    ``frozen-read-only``; subsequent captures refuse loudly. Operator
    remediation: ``engram team-vault unmount --remove-local <name>`` or
    ``engram orphan-recover --to personal``.
    """

    error_code: str = "team_membership_revoked"


class AttributionCommitterMismatch(SyncError):
    """A pushed thought's ``captured_by:`` does not match the signed committer fingerprint.

    Raised by the server-side ``pre-receive`` hook to defend against
    attribution forgery. The whole push is refused; the rejection message
    lists every offending file.
    """

    error_code: str = "attribution_committer_mismatch"


class TeamWriteRequiresRemote(ConfigError):
    """A vault declared ``role: team-write`` without a ``remote_url``.

    The team-vault trust model presumes a remote where the
    ``pre-receive`` hook lives; a team-write vault without a remote is
    structurally meaningless and refused at config-load.
    """

    error_code: str = "team_write_requires_remote"


class TeamVaultAlreadyInitialized(VaultError):
    """``engram team-vault setup`` ran against a remote already initialized as a team vault.

    First-writer-wins via the existing remote: a second ``setup`` run
    detects pre-existing canonical files and refuses rather than
    overwriting.
    """

    error_code: str = "team_vault_already_initialized"


class PushQueuePersistenceFailed(EngramError):
    """The persistent push queue could not append to its on-disk state.

    Common cause: disk full at enqueue. Capture refuses (rather than
    silently losing the queued push) so the user knows the thought was
    NOT durably enqueued.
    """

    error_code: str = "push_queue_persistence_failed"


# Daemon-mode errors --------------------------------------------------------


class DaemonError(EngramError):
    """Base class for all daemon-mode errors."""

    error_code: str = "daemon_error"


class DaemonSpawnError(DaemonError):
    """Daemon spawn dance failed (timeout, lock contention, init failure)."""

    error_code: str = "daemon_spawn_error"


class DaemonConnectionError(DaemonError):
    """Proxy could not connect to the daemon over UDS."""

    error_code: str = "daemon_connection_error"


class DaemonNotRunningError(DaemonError):
    """No daemon is running and auto-spawn is disabled."""

    error_code: str = "daemon_not_running_error"


class PeerCredRejectError(DaemonError):
    """Peer credential check rejected a connection from a non-self UID."""

    error_code: str = "peer_cred_reject_error"


__all__ = [
    "AttributionCommitterMismatch",
    "BlockThoughtInTeamVaultDisallowed",
    "BlockThoughtLLMDisallowed",
    "BundleCycleDetected",
    "BundleImportError",
    "ConfigError",
    "DaemonConnectionError",
    "DaemonError",
    "DaemonNotRunningError",
    "DaemonSpawnError",
    "DuplicateVaultName",
    "EmbeddingError",
    "EmbeddingModelMismatch",
    "EngramError",
    "IndexError",
    "LLMProviderError",
    "LockError",
    "MigrationError",
    "PeerCredRejectError",
    "PushQueuePersistenceFailed",
    "RoutingRuleAmbiguous",
    "RoutingTargetNotMounted",
    "SyncError",
    "TeamMemberNotEnrolled",
    "TeamMembershipRevoked",
    "TeamPolicyViolation",
    "TeamVaultAlreadyInitialized",
    "TeamVaultEmbeddingMismatch",
    "TeamWriteRequiresRemote",
    "VaultError",
    "VaultPathCollision",
    "VaultReadOnlyError",
]
