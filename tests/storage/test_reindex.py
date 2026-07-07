"""Tests for engram.storage.reindex."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engram.storage.facade import VaultStorage
from engram.storage.markdown import write_thought
from engram.storage.reindex import ReindexMode, reindex_vault
from engram.storage.sqlite_queries import get_thought_row
from engram.utils.fingerprint import compute_fingerprint

_DIM = 384


def _zero_vec() -> list[float]:
    return [0.0] * _DIM


def _vec_a() -> list[float]:
    v = [0.0] * _DIM
    v[0] = 1.0
    return v


def _vec_b() -> list[float]:
    v = [0.0] * _DIM
    v[5] = 1.0
    return v


def _embed_stub(text: str) -> list[float]:
    """Deterministic stub embedder."""
    v = [0.0] * _DIM
    v[hash(text) % _DIM] = 1.0
    return v


@pytest.fixture
def vault(tmp_path: Path):
    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=_DIM,
    )
    yield storage
    storage.close()


# === incremental ===


def test_incremental_inserts_new_files(vault: VaultStorage):
    """File on disk that is missing from SQLite -> insert."""
    captured = vault.capture(content="[Lesson] one", embedding=_zero_vec())
    # Wipe SQLite to simulate a missing row.
    vault.conn.execute("DELETE FROM thoughts WHERE id = ?", (str(captured.id),))
    vault.conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (str(captured.id),))
    assert get_thought_row(vault.conn, captured.id) is None

    report = reindex_vault(vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub)
    assert report.inserted == 1
    assert get_thought_row(vault.conn, captured.id) is not None


def test_incremental_re_embeds_drifted_body(vault: VaultStorage):
    """Body fingerprint drift -> re-embed + bump updated_at."""
    captured = vault.capture(content="[Lesson] original", embedding=_vec_a())
    # Externally edit the markdown body (fingerprint will differ).
    new_thought = captured.model_copy(
        update={
            "content": "[Lesson] modified content",
            "fingerprint": "f" * 64,
            "updated_at": captured.updated_at + timedelta(seconds=10),
        }
    )
    write_thought(new_thought, base_dir=vault.thoughts_dir)

    report = reindex_vault(vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub)
    assert report.body_reindexed == 1


def test_incremental_updates_drifted_metadata(vault: VaultStorage):
    """Frontmatter-only edit -> metadata update; no re-embed."""
    captured = vault.capture(content="[Lesson] body", embedding=_vec_a())
    # Edit only metadata (tags), keep body identical.
    new_thought = captured.model_copy(
        update={
            "tags": ["new-tag"],
            "updated_at": captured.updated_at + timedelta(seconds=10),
        }
    )
    write_thought(new_thought, base_dir=vault.thoughts_dir)

    report = reindex_vault(vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub)
    assert report.metadata_reindexed == 1
    assert report.body_reindexed == 0


def test_incremental_detects_orphans(vault: VaultStorage):
    """SQLite row whose markdown file is gone -> reported as orphan, not deleted by default."""
    captured = vault.capture(content="[Lesson] orphan", embedding=_zero_vec())
    captured.file_path.unlink()
    report = reindex_vault(vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub)
    assert report.orphans_detected == 1
    assert report.orphans_removed == 0
    # Orphan row still in SQLite.
    assert get_thought_row(vault.conn, captured.id) is not None


def test_incremental_remove_orphans_when_flagged(vault: VaultStorage):
    captured = vault.capture(content="[Lesson] orphan", embedding=_zero_vec())
    captured.file_path.unlink()
    report = reindex_vault(
        vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub, remove_orphans=True
    )
    assert report.orphans_removed == 1
    assert get_thought_row(vault.conn, captured.id) is None


def test_incremental_concurrent_capture_protected_by_snapshot(
    vault: VaultStorage,
):
    """R11: a row whose updated_at is AFTER the reindex snapshot must NOT be removed."""
    # Pre-existing orphan to remove.
    older = vault.capture(content="[Lesson] orphan", embedding=_zero_vec())
    older.file_path.unlink()

    # Simulate a "just-captured" row with future updated_at by patching the row directly.
    vault.conn.execute(
        "UPDATE thoughts SET updated_at = ? WHERE id = ?",
        (
            (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            str(older.id),
        ),
    )
    report = reindex_vault(
        vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub, remove_orphans=True
    )
    assert report.orphans_detected == 1
    # NOT removed because updated_at > snapshot.
    assert report.orphans_removed == 0
    assert get_thought_row(vault.conn, older.id) is not None


# === full ===


def test_full_drops_and_rebuilds(vault: VaultStorage):
    a = vault.capture(content="[Lesson] one", embedding=_vec_a())
    b = vault.capture(content="[Pattern] two", embedding=_vec_b())
    # Verify pre-state.
    assert get_thought_row(vault.conn, a.id) is not None
    assert get_thought_row(vault.conn, b.id) is not None

    report = reindex_vault(vault, mode=ReindexMode.FULL, embed_fn=_embed_stub)
    assert report.inserted == 2
    assert report.walked == 2
    # Both still indexed (now via markdown round-trip).
    assert get_thought_row(vault.conn, a.id) is not None
    assert get_thought_row(vault.conn, b.id) is not None


def test_full_requires_embed_fn(vault: VaultStorage):
    from engram.errors import IndexError as EngramIndexError

    with pytest.raises(EngramIndexError, match="embed_fn"):
        reindex_vault(vault, mode=ReindexMode.FULL, embed_fn=None)


# === repair ===


def test_repair_promotes_pending_to_ok(vault: VaultStorage):
    pending = vault.capture(content="[Lesson] pending")
    row = get_thought_row(vault.conn, pending.id)
    assert row is not None
    assert row["embedding_status"] == "pending"

    report = reindex_vault(vault, mode=ReindexMode.REPAIR, embed_fn=_embed_stub)
    assert report.embeddings_repaired == 1

    row = get_thought_row(vault.conn, pending.id)
    assert row is not None
    assert row["embedding_status"] == "ok"


def test_repair_marks_failed_on_embed_exception(vault: VaultStorage):
    pending = vault.capture(content="[Lesson] pending")

    def bad_embed(_text: str) -> list[float]:
        msg = "embed exploded"
        raise RuntimeError(msg)

    report = reindex_vault(vault, mode=ReindexMode.REPAIR, embed_fn=bad_embed)
    assert report.embedding_failures == 1
    assert report.embeddings_repaired == 0
    row = get_thought_row(vault.conn, pending.id)
    assert row is not None
    assert row["embedding_status"] == "failed"
    assert row["embedding_error"] == "embed exploded"


def test_repair_skips_when_no_pending(vault: VaultStorage):
    vault.capture(content="[Lesson] ok", embedding=_zero_vec())
    report = reindex_vault(vault, mode=ReindexMode.REPAIR, embed_fn=_embed_stub)
    assert report.embeddings_repaired == 0


# === remove_orphans ===


def test_remove_orphans_only_removes_files_missing_from_disk(vault: VaultStorage):
    keeper = vault.capture(content="[Lesson] keeper", embedding=_zero_vec())
    orphan = vault.capture(content="[Lesson] orphan", embedding=_zero_vec())
    orphan.file_path.unlink()

    report = reindex_vault(vault, mode=ReindexMode.REMOVE_ORPHANS)
    assert report.orphans_detected == 1
    assert report.orphans_removed == 1
    assert get_thought_row(vault.conn, keeper.id) is not None
    assert get_thought_row(vault.conn, orphan.id) is None


def test_remove_orphans_respects_snapshot_for_concurrent_safety(vault: VaultStorage):
    orphan = vault.capture(content="[Lesson] orphan", embedding=_zero_vec())
    orphan.file_path.unlink()
    # Push updated_at into the future to simulate a concurrent capture.
    future = datetime.now(UTC) + timedelta(hours=1)
    vault.conn.execute(
        "UPDATE thoughts SET updated_at = ? WHERE id = ?",
        (future.isoformat(), str(orphan.id)),
    )
    report = reindex_vault(vault, mode=ReindexMode.REMOVE_ORPHANS)
    assert report.orphans_detected == 1
    assert report.orphans_removed == 0  # protected by snapshot guard


# === report ===


def test_report_records_duration_and_mode(vault: VaultStorage):
    vault.capture(content="[Lesson] x", embedding=_zero_vec())
    report = reindex_vault(vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub)
    assert report.mode is ReindexMode.INCREMENTAL
    assert report.duration_seconds >= 0.0


# === index-only contract: reindex must never rewrite the markdown SoT ===

_TEAM_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic test fingerprint


def test_full_reindex_preserves_markdown_and_row_metadata(vault: VaultStorage):
    """--full must not rewrite markdown; captured_by + updated_at survive.

    Regression: _full_reindex used to route through storage.capture(),
    which rewrote the file, reset updated_at to created_at, and dropped
    captured_by from both the row and the markdown.
    """
    captured = vault.capture(
        content="[Lesson] team attribution survives reindex",
        embedding=_vec_a(),
        captured_by=_TEAM_FP,
    )
    assert vault.update_body(
        captured.id,
        new_content="[Lesson] edited after capture",
        embedding=_vec_b(),
    )
    row_before = get_thought_row(vault.conn, captured.id)
    assert row_before is not None
    assert row_before["captured_by"] == _TEAM_FP
    assert row_before["updated_at"] > row_before["created_at"]
    bytes_before = captured.file_path.read_bytes()

    report = reindex_vault(vault, mode=ReindexMode.FULL, embed_fn=_embed_stub)

    assert report.inserted == 1
    assert captured.file_path.read_bytes() == bytes_before
    row_after = get_thought_row(vault.conn, captured.id)
    assert row_after is not None
    assert row_after["captured_by"] == _TEAM_FP
    assert row_after["updated_at"] == row_before["updated_at"]
    assert row_after["created_at"] == row_before["created_at"]


def test_full_reindex_no_duplicate_file_on_slug_drift(vault: VaultStorage):
    """A hand-edited body (slug source drifted) must not spawn a second file."""
    captured = vault.capture(
        content="[Lesson] original body text here",
        embedding=_vec_a(),
    )
    new_content = "[Lesson] a wholly different opening line"
    edited = captured.model_copy(
        update={
            "content": new_content,
            "fingerprint": compute_fingerprint(new_content),
        }
    )
    write_thought(edited, base_dir=vault.thoughts_dir)

    reindex_vault(vault, mode=ReindexMode.FULL, embed_fn=_embed_stub)

    md_files = list(vault.thoughts_dir.rglob("*.md"))
    assert len(md_files) == 1, f"duplicate markdown files created: {md_files}"
    row = get_thought_row(vault.conn, captured.id)
    assert row is not None
    assert (vault.thoughts_dir / row["file_path"]).resolve() == captured.file_path.resolve()


def test_full_reindex_preserves_legacy_created_at(vault: VaultStorage):
    """Migrated thoughts keep their legacy_created_at through --full."""
    legacy_dt = datetime(2020, 1, 1, tzinfo=UTC)
    captured = vault.capture(
        content="[Lesson] migrated from another store",
        embedding=_vec_a(),
        legacy_id="ob-123",
        legacy_created_at=legacy_dt,
    )
    row_before = get_thought_row(vault.conn, captured.id)
    assert row_before is not None
    assert row_before["legacy_created_at"] is not None

    reindex_vault(vault, mode=ReindexMode.FULL, embed_fn=_embed_stub)

    row_after = get_thought_row(vault.conn, captured.id)
    assert row_after is not None
    assert row_after["legacy_created_at"] == row_before["legacy_created_at"]
    assert row_after["legacy_id"] == "ob-123"


def test_incremental_missing_row_insert_is_index_only(vault: VaultStorage):
    """The incremental missing-row branch must not rewrite markdown either."""
    captured = vault.capture(
        content="[Lesson] incremental attribution",
        embedding=_vec_a(),
        captured_by=_TEAM_FP,
    )
    bytes_before = captured.file_path.read_bytes()
    vault.conn.execute("DELETE FROM thoughts WHERE id = ?", (str(captured.id),))
    vault.conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (str(captured.id),))

    report = reindex_vault(vault, mode=ReindexMode.INCREMENTAL, embed_fn=_embed_stub)

    assert report.inserted == 1
    assert captured.file_path.read_bytes() == bytes_before
    row = get_thought_row(vault.conn, captured.id)
    assert row is not None
    assert row["captured_by"] == _TEAM_FP
