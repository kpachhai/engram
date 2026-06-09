"""Tests for the consolidation storage primitives.

Covers the bulk-embedding reader, batched row deletion, the read-only
connection opener, the archive-move helper, and provenance frontmatter
flowing through capture AND surviving rewrite paths (update_metadata /
reindex re-capture both re-serialize markdown).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from engram.errors import IndexError as EngramIndexError
from engram.errors import ThoughtNotFoundError, VaultError
from engram.storage.archive import archive_thought_file
from engram.storage.facade import VaultStorage
from engram.storage.markdown import DriftReason, read_thought, split_frontmatter
from engram.storage.sqlite import open_connection_readonly
from engram.storage.sqlite_queries import delete_thought_rows, fetch_all_embeddings

_DIM = 4
_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path: Path) -> Generator[VaultStorage, None, None]:
    vault = tmp_path / "vault"
    store = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        embedding_dim=_DIM,
        vault_name="test-vault",
    )
    yield store
    store.close()


def _vec(seed: float) -> list[float]:
    return [seed, 0.0, 0.0, 1.0]


class TestFetchAllEmbeddings:
    def test_returns_only_ok_embeddings(self, storage: VaultStorage):
        with_vec = storage.capture(content="[Lesson] embedded", embedding=_vec(0.1))
        storage.capture(content="[Lesson] pending one")  # no embedding -> pending
        embeddings = fetch_all_embeddings(storage.conn)
        assert set(embeddings) == {str(with_vec.id)}

    def test_vectors_roundtrip(self, storage: VaultStorage):
        thought = storage.capture(content="[Lesson] precise", embedding=[0.25, 0.5, -0.5, 1.0])
        embeddings = fetch_all_embeddings(storage.conn)
        assert embeddings[str(thought.id)] == pytest.approx([0.25, 0.5, -0.5, 1.0])

    def test_empty_vault_returns_empty(self, storage: VaultStorage):
        assert fetch_all_embeddings(storage.conn) == {}


class TestDeleteThoughtRows:
    def test_deletes_rows_and_embeddings_in_one_call(self, storage: VaultStorage):
        first = storage.capture(content="[Lesson] one", embedding=_vec(0.1))
        second = storage.capture(content="[Lesson] two", embedding=_vec(0.2))
        survivor = storage.capture(content="[Lesson] three", embedding=_vec(0.3))
        deleted = delete_thought_rows(storage.conn, [first.id, second.id])
        assert deleted == 2
        assert storage.get_by_id(first.id) is None
        assert storage.get_by_id(second.id) is None
        assert storage.get_by_id(survivor.id) is not None
        assert set(fetch_all_embeddings(storage.conn)) == {str(survivor.id)}

    def test_missing_ids_are_counted_as_zero(self, storage: VaultStorage):
        assert delete_thought_rows(storage.conn, [uuid4()]) == 0


class TestOpenConnectionReadonly:
    def test_reads_work_writes_refused(self, storage: VaultStorage, tmp_path: Path):
        storage.capture(content="[Lesson] readable", embedding=_vec(0.1))
        db_path = storage.index_db_path
        storage.close()
        conn = open_connection_readonly(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]
            assert count == 1
            with pytest.raises(Exception, match=r"readonly|read-only"):
                conn.execute("INSERT INTO engram_settings(key, value) VALUES ('x', 'y')")
        finally:
            conn.close()

    def test_missing_database_raises_with_remediation(self, tmp_path: Path):
        with pytest.raises(EngramIndexError, match="does not exist"):
            open_connection_readonly(tmp_path / "absent" / "engram.db")

    def test_opens_beside_live_writer(self, storage: VaultStorage):
        """Report mode's core safety property: a read-only open succeeds while
        a writer connection holds the vault's WAL, without becoming a second
        writer."""
        storage.capture(content="[Lesson] live", embedding=_vec(0.1))
        conn = open_connection_readonly(storage.index_db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_unreadable_index_raises_remediation(self, tmp_path: Path):
        """Corruption / unrecoverable-WAL shapes surface as a clear engram
        error with remediation, not a raw sqlite3 exception."""
        bad_db = tmp_path / "engram.db"
        bad_db.write_bytes(b"this is not a sqlite database, not even close.\x00" * 4)
        with pytest.raises(EngramIndexError, match=r"doctor|reindex"):
            open_connection_readonly(bad_db)


class TestArchiveThoughtFile:
    def _capture_one(self, storage: VaultStorage, content: str = "[Lesson] body text\nline2"):
        thought = storage.capture(content=content, embedding=_vec(0.1))
        rel_path = str(thought.file_path.relative_to(storage.thoughts_dir))
        return thought, rel_path

    def test_moves_file_and_annotates_frontmatter(self, storage: VaultStorage, tmp_path: Path):
        _, rel_path = self._capture_one(storage)
        archive_dir = tmp_path / "vault" / "archive"
        merged_id = uuid4()
        original, archived = archive_thought_file(
            thoughts_dir=storage.thoughts_dir,
            archive_dir=archive_dir,
            rel_path=rel_path,
            superseded_by=merged_id,
            archived_at=_NOW,
        )
        assert not original.exists()
        assert archived == archive_dir / rel_path
        result = read_thought(archived)
        assert result is not None
        archived_thought, drifts = result
        assert archived_thought is not None
        assert not any(d.reason == DriftReason.UNKNOWN_EXTRA_FIELD for d in drifts)
        split = split_frontmatter(archived.read_text(encoding="utf-8"))
        assert split is not None
        fm_yaml = split[0]
        assert str(merged_id) in fm_yaml
        assert "archived_at" in fm_yaml

    def test_body_bytes_identical(self, storage: VaultStorage, tmp_path: Path):
        content = "[Lesson] tricky body\n\n---\n\nwith a fence line and trailing spaces  \n"
        thought, rel_path = self._capture_one(storage, content=content)
        original_text = thought.file_path.read_text(encoding="utf-8")
        original_split = split_frontmatter(original_text)
        assert original_split is not None
        original_body = original_split[1]
        _, archived = archive_thought_file(
            thoughts_dir=storage.thoughts_dir,
            archive_dir=tmp_path / "vault" / "archive",
            rel_path=rel_path,
            superseded_by=uuid4(),
            archived_at=_NOW,
        )
        archived_split = split_frontmatter(archived.read_text(encoding="utf-8"))
        assert archived_split is not None
        archived_body = archived_split[1]
        assert archived_body == original_body

    def test_resume_after_move_is_idempotent(self, storage: VaultStorage, tmp_path: Path):
        """Original gone + archive present (crash after move) returns the
        archive path instead of failing."""
        _, rel_path = self._capture_one(storage)
        archive_dir = tmp_path / "vault" / "archive"
        merged_id = uuid4()
        kwargs = {
            "thoughts_dir": storage.thoughts_dir,
            "archive_dir": archive_dir,
            "rel_path": rel_path,
            "superseded_by": merged_id,
            "archived_at": _NOW,
        }
        _, first = archive_thought_file(**kwargs)
        _, second = archive_thought_file(**kwargs)
        assert first == second

    def test_both_missing_raises(self, storage: VaultStorage, tmp_path: Path):
        with pytest.raises(ThoughtNotFoundError):
            archive_thought_file(
                thoughts_dir=storage.thoughts_dir,
                archive_dir=tmp_path / "vault" / "archive",
                rel_path="Lesson/nope.md",
                superseded_by=uuid4(),
                archived_at=_NOW,
            )

    def test_both_present_raises(self, storage: VaultStorage, tmp_path: Path):
        _, rel_path = self._capture_one(storage)
        archive_dir = tmp_path / "vault" / "archive"
        clash = archive_dir / rel_path
        clash.parent.mkdir(parents=True, exist_ok=True)
        clash.write_text("existing archive content\n")
        with pytest.raises(VaultError, match="both"):
            archive_thought_file(
                thoughts_dir=storage.thoughts_dir,
                archive_dir=archive_dir,
                rel_path=rel_path,
                superseded_by=uuid4(),
                archived_at=_NOW,
            )


class TestProvenanceCapture:
    def test_capture_writes_provenance_fields(self, storage: VaultStorage):
        sources = [str(uuid4()), str(uuid4())]
        thought = storage.capture(
            content="[Lesson] merged distillation",
            source="engram-consolidate",
            embedding=_vec(0.5),
            extra_frontmatter={
                "consolidated_from": sources,
                "consolidated_range": ["2024-01-01T00:00:00+00:00", "2026-06-09T00:00:00+00:00"],
            },
        )
        text = thought.file_path.read_text(encoding="utf-8")
        assert "consolidated_from" in text
        result = read_thought(thought.file_path)
        assert result is not None
        parsed, drifts = result
        assert parsed is not None
        assert not any(d.reason == DriftReason.UNKNOWN_EXTRA_FIELD for d in drifts)

    def test_provenance_survives_metadata_rewrite(self, storage: VaultStorage):
        """update_metadata re-serializes the markdown; provenance must survive
        (the same mechanism covers reindex re-capture on another machine)."""
        thought = storage.capture(
            content="[Lesson] merged distillation",
            embedding=_vec(0.5),
            extra_frontmatter={"consolidated_from": [str(uuid4())]},
        )
        assert storage.update_metadata(thought.id, tags=["curated"])
        text = thought.file_path.read_text(encoding="utf-8")
        assert "consolidated_from" in text
        assert "curated" in text
