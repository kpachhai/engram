"""Consolidation doctor checks.

Two rows: an interrupted-apply journal (resumable; surfaced so the operator
knows a run stopped midway) and conflict markers inside ``<vault>/archive/``
(the thoughts-dir conflict scan deliberately does not cover the archive).
Vaults that never ran consolidate report SKIP rows rather than failing on
the missing state (doctor checks handle absent preconditions gracefully);
SKIP rather than OK because the check did not run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.consolidate.apply import load_journal_state
from engram.consolidate.models import JournalEntryState
from engram.consolidate.report import consolidate_state_dir
from engram.diagnostics.check_codes import (
    ARCHIVE_CONFLICT_MARKERS,
    CONSOLIDATE_JOURNAL_ORPHAN,
)
from engram.diagnostics.doctor import CheckStatus
from engram.sync.gitops import conflict_marker_scan

if TYPE_CHECKING:
    from engram.config.models import EffectiveConfig
    from engram.diagnostics.doctor import DoctorReport

_TERMINAL_STATES = (
    JournalEntryState.COMPLETED,
    JournalEntryState.SKIPPED,
    JournalEntryState.FAILED,
)


def check_consolidate_journal_orphan(report: DoctorReport, config: EffectiveConfig) -> None:
    """WARN when an apply journal shows clusters without a terminal state."""
    state_dir = consolidate_state_dir(config.index_dir)
    if not state_dir.exists():
        report.add(
            CONSOLIDATE_JOURNAL_ORPHAN,
            CheckStatus.SKIP,
            "skipped (no consolidation state)",
        )
        return
    state = load_journal_state(state_dir)
    interrupted = sorted(
        cluster_id for cluster_id, entry in state.items() if entry.state not in _TERMINAL_STATES
    )
    if interrupted:
        report.add(
            CONSOLIDATE_JOURNAL_ORPHAN,
            CheckStatus.WARN,
            f"{len(interrupted)} consolidation cluster(s) have no terminal journal "
            "state (interrupted apply); re-run `engram consolidate --apply` to resume",
            detail=", ".join(interrupted[:5]),
        )
        return
    report.add(
        CONSOLIDATE_JOURNAL_ORPHAN,
        CheckStatus.OK,
        "no interrupted consolidation applies",
    )


def check_archive_conflict_markers(report: DoctorReport, config: EffectiveConfig) -> None:
    """WARN when archived files carry git conflict markers (bad merge landed)."""
    archive_dir = config.vault_path / "archive"
    if not archive_dir.exists():
        report.add(
            ARCHIVE_CONFLICT_MARKERS,
            CheckStatus.SKIP,
            "skipped (no archive)",
        )
        return
    flagged = conflict_marker_scan(archive_dir)
    if flagged:
        report.add(
            ARCHIVE_CONFLICT_MARKERS,
            CheckStatus.WARN,
            f"{len(flagged)} archived file(s) contain git conflict markers; "
            "resolve the markers manually (archive bodies are otherwise immutable)",
            detail=", ".join(str(p) for p in flagged[:5]),
        )
        return
    report.add(
        ARCHIVE_CONFLICT_MARKERS,
        CheckStatus.OK,
        "archive is conflict-marker free",
    )


def run_consolidate_checks(report: DoctorReport, config: EffectiveConfig) -> None:
    """Fold both consolidation rows into the doctor report."""
    check_consolidate_journal_orphan(report, config)
    check_archive_conflict_markers(report, config)


__all__ = [
    "check_archive_conflict_markers",
    "check_consolidate_journal_orphan",
    "run_consolidate_checks",
]
