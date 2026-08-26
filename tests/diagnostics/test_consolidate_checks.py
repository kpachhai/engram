"""Tests for engram.diagnostics.consolidate_checks + machine-B convergence reuse."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from engram.config.models import DEFAULT_EMBEDDING_MODEL, EffectiveConfig, LLMConfig, SyncConfig
from engram.consolidate.models import JournalEntry, JournalEntryState
from engram.diagnostics.check_codes import (
    ARCHIVE_CONFLICT_MARKERS,
    CONSOLIDATE_JOURNAL_ORPHAN,
)
from engram.diagnostics.consolidate_checks import run_consolidate_checks
from engram.diagnostics.doctor import CheckStatus, DoctorReport, _check_orphan_sqlite_rows
from engram.storage.facade import VaultStorage

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _make_config(tmp_path: Path) -> EffectiveConfig:
    thoughts = tmp_path / "thoughts"
    indexes = tmp_path / ".indexes"
    thoughts.mkdir(exist_ok=True)
    indexes.mkdir(exist_ok=True)
    return EffectiveConfig(
        default_user="test-user",
        vault_path=tmp_path,
        thoughts_dir=thoughts,
        index_dir=indexes,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        vault_name="default",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )


def _row(report: DoctorReport, name: str):
    matches = [c for c in report.checks if c.name == name]
    assert len(matches) == 1
    return matches[0]


def _journal_line(state: JournalEntryState) -> str:
    return JournalEntry(cluster_id="c-abc123", state=state, at=_NOW).model_dump_json()


class TestJournalOrphanCheck:
    def test_no_state_dir_is_skipped_not_passed(self, tmp_path: Path):
        """A vault that never consolidated did not pass this check; it skipped it."""
        report = DoctorReport()
        run_consolidate_checks(report, _make_config(tmp_path))
        row = _row(report, CONSOLIDATE_JOURNAL_ORPHAN)
        assert row.status is CheckStatus.SKIP
        assert "skipped" in row.message

    def test_interrupted_journal_warns_with_resume_guidance(self, tmp_path: Path):
        config = _make_config(tmp_path)
        state_dir = config.index_dir / "consolidate"
        state_dir.mkdir(parents=True)
        (state_dir / "journal-1.jsonl").write_text(
            _journal_line(JournalEntryState.MERGED_CAPTURED) + "\n"
        )
        report = DoctorReport()
        run_consolidate_checks(report, config)
        row = _row(report, CONSOLIDATE_JOURNAL_ORPHAN)
        assert row.status is CheckStatus.WARN
        assert "--apply" in row.message
        assert "c-abc123" in (row.detail or "")

    def test_completed_journal_is_ok(self, tmp_path: Path):
        config = _make_config(tmp_path)
        state_dir = config.index_dir / "consolidate"
        state_dir.mkdir(parents=True)
        (state_dir / "journal-1.jsonl").write_text(
            _journal_line(JournalEntryState.INTENT)
            + "\n"
            + _journal_line(JournalEntryState.COMPLETED)
            + "\n"
        )
        report = DoctorReport()
        run_consolidate_checks(report, config)
        assert _row(report, CONSOLIDATE_JOURNAL_ORPHAN).status is CheckStatus.OK


class TestArchiveConflictCheck:
    def test_no_archive_is_skipped_not_passed(self, tmp_path: Path):
        """No archive means the marker scan never ran; that is not a pass."""
        report = DoctorReport()
        run_consolidate_checks(report, _make_config(tmp_path))
        row = _row(report, ARCHIVE_CONFLICT_MARKERS)
        assert row.status is CheckStatus.SKIP
        assert "skipped" in row.message

    def test_conflict_markers_in_archive_warn(self, tmp_path: Path):
        config = _make_config(tmp_path)
        archive = tmp_path / "archive" / "Lesson"
        archive.mkdir(parents=True)
        (archive / "bad.md").write_text(
            "---\nfoo: bar\n---\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        )
        report = DoctorReport()
        run_consolidate_checks(report, config)
        row = _row(report, ARCHIVE_CONFLICT_MARKERS)
        assert row.status is CheckStatus.WARN
        assert "bad.md" in (row.detail or "")

    def test_clean_archive_is_ok(self, tmp_path: Path):
        config = _make_config(tmp_path)
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "clean.md").write_text("---\nfoo: bar\n---\nbody\n")
        report = DoctorReport()
        run_consolidate_checks(report, config)
        assert _row(report, ARCHIVE_CONFLICT_MARKERS).status is CheckStatus.OK


class TestMachineBConvergence:
    def test_existing_orphan_rows_check_fires_after_synced_consolidation(self, tmp_path: Path):
        """Machine B's state after a consolidation commit syncs: markdown moved
        out of thoughts_dir by git, SQLite rows still present. The EXISTING
        orphan-rows doctor check covers this - no new check needed."""
        vault = tmp_path / "vault"
        storage = VaultStorage(
            thoughts_dir=vault / "thoughts",
            index_db_path=vault / ".indexes" / "engram.db",
            embedding_dim=4,
            vault_name="machine-b",
        )
        try:
            thought = storage.capture(
                content="[Lesson] consolidated elsewhere", embedding=[1.0, 0.0, 0.0, 0.0]
            )
            # Simulate the synced consolidation commit: the file moves to archive/.
            archive_path = vault / "archive" / "Lesson" / thought.file_path.name
            archive_path.parent.mkdir(parents=True)
            thought.file_path.rename(archive_path)

            report = DoctorReport()
            _check_orphan_sqlite_rows(report, storage)
            rows = [c for c in report.checks if c.name == "orphan_rows"]
            assert len(rows) == 1
            assert rows[0].status is not CheckStatus.OK
            assert "remove-orphans" in rows[0].message
        finally:
            storage.close()
