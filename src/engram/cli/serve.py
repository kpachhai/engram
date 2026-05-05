"""``engram serve`` CLI command - launches the FastMCP stdio server.

Phase 2 Step 17 lifecycle:

1. Load resolved configuration.
2. Run :func:`engram.sync.startup_probes.run_startup_probes`. On any FAIL,
   exit 2 with a serialized failure list (refuse to serve).
3. Detect cloud-sync vault paths and warn (Q10 default).
4. Acquire the per-vault advisory lock.
5. If ``sync.auto_pull_on_startup``, run :func:`maybe_startup_pull`.
6. Scan markdown for conflict markers; if found, enter degraded mode
   (search OK, capture refused).
7. Open :class:`VaultStorage`.
8. Build the :class:`SyncCoordinator` and attach it to storage; start it.
9. Construct (lazy) :class:`FastEmbedProvider`.
10. Build the FastMCP server and run its stdio loop.
11. On exit (graceful or otherwise): drain the coordinator queue, release
    the lock, close storage.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.config.models import EffectiveConfig
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import ConfigError, LockError
from engram.errors import IndexError as EngramIndexError
from engram.logging import configure_logging
from engram.mcp.server import build_server
from engram.storage.facade import VaultStorage
from engram.sync import startup_probes
from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator
from engram.sync.gitops import conflict_marker_scan
from engram.sync.serve_hooks import maybe_startup_pull
from engram.utils.lock import MigrationLock, VaultLock

_log = logging.getLogger("engram.cli.serve")

# Common consumer cloud-sync directory roots. SQLite + flock semantics on these
# providers are unreliable (Risk R9 / Open Question Q10); WARN on detect.
_CLOUD_SYNC_PATH_HINTS = (
    "Dropbox",
    "iCloud Drive",
    "Library/CloudStorage",
    "OneDrive",
    "Google Drive",
)


def _looks_like_cloud_sync_path(path: Path) -> str | None:
    """Return the matching cloud-sync hint if ``path`` lives under one, else None."""
    parts = {p.casefold() for p in path.parts}
    for hint in _CLOUD_SYNC_PATH_HINTS:
        if hint.casefold() in parts:
            return hint
    return None


def _coordinator_config_from(config: EffectiveConfig) -> CoordinatorConfig:
    return CoordinatorConfig(
        debounce_window_seconds=config.sync.debounce_window_seconds,
        max_deferral_seconds=config.sync.max_deferral_seconds,
        push_retry_count=config.sync.push_retry_count,
        push_retry_backoff_seconds=config.sync.push_retry_backoff_seconds,
        push_timeout_seconds=config.sync.push_timeout_seconds,
        git_remote=config.sync.git_remote,
        git_branch=config.sync.git_branch,
        role=config.sync.role,
        auto_commit_on_capture=config.sync.auto_commit_on_capture,
        auto_push_on_capture=config.sync.auto_push_on_capture,
        use_no_verify=config.sync.use_no_verify,
        migration_held=lambda: MigrationLock.is_held(config.vault_path),
    )


def register(app: typer.Typer) -> None:
    """Attach the ``serve`` subcommand to a typer app."""

    @app.command(name="serve")
    def serve_cmd(
        config_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--config",
            help="Path to a vault's engram.config.yaml; bypasses per-user vaults: list.",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--vault",
            help="Which vault from the per-user vaults: list to target.",
        ),
        log_level: str | None = typer.Option(
            None,
            "--log-level",
            help="Override log level (DEBUG/INFO/WARNING/ERROR).",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Take over the vault lock even if another process appears to hold it.",
        ),
        skip_probes: bool = typer.Option(
            False,
            "--skip-probes",
            help="Skip Phase 2 startup probes (debugging only).",
        ),
    ) -> None:
        """Start the engram MCP server (stdio) for the configured vault."""
        try:
            config = load_config(
                explicit_vault_config=config_path,
                vault_name=vault_name,
                cli_overrides={"log_level": log_level} if log_level else None,
            )
        except ConfigError as exc:
            typer.secho(f"engram serve: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        configure_logging(level=config.log_level, log_format=config.log_format)

        # Step 2: startup probes BEFORE acquiring lock (per Step 17).
        if not skip_probes and (config.vault_path / ".git").exists():
            probe_report = asyncio.run(
                startup_probes.run_startup_probes(
                    config.sync,
                    config.vault_path,
                    thoughts_dir=config.thoughts_dir,
                )
            )
            if probe_report.has_failures:
                typer.secho(
                    "engram serve: startup probes failed:\n"
                    + startup_probes.serialize_failures(probe_report.failures),
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(2)
            for warning in probe_report.warnings:
                _log.warning("startup probe %s: %s", warning.code, warning.message)

        cloud_hint = _looks_like_cloud_sync_path(config.vault_path)
        if cloud_hint is not None:
            _log.warning(
                "vault path %s is under a consumer cloud-sync provider (%s); "
                "SQLite locking semantics on these are unreliable. If you need "
                "multi-machine sync, use git-based sync with a non-synced "
                "vault directory instead.",
                config.vault_path,
                cloud_hint,
            )

        try:
            lock = VaultLock(config.vault_path, force=force)
            lock.acquire()
        except LockError as exc:
            typer.secho(f"engram serve: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        # Step 5: startup pull (no-op when no remote / disabled).
        if (config.vault_path / ".git").exists():
            try:
                asyncio.run(maybe_startup_pull(config.vault_path, config.sync))
            except Exception:
                _log.exception("startup pull crashed; continuing")

        # Step 6: conflict-marker scan -> degraded mode FAIL.
        if conflict_marker_scan(config.thoughts_dir):
            typer.secho(
                "engram serve: conflict markers detected in thoughts/; "
                "resolve them then re-run `engram serve`",
                fg=typer.colors.RED,
                err=True,
            )
            lock.release()
            raise typer.Exit(2)

        try:
            embedder = FastEmbedProvider(model_name=config.embedding_model)
            storage = VaultStorage(
                thoughts_dir=config.thoughts_dir,
                index_db_path=config.index_dir / "engram.db",
                embedding_dim=embedder.dimension,
                embedding_model_name=config.embedding_model,
                vault_name=config.vault_name,
            )
        except EngramIndexError as exc:
            typer.secho(f"engram serve: {exc}", fg=typer.colors.RED, err=True)
            lock.release()
            raise typer.Exit(2) from exc

        # Step 8: build + attach the coordinator (only if vault is a git repo).
        coordinator: SyncCoordinator | None = None
        coordinator_loop: asyncio.AbstractEventLoop | None = None
        if (config.vault_path / ".git").exists() and not config.sync.disabled:
            coordinator = SyncCoordinator(
                repo_dir=config.vault_path,
                config=_coordinator_config_from(config),
            )
            storage.set_sync_coordinator(coordinator)
            # The coordinator's asyncio task is started by the FastMCP loop's
            # event loop. We start it lazily on first enqueue via storage.

        try:
            server = build_server(
                storage,
                embedder,
                default_user=config.default_user,
                server_name="engram",
            )
            _log.info(
                "engram serve starting: vault=%s default_user=%s model=%s",
                config.vault_name,
                config.default_user,
                config.embedding_model,
            )
            server.run()
        finally:
            # Step 11: drain the coordinator on shutdown (best-effort).
            if coordinator is not None:
                try:
                    asyncio.run(coordinator.stop())
                except Exception:
                    _log.exception("coordinator drain raised on shutdown")
            storage.close()
            lock.release()
            del coordinator_loop


__all__ = ["register"]
