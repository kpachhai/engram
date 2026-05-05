"""``engram export`` and ``engram import`` CLI commands.

* ``engram export --vault <name> --portability portable [--portability sensitive]
  --output <path>`` builds a bundle from the named vault. The
  ``--portability`` flag is repeatable; default is ``["portable"]``.
* ``engram import <bundle> --vault <target>`` ingests a bundle into a
  target vault. Refuses if the target is mounted read-only unless the
  operator passes ``--allow-read-only``.

Both commands resolve the vault from the per-user config (NOT from a
running serve loop's :class:`VaultRegistry`): the CLI must work offline,
including when ``engram serve`` is not running.

While ``engram serve`` holds the vault lock, export refuses with a
clear message; the operator stops serve before exporting.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from engram.bundle import BundleExporter, BundleImporter
from engram.config import load_config
from engram.config.loader import _load_user_config_if_present
from engram.errors import EngramError
from engram.storage.facade import VaultStorage

if TYPE_CHECKING:
    from engram.config.models import EffectiveConfig
    from engram.models.frontmatter import Portability


_LOCK_FILENAME = "engram.lock"


def _resolve_vault_effective(*, vault_name: str | None) -> EffectiveConfig:
    """Build an ``EffectiveConfig`` for the named vault (or primary)."""
    return load_config(vault_name=vault_name)


def _abort_if_serve_lock_held(effective: EffectiveConfig) -> None:
    lock_path = effective.index_dir / _LOCK_FILENAME
    if lock_path.exists():
        typer.secho(
            f"engram bundle: vault lock {lock_path} is held; refuse to operate while "
            "engram serve is running. Stop serve first, then retry.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


def _open_target_storage(effective: EffectiveConfig) -> VaultStorage:
    """Open a :class:`VaultStorage` matching ``effective``."""
    return VaultStorage(
        thoughts_dir=effective.thoughts_dir,
        index_db_path=effective.index_dir / "engram.db",
        embedding_model_name=effective.embedding_model,
        vault_name=effective.vault_name,
    )


def _is_read_only(vault_name: str | None) -> bool:
    """Check whether the named vault is declared as ``role: read-only``.

    ``vault_name=None`` resolves the primary which is by definition not
    read-only; only an explicitly named vault may be a read-only target.
    """
    user_config = _load_user_config_if_present()
    if user_config is None:
        return False
    if vault_name is None:
        return False
    for mount in user_config.vaults:
        if mount.name == vault_name:
            return mount.role == "read-only"
    return False


def register(app: typer.Typer) -> None:
    """Attach the ``export`` and ``import`` subcommands."""

    @app.command(name="export")
    def export_cmd(
        output: Path = typer.Option(  # noqa: B008
            ...,
            "--output",
            "-o",
            help="Destination path for the bundle .tar.gz file.",
        ),
        vault: str | None = typer.Option(
            None,
            "--vault",
            help="Name of the source vault. Defaults to the primary.",
        ),
        portability: list[str] | None = typer.Option(  # noqa: B008
            None,
            "--portability",
            help=(
                "Repeatable: portability tier(s) to include "
                "(portable | sensitive). Default: portable."
            ),
        ),
        source_user: str | None = typer.Option(
            None,
            "--source-user",
            help="Logical user identifier embedded in the bundle manifest.",
        ),
    ) -> None:
        """Export a bundle of thoughts from a vault."""
        try:
            effective = _resolve_vault_effective(vault_name=vault)
        except EngramError as exc:
            typer.secho(f"engram export: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        _abort_if_serve_lock_held(effective)

        portabilities: list[Portability] = []
        for tier in portability or ["portable"]:
            if tier not in ("portable", "sensitive"):
                typer.secho(
                    f"engram export: --portability {tier!r} is invalid; "
                    "must be 'portable' or 'sensitive'",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(2)
            portabilities.append(tier)  # type: ignore[arg-type]

        storage = _open_target_storage(effective)
        try:
            exporter = BundleExporter(
                storage=storage,
                portability_filter=portabilities,
                source_user=source_user or effective.default_user,
                embedding_model=effective.embedding_model,
            )
            try:
                result = exporter.export_to(output)
            except EngramError as exc:
                typer.secho(f"engram export: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from exc
        finally:
            storage.close()

        typer.secho(
            (
                f"engram export: wrote {result.bundle_path} "
                f"({result.bytes_written} bytes, "
                f"{result.manifest.thought_count} thoughts, "
                f"bundle_id={result.manifest.bundle_id})"
            ),
            fg=typer.colors.GREEN,
        )

    @app.command(name="import")
    def import_cmd(
        bundle_path: Path = typer.Argument(  # noqa: B008
            ...,
            help="Path to the bundle .tar.gz to import.",
        ),
        vault: str | None = typer.Option(
            None,
            "--vault",
            help="Name of the target vault. Defaults to the primary.",
        ),
        allow_read_only: bool = typer.Option(
            False,
            "--allow-read-only",
            help=(
                "Permit importing into a read-only-mounted vault. "
                "Required when seeding a friend-share mirror."
            ),
        ),
    ) -> None:
        """Import a bundle into a vault."""
        try:
            effective = _resolve_vault_effective(vault_name=vault)
        except EngramError as exc:
            typer.secho(f"engram import: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        _abort_if_serve_lock_held(effective)

        target_is_read_only = _is_read_only(vault)
        if target_is_read_only and not allow_read_only:
            typer.secho(
                f"engram import: vault {vault!r} is read-only; "
                "pass --allow-read-only to import anyway.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        storage = _open_target_storage(effective)
        if target_is_read_only:
            storage.set_read_only_role(read_only=True)
        # Build an embedder so imported thoughts land with embeddings ready
        # for cross-vault search. A read-only target can't be repaired
        # post-import (doctor --repair refuses on read-only role), so the
        # CLI path always supplies an embedder during the merge.
        from engram.embedding.fastembed import FastEmbedProvider

        embedder = FastEmbedProvider(model_name=effective.embedding_model)
        try:
            importer = BundleImporter(
                target=storage,
                allow_read_only=allow_read_only,
                embedder=embedder,
            )
            try:
                result = importer.import_into(bundle_path)
            except EngramError as exc:
                typer.secho(f"engram import: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from exc
        finally:
            with contextlib.suppress(Exception):
                storage.close()

        typer.secho(
            (
                f"engram import: imported {result.imported_count} thoughts "
                f"(skipped {result.skipped_block_count} block-portability, "
                f"{len(result.skipped_oversized)} oversized); "
                f"report at {result.migration_report_path}"
            ),
            fg=typer.colors.GREEN,
        )
