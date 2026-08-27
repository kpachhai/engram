"""``engram doctor`` CLI command - thin wrapper over engram.diagnostics.run_diagnostics.

The LLM rows (provider reachability, daily cost cap) run for every
install via :func:`engram.diagnostics.multivault_checks.run_llm_checks`.
When the per-user config lists more than one vault, the CLI ALSO runs
the cross-vault checks via
:func:`engram.diagnostics.multivault_checks.run_multivault_checks`. The
single-vault rows still surface for the targeted vault.

The default exit code counts a skipped row as clean, which is a published
contract; ``--strict`` is the opt-in that exits 3 when any row never ran.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import typer

from engram.config.loader import _load_user_config_if_present, load_config
from engram.diagnostics.doctor import CheckStatus, DoctorReport, run_diagnostics
from engram.diagnostics.multivault_checks import run_multivault_checks
from engram.errors import ConfigError

_STATUS_COLOR = {
    CheckStatus.OK: typer.colors.GREEN,
    CheckStatus.SKIP: typer.colors.BLUE,
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
        strict: bool = typer.Option(
            False,
            "--strict",
            help=(
                "Exit 3 when any check did not run. Without it the exit code "
                "cannot tell a skipped check from a passing one."
            ),
        ),
        print_hashes: bool = typer.Option(
            False,
            "--print-hashes",
            help=(
                "After --download-model, print SHA-256 hashes of the cached "
                "model files in manifest-ready format and exit. Used by "
                "maintainers to populate engram/embedding/model_hashes.py."
            ),
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

        if print_hashes:
            _print_model_hashes(config)
            raise typer.Exit(0)

        report = run_diagnostics(
            config,
            download_model=download_model,
            repair=repair,
            remove_orphans=remove_orphans,
        )

        # LLM rows: measured against the configured provider and budget,
        # for every install rather than only multi-vault ones.
        _append_llm_rows(report=report, config=config)

        # Multi-vault checks: when the per-user config has >1
        # vault entry, surface the cross-vault rows on top. The
        # registry built here is for read-only inspection; we close it
        # before exiting.
        user_config = _load_user_config_if_present()
        if user_config is not None and len(user_config.vaults) > 1:
            _append_phase3_rows(report=report, user_config=user_config)

        # Team-vault checks: surfaced whenever a team-write vault is
        # configured (enrollment, pending pushes, orphan quarantine,
        # routing-priority collisions).
        if user_config is not None and any(v.role == "team-write" for v in user_config.vaults):
            from engram.diagnostics.team_checks import run_team_checks

            try:
                run_team_checks(
                    report,
                    user_config,
                    primary_vault_path=config.vault_path,
                )
            except Exception as exc:
                report.add(
                    "team_checks_internal",
                    CheckStatus.FAIL,
                    "team-vault diagnostics raised an unexpected error",
                    detail=str(exc),
                )

        for check in report.checks:
            color = _STATUS_COLOR[check.status]
            label = check.status.value.upper()
            typer.secho(f"  [{label:4}] {check.name}: {check.message}", fg=color)
            if check.detail:
                typer.echo(f"           {check.detail}")

        typer.echo()
        # Print the size of each bucket, including the zero ones. A verdict
        # alone hides a run whose executed-check count collapsed: "34 checks"
        # and "20 executed, 14 skipped" read identically without this line.
        counts = {status: 0 for status in CheckStatus}
        for check in report.checks:
            counts[check.status] += 1
        typer.echo(
            f"engram doctor: {len(report.checks)} checks - "
            f"{counts[CheckStatus.OK]} passed, "
            f"{counts[CheckStatus.SKIP]} skipped, "
            f"{counts[CheckStatus.WARN]} warnings, "
            f"{counts[CheckStatus.FAIL]} failures"
        )
        exit_code = report.strict_exit_code if strict else report.exit_code
        message, color = _verdict_line(
            exit_code=exit_code,
            skipped=counts[CheckStatus.SKIP],
            total=len(report.checks),
        )
        typer.secho(message, fg=color)

        raise typer.Exit(exit_code)


def _verdict_line(*, exit_code: int, skipped: int, total: int) -> tuple[str, str]:
    """The closing verdict sentence and its colour.

    Exit 0 with skipped rows is not "all checks green": those rows answered
    nothing, and a verdict that reads identically either way lets "did not
    run" pass for "passed". Without ``--strict`` only the sentence changes -
    the exit code treats a skip as clean, because it is an unanswered
    question rather than a degradation, and because wrappers already branch
    on it. ``--strict`` raises the same run to exit 3.
    """
    if total == 0:
        return (
            "engram doctor: no checks ran at all - the diagnostic examined "
            "nothing, which is a wiring failure and not a clean vault; check "
            "that --config or --vault names a real vault",
            typer.colors.RED,
        )
    if exit_code == 3:
        return (
            f"engram doctor: --strict: {skipped} of {total} checks did not "
            "run; each [SKIP] row above names the precondition it wanted",
            typer.colors.RED,
        )
    if exit_code == 0:
        if skipped:
            return (
                f"engram doctor: no warnings or failures, but {skipped} of "
                f"{total} checks did not run",
                typer.colors.BLUE,
            )
        return "engram doctor: all checks green", typer.colors.GREEN
    if exit_code == 1:
        return (
            "engram doctor: warnings (operational, with caveats)",
            typer.colors.YELLOW,
        )
    return "engram doctor: failures detected", typer.colors.RED


def _append_llm_rows(*, report: DoctorReport, config: object) -> None:
    """Measure the LLM rows against the configured provider and budget.

    Doctor previously called both checks with no provider and no budget, so
    each took its "nothing configured" branch and reported OK for something
    it had never looked at.
    """
    from engram.diagnostics.multivault_checks import run_llm_checks
    from engram.llm.budget import LLMBudget, usage_state_path_for
    from engram.llm.protocol import LLMProvider
    from engram.llm.resolver import resolve_provider

    llm_config = getattr(config, "llm", None)
    configured = llm_config is not None and llm_config.provider is not None
    cap = float(getattr(llm_config, "daily_cost_cap_usd", 0.0) or 0.0)

    provider: LLMProvider | None = None
    unmeasured_reason: str | None = None
    if configured:
        try:
            # No thoughts: this resolves the provider only, so the
            # portability gates have nothing to refuse.
            provider = resolve_provider([], config)  # type: ignore[arg-type]
        except Exception as exc:
            unmeasured_reason = f"{type(exc).__name__}: {exc}"

    budget: LLMBudget | None = None
    index_dir = getattr(config, "index_dir", None)
    if configured and cap > 0 and index_dir is not None:
        budget = LLMBudget.load_or_init(
            state_path=usage_state_path_for(primary_vault_index_dir=index_dir),
            daily_cost_cap_usd=cap,
        )

    run_llm_checks(
        report,
        provider=provider,
        budget=budget,
        daily_cost_cap_usd=cap if configured else 0.0,
        configured=configured,
        unmeasured_reason=unmeasured_reason,
    )


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
        run_multivault_checks(
            report,
            user_config=user_config,  # type: ignore[arg-type]
            registry=registry,
            per_vault_llm=per_vault_llm,
        )
    finally:
        for storage in reversed(storages):
            with contextlib.suppress(Exception):
                storage.close()


def _print_model_hashes(config: object) -> None:
    """Compute + print SHA-256 hashes of cached model files (maintainer helper).

    Used by ``engram doctor --download-model --print-hashes`` after a
    model upgrade to recompute the manifest pinned in
    :mod:`engram.embedding.model_hashes`.
    """
    import hashlib

    from engram.embedding.fastembed import FastEmbedProvider

    embedding_model = getattr(config, "embedding_model", "BAAI/bge-small-en-v1.5")
    index_dir = getattr(config, "index_dir", None)
    cache_dir = index_dir / "fastembed" if index_dir is not None else None
    provider = FastEmbedProvider(
        model_name=embedding_model,
        cache_dir=cache_dir,
    )
    # Force load so the cache is populated.
    provider.embed("hash-manifest probe")
    files = provider.list_cached_files()
    if not files:
        typer.secho(
            "no cached model files found; run with --download-model and "
            "ensure the model downloaded successfully",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"# SHA-256 manifest for {embedding_model!r}")
    typer.echo("# Paste into engram/embedding/model_hashes.py")
    typer.echo("{")
    for name in sorted(files):
        path = files[name].resolve()
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        typer.echo(f'    "{name}": "{digest}",')
    typer.echo("}")


__all__ = ["register"]
