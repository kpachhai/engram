"""Report persistence under ``<vault>/.indexes/consolidate/``.

Reports are per-machine operational state: ``.indexes/`` is wholesale
gitignored, so reports and journals never sync (and never face the team
pre-receive hook). ``--apply`` defaults to the newest report on disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from engram.consolidate.models import ConsolidationReport
from engram.errors import ConsolidateReportStale
from engram.utils.atomic_write import atomic_write_text

if TYPE_CHECKING:
    from pathlib import Path

_REPORT_PREFIX = "report-"
_REPORT_SUFFIX = ".json"


def consolidate_state_dir(index_dir: Path) -> Path:
    """Per-machine consolidation state dir (reports + apply journals)."""
    return index_dir / "consolidate"


def write_report(report: ConsolidationReport, *, index_dir: Path) -> Path:
    """Write the report JSON; returns its path."""
    state_dir = consolidate_state_dir(index_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    path = state_dir / f"{_REPORT_PREFIX}{stamp}{_REPORT_SUFFIX}"
    atomic_write_text(path, report.model_dump_json(indent=2) + "\n")
    return path


def load_report(path: Path) -> ConsolidationReport:
    """Load + validate a report file.

    Raises:
        ConsolidateReportStale: missing, unreadable, or schema-invalid report.
    """
    if not path.exists():
        msg = f"report not found at {path}; run `engram consolidate` first"
        raise ConsolidateReportStale(msg)
    try:
        return ConsolidationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        msg = f"report at {path} is unreadable or invalid: {exc}"
        raise ConsolidateReportStale(msg) from exc


def latest_report_path(index_dir: Path) -> Path | None:
    """Newest report on disk, or None when none exist."""
    state_dir = consolidate_state_dir(index_dir)
    if not state_dir.exists():
        return None
    candidates = sorted(state_dir.glob(f"{_REPORT_PREFIX}*{_REPORT_SUFFIX}"))
    return candidates[-1] if candidates else None


__all__ = [
    "consolidate_state_dir",
    "latest_report_path",
    "load_report",
    "write_report",
]
