"""Doctor diagnostic checks.

The doctor command runs a series of read-only checks against an engram vault
and reports their status. Each check produces a :class:`CheckResult` with one
of three statuses: ``OK``, ``WARN``, or ``FAIL``. The aggregated
:class:`DoctorReport` exposes ``exit_code`` per the spec convention:

* ``0`` - all green
* ``1`` - warnings only (degraded but operational)
* ``2`` - any failure (refuse to serve)
"""

from __future__ import annotations

import enum
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from engram.config.models import EffectiveConfig
from engram.embedding.fastembed import FastEmbedProvider
from engram.embedding.protocol import EmbeddingProvider
from engram.errors import EmbeddingError
from engram.errors import IndexError as EngramIndexError
from engram.storage.facade import VaultStorage
from engram.storage.reindex import ReindexMode, reindex_vault
from engram.storage.sqlite import (
    SETTING_EMBEDDING_DIM,
    SETTING_EMBEDDING_MODEL_NAME,
    get_setting,
)
from engram.storage.sqlite_queries import (
    get_stats,
    iter_all_thought_paths,
)

_log = logging.getLogger("engram.diagnostics.doctor")


class CheckStatus(enum.StrEnum):
    """Per-check outcome category."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row of the doctor report."""

    name: str
    status: CheckStatus
    message: str
    detail: str | None = None


@dataclass(slots=True)
class DoctorReport:
    """Aggregated diagnostic output."""

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """0 if all OK; 1 if any WARN (no FAILs); 2 if any FAIL."""
        if any(c.status is CheckStatus.FAIL for c in self.checks):
            return 2
        if any(c.status is CheckStatus.WARN for c in self.checks):
            return 1
        return 0

    def add(
        self,
        name: str,
        status: CheckStatus,
        message: str,
        detail: str | None = None,
    ) -> None:
        """Append a check result to the report."""
        self.checks.append(CheckResult(name=name, status=status, message=message, detail=detail))


# === individual checks ===


def _check_thoughts_dir(report: DoctorReport, config: EffectiveConfig) -> None:
    path = config.thoughts_dir
    if not path.exists():
        report.add(
            "thoughts_dir",
            CheckStatus.FAIL,
            f"thoughts directory does not exist: {path}",
        )
        return
    if not os.access(path, os.R_OK | os.W_OK):
        report.add(
            "thoughts_dir",
            CheckStatus.FAIL,
            f"thoughts directory not readable+writable: {path}",
        )
        return
    report.add("thoughts_dir", CheckStatus.OK, f"readable+writable at {path}")


def _check_index_dir(report: DoctorReport, config: EffectiveConfig) -> None:
    path = config.index_dir
    if not path.exists():
        report.add(
            "index_dir",
            CheckStatus.FAIL,
            f"index directory does not exist: {path}",
        )
        return
    if not os.access(path, os.R_OK | os.W_OK):
        report.add(
            "index_dir",
            CheckStatus.FAIL,
            f"index directory not readable+writable: {path}",
        )
        return
    report.add("index_dir", CheckStatus.OK, f"readable+writable at {path}")


def _check_sqlite_and_vec(
    report: DoctorReport,
    config: EffectiveConfig,
) -> VaultStorage | None:
    """Open SQLite + sqlite-vec; return the storage handle if successful."""
    try:
        storage = VaultStorage(
            thoughts_dir=config.thoughts_dir,
            index_db_path=config.index_dir / "engram.db",
            embedding_dim=384,
            embedding_model_name=config.embedding_model,
            vault_name=config.vault_name,
        )
    except EngramIndexError as exc:
        report.add(
            "sqlite_vec",
            CheckStatus.FAIL,
            "SQLite + sqlite-vec failed to initialize",
            detail=str(exc),
        )
        return None
    except Exception as exc:
        report.add(
            "sqlite_vec",
            CheckStatus.FAIL,
            "SQLite open raised unexpectedly",
            detail=str(exc),
        )
        return None
    report.add("sqlite_vec", CheckStatus.OK, "SQLite + sqlite-vec extension loaded")
    return storage


def _check_embedding_dimension_recorded(
    report: DoctorReport,
    storage: VaultStorage,
    expected_model: str,
) -> None:
    recorded_dim = get_setting(storage.conn, SETTING_EMBEDDING_DIM)
    recorded_model = get_setting(storage.conn, SETTING_EMBEDDING_MODEL_NAME)
    if recorded_dim is None:
        report.add(
            "embedding_settings",
            CheckStatus.WARN,
            "no embedding dimension recorded yet (fresh vault?)",
        )
        return
    if int(recorded_dim) != storage.embedding_dim:
        report.add(
            "embedding_settings",
            CheckStatus.FAIL,
            (
                f"index dimension {recorded_dim} != configured {storage.embedding_dim}; "
                "run `engram reindex --full --model <name>`"
            ),
        )
        return
    if recorded_model is not None and recorded_model != expected_model:
        report.add(
            "embedding_settings",
            CheckStatus.FAIL,
            (
                f"index model {recorded_model!r} != configured {expected_model!r}; "
                f"run `engram reindex --full --model {expected_model}`"
            ),
        )
        return
    report.add(
        "embedding_settings",
        CheckStatus.OK,
        f"dim {recorded_dim} model {recorded_model or expected_model}",
    )


def _check_embedding_model(
    report: DoctorReport,
    config: EffectiveConfig,
    *,
    download: bool,
) -> EmbeddingProvider | None:
    """Verify (and optionally trigger first download of) the embedding model."""
    provider = FastEmbedProvider(model_name=config.embedding_model)
    if not download:
        report.add(
            "embedding_model",
            CheckStatus.OK,
            f"model {config.embedding_model!r} configured (lazy load on first use)",
        )
        return provider
    try:
        # Force a load + small probe.
        provider.embed("doctor probe")
    except EmbeddingError as exc:
        report.add(
            "embedding_model",
            CheckStatus.FAIL,
            "embedding model failed to load",
            detail=str(exc),
        )
        return None
    except ImportError as exc:
        report.add(
            "embedding_model",
            CheckStatus.FAIL,
            "fastembed not importable",
            detail=str(exc),
        )
        return None
    except Exception as exc:
        report.add(
            "embedding_model",
            CheckStatus.FAIL,
            "embedding model load raised unexpectedly",
            detail=str(exc),
        )
        return None
    report.add(
        "embedding_model",
        CheckStatus.OK,
        f"model {config.embedding_model!r} loaded; dim {provider.dimension}",
    )
    return provider


def _check_index_disk_consistency(
    report: DoctorReport,
    storage: VaultStorage,
) -> None:
    """Compare SQLite row count against on-disk markdown count + report drift."""
    stats = get_stats(storage.conn)
    sqlite_count = int(stats["total_count"])
    on_disk = list(storage.thoughts_dir.rglob("*.md"))
    on_disk_count = len(on_disk)

    if sqlite_count == on_disk_count:
        report.add(
            "index_consistency",
            CheckStatus.OK,
            f"{sqlite_count} thoughts indexed; matches on-disk count",
        )
        return

    drift = abs(sqlite_count - on_disk_count)
    direction = "fewer" if sqlite_count < on_disk_count else "more"
    report.add(
        "index_consistency",
        CheckStatus.WARN,
        (
            f"index has {sqlite_count} rows but disk has {on_disk_count} markdown files "
            f"({drift} {direction} in index); run `engram reindex` to reconcile"
        ),
    )


def _check_orphan_sqlite_rows(
    report: DoctorReport,
    storage: VaultStorage,
) -> None:
    orphans: list[str] = []
    for thought_id, file_path_rel in iter_all_thought_paths(storage.conn):
        abs_path = (storage.thoughts_dir / Path(file_path_rel)).resolve()
        if not abs_path.exists():
            orphans.append(str(thought_id))
    if not orphans:
        report.add("orphan_rows", CheckStatus.OK, "no orphan SQLite rows")
        return
    report.add(
        "orphan_rows",
        CheckStatus.WARN,
        (
            f"{len(orphans)} SQLite rows reference missing markdown files; "
            f"run `engram doctor --repair --remove-orphans` to clean up"
        ),
        detail=", ".join(orphans[:10]) + ("..." if len(orphans) > 10 else ""),
    )


def _check_orphan_tempfiles(
    report: DoctorReport,
    config: EffectiveConfig,
) -> None:
    """A14: surface .tmp files left behind by an interrupted atomic write."""
    tempfiles = list(config.thoughts_dir.rglob("*.tmp"))
    if not tempfiles:
        report.add("orphan_tempfiles", CheckStatus.OK, "no orphan .tmp files")
        return
    report.add(
        "orphan_tempfiles",
        CheckStatus.WARN,
        f"{len(tempfiles)} orphan .tmp file(s) from interrupted atomic writes",
        detail=", ".join(str(p) for p in tempfiles[:5]) + ("..." if len(tempfiles) > 5 else ""),
    )


def _check_pending_embeddings(
    report: DoctorReport,
    storage: VaultStorage,
) -> int:
    """Count rows with embedding_status='pending'; report as WARN if >0."""
    cursor = storage.conn.execute(
        "SELECT COUNT(*) FROM thoughts WHERE embedding_status = 'pending'"
    )
    pending_count = int(cursor.fetchone()[0])
    if pending_count == 0:
        report.add("pending_embeddings", CheckStatus.OK, "no pending embeddings")
        return 0
    report.add(
        "pending_embeddings",
        CheckStatus.WARN,
        f"{pending_count} thought(s) awaiting embedding regeneration; run `engram doctor --repair`",
    )
    return pending_count


def _maybe_repair(
    report: DoctorReport,
    storage: VaultStorage,
    embedder: EmbeddingProvider | None,
    *,
    remove_orphans: bool,
) -> None:
    """If --repair: regenerate pending embeddings + optionally prune orphans."""
    if embedder is None:
        report.add(
            "repair",
            CheckStatus.WARN,
            "skipping repair: embedding model not available",
        )
        return
    repair_report = reindex_vault(
        storage,
        mode=ReindexMode.REPAIR,
        embed_fn=embedder.embed,
    )
    msg = (
        f"repair regenerated {repair_report.embeddings_repaired} pending embedding(s); "
        f"{repair_report.embedding_failures} failure(s)"
    )
    status = CheckStatus.WARN if repair_report.embedding_failures else CheckStatus.OK
    report.add("repair", status, msg)

    if remove_orphans:
        orphan_report = reindex_vault(storage, mode=ReindexMode.REMOVE_ORPHANS)
        report.add(
            "remove_orphans",
            CheckStatus.OK,
            (f"detected {orphan_report.orphans_detected}; removed {orphan_report.orphans_removed}"),
        )


def run_diagnostics(
    config: EffectiveConfig,
    *,
    download_model: bool = False,
    repair: bool = False,
    remove_orphans: bool = False,
    embedder_factory: Callable[[EffectiveConfig], EmbeddingProvider] | None = None,
) -> DoctorReport:
    """Run all diagnostic checks against the configured vault.

    Args:
        config: Resolved :class:`EffectiveConfig` from
            :func:`engram.config.loader.load_config`.
        download_model: If True, force-load the embedding model (triggers
            HuggingFace download on first use).
        repair: If True, regenerate pending embeddings via reindex --repair.
        remove_orphans: If True (and ``repair`` is True), prune SQLite rows
            whose markdown file is missing.
        embedder_factory: Optional override that returns an :class:`EmbeddingProvider`
            given the config. Defaults to building a :class:`FastEmbedProvider`.
            Tests inject a stub here.

    Returns:
        :class:`DoctorReport` with one :class:`CheckResult` per check and an
        ``exit_code`` derived from the worst status.
    """
    report = DoctorReport()
    _check_thoughts_dir(report, config)
    _check_index_dir(report, config)

    storage = _check_sqlite_and_vec(report, config)
    if storage is None:
        return report

    try:
        _check_embedding_dimension_recorded(report, storage, config.embedding_model)
        if embedder_factory is not None:
            try:
                embedder: EmbeddingProvider | None = embedder_factory(config)
                report.add(
                    "embedding_model",
                    CheckStatus.OK,
                    f"model {config.embedding_model!r} (custom factory)",
                )
            except Exception as exc:
                report.add(
                    "embedding_model",
                    CheckStatus.FAIL,
                    "embedder factory raised",
                    detail=str(exc),
                )
                embedder = None
        else:
            embedder = _check_embedding_model(report, config, download=download_model)
        _check_index_disk_consistency(report, storage)
        _check_orphan_sqlite_rows(report, storage)
        _check_orphan_tempfiles(report, config)
        _check_pending_embeddings(report, storage)

        if repair:
            _maybe_repair(report, storage, embedder, remove_orphans=remove_orphans)
    finally:
        storage.close()

    return report


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "run_diagnostics",
]
