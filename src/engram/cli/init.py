"""``engram init <path>`` - scaffold a fresh vault directory.

Creates the on-disk skeleton a vault needs:

* ``<path>/thoughts/<prefix-dir>/`` for each canonical prefix
* ``<path>/.indexes/`` for the SQLite + sqlite-vec index files
* ``<path>/engram.config.yaml`` with starter values
* ``<path>/.gitignore`` ignoring ``.indexes/``, ``*.tmp``, swap files, ``.DS_Store``
* ``<path>/README.md`` stub

Refuses to overwrite an existing vault (any of the above already present).
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from engram.errors import VaultError
from engram.models.frontmatter import CANONICAL_PREFIXES
from engram.utils.file_naming import derive_prefix_dirname

_log = logging.getLogger("engram.cli.init")


_GITIGNORE_BODY = """\
# engram-managed; do not commit the regenerable index or transient files.
.indexes/
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.tmp
*.swp
*.swo
.DS_Store
# Per-vault identity is machine-local (Phase 2 R-H3 cross-vault contamination guard).
.engram/identity.local
"""

_README_STUB = """\
# Engram Vault

This directory is an engram vault: markdown thoughts under `thoughts/`,
a regenerable SQLite + sqlite-vec index under `.indexes/`.

* `engram serve` to run the MCP server
* `engram doctor` to verify health
* `engram reindex` to rebuild the index from markdown

The markdown files ARE the source of truth. The `.indexes/` directory
is regenerable from them via `engram reindex --full`.
"""


def _starter_config(vault_name: str) -> str:
    return f"""\
vault_name: {vault_name}
thoughts_dir: ./thoughts
index_dir: ./.indexes
embedding_model: BAAI/bge-small-en-v1.5
sync:
  auto_pull_on_startup: true
  auto_commit_on_capture: true
  auto_push_on_capture: false
  git_remote: origin
  git_branch: main
"""


def _create_vault(vault_path: Path, *, vault_name: str | None = None) -> None:
    """Build the vault skeleton at ``vault_path``."""
    if any((vault_path / sub).exists() for sub in ("thoughts", ".indexes", "engram.config.yaml")):
        msg = (
            f"vault path {vault_path} already contains engram artifacts; "
            "refusing to overwrite. Move or delete the existing vault first."
        )
        raise VaultError(msg)

    vault_path.mkdir(parents=True, exist_ok=True)
    thoughts_dir = vault_path / "thoughts"
    thoughts_dir.mkdir()
    (vault_path / ".indexes").mkdir()

    for prefix in CANONICAL_PREFIXES:
        (thoughts_dir / derive_prefix_dirname(prefix)).mkdir(parents=True, exist_ok=True)

    resolved_name = vault_name or vault_path.name or "default"
    (vault_path / "engram.config.yaml").write_text(_starter_config(resolved_name), encoding="utf-8")
    (vault_path / ".gitignore").write_text(_GITIGNORE_BODY, encoding="utf-8")
    (vault_path / "README.md").write_text(_README_STUB, encoding="utf-8")


def register(app: typer.Typer) -> None:
    """Attach the ``init`` subcommand to a typer app."""

    @app.command(name="init")
    def init_cmd(
        path: Path = typer.Argument(  # noqa: B008
            ...,
            help="Directory to scaffold as a new engram vault.",
        ),
        vault_name: str | None = typer.Option(
            None,
            "--vault-name",
            help="Name to record in engram.config.yaml; defaults to the directory name.",
        ),
    ) -> None:
        """Scaffold a fresh engram vault at PATH."""
        path = path.expanduser().resolve()
        try:
            _create_vault(path, vault_name=vault_name)
        except VaultError as exc:
            typer.secho(f"engram init failed: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc
        typer.echo(f"engram vault initialized at {path}")
        typer.echo(
            "Next: configure ~/.config/engram/config.yaml to mount it, then run `engram doctor`."
        )


__all__ = ["register"]
