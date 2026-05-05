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
    """Git sync issue (conflict, push rejected, network failure)."""

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


__all__ = [
    "ConfigError",
    "EmbeddingError",
    "EngramError",
    "IndexError",
    "LockError",
    "MigrationError",
    "SyncError",
    "VaultError",
]
