"""``engram doctor`` CLI command - thin wrapper over engram.diagnostics.run_diagnostics.

When the per-user config lists more than one vault (the multi-vault
case), the CLI ALSO runs the multi-vault checks via
:func:`engram.diagnostics.phase3_checks.run_phase3_checks`. The
single-vault rows still surface for the targeted vault; the multi-vault
rows surface eight cross-vault invariants on top.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import typer

from engram.config.loader import _load_user_config_if_present, load_config
from engram.diagnostics.doctor import CheckStatus, DoctorReport, run_diagnostics
from engram.diagnostics.phase3_checks import run_phase3_checks
from engram.errors import ConfigError

_STATUS_COLOR = {
    CheckStatus.OK: typer.colors.GREEN,
    CheckStatus.WARN: typer.colors.YELLOW,
    CheckStatus.FAIL: typer.colors.RED,
}


def register(app: typer.Typer) -> None:
    """Attach the ``doctor`` subcommand."""

    @app.command(name="doctor")
    def doctor_cmd(
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
        download_model: bool = typer.Option(
            False,
            "--download-model",
            help="Force-load the embedding model (triggers HuggingFace download).",
        ),
        repair: bool = typer.Option(
            False,
            "--repair",
            help="Regenerate pending embeddings via reindex --repair.",
        ),
        remove_orphans: bool = typer.Option(
            False,
            "--remove-orphans",
            help="With --repair: also delete SQLite rows whose markdown is missing.",
        ),
    ) -> None:
        """Run health checks against the configured vault."""
        try:
            config = load_config(
                explicit_vault_config=config_path,
                vault_name=vault_name,
            )
        except ConfigError as exc:
            typer.secho(f"engram doctor: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from exc

        report = run_diagnostics(
            config,
            download_model=download_model,
            repair=repair,
            remove_orphans=remove_orphans,
        )

        # Multi-vault checks: when the per-user config has >1
        # vault entry, surface the eight cross-vault rows on top. The
        # registry built here is for read-only inspection; we close it
        # before exiting.
        user_config = _load_user_config_if_present()
        if user_config is not None and len(user_config.vaults) > 1:
            _append_phase3_rows(report=report, user_config=user_config)

        for check in report.checks:
            color = _STATUS_COLOR[check.status]
            label = check.status.value.upper()
            typer.secho(f"  [{label:4}] {check.name}: {check.message}", fg=color)
            if check.detail:
                typer.echo(f"           {check.detail}")

        typer.echo()
        if report.exit_code == 0:
            typer.secho("engram doctor: all checks green", fg=typer.colors.GREEN)
        elif report.exit_code == 1:
            typer.secho(
                "engram doctor: warnings (operational, with caveats)", fg=typer.colors.YELLOW
            )
        else:
            typer.secho("engram doctor: failures detected", fg=typer.colors.RED)

        raise typer.Exit(report.exit_code)


def _append_phase3_rows(*, report: DoctorReport, user_config: object) -> None:
    """Mount each user-config vault read-only + run the multi-vault checks.

    The registry built here exists for the duration of doctor's read-only
    pass; storages are closed in reverse-mount order before returning so
    the next ``engram serve`` invocation can re-open them.
    """
    from engram.embedding.fastembed import FastEmbedProvider
    from engram.multivault.registry import VaultRegistry
    from engram.storage.facade import VaultStorage
    from engram.storage.sqlite import set_setting

    registry = VaultRegistry()
    storages: list[VaultStorage] = []
    embedder: FastEmbedProvider | None = None
    try:
        for mount in user_config.vaults:  # type: ignore[attr-defined]
            vault_path = mount.path.expanduser().resolve()
            if not vault_path.exists():
                continue
            try:
                if embedder is None:
                    embedder = FastEmbedProvider(model_name="BAAI/bge-small-en-v1.5")
                storage = VaultStorage(
                    thoughts_dir=vault_path / "thoughts",
                    index_db_path=vault_path / ".indexes" / "engram.db",
                    embedding_dim=embedder.dimension,
                    embedding_model_name="BAAI/bge-small-en-v1.5",
                    vault_name=mount.name,
                )
                set_setting(storage.conn, "embedding_model_name", "BAAI/bge-small-en-v1.5")
                set_setting(storage.conn, "embedding_dim", str(embedder.dimension))
                storages.append(storage)
                registry.mount(name=mount.name, storage=storage, role=mount.role)
            except Exception as exc:
                # Mount failure surfaces as a doctor row so the operator
                # sees what went wrong without taking down the whole pass.
                typer.echo(
                    f"  doctor: skipping vault {mount.name!r}: {exc}",
                    err=True,
                )
                continue

        per_vault_llm = {
            mount.name: getattr(mount, "llm", None)
            for mount in user_config.vaults  # type: ignore[attr-defined]
        }
        run_phase3_checks(
            report,
            user_config=user_config,  # type: ignore[arg-type]
            registry=registry,
            per_vault_llm=per_vault_llm,
        )
    finally:
        for storage in reversed(storages):
            with contextlib.suppress(Exception):
                storage.close()


__all__ = ["register"]
