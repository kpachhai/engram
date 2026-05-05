"""Multi-vault ``engram serve`` startup helpers.

Startup-ordering rules:

1. Load resolved per-user config; build :class:`VaultRegistry`.
2. For each vault in ``config.vaults``: run startup probes against
   THAT vault. Aggregate FAILs across vaults; on any FAIL, exit 2.
3. Embedding-model compatibility check across all mounted vaults.
4. Acquire per-vault ``VaultLock`` for each in iteration order
   (deterministic).
5. Per-vault startup pull (primary + read-only mounted via clone or
   import).
6. Per-vault conflict-marker scan; vaults with markers are skipped
   (others continue).
7. For each vault, build :class:`SyncCoordinator` (read-only vaults
   get ``role="read-only"`` + ``auto_push_on_capture=False``).
8. Build :class:`engram.llm.budget.LLMBudget` singleton +
   :class:`engram.llm.protocol.LLMProvider` singleton (lazy).
9. Build the FastMCP server via
   :func:`engram.mcp.server.build_multivault_server`.
10. Run loop.
11. Drain every coordinator + release every lock + close every storage
    on shutdown (reverse-mount order).

This module exposes the pieces that the ``engram serve`` CLI composes;
each is independently testable without spawning a real serve process.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from engram.config.models import EffectiveConfig, VaultMount
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import VaultError
from engram.multivault.aggregator import assert_compatible_embeddings
from engram.multivault.registry import VaultRegistry
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting
from engram.sync import startup_probes
from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator
from engram.utils.lock import MigrationLock, VaultLock

if TYPE_CHECKING:
    from engram.embedding.protocol import EmbeddingProvider

_log = logging.getLogger("engram.cli.serve_multivault")


@dataclass(slots=True)
class MountedVault:
    """Bundle of state for one mounted vault inside the registry."""

    name: str
    role: str
    storage: VaultStorage
    lock: VaultLock | None = None
    coordinator: SyncCoordinator | None = None


@dataclass(slots=True)
class MultiVaultStartupResult:
    """Output of :func:`startup_multivault`."""

    registry: VaultRegistry
    mounted: list[MountedVault] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    embedder: EmbeddingProvider | None = None


def _coordinator_config_for(*, vault_path: Path, sync_config: object) -> CoordinatorConfig:
    """Build a CoordinatorConfig from the vault's sync settings.

    ``sync_config`` is a duck-typed alias for SyncConfig so this helper
    works for both EffectiveConfig.sync (per-vault) and VaultConfig.sync.
    """
    s = sync_config
    return CoordinatorConfig(
        debounce_window_seconds=s.debounce_window_seconds,  # type: ignore[attr-defined]
        max_deferral_seconds=s.max_deferral_seconds,  # type: ignore[attr-defined]
        push_retry_count=s.push_retry_count,  # type: ignore[attr-defined]
        push_retry_backoff_seconds=s.push_retry_backoff_seconds,  # type: ignore[attr-defined]
        push_timeout_seconds=s.push_timeout_seconds,  # type: ignore[attr-defined]
        git_remote=s.git_remote,  # type: ignore[attr-defined]
        git_branch=s.git_branch,  # type: ignore[attr-defined]
        role=s.role,  # type: ignore[attr-defined]
        auto_commit_on_capture=s.auto_commit_on_capture,  # type: ignore[attr-defined]
        auto_push_on_capture=s.auto_push_on_capture and s.role == "primary",  # type: ignore[attr-defined]
        use_no_verify=s.use_no_verify,  # type: ignore[attr-defined]
        migration_held=lambda: MigrationLock.is_held(vault_path),
    )


def _open_storage_for(
    *,
    mount: VaultMount,
    embedder: EmbeddingProvider,
) -> VaultStorage:
    """Open a VaultStorage for ``mount`` after expanding paths."""
    vault_path = mount.path.expanduser().resolve()
    thoughts_dir = vault_path / "thoughts"
    indexes_dir = vault_path / ".indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=embedder.dimension,
        embedding_model_name=getattr(embedder, "model_name", None),
        vault_name=mount.name,
    )
    set_setting(
        storage.conn,
        "embedding_model_name",
        getattr(embedder, "model_name", "unknown"),
    )
    set_setting(storage.conn, "embedding_dim", str(embedder.dimension))
    return storage


def startup_multivault(
    config: EffectiveConfig,
    *,
    embedder: EmbeddingProvider | None = None,
    skip_probes: bool = False,
) -> MultiVaultStartupResult:
    """Walk the multi-vault startup sequence and return the live registry.

    Args:
        config: Resolved per-user effective config; ``config.vaults``
            drives the mount list.
        embedder: Optional embedding provider; if absent, a
            :class:`FastEmbedProvider` is constructed from the config's
            ``embedding_model``.
        skip_probes: Test seam to bypass per-vault startup probes
            (matches the existing ``engram serve --skip-probes`` flag).

    Returns:
        A :class:`MultiVaultStartupResult` whose ``registry`` is ready
        to feed :func:`engram.mcp.server.build_multivault_server`.
    """
    if not config.vaults:
        msg = "startup_multivault requires config.vaults to be non-empty"
        raise VaultError(msg)

    if embedder is None:
        embedder = FastEmbedProvider(model_name=config.embedding_model)

    registry = VaultRegistry()
    mounted: list[MountedVault] = []
    skipped: list[tuple[str, str]] = []
    result = MultiVaultStartupResult(registry=registry, embedder=embedder)

    for mount in config.vaults:
        vault_path = mount.path.expanduser().resolve()
        if not vault_path.exists():
            _log.warning("vault %r path %s missing; skipping", mount.name, vault_path)
            skipped.append((mount.name, "path_missing"))
            continue

        if not skip_probes and (vault_path / ".git").exists():
            try:
                probe_report = asyncio.run(
                    startup_probes.run_startup_probes(
                        config.sync,
                        vault_path,
                        thoughts_dir=vault_path / "thoughts",
                    )
                )
                if probe_report.has_failures:
                    _log.warning(
                        "vault %r failed startup probes: %s",
                        mount.name,
                        [f.code for f in probe_report.failures],
                    )
                    skipped.append((mount.name, "probe_failure"))
                    continue
            except Exception:
                _log.exception("startup probes raised for %r; skipping", mount.name)
                skipped.append((mount.name, "probe_error"))
                continue

        try:
            storage = _open_storage_for(mount=mount, embedder=embedder)
        except Exception:
            _log.exception("could not open storage for %r; skipping", mount.name)
            skipped.append((mount.name, "open_failure"))
            continue

        try:
            lock = VaultLock(vault_path)
            lock.acquire()
        except Exception:
            _log.exception("could not acquire lock for %r; skipping", mount.name)
            storage.close()
            skipped.append((mount.name, "lock_failure"))
            continue

        registry.mount(name=mount.name, storage=storage, role=mount.role)
        mv = MountedVault(name=mount.name, role=mount.role, storage=storage, lock=lock)
        mounted.append(mv)

    if registry.read_only_vaults() | set(registry.names()):
        # Compat check is best-effort: missing settings are tolerated;
        # actual mismatch raises EmbeddingModelMismatch which propagates.
        assert_compatible_embeddings(registry)

    result.mounted = mounted
    result.skipped = skipped
    return result


def shutdown_multivault(result: MultiVaultStartupResult) -> None:
    """Drain coordinators + release locks + close storages in reverse order.

    Idempotent. Used by the serve CLI's ``finally`` block.
    """
    for mv in reversed(result.mounted):
        if mv.coordinator is not None:
            try:
                asyncio.run(mv.coordinator.stop())
            except Exception:
                _log.exception("coordinator drain raised for %r", mv.name)
        if mv.lock is not None:
            try:
                mv.lock.release()
            except Exception:
                _log.exception("lock release raised for %r", mv.name)
        try:
            mv.storage.close()
        except Exception:
            _log.exception("storage close raised for %r", mv.name)
    result.registry.close_all()


__all__ = [
    "MountedVault",
    "MultiVaultStartupResult",
    "shutdown_multivault",
    "startup_multivault",
]
