"""``engram delete`` - permanently remove a thought from the vault.

Removes both the markdown file (source of truth) AND the SQLite row +
embedding. The deletion shows up in ``git status`` once the operator
runs ``engram sync --push`` (or the next ``engram serve`` cycle picks
it up automatically when ``auto_push_on_capture`` is on).

Confirmation contract:

* ``--dry-run`` prints what would be deleted and exits 0.
* ``--yes`` skips the interactive prompt. Intended for CI/scripts that
  have already validated the id; documented as dangerous in ``--help``.
* Otherwise, the command prints the thought's content and prompts the
  operator to type the literal string ``delete``. Any other input
  aborts.

Audit trail: every deletion emits a structured INFO log line via
:meth:`engram.storage.facade.VaultStorage.delete` (``thought_deleted
id=... prefix=... ...``). The daemon log captures these with 7-day
retention.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import typer

from engram.config.loader import load_config
from engram.embedding.fastembed import FastEmbedProvider
from engram.errors import ConfigError, ThoughtNotFoundError, VaultReadOnlyError
from engram.errors import IndexError as EngramIndexError
from engram.storage.facade import VaultStorage

_TYPED_CONFIRMATION_TOKEN = "delete"  # noqa: S105 - confirmation word, not a credential


def _print_preview(thought_id: UUID, t: object) -> None:
    """Print the thought's metadata + full body for the operator to inspect."""
    # ``t`` is an ``engram.models.thought.Thought`` but typed as object to
    # avoid an import cycle at module import time.
    typer.echo(f"id:            {thought_id}")
    typer.echo(f"prefix:        {getattr(t, 'prefix', '')}")
    typer.echo(f"portability:   {getattr(t, 'portability', '')}")
    typer.echo(f"vault:         {getattr(t, 'vault', '')}")
    created_at = getattr(t, "created_at", None)
    if created_at is not None:
        typer.echo(f"created_at:    {created_at.isoformat()}")
    file_path = getattr(t, "file_path", None)
    if file_path is not None:
        typer.echo(f"file_path:     {file_path}")
    typer.echo("---")
    typer.echo(getattr(t, "content", "") or "")
    typer.echo("---")


def register(app: typer.Typer) -> None:
    """Attach the ``delete`` subcommand."""

    @app.command(name="delete")
    def delete_cmd(
        thought_id: str = typer.Argument(
            ...,
            metavar="ID",
            help="UUID of the thought to delete.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Show what would be deleted and exit without modifying the vault.",
        ),
        yes: bool = typer.Option(
            False,
            "--yes",
            help=(
                "Skip the typed-confirmation prompt. Dangerous; intended for "
                "CI/scripts that have already validated the id."
            ),
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
        """Permanently delete a thought from the vault.

        Removes the markdown file, the SQLite row, and the embedding.
        The deletion propagates to other machines on the next git push
        (run ``engram sync --push`` after this command to push
        immediately).
        """
        try:
            parsed_id = UUID(thought_id)
        except ValueError as exc:
            typer.secho(
                f"engram delete: invalid UUID: {thought_id!r}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2) from exc

        try:
            config = load_config(
                explicit_vault_config=config_path,
                vault_name=vault_name,
            )
        except ConfigError as exc:
            typer.secho(f"engram delete: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

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
            typer.secho(f"engram delete: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        exit_code = 0
        try:
            existing = storage.get_by_id(parsed_id)
            if existing is None:
                typer.secho(
                    f"engram delete: not found: no thought with id={parsed_id}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
                raise typer.Exit(1)

            _print_preview(parsed_id, existing)

            if dry_run:
                typer.echo("Dry run - nothing deleted.")
                raise typer.Exit(0)

            if not yes:
                typed = typer.prompt(
                    "Type 'delete' to confirm permanent deletion, or Ctrl-C to abort",
                    default="",
                    show_default=False,
                )
                if typed != _TYPED_CONFIRMATION_TOKEN:
                    typer.secho("Aborted.", fg=typer.colors.YELLOW, err=True)
                    raise typer.Exit(1)

            try:
                deleted = storage.delete(parsed_id, source="cli")
            except ThoughtNotFoundError as exc:
                # Race: another process deleted between our preview and
                # confirm. Report and exit 1 rather than crash.
                typer.secho(
                    f"engram delete: {exc}",
                    fg=typer.colors.YELLOW,
                    err=True,
                )
                raise typer.Exit(1) from exc
            except VaultReadOnlyError as exc:
                typer.secho(f"engram delete: {exc}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from exc

            short_id = str(deleted.id)[:8]
            typer.echo(f"Deleted: {deleted.prefix}/{short_id}")
            typer.echo(
                "Run `engram sync --push` to propagate the deletion to "
                "other machines, or wait for the next serve cycle.",
            )
        except typer.Exit as exit_exc:
            exit_code = exit_exc.exit_code
            raise
        finally:
            storage.close()
        # Defensive: unreachable but keeps mypy happy on the implicit
        # return path.
        if exit_code != 0:  # pragma: no cover
            raise typer.Exit(exit_code)


__all__ = ["register"]
