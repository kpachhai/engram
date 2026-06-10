"""Integration tests: consolidation end-to-end against engram's own machinery.

The load-bearing invariant lives here: after apply, ``engram reindex``
(incremental AND --full) must NOT resurrect archived thoughts, and the
doctor must come back clean. Crash-injection proves a failed cluster
converges on re-run without duplicating the merged thought.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import engram.consolidate.apply as apply_module
from engram.consolidate.apply import apply_report
from engram.consolidate.passes import ReportSettings, generate_report
from engram.consolidate.report import consolidate_state_dir
from engram.storage.facade import VaultStorage
from engram.storage.reindex import ReindexMode, reindex_vault
from engram.storage.sqlite_queries import list_all_thought_rows

_DIM = 4
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(minutes=5)


@pytest.fixture
def storage(tmp_path: Path) -> Generator[VaultStorage, None, None]:
    vault = tmp_path / "vault"
    store = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        embedding_dim=_DIM,
        embedding_model_name="test-model",
        vault_name="test-vault",
    )
    yield store
    store.close()


def _embed(_content: str) -> list[float]:
    return [0.5, 0.5, 0.0, 0.0]


def _make_report(storage: VaultStorage):
    def loader(thought_id: str) -> str:
        thought = storage.get_by_id(thought_id)
        assert thought is not None
        return thought.content

    def distiller(members: list[tuple[str, str]], prefix: str) -> str:
        return f"[{prefix}] distilled from {len(members)} notes"

    return generate_report(
        conn=storage.conn,
        vault_name="test-vault",
        configured_model="test-model",
        now=_NOW,
        settings=ReportSettings(),
        content_loader=loader,
        judge=None,
        distiller=distiller,
    )


def _run_apply(storage: VaultStorage, report):
    state_dir = consolidate_state_dir(storage.index_db_path.parent)
    return apply_report(
        storage=storage,
        report=report,
        report_path=state_dir / "report-test.json",
        archive_dir=storage.thoughts_dir.parent / "archive",
        journal_dir=state_dir,
        embed_fn=_embed,
        now=_LATER,
        commit=False,
    )


def _seed_near_dups(storage: VaultStorage):
    first = storage.capture(
        content="[Lesson] near duplicate alpha",
        embedding=[1.0, 0.0, 0.0, 0.0],
        created_at=_NOW - timedelta(days=5),
    )
    second = storage.capture(
        content="[Lesson] near duplicate beta",
        embedding=[1.0, 0.05, 0.0, 0.0],
        created_at=_NOW - timedelta(days=4),
    )
    return first, second


class TestReindexDoesNotResurrect:
    @pytest.mark.parametrize("mode", [ReindexMode.INCREMENTAL, ReindexMode.FULL])
    def test_archived_thoughts_stay_archived_after_reindex(
        self, storage: VaultStorage, mode: ReindexMode
    ):
        first, second = _seed_near_dups(storage)
        survivor = storage.capture(content="[Decision] unrelated", embedding=[0.0, 1.0, 0.0, 0.0])
        report = _make_report(storage)
        result = _run_apply(storage, report)
        assert result.applied == 1
        merged_id = result.id_map[str(first.id)]

        reindex_vault(storage, mode=mode, embed_fn=_embed)

        rows = list_all_thought_rows(storage.conn)
        ids = {str(row["id"]) for row in rows}
        assert str(first.id) not in ids  # archived: NOT resurrected
        assert str(second.id) not in ids
        assert merged_id in ids
        assert str(survivor.id) in ids
        assert len(ids) == 2


class TestCrashResume:
    def test_failure_after_capture_resumes_without_duplicate_merge(
        self, storage: VaultStorage, monkeypatch: pytest.MonkeyPatch
    ):
        """Index-row deletion fails after the merged thought was captured and
        originals archived; the re-run reuses the captured merged thought."""
        _seed_near_dups(storage)
        report = _make_report(storage)

        from engram.storage.sqlite_queries import delete_thought_rows as real_delete

        calls: list[int] = []

        def flaky_delete(conn, ids):
            calls.append(1)
            if len(calls) == 1:
                msg = "simulated crash"
                raise OSError(msg)
            return real_delete(conn, ids)

        monkeypatch.setattr(apply_module, "delete_thought_rows", flaky_delete)
        first_run = _run_apply(storage, report)
        assert first_run.failed == 1

        second_run = _run_apply(storage, report)
        assert second_run.applied == 1
        merged = [
            p
            for p in storage.thoughts_dir.rglob("*.md")
            if "engram-consolidate" in p.read_text(encoding="utf-8")
        ]
        assert len(merged) == 1  # no duplicate from the resume
        rows = list_all_thought_rows(storage.conn)
        assert len(rows) == 1  # both originals gone from the index

    def test_failure_mid_archive_converges_on_rerun(
        self, storage: VaultStorage, monkeypatch: pytest.MonkeyPatch
    ):
        _seed_near_dups(storage)
        report = _make_report(storage)

        from engram.storage.archive import archive_thought_file as real_archive

        calls: list[int] = []

        def flaky_archive(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                msg = "simulated crash mid-archive"
                raise OSError(msg)
            return real_archive(**kwargs)

        monkeypatch.setattr(apply_module, "archive_thought_file", flaky_archive)
        first_run = _run_apply(storage, report)
        assert first_run.failed == 1

        monkeypatch.setattr(apply_module, "archive_thought_file", real_archive)
        second_run = _run_apply(storage, report)
        assert second_run.applied == 1
        archive_root = storage.thoughts_dir.parent / "archive"
        assert len(list(archive_root.rglob("*.md"))) == 2
        assert len(list_all_thought_rows(storage.conn)) == 1


class TestDoctorCleanAfterApply:
    def test_consolidated_vault_has_no_drift_or_orphans(self, storage: VaultStorage):
        from engram.diagnostics.doctor import (
            CheckStatus,
            DoctorReport,
            _check_orphan_markdown_files,
            _check_orphan_sqlite_rows,
        )
        from engram.storage.markdown import DriftReason, read_thought

        _seed_near_dups(storage)
        report = _make_report(storage)
        result = _run_apply(storage, report)
        assert result.applied == 1

        doctor_report = DoctorReport()
        _check_orphan_sqlite_rows(doctor_report, storage)
        _check_orphan_markdown_files(doctor_report, storage)
        assert all(c.status is CheckStatus.OK for c in doctor_report.checks), doctor_report.checks

        # Every surviving + archived file parses drift-free.
        vault_root = storage.thoughts_dir.parent
        for path in [*storage.thoughts_dir.rglob("*.md"), *(vault_root / "archive").rglob("*.md")]:
            parsed = read_thought(path)
            assert parsed is not None
            thought, drifts = parsed
            assert thought is not None, path
            assert not any(d.reason == DriftReason.UNKNOWN_EXTRA_FIELD for d in drifts), path
