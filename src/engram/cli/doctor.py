"""``engram doctor`` CLI command - thin wrapper over engram.diagnostics.run_diagnostics."""

from __future__ import annotations

from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.diagnostics.doctor import CheckStatus, run_diagnostics
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


__all__ = ["register"]
