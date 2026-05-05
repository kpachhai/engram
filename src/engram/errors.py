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


# Phase 3 additions ---------------------------------------------------------


class VaultReadOnlyError(VaultError):
    """A write was attempted against a vault mounted with ``role: read-only``.

    Raised at the storage-layer write boundary (``update_metadata``,
    ``update_body``, ``delete``, ``write_thought``, ``_q_upsert_embedding``,
    ``_q_mark_embedding_status``, ``reindex_vault``,
    ``_repair_pending_embeddings``) to enforce R-H7/R-H8 from the Phase 3
    plan as a hard refusal rather than a soft skip.
    """

    error_code: str = "vault_read_only"


class VaultPathCollision(VaultError):
    """Two configured vaults resolve to the same on-disk path after ``realpath``.

    Canonical enforcement point per R-M9: raised by ``VaultRegistry.__init__``
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
    than returning rankings the user cannot reason about (R-M11).
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
    is refused (R-M13 cycle detection). Multi-machine same-user imports are
    not cycles because each export gets a distinct ``bundle_id`` UUID-v7.
    """

    error_code: str = "bundle_cycle_detected"


class BlockThoughtLLMDisallowed(EngramError):
    """A thought with ``portability: block`` reached an LLM call site.

    The absolute floor of the LLM portability gate (R-H10): no flag,
    config, or provider locality overrides this refusal. Raised by the
    resolver and re-asserted as defense-in-depth at the portability gate
    (Step 6) and at every LLM tool entry point (Step 14).
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


__all__ = [
    "BlockThoughtLLMDisallowed",
    "BundleCycleDetected",
    "BundleImportError",
    "ConfigError",
    "DuplicateVaultName",
    "EmbeddingError",
    "EmbeddingModelMismatch",
    "EngramError",
    "IndexError",
    "LLMProviderError",
    "LockError",
    "MigrationError",
    "SyncError",
    "VaultError",
    "VaultPathCollision",
    "VaultReadOnlyError",
]
