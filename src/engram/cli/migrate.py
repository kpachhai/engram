"""``engram migrate-from-open-brain`` CLI command."""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import ConfigError, MigrationError
from engram.migration.open_brain import MigrationConfig, run_migration
from engram.storage.facade import VaultStorage

_DEVKIT_REFERENCES_PATH = Path.home() / ".config" / "devkit" / "references.json"


def _load_devkit_open_brain_url() -> str | None:
    """Soft-read ``open_brain_mcp_url`` from devkit references.json if present."""
    if not _DEVKIT_REFERENCES_PATH.exists():
        return None
    try:
        data = json.loads(_DEVKIT_REFERENCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        url = data.get("open_brain_mcp_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def register(app: typer.Typer) -> None:
    """Attach the ``migrate-from-open-brain`` subcommand."""

    @app.command(name="migrate-from-open-brain")
    def migrate_cmd(
        url: str | None = typer.Option(
            None,
            "--url",
            help="Open Brain MCP endpoint URL (else read from ~/.config/devkit/references.json).",
        ),
        key: str | None = typer.Option(
            None,
            "--key",
            help="Open Brain access key (prefer env var OPEN_BRAIN_KEY for ps-aux safety).",
        ),
        config_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--config",
            help="Vault config path; overrides per-user vaults: list.",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--vault",
            help="Which vault from per-user vaults: list to target.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Read everything from Open Brain but write nothing to vault.",
        ),
        limit: int | None = typer.Option(
            None,
            "--limit",
            help="Only migrate the first N thoughts (testing).",
        ),
        prefer_legacy_id_match: bool = typer.Option(
            False,
            "--prefer-legacy-id-match",
            help="Match by (legacy_id, source) before the triple - "
            "for re-migrating an actively-edited source.",
        ),
        confirm_supabase_snapshot_taken: bool = typer.Option(
            False,
            "--confirm-supabase-snapshot-taken",
            help="Required for non-dry-run: affirms the operator has backed up Open Brain.",
        ),
        report_path: Path | None = typer.Option(  # noqa: B008
            None,
            "--report-path",
            help="Where to write migration-report.json. Default: <vault>/migration-report.json.",
        ),
    ) -> None:
        """Migrate the maintainer's Open Brain corpus into the engram vault."""
        try:
            config = load_config(
                explicit_vault_config=config_path,
                vault_name=vault_name,
            )
        except ConfigError as exc:
            typer.secho(f"engram migrate: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        # Resolve URL: --url > devkit references.json.
        resolved_url = url or _load_devkit_open_brain_url()
        if not resolved_url:
            typer.secho(
                "engram migrate: no Open Brain URL provided. Either pass --url, "
                "or populate ~/.config/devkit/references.json with `open_brain_mcp_url`.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        # Resolve key: env var > --key (warn on raw --key; argv leaks via ps).
        resolved_key = os.environ.get("OPEN_BRAIN_KEY")
        if not resolved_key and key:
            typer.secho(
                "engram migrate: --key passed on the command line is visible to other "
                "processes via 'ps aux'. Prefer setting OPEN_BRAIN_KEY in the environment.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            resolved_key = key

        if not dry_run and not confirm_supabase_snapshot_taken:
            typer.secho(
                "engram migrate: refusing to run without --confirm-supabase-snapshot-taken "
                "(or --dry-run). Take a Supabase backup of the thoughts table first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        embedder = FastEmbedProvider(model_name=config.embedding_model)
        storage = VaultStorage(
            thoughts_dir=config.thoughts_dir,
            index_db_path=config.index_dir / "engram.db",
            embedding_dim=embedder.dimension,
            embedding_model_name=config.embedding_model,
            vault_name=config.vault_name,
        )

        try:
            mig_config = MigrationConfig(
                open_brain_url=resolved_url,
                open_brain_key=resolved_key,
                vault_storage=storage,
                embedder=embedder,
                default_user=config.default_user,
                dry_run=dry_run,
                limit=limit,
                prefer_legacy_id_match=prefer_legacy_id_match,
                report_path=report_path,
            )
            try:
                report = run_migration(mig_config)
            except MigrationError as exc:
                typer.secho(f"engram migrate failed: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from exc
        finally:
            storage.close()

        typer.echo(
            f"engram migrate completed in {report.duration_seconds:.1f}s "
            f"(enumerated={report.enumerated}, migrated={report.migrated}, "
            f"skipped_existing={report.skipped_existing}, errors={report.errors_count})"
        )
        if report.validation_passed + report.validation_failed:
            typer.echo(
                f"  round-trip validation: {report.validation_passed} passed, "
                f"{report.validation_failed} failed"
            )

        raise typer.Exit(0 if report.errors_count == 0 else 1)


__all__ = ["register"]
