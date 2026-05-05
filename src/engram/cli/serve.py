"""``engram serve`` CLI command - launches the FastMCP stdio server.

Lifecycle:

1. Load resolved configuration.
2. Detect cloud-sync vault paths and warn (Q10 default; per ``02-TECHNICAL_DESIGN.md``).
3. Acquire the per-vault advisory lock.
4. Open :class:`VaultStorage`.
5. Construct (lazy) :class:`FastEmbedProvider`.
6. Build the FastMCP server and run its stdio loop.
7. On exit (graceful or otherwise), release the lock and close storage.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import ConfigError, LockError
from engram.errors import IndexError as EngramIndexError
from engram.logging import configure_logging
from engram.mcp.server import build_server
from engram.storage.facade import VaultStorage
from engram.utils.lock import VaultLock

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

        cloud_hint = _looks_like_cloud_sync_path(config.vault_path)
        if cloud_hint is not None:
            _log.warning(
                "vault path %s is under a consumer cloud-sync provider (%s); "
                "SQLite locking semantics on these are unreliable. If you need "
                "multi-machine sync, use git-based sync (Phase 2+) with a "
                "non-synced vault directory instead.",
                config.vault_path,
                cloud_hint,
            )

        try:
            lock = VaultLock(config.vault_path, force=force)
            lock.acquire()
        except LockError as exc:
            typer.secho(f"engram serve: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

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
            storage.close()
            lock.release()


__all__ = ["register"]
