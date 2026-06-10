"""``engram consolidate`` - report-then-action curation of the semantic index.

Report mode (default) is zero-vault-mutation: it opens the index read-only
(safe beside a live daemon), runs the detection passes, and writes a JSON
report under ``<vault>/.indexes/consolidate/``. Note that LLM passes still
write budget usage state and send portable thought content to the resolved
provider - zero VAULT mutation, not zero egress.

``--apply`` executes merge proposals only: originals are archived
body-immutably under ``<vault>/archive/`` and the index rows are removed.
It requires the daemon stopped (``engram daemon stop``), holds the vault
lock for the full run, and asks for typed confirmation (``consolidate``).
Stale and contradiction findings are report-only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import typer

from engram.config.loader import load_config
from engram.config.models import EffectiveConfig
from engram.consolidate.guards import acquire_apply_lock, ensure_vault_applyable
from engram.consolidate.llm import build_distiller, build_judge
from engram.consolidate.models import ClusterAction, ConsolidationReport, PassStatus
from engram.consolidate.passes import DistillFn, JudgeFn, ReportSettings, generate_report
from engram.consolidate.report import (
    consolidate_state_dir,
    latest_report_path,
    load_report,
    write_report,
)
from engram.errors import (
    ConfigError,
    ConsolidateError,
    EngramError,
    LLMProviderError,
    VaultReadOnlyError,
)
from engram.errors import IndexError as EngramIndexError
from engram.models import ThoughtWithSimilarity
from engram.storage.markdown import read_thought
from engram.storage.sqlite import open_connection_readonly
from engram.storage.sqlite_queries import get_thought_row, list_all_thought_rows

_TYPED_CONFIRMATION_TOKEN = "consolidate"  # noqa: S105 - confirmation word, not a credential

#: Exit codes: 0 complete, 1 error, 2 refused, 3 partially applied.
EXIT_PARTIAL = 3


def _fail(message: str, *, code: int = 2) -> typer.Exit:
    typer.secho(f"engram consolidate: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _load(config_path: Path | None, vault_name: str | None) -> EffectiveConfig:
    try:
        return load_config(explicit_vault_config=config_path, vault_name=vault_name)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


def _vault_role(config: EffectiveConfig) -> str:
    for mount in config.vaults:
        if mount.name == config.vault_name:
            return mount.role
    if (config.vault_path / ".engram" / "members.yaml").exists():
        # Defense-in-depth: a team vault is a team vault even when the
        # mount metadata is absent (explicit --config path).
        return "team-write"
    return "primary"


def _content_loader(conn: sqlite3.Connection, thoughts_dir: Path) -> Callable[[str], str]:
    def _load_content(thought_id: str) -> str:
        row = get_thought_row(conn, thought_id)
        if row is None:
            return ""
        result = read_thought((thoughts_dir / str(row["file_path"])).resolve())
        if result is None or result[0] is None:
            return ""
        return result[0].content

    return _load_content


def _build_llm_callables(
    config: EffectiveConfig,
    conn: sqlite3.Connection,
    *,
    no_llm: bool,
    prefix: str | None = None,
) -> tuple[JudgeFn | None, DistillFn | None, str | None]:
    """Resolve a provider over the non-block, prefix-scoped corpus.

    Scoping matters: a sensitive thought OUTSIDE the run's --prefix scope
    must not disable the LLM for an all-portable prefix run (it will never
    appear in any prompt). Degrades to None on refusal.
    """
    if no_llm:
        return None, None, None
    if config.llm.provider is None:
        return None, None, "no LLM provider configured; clusters will be manual-review"
    from engram.llm.resolver import resolve_provider
    from engram.mcp.llm_tools import build_default_budget

    rows = list_all_thought_rows(conn, prefix=prefix)
    corpus = [
        ThoughtWithSimilarity.model_validate(
            {
                "id": row["id"],
                "schema_version": row["schema_version"],
                "prefix": row["prefix"],
                "portability": row["portability"],
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "fingerprint": row["fingerprint"],
                "tags": row["tags"],
                "vault": row["vault_name"],
                "legacy_id": row["legacy_id"],
                "captured_by": row.get("captured_by"),
                "content": "",
                "file_path": Path(str(row["file_path"])),
                "similarity": 1.0,
            }
        )
        for row in rows
        if row["portability"] != "block"
    ]
    try:
        provider = resolve_provider(corpus, config)
    except (LLMProviderError, EngramError) as exc:
        return None, None, f"LLM unavailable ({exc}); clusters will be manual-review"
    budget = build_default_budget(config)
    judge = build_judge(provider=provider, llm_config=config.llm, budget=budget)
    distiller = build_distiller(provider=provider, llm_config=config.llm, budget=budget)
    return judge, distiller, None


def _pass_line(name: str, status: PassStatus) -> str:
    suffix = f" ({status.reason})" if status.reason else ""
    return f"  {name:<16} {status.state.value:<10} {status.done}/{status.total}{suffix}"


def _echo_report_summary(report: ConsolidationReport, report_path: Path) -> None:
    actionable = [c for c in report.clusters if c.action is not ClusterAction.MANUAL_REVIEW]
    reviews = [c for c in report.clusters if c.action is ClusterAction.MANUAL_REVIEW]
    typer.echo(f"Vault: {report.vault_name} (model {report.embedding_model})")
    typer.echo(_pass_line("near-duplicates", report.pass_near_duplicate))
    typer.echo(_pass_line("merge proposals", report.pass_merge))
    typer.echo(_pass_line("stale (age-only)", report.pass_stale))
    typer.echo(_pass_line("contradictions", report.pass_contradiction))
    exclusions = report.exclusions
    if exclusions.pending_embeddings or exclusions.failed_embeddings:
        typer.echo(
            f"  excluded: {exclusions.pending_embeddings} pending / "
            f"{exclusions.failed_embeddings} failed embeddings - "
            "run `engram doctor --repair` first for full coverage"
        )
    if exclusions.block_thoughts_llm:
        typer.echo(
            f"  excluded from LLM passes: {exclusions.block_thoughts_llm} "
            "block-portability thought(s)"
        )
    if exclusions.future_dated:
        typer.echo(
            f"  data quality: {exclusions.future_dated} future-dated thought(s); "
            "check machine clocks"
        )
    typer.echo(
        f"Proposals: {len(actionable)} actionable, {len(reviews)} manual-review, "
        f"{len(report.stale_candidates)} stale candidate(s), "
        f"{len(report.contradiction_candidates)} contradiction candidate(s)"
    )
    typer.echo(f"Report: {report_path}")
    if actionable:
        typer.echo("Review the report, then run `engram consolidate --apply`.")


def register(app: typer.Typer) -> None:
    """Attach the ``consolidate`` subcommand."""

    @app.command(name="consolidate")
    def consolidate_cmd(
        apply: bool = typer.Option(
            False,
            "--apply",
            help=(
                "Execute the merge proposals from a report (default: newest). "
                "Requires the daemon stopped; archives originals under "
                "<vault>/archive/ and curates the index."
            ),
        ),
        report_path_opt: Path | None = typer.Option(  # noqa: B008
            None,
            "--report",
            help="Apply a specific report file instead of the newest one.",
        ),
        threshold: float = typer.Option(
            0.90,
            "--threshold",
            min=0.0,
            max=1.0,
            help="Near-duplicate similarity threshold.",
        ),
        contradiction_threshold: float = typer.Option(
            0.75,
            "--contradiction-threshold",
            min=0.0,
            max=1.0,
            help="Lower bound of the contradiction candidate band.",
        ),
        stale_days: int = typer.Option(
            180,
            "--stale-days",
            min=1,
            help="Age threshold for stale candidates (age-only; report-only).",
        ),
        max_cluster_size: int = typer.Option(
            12,
            "--max-cluster-size",
            min=2,
            help="Clusters larger than this become manual-review, never auto-merged.",
        ),
        prefix: str | None = typer.Option(
            None,
            "--prefix",
            help="Scope the run to one prefix (e.g. Lesson).",
        ),
        no_llm: bool = typer.Option(
            False,
            "--no-llm",
            help=(
                "Skip LLM judging/distillation: contradiction pass is skipped and "
                "near-duplicate clusters are emitted as manual-review."
            ),
        ),
        yes: bool = typer.Option(
            False,
            "--yes",
            help="Skip the typed-confirmation prompt on --apply (CI/scripts).",
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
        """Detect near-duplicates, stale thoughts, and contradictions; curate on --apply.

        Report mode never mutates the vault. --apply executes merge proposals
        only (exact-duplicate keep-newest + LLM-distilled merges): originals
        move to <vault>/archive/ with their bodies untouched, and only the
        SQLite index is curated. Archiving is NOT deletion - archived content
        stays in archive/ and in git history.
        """
        config = _load(config_path, vault_name)
        if apply:
            _run_apply_mode(config, report_path_opt, yes=yes)
            return
        settings = ReportSettings(
            near_dup_threshold=threshold,
            contradiction_threshold=contradiction_threshold,
            stale_days=stale_days,
            max_cluster_size=max_cluster_size,
            prefix=prefix,
        )
        _run_report_mode(config, settings, no_llm=no_llm)


def _run_report_mode(config: EffectiveConfig, settings: ReportSettings, *, no_llm: bool) -> None:
    db_path = config.index_dir / "engram.db"
    try:
        conn = open_connection_readonly(db_path)
    except EngramIndexError as exc:
        raise _fail(str(exc)) from exc
    try:
        judge, distiller, llm_notice = _build_llm_callables(
            config, conn, no_llm=no_llm, prefix=settings.prefix
        )
        if llm_notice:
            typer.secho(f"note: {llm_notice}", fg=typer.colors.YELLOW, err=True)
        try:
            report = generate_report(
                conn=conn,
                vault_name=config.vault_name,
                configured_model=config.embedding_model,
                now=datetime.now(UTC),
                settings=settings,
                content_loader=_content_loader(conn, config.thoughts_dir),
                judge=judge,
                distiller=distiller,
            )
        except ConsolidateError as exc:
            raise _fail(str(exc)) from exc
    finally:
        conn.close()
    report_path = write_report(report, index_dir=config.index_dir)
    _echo_report_summary(report, report_path)


def _run_apply_mode(config: EffectiveConfig, report_path_opt: Path | None, *, yes: bool) -> None:
    from engram.consolidate.apply import apply_report
    from engram.embedding.fastembed import FastEmbedProvider
    from engram.storage.facade import VaultStorage

    try:
        ensure_vault_applyable(role=_vault_role(config), vault_path=config.vault_path)
    except (VaultReadOnlyError, ConsolidateError) as exc:
        raise _fail(str(exc)) from exc

    resolved_report_path = report_path_opt or latest_report_path(config.index_dir)
    if resolved_report_path is None:
        raise _fail("no report found; run `engram consolidate` first")
    try:
        report = load_report(resolved_report_path)
    except ConsolidateError as exc:
        raise _fail(str(exc)) from exc
    if report.vault_name != config.vault_name:
        raise _fail(
            f"report was generated for vault {report.vault_name!r}, "
            f"not {config.vault_name!r}; re-run `engram consolidate`"
        )

    actionable = [c for c in report.clusters if c.action is not ClusterAction.MANUAL_REVIEW]
    if not actionable:
        typer.echo("Nothing to apply: the report has no actionable proposals.")
        raise typer.Exit(0)

    merges = sum(1 for c in actionable if c.action is ClusterAction.MERGE)
    keeps = len(actionable) - merges
    archived_count = sum(
        len(c.members) if c.action is ClusterAction.MERGE else len(c.members) - 1
        for c in actionable
    )
    typer.echo(
        f"About to apply {merges} merge(s) + {keeps} keep-newest cluster(s); "
        f"{archived_count} original(s) will be archived to "
        f"{config.vault_path / 'archive'} (bodies untouched; this is NOT deletion)."
    )
    if not yes:
        typed = typer.prompt(
            "Type 'consolidate' to proceed, or Ctrl-C to abort",
            default="",
            show_default=False,
        )
        if typed != _TYPED_CONFIRMATION_TOKEN:
            typer.secho("Aborted.", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(1)

    try:
        lock = acquire_apply_lock(config.vault_path)
    except ConsolidateError as exc:
        raise _fail(str(exc)) from exc

    try:
        try:
            storage = VaultStorage(
                thoughts_dir=config.thoughts_dir,
                index_db_path=config.index_dir / "engram.db",
                embedding_dim=FastEmbedProvider(model_name=config.embedding_model).dimension,
                embedding_model_name=config.embedding_model,
                vault_name=config.vault_name,
            )
        except EngramIndexError as exc:
            raise _fail(str(exc)) from exc

        embedder = FastEmbedProvider(model_name=config.embedding_model)

        def _embed(content: str) -> list[float]:
            return embedder.embed(content)

        try:
            result = apply_report(
                storage=storage,
                report=report,
                report_path=resolved_report_path,
                archive_dir=config.vault_path / "archive",
                journal_dir=consolidate_state_dir(config.index_dir),
                embed_fn=_embed,
                now=datetime.now(UTC),
                commit=True,
            )
        finally:
            storage.close()
    finally:
        lock.release()

    typer.echo(
        f"Applied {result.applied} cluster(s); skipped {result.skipped}; failed {result.failed}."
    )
    if result.commit:
        typer.echo(f"Consolidation commit: {result.commit}")
    elif result.applied:
        typer.echo("Vault is not a git repository; archive applied on disk, no commit.")
    if result.skipped or result.failed:
        typer.echo(
            "Some proposals did not apply (vault changed since the report, or "
            "errors occurred); re-run `engram consolidate` for a fresh report."
        )
        raise typer.Exit(EXIT_PARTIAL)


__all__ = ["register"]
