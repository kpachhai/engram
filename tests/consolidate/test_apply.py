"""Tests for engram.consolidate.apply - the journaled apply engine."""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engram.consolidate.apply import apply_report, load_journal_state
from engram.consolidate.models import JournalEntry, JournalEntryState
from engram.consolidate.passes import ReportSettings, generate_report
from engram.consolidate.report import consolidate_state_dir
from engram.storage.facade import VaultStorage
from engram.storage.sqlite_queries import fetch_all_embeddings

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


def _vault_root(storage: VaultStorage) -> Path:
    return storage.thoughts_dir.parent


def _embed(_content: str) -> list[float]:
    return [0.5, 0.5, 0.0, 0.0]


def _make_report(storage: VaultStorage, *, distill: bool = True, **settings_overrides):
    def loader(thought_id: str) -> str:
        thought = storage.get_by_id(thought_id)
        assert thought is not None
        return thought.content

    def distiller(members: list[tuple[str, str]], prefix: str) -> str:
        joined = " + ".join(content for _, content in members)
        return f"[{prefix}] distilled: {joined}"

    return generate_report(
        conn=storage.conn,
        vault_name="test-vault",
        configured_model="test-model",
        now=_NOW,
        settings=ReportSettings(**settings_overrides),
        content_loader=loader,
        judge=None,
        distiller=distiller if distill else None,
    )


def _run_apply(storage: VaultStorage, report, *, embed_fn=_embed, commit: bool = False):
    state_dir = consolidate_state_dir(storage.index_db_path.parent)
    return apply_report(
        storage=storage,
        report=report,
        report_path=state_dir / "report-test.json",
        archive_dir=_vault_root(storage) / "archive",
        journal_dir=state_dir,
        embed_fn=embed_fn,
        now=_LATER,
        commit=commit,
    )


def _near_dup_pair(storage: VaultStorage):
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


class TestMergeApply:
    def test_end_to_end_merge(self, storage: VaultStorage):
        first, second = _near_dup_pair(storage)
        report = _make_report(storage)
        result = _run_apply(storage, report)

        assert (result.applied, result.skipped, result.failed) == (1, 0, 0)
        # Originals archived out of thoughts_dir, rows gone.
        assert storage.get_by_id(first.id) is None
        assert storage.get_by_id(second.id) is None
        assert not first.file_path.exists()
        archive_root = _vault_root(storage) / "archive"
        archived = list(archive_root.rglob("*.md"))
        assert len(archived) == 2
        # Archived files carry superseded_by pointing at the merged thought.
        merged_id = result.id_map[str(first.id)]
        for path in archived:
            text = path.read_text(encoding="utf-8")
            assert merged_id in text
            assert "archived_at" in text
        # The merged thought is indexed with an embedding + provenance.
        embeddings = fetch_all_embeddings(storage.conn)
        assert merged_id in embeddings
        merged = storage.get_by_id(merged_id)
        assert merged is not None
        assert merged.source == "engram-consolidate"
        merged_text = merged.file_path.read_text(encoding="utf-8")
        assert "consolidated_from" in merged_text
        assert "consolidated_range" in merged_text
        # Non-git vault: no commit.
        assert result.commit is None

    def test_merge_without_embed_fn_lands_pending(self, storage: VaultStorage):
        _near_dup_pair(storage)
        report = _make_report(storage)
        result = _run_apply(storage, report, embed_fn=None)
        assert result.applied == 1
        merged_id = next(iter(result.id_map.values()))
        assert merged_id not in fetch_all_embeddings(storage.conn)  # pending

    def test_index_insert_failure_fails_cluster_and_cleans_up(
        self, storage: VaultStorage, monkeypatch: pytest.MonkeyPatch
    ):
        first, second = _near_dup_pair(storage)
        report = _make_report(storage)

        import sqlite3 as sqlite3_module

        import engram.storage.facade as facade_module

        def boom(*args: object, **kwargs: object) -> None:
            raise sqlite3_module.OperationalError("disk I/O error")

        monkeypatch.setattr(facade_module, "_q_insert_thought", boom)
        result = _run_apply(storage, report)
        assert result.failed == 1
        assert result.applied == 0
        # Originals untouched; no stray merged markdown left behind.
        assert storage.get_by_id(first.id) is not None
        assert storage.get_by_id(second.id) is not None
        consolidate_files = [
            p
            for p in storage.thoughts_dir.rglob("*.md")
            if "engram-consolidate" in p.read_text(encoding="utf-8")
        ]
        assert consolidate_files == []


class TestKeepNewest:
    def test_exact_duplicates_archive_all_but_newest(self, storage: VaultStorage):
        old = storage.capture(
            content="[Lesson] identical content",
            embedding=[1.0, 0.0, 0.0, 0.0],
            created_at=_NOW - timedelta(days=10),
        )
        new = storage.capture(
            content="[Lesson] identical content",
            embedding=[1.0, 0.0, 0.0, 0.0],
            created_at=_NOW - timedelta(days=1),
        )
        report = _make_report(storage, distill=False)
        result = _run_apply(storage, report)
        assert result.applied == 1
        assert storage.get_by_id(new.id) is not None
        assert storage.get_by_id(old.id) is None
        assert result.id_map[str(old.id)] == str(new.id)
        archived_text = next((_vault_root(storage) / "archive").rglob("*.md")).read_text(
            encoding="utf-8"
        )
        assert str(new.id) in archived_text


class TestSafetyGuards:
    def test_changed_fingerprint_skips_proposal(self, storage: VaultStorage):
        first, second = _near_dup_pair(storage)
        report = _make_report(storage)
        storage.update_body(first.id, new_content="[Lesson] edited after report")
        result = _run_apply(storage, report)
        assert (result.applied, result.skipped) == (0, 1)
        assert storage.get_by_id(first.id) is not None
        assert storage.get_by_id(second.id) is not None

    def test_post_snapshot_modification_skips_proposal(self, storage: VaultStorage):
        first, _second = _near_dup_pair(storage)
        report = _make_report(storage)
        storage.update_metadata(first.id, tags=["touched"], updated_at=_LATER)
        result = _run_apply(storage, report)
        assert (result.applied, result.skipped) == (0, 1)

    def test_portability_retag_skips_proposal(self, storage: VaultStorage):
        """A ``portable -> block`` re-tag is invisible to the other two gates.

        The fingerprint covers the body only, and a metadata-only re-tag need
        not move ``updated_at`` - so without a portability pin the merged
        thought would be written at the report-time tier, against pinned
        invariant 2.
        """
        first, second = _near_dup_pair(storage)
        report = _make_report(storage)
        assert storage.update_metadata(first.id, portability="block")
        before = storage.get_by_id(first.id)
        assert before is not None
        assert before.updated_at < report.snapshot_at, "re-tag moved updated_at"

        result = _run_apply(storage, report)

        assert (result.applied, result.skipped) == (0, 1)
        assert storage.get_by_id(first.id) is not None
        assert storage.get_by_id(second.id) is not None

    def test_manual_review_proposals_never_touched(self, storage: VaultStorage):
        first, second = _near_dup_pair(storage)
        report = _make_report(storage, distill=False)  # degrades to manual-review
        result = _run_apply(storage, report)
        assert (result.applied, result.skipped, result.failed) == (0, 0, 0)
        assert storage.get_by_id(first.id) is not None
        assert storage.get_by_id(second.id) is not None


class TestResume:
    def test_completed_cluster_skipped_on_rerun(self, storage: VaultStorage):
        _near_dup_pair(storage)
        report = _make_report(storage)
        first_run = _run_apply(storage, report)
        assert first_run.applied == 1
        second_run = _run_apply(storage, report)
        assert second_run.applied == 0
        assert second_run.skipped == 1
        # Exactly one merged thought exists (no duplicate from the re-run).
        merged = [
            p
            for p in storage.thoughts_dir.rglob("*.md")
            if "engram-consolidate" in p.read_text(encoding="utf-8")
        ]
        assert len(merged) == 1

    def test_journal_state_reads_latest_entry(self, tmp_path: Path):
        journal_dir = tmp_path / "consolidate"
        journal_dir.mkdir()
        lines = [
            JournalEntry(
                cluster_id="c-abc", state=JournalEntryState.INTENT, at=_NOW
            ).model_dump_json(),
            JournalEntry(
                cluster_id="c-abc", state=JournalEntryState.COMPLETED, at=_NOW
            ).model_dump_json(),
            "{not valid json",
        ]
        (journal_dir / "journal-1.jsonl").write_text("\n".join(lines) + "\n")
        state = load_journal_state(journal_dir)
        assert state["c-abc"].state is JournalEntryState.COMPLETED


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 - test-only helper; args are static literals
        ["git", *args],  # noqa: S607 - test fixtures rely on $PATH
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


class TestGitCommit:
    def test_apply_commits_in_git_vault(self, storage: VaultStorage):
        root = _vault_root(storage)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        _git(root, "config", "commit.gpgsign", "false")
        _near_dup_pair(storage)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "seed")

        report = _make_report(storage)
        result = _run_apply(storage, report, commit=True)
        assert result.commit is not None
        status = _git(root, "status", "--porcelain", "thoughts", "archive").strip()
        assert status == ""
