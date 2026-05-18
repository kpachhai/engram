"""``engram reindex`` CLI command - thin wrapper over engram.storage.reindex.reindex_vault."""

from __future__ import annotations

from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import ConfigError
from engram.errors import IndexError as EngramIndexError
from engram.storage.facade import VaultStorage
from engram.storage.reindex import ReindexMode, reindex_vault
from engram.utils.lock import serve_lock_metadata


def register(app: typer.Typer) -> None:
    """Attach the ``reindex`` subcommand."""

    @app.command(name="reindex")
    def reindex_cmd(
        full: bool = typer.Option(
            False,
            "--full",
            help="Drop and rebuild the entire SQLite index from markdown SoT.",
        ),
        repair: bool = typer.Option(
            False,
            "--repair",
            help="Regenerate embeddings for rows in 'pending' status.",
        ),
        remove_orphans: bool = typer.Option(
            False,
            "--remove-orphans",
            help="Delete SQLite rows whose markdown file no longer exists "
            "(applies during incremental reindex too).",
        ),
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
    ) -> None:
        """Rebuild or repair the SQLite + sqlite-vec index from markdown SoT."""
        try:
            config = load_config(
                explicit_vault_config=config_path,
                vault_name=vault_name,
            )
        except ConfigError as exc:
            typer.secho(f"engram reindex: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        # Refuse if a daemon / serve loop holds the vault. Reindex opens a
        # second SQLite connection and walks the full thoughts tree; running
        # it concurrently with serve can wedge the daemon's WAL handle and
        # silently drop in-flight captures (markdown remains as SoT, but
        # the operator has no signal at write time).
        lock_meta = serve_lock_metadata(config.vault_path)
        if lock_meta is not None:
            pid = lock_meta.get("pid", "?")
            typer.secho(
                (
                    "engram reindex: vault lock at "
                    f"{config.index_dir / 'engram.lock'} is held "
                    f"(pid={pid}); stop the serve loop first "
                    "(`engram daemon stop`, or stop `engram serve`)."
                ),
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        if full and repair:
            typer.secho(
                "engram reindex: --full and --repair are mutually exclusive",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        # Resolve mode.
        if full:
            mode = ReindexMode.FULL
        elif repair:
            mode = ReindexMode.REPAIR
        else:
            mode = ReindexMode.INCREMENTAL

        embedder = FastEmbedProvider(model_name=config.embedding_model)

        try:
            storage = VaultStorage(
                thoughts_dir=config.thoughts_dir,
                index_db_path=config.index_dir / "engram.db",
                embedding_dim=embedder.dimension,
                embedding_model_name=config.embedding_model,
                vault_name=config.vault_name,
            )
        except EngramIndexError as exc:
            typer.secho(f"engram reindex: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        try:
            report = reindex_vault(
                storage,
                mode=mode,
                embed_fn=embedder.embed,
                remove_orphans=remove_orphans,
            )
        finally:
            storage.close()

        typer.echo(f"engram reindex --{mode.value} completed in {report.duration_seconds:.2f}s")
        typer.echo(f"  walked: {report.walked}")
        typer.echo(f"  inserted: {report.inserted}")
        typer.echo(f"  body_reindexed: {report.body_reindexed}")
        typer.echo(f"  metadata_reindexed: {report.metadata_reindexed}")
        typer.echo(f"  embeddings_repaired: {report.embeddings_repaired}")
        typer.echo(f"  embedding_failures: {report.embedding_failures}")
        typer.echo(f"  orphans_detected: {report.orphans_detected}")
        typer.echo(f"  orphans_removed: {report.orphans_removed}")
        if report.drift_observations:
            typer.echo(f"  drift_observations: {len(report.drift_observations)}")
        raise typer.Exit(0)


__all__ = ["register"]
