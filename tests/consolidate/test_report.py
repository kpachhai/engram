"""Tests for engram.consolidate.report - report persistence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engram.consolidate.models import (
    ConsolidationReport,
    ExclusionCounts,
    PassState,
    PassStatus,
)
from engram.consolidate.report import latest_report_path, load_report, write_report
from engram.errors import ConsolidateReportStale

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _report(generated_at: datetime = _NOW) -> ConsolidationReport:
    complete = PassStatus(state=PassState.COMPLETE)
    return ConsolidationReport(
        vault_name="personal",
        generated_at=generated_at,
        snapshot_at=generated_at,
        embedding_model="test-model",
        near_dup_threshold=0.9,
        contradiction_threshold=0.75,
        stale_days=180,
        max_cluster_size=12,
        pass_near_duplicate=complete,
        pass_stale=complete,
        pass_contradiction=complete,
        pass_merge=complete,
        exclusions=ExclusionCounts(),
        clusters=[],
        stale_candidates=[],
        contradiction_candidates=[],
    )


def test_write_then_load_roundtrip(tmp_path: Path):
    path = write_report(_report(), index_dir=tmp_path)
    assert path.parent == tmp_path / "consolidate"
    again = load_report(path)
    assert again == _report()


def test_latest_report_path_picks_newest(tmp_path: Path):
    write_report(_report(_NOW - timedelta(hours=2)), index_dir=tmp_path)
    newest = write_report(_report(_NOW), index_dir=tmp_path)
    assert latest_report_path(tmp_path) == newest


def test_latest_report_path_none_when_empty(tmp_path: Path):
    assert latest_report_path(tmp_path) is None


def test_load_missing_report_raises(tmp_path: Path):
    with pytest.raises(ConsolidateReportStale, match="not found"):
        load_report(tmp_path / "consolidate" / "report-nope.json")


def test_load_invalid_report_raises(tmp_path: Path):
    bad = tmp_path / "consolidate" / "report-bad.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"not": "a report"}')
    with pytest.raises(ConsolidateReportStale, match="invalid"):
        load_report(bad)
