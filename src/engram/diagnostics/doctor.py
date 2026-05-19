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

import asyncio
import enum
import logging
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from engram.config.models import EffectiveConfig
from engram.diagnostics import check_codes
from engram.embedding.fastembed import FastEmbedProvider
from engram.embedding.protocol import EmbeddingProvider
from engram.errors import EmbeddingError
from engram.errors import IndexError as EngramIndexError
from engram.storage.facade import VaultStorage
from engram.storage.markdown import read_thought
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
from engram.sync import gitops, startup_probes
from engram.sync.gitops import conflict_marker_scan

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


def _check_embedding_cache_integrity(
    report: DoctorReport,
    config: EffectiveConfig,
) -> None:
    """Surface FastEmbed cache snapshots that are present but incomplete.

    Counters a silent half-failure mode: when a previous download was
    interrupted (sleep, network blip, process kill), the snapshot dir
    can end up with the symlinks but not the blobs they point at. The
    next ``engram serve`` brings up an embedding provider that loads
    fine until the first search call - then ONNX runtime fails with a
    cryptic ``NO_SUCHFILE``. This check makes that state visible during
    ``engram doctor`` without triggering a re-download.
    """
    provider = FastEmbedProvider(model_name=config.embedding_model)
    integrity = provider.check_cache_integrity()

    if integrity.cache_dir is None:
        report.add(
            "embedding_cache_integrity",
            CheckStatus.OK,
            "no FastEmbed cache yet (model will lazy-download on first use)",
        )
        return
    if not integrity.has_snapshot:
        report.add(
            "embedding_cache_integrity",
            CheckStatus.OK,
            f"no cached snapshot for {config.embedding_model!r} yet (will lazy-download)",
        )
        return
    if not integrity.manifest_populated:
        report.add(
            "embedding_cache_integrity",
            CheckStatus.OK,
            (
                f"snapshot present at {integrity.snapshot_dir} but model has no pinned "
                f"manifest; skipping presence check (trust-on-first-use)"
            ),
        )
        return
    if integrity.is_intact:
        report.add(
            "embedding_cache_integrity",
            CheckStatus.OK,
            (
                f"FastEmbed snapshot intact "
                f"({len(integrity.expected_files)} files present at {integrity.snapshot_dir})"
            ),
        )
        return

    missing_count = len(integrity.missing_files)
    expected_count = len(integrity.expected_files)
    report.add(
        "embedding_cache_integrity",
        CheckStatus.WARN,
        (
            f"FastEmbed snapshot incomplete ({missing_count} of {expected_count} files "
            f"missing or broken-symlink); embedding load will fail at first use. "
            f"Remediation: delete {integrity.snapshot_dir} and rerun "
            f"`engram doctor --download-model` to re-fetch."
        ),
        detail=", ".join(integrity.missing_files),
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


#: macOS-specific tail appended to the disk_usage message to explain why
#: the doctor's "free" number can be much smaller than Finder's "Available."
#: shutil.disk_usage reports `f_bavail` (real bytes APFS can allocate right
#: now without purging anything); Finder reports `volumeAvailableCapacity
#: ForImportantUsage` which adds purgeable space (Time Machine local
#: snapshots, app caches APFS can free if asked). SQLite EIO predictions
#: track the former, not the latter: APFS may not be able to reclaim
#: purgeable fast enough under write pressure to keep up. The canonical
#: APFS-side view is `diskutil apfs list`.
_MACOS_DISK_USAGE_NOTE = (
    " Note: on macOS, Finder may show more 'available' space than this - "
    "Finder includes purgeable (Time Machine local snapshots + caches APFS "
    "can free on demand); doctor reports the real allocatable space. "
    "`diskutil apfs list` is the canonical view; `tmutil thinlocalsnapshots "
    "/ 1000000000 4` can reclaim local-snapshot purgeable space."
)


def _check_disk_usage(
    report: DoctorReport,
    config: EffectiveConfig,
) -> None:
    """Surface disk pressure on the vault's volume.

    APFS and most journaling filesystems get progressively unhappy as
    free space drops below 10%. SQLite under disk pressure can return
    sporadic ``SQLITE_IOERR`` on writes - the 2026-05-13 -> 2026-05-16
    silent-swallow incident class involved a vault on a disk at 94%
    capacity. The `busy_timeout` PRAGMA + insert retry path masks
    transient bursts; this check surfaces the underlying condition so
    the operator can free space before the retries also exhaust.

    The "free" number is what the filesystem can actually allocate
    right now (``f_bavail`` via ``shutil.disk_usage``), NOT what macOS
    Finder shows as "Available" (which includes purgeable space APFS
    may or may not be able to reclaim in time). When firing on macOS,
    the message points at ``diskutil apfs list`` for the canonical
    APFS-side view and ``tmutil thinlocalsnapshots`` as a remediation
    handle.

    Thresholds: WARN at >=90% used, FAIL at >=95%. Stat failures
    surface as WARN with the OSError reason rather than crashing the
    doctor run.
    """
    try:
        usage = shutil.disk_usage(config.vault_path)
    except OSError as exc:
        report.add(
            "disk_usage",
            CheckStatus.WARN,
            f"could not stat vault disk: {exc}",
        )
        return
    used_pct = (usage.total - usage.free) / usage.total * 100.0
    free_gb = usage.free / 1e9
    total_gb = usage.total / 1e9
    macos_note = _MACOS_DISK_USAGE_NOTE if sys.platform == "darwin" else ""
    msg = (
        f"vault disk {used_pct:.1f}% real-free ({free_gb:.1f} GB allocatable of "
        f"{total_gb:.1f} GB total)"
    )
    if used_pct >= 95.0:
        report.add(
            "disk_usage",
            CheckStatus.FAIL,
            f"{msg} - SQLite EIO likely at this pressure; free space immediately." + macos_note,
        )
        return
    if used_pct >= 90.0:
        report.add(
            "disk_usage",
            CheckStatus.WARN,
            f"{msg} - approaching SQLite EIO territory; free space soon." + macos_note,
        )
        return
    report.add("disk_usage", CheckStatus.OK, msg)


def _check_orphan_markdown_files(
    report: DoctorReport,
    storage: VaultStorage,
) -> None:
    """Inverse of `_check_orphan_sqlite_rows`: markdown files with no SQLite row.

    Markdown SoT can drift "above" SQLite when the SQLite insert raises but
    the markdown write succeeds - the capture path is intentionally
    log-and-continue (Flow A step 3 commentary), so the operator gets no
    signal at write time. Over a multi-day window of disk pressure /
    transient I/O errors, these silent failures accumulate (38 orphans
    observed in the 2026-05-13 -> 2026-05-16 incident class before
    ``engram reindex`` recovered them).

    This check surfaces the count so the operator can run reindex
    proactively. The MCP ``CaptureOutput.index_state`` field carries the
    same signal at write time for clients that read it; this is the
    backstop for accumulated drift that escaped the in-the-moment signal
    (vaults imported from another tool, vaults that ran on an older
    engram before the field existed, etc.).
    """
    orphans: list[str] = []
    for md_path in sorted(storage.thoughts_dir.rglob("*.md")):
        if not md_path.is_file():
            continue
        result = read_thought(md_path)
        if result is None:
            continue
        thought, _drifts = result
        if thought is None:
            continue
        if storage.get_by_id(thought.id) is None:
            rel = md_path.relative_to(storage.thoughts_dir)
            orphans.append(f"{thought.id} -> {rel}")
    if not orphans:
        report.add("orphan_markdown", CheckStatus.OK, "no orphan markdown files")
        return
    report.add(
        "orphan_markdown",
        CheckStatus.WARN,
        (
            f"{len(orphans)} markdown file(s) have no SQLite row; "
            "run `engram reindex` to recover (or `engram reindex --full` "
            "to fully rebuild the index)"
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


# === Sync checks ===


_PROBE_CODE_TO_STATUS_DEFAULT: dict[str, CheckStatus] = {
    # Codes whose probe surfaces them as WARN map to WARN in the doctor;
    # codes whose probe surfaces them as FAIL map to FAIL.
    check_codes.GIT_VERSION_FLOOR: CheckStatus.FAIL,
    check_codes.BRANCH_ALIGNMENT: CheckStatus.WARN,
    check_codes.CONFLICT_MARKERS_PRESENT: CheckStatus.FAIL,
    check_codes.CLOUD_SYNC_UNDER_DOTGIT: CheckStatus.FAIL,
    check_codes.GITIGNORE_INDEXES: CheckStatus.FAIL,
    check_codes.SIGNED_COMMITS_REQUIRED: CheckStatus.WARN,
    check_codes.LFS_DRIFT: CheckStatus.WARN,
    check_codes.AUTOCRLF_DRIFT: CheckStatus.FAIL,
    check_codes.SUBMODULE_UNDER_VAULT: CheckStatus.FAIL,
    check_codes.GPG_AGENT_REACHABLE: CheckStatus.WARN,
    check_codes.VAULT_IDENTITY_REMOTE_MATCH: CheckStatus.WARN,
    check_codes.SYNC_USER_IDENTITY_SET: CheckStatus.WARN,
    check_codes.WORKING_TREE_DIRTY_AT_STARTUP: CheckStatus.WARN,  # WARN at runtime
    check_codes.READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH: CheckStatus.FAIL,
}


def _check_conflict_markers_present(
    report: DoctorReport,
    config: EffectiveConfig,
) -> None:
    """Refuse vaults with merge-conflict markers in any markdown file."""
    found = conflict_marker_scan(config.thoughts_dir)
    if not found:
        report.add(
            check_codes.CONFLICT_MARKERS_PRESENT,
            CheckStatus.OK,
            "no conflict markers in any thought file",
        )
        return
    report.add(
        check_codes.CONFLICT_MARKERS_PRESENT,
        CheckStatus.FAIL,
        f"{len(found)} thought file(s) contain conflict markers; resolve before serving",
        detail=", ".join(str(p) for p in found[:5]) + ("..." if len(found) > 5 else ""),
    )


def _is_git_vault(vault_path: Path) -> bool:
    """Cheap check: does the vault have a ``.git`` entry?"""
    git_marker = vault_path / ".git"
    return git_marker.exists()


def run_sync_diagnostics(
    report: DoctorReport,
    config: EffectiveConfig,
) -> None:
    """Append the 14 sync checks to ``report``.

    Most checks reuse the probe logic from
    :mod:`engram.sync.startup_probes`. The ``conflict_markers_present``
    check is doctor-specific (the startup probe runs identical logic via
    its own pathway during ``engram serve`` startup).

    When the vault is NOT a git working tree, the entire sync-check
    sweep is skipped (returning OK for every code) - non-git vaults
    are a valid local-only configuration.
    """
    # Conflict-marker scan first - it is the highest-priority FAIL.
    _check_conflict_markers_present(report, config)

    if not _is_git_vault(config.vault_path):
        for code in check_codes.ALL_PHASE_2_CHECK_CODES:
            if code == check_codes.CONFLICT_MARKERS_PRESENT:
                continue
            report.add(
                code,
                CheckStatus.OK,
                f"{code}: skipped (vault is not a git working tree)",
            )
        return

    # Run the probes synchronously so doctor stays a sync API.
    probe_report = asyncio.run(
        startup_probes.run_startup_probes(
            config.sync,
            config.vault_path,
            thoughts_dir=config.thoughts_dir,
        )
    )

    seen_codes: set[str] = set()

    for failure in probe_report.failures:
        seen_codes.add(failure.code)
        report.add(
            failure.code,
            CheckStatus.FAIL,
            failure.message,
            detail=failure.detail,
        )

    for warning in probe_report.warnings:
        if warning.code in seen_codes:
            continue
        seen_codes.add(warning.code)
        report.add(
            warning.code,
            CheckStatus.WARN,
            warning.message,
            detail=warning.detail,
        )

    # Codes that did NOT surface from probes are silently OK; advertise
    # them as OK rows so doctor output is comprehensive.
    for code in check_codes.ALL_PHASE_2_CHECK_CODES:
        if code in seen_codes:
            continue
        if code == check_codes.CONFLICT_MARKERS_PRESENT:
            continue  # already added above
        report.add(
            code,
            CheckStatus.OK,
            f"{code}: ok",
        )


def run_diagnostics(
    config: EffectiveConfig,
    *,
    download_model: bool = False,
    repair: bool = False,
    remove_orphans: bool = False,
    embedder_factory: Callable[[EffectiveConfig], EmbeddingProvider] | None = None,
    skip_sync_checks: bool = False,
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
        skip_sync_checks: If True, skip the sync diagnostics. Useful for
            unit tests that target single-vault behavior on non-git vaults.

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
        _check_embedding_cache_integrity(report, config)
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
        _check_disk_usage(report, config)
        _check_orphan_sqlite_rows(report, storage)
        _check_orphan_markdown_files(report, storage)
        _check_orphan_tempfiles(report, config)
        _check_pending_embeddings(report, storage)

        if repair:
            _maybe_repair(report, storage, embedder, remove_orphans=remove_orphans)
    finally:
        storage.close()

    if not skip_sync_checks:
        try:
            run_sync_diagnostics(report, config)
        except Exception as exc:
            _log.exception("sync diagnostics raised: %s", exc)
            report.add(
                "sync_checks_internal",
                CheckStatus.FAIL,
                "sync diagnostics raised an unexpected error",
                detail=str(exc),
            )

    return report


# Suppress unused-import lint - gitops is referenced through public
# helpers run inside run_sync_diagnostics.
_ = gitops


__all__ = [
    "CheckResult",
    "CheckStatus",
    "DoctorReport",
    "run_diagnostics",
    "run_sync_diagnostics",
]
