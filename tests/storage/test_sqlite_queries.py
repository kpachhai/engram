"""Tests for engram.storage.sqlite_queries."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from engram.models.mcp import Filter
from engram.storage.sqlite import open_connection
from engram.storage.sqlite_queries import (
    delete_thought,
    get_stats,
    get_thought_row,
    insert_thought,
    list_thoughts,
    list_thoughts_with_status,
    mark_embedding_status,
    record_migration_complete,
    record_migration_start,
    search_thoughts_by_vector,
    update_thought_body,
    update_thought_metadata,
    upsert_embedding,
)

_DIM = 384
_FP_BASE = "0" * 64


@pytest.fixture
def conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    c = open_connection(tmp_path / "engram.db")
    yield c
    c.close()


def _zero_vec(dim: int = _DIM) -> list[float]:
    return [0.0] * dim


def _make_vec(value: float, dim: int = _DIM) -> list[float]:
    """Distinct vector for ranking checks."""
    v = [0.0] * dim
    v[0] = value
    return v


def _insert(
    conn: sqlite3.Connection,
    *,
    thought_id: UUID | None = None,
    prefix: str = "Lesson",
    portability: str = "portable",
    source: str = "kpachhai",
    created_at: datetime | None = None,
    fingerprint: str | None = None,
    file_path: str | None = None,
    vault_name: str = "default",
    tags: Sequence[str] | None = None,
    embedding: Sequence[float] | None = None,
    embedding_status: str | None = None,
) -> UUID:
    """Helper that inserts a thought and returns its id."""
    tid = thought_id or uuid4()
    ts = created_at or datetime.now(UTC)
    insert_thought(
        conn,
        thought_id=tid,
        prefix=prefix,
        portability=portability,  # type: ignore[arg-type]
        source=source,
        created_at=ts,
        updated_at=ts,
        fingerprint=fingerprint or _FP_BASE,
        file_path=file_path or f"lesson/{tid.hex[:8]}.md",
        vault_name=vault_name,
        tags=tags,
        embedding=embedding,
        embedding_status=embedding_status,  # type: ignore[arg-type]
    )
    return tid


# === insert + get ===


def test_insert_and_get_thought(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_zero_vec())
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["id"] == str(tid)
    assert row["prefix"] == "Lesson"
    assert row["portability"] == "portable"
    assert row["embedding_status"] == "ok"


def test_insert_without_embedding_marks_pending(conn: sqlite3.Connection) -> None:
    tid = _insert(conn)
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["embedding_status"] == "pending"
    # No embedding row exists.
    cursor = conn.execute(
        "SELECT COUNT(*) FROM thought_embeddings WHERE thought_id = ?", (str(tid),)
    )
    assert cursor.fetchone()[0] == 0


def test_get_unknown_returns_none(conn: sqlite3.Connection) -> None:
    assert get_thought_row(conn, uuid4()) is None


def test_insert_with_tags_round_trip(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, tags=["debugging", "sqlite"], embedding=_zero_vec())
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["tags"] == ["debugging", "sqlite"]


def test_insert_with_legacy_id(conn: sqlite3.Connection) -> None:
    tid = uuid4()
    insert_thought(
        conn,
        thought_id=tid,
        prefix="Lesson",
        portability="portable",
        source="kpachhai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        fingerprint=_FP_BASE,
        file_path="lesson/x.md",
        legacy_id="ob-uuid-v4-here",
        legacy_created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["legacy_id"] == "ob-uuid-v4-here"


# === list_thoughts ===


def test_list_thoughts_empty_vault(conn: sqlite3.Connection) -> None:
    rows, total = list_thoughts(conn)
    assert rows == []
    assert total == 0


def test_list_thoughts_pagination_and_total_count(conn: sqlite3.Connection) -> None:
    """Risk R23: total_count is the true count, not len(rows)."""
    base_time = datetime.now(UTC)
    for i in range(25):
        _insert(
            conn,
            created_at=base_time + timedelta(seconds=i),
            file_path=f"lesson/file_{i:03}.md",
            embedding=_zero_vec(),
        )
    rows, total = list_thoughts(conn, limit=5, offset=0)
    assert total == 25
    assert len(rows) == 5
    rows2, total2 = list_thoughts(conn, limit=5, offset=20)
    assert total2 == 25
    assert len(rows2) == 5


def test_list_thoughts_offset_overflow_returns_empty(conn: sqlite3.Connection) -> None:
    """B4: offset > total_count returns empty results, correct total."""
    _insert(conn, embedding=_zero_vec())
    rows, total = list_thoughts(conn, limit=10, offset=100)
    assert rows == []
    assert total == 1


def test_list_thoughts_includes_pending_rows(conn: sqlite3.Connection) -> None:
    """R2: pending rows appear in list_thoughts (excluded only from search)."""
    _insert(conn)  # pending
    _insert(conn, embedding=_zero_vec())  # ok
    rows, total = list_thoughts(conn)
    assert total == 2
    assert len(rows) == 2


def test_list_thoughts_sort_descending_by_default(conn: sqlite3.Connection) -> None:
    base = datetime.now(UTC)
    older = _insert(conn, created_at=base, embedding=_zero_vec())
    newer = _insert(conn, created_at=base + timedelta(minutes=5), embedding=_zero_vec())
    rows, _ = list_thoughts(conn)
    assert rows[0]["id"] == str(newer)
    assert rows[1]["id"] == str(older)


def test_list_thoughts_sort_ascending(conn: sqlite3.Connection) -> None:
    base = datetime.now(UTC)
    older = _insert(conn, created_at=base, embedding=_zero_vec())
    newer = _insert(conn, created_at=base + timedelta(minutes=5), embedding=_zero_vec())
    rows, _ = list_thoughts(conn, sort="created_at_asc")
    assert rows[0]["id"] == str(older)
    assert rows[1]["id"] == str(newer)


def test_list_thoughts_filter_by_prefix(conn: sqlite3.Connection) -> None:
    _insert(conn, prefix="Lesson", embedding=_zero_vec())
    _insert(conn, prefix="Pattern", embedding=_zero_vec())
    rows, total = list_thoughts(conn, filter_=Filter(prefix="Lesson"))
    assert total == 1
    assert rows[0]["prefix"] == "Lesson"


def test_list_thoughts_filter_by_prefix_list(conn: sqlite3.Connection) -> None:
    _insert(conn, prefix="Lesson", embedding=_zero_vec())
    _insert(conn, prefix="Pattern", embedding=_zero_vec())
    _insert(conn, prefix="Friction", embedding=_zero_vec())
    _, total = list_thoughts(conn, filter_=Filter(prefix=["Lesson", "Pattern"]))
    assert total == 2


def test_list_thoughts_filter_by_tags_no_substring_false_match(
    conn: sqlite3.Connection,
) -> None:
    """R24: tag filter must NOT false-match substrings."""
    _insert(conn, tags=["debugging-old"], embedding=_zero_vec())
    _insert(conn, tags=["debugging"], embedding=_zero_vec())
    rows, total = list_thoughts(conn, filter_=Filter(tags=["debugging"]))
    assert total == 1
    # Confirm the matched row is the exact-tag one.
    assert rows[0]["tags"] == ["debugging"]


def test_list_thoughts_filter_by_date_range(conn: sqlite3.Connection) -> None:
    base = datetime.now(UTC)
    _insert(conn, created_at=base - timedelta(days=10), embedding=_zero_vec())
    _insert(conn, created_at=base - timedelta(days=5), embedding=_zero_vec())
    _insert(conn, created_at=base - timedelta(days=1), embedding=_zero_vec())
    _, total = list_thoughts(
        conn,
        filter_=Filter(
            created_after=base - timedelta(days=7),
            created_before=base,
        ),
    )
    assert total == 2


def test_list_thoughts_combined_filter(conn: sqlite3.Connection) -> None:
    _insert(conn, prefix="Lesson", source="alice", embedding=_zero_vec())
    _insert(conn, prefix="Lesson", source="bob", embedding=_zero_vec())
    _insert(conn, prefix="Pattern", source="alice", embedding=_zero_vec())
    _, total = list_thoughts(
        conn,
        filter_=Filter(prefix="Lesson", source="alice"),
    )
    assert total == 1


def test_list_thoughts_limit_zero(conn: sqlite3.Connection) -> None:
    """B4: limit=0 returns empty results but accurate total_count."""
    _insert(conn, embedding=_zero_vec())
    _insert(conn, embedding=_zero_vec())
    rows, total = list_thoughts(conn, limit=0)
    assert rows == []
    assert total == 2


# === update ===


def test_update_thought_metadata(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_zero_vec())
    new_ts = datetime.now(UTC) + timedelta(minutes=1)
    assert update_thought_metadata(
        conn,
        tid,
        prefix="Pattern",
        tags=["new-tag"],
        updated_at=new_ts,
    )
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["prefix"] == "Pattern"
    assert row["tags"] == ["new-tag"]


def test_update_thought_metadata_unknown_id_returns_false(
    conn: sqlite3.Connection,
) -> None:
    result = update_thought_metadata(conn, uuid4(), prefix="Pattern")
    assert result is False


def test_update_thought_metadata_no_fields_is_noop(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_zero_vec())
    assert update_thought_metadata(conn, tid) is False


def test_update_thought_body_re_embeds(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_make_vec(0.1))
    new_ts = datetime.now(UTC) + timedelta(minutes=1)
    new_fp = "f" * 64
    assert update_thought_body(
        conn,
        tid,
        fingerprint=new_fp,
        updated_at=new_ts,
        embedding=_make_vec(0.5),
    )
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["fingerprint"] == new_fp
    assert row["embedding_status"] == "ok"


def test_update_thought_body_without_embedding_marks_pending(
    conn: sqlite3.Connection,
) -> None:
    tid = _insert(conn, embedding=_zero_vec())
    new_ts = datetime.now(UTC) + timedelta(minutes=1)
    new_fp = "1" * 64
    assert update_thought_body(conn, tid, fingerprint=new_fp, updated_at=new_ts)
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["embedding_status"] == "pending"
    cursor = conn.execute(
        "SELECT COUNT(*) FROM thought_embeddings WHERE thought_id = ?", (str(tid),)
    )
    assert cursor.fetchone()[0] == 0


def test_update_thought_body_unknown_id_returns_false(conn: sqlite3.Connection) -> None:
    assert (
        update_thought_body(
            conn,
            uuid4(),
            fingerprint="0" * 64,
            updated_at=datetime.now(UTC),
        )
        is False
    )


# === delete ===


def test_delete_thought_removes_row_and_embedding(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_zero_vec())
    assert delete_thought(conn, tid)
    assert get_thought_row(conn, tid) is None
    cursor = conn.execute(
        "SELECT COUNT(*) FROM thought_embeddings WHERE thought_id = ?", (str(tid),)
    )
    assert cursor.fetchone()[0] == 0


def test_delete_thought_unknown_returns_false(conn: sqlite3.Connection) -> None:
    assert delete_thought(conn, uuid4()) is False


# === upsert_embedding ===


def test_upsert_embedding_promotes_pending_to_ok(conn: sqlite3.Connection) -> None:
    tid = _insert(conn)  # pending
    upsert_embedding(conn, tid, _make_vec(0.7))
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["embedding_status"] == "ok"


def test_upsert_embedding_replaces_existing(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_make_vec(0.1))
    upsert_embedding(conn, tid, _make_vec(0.9))
    cursor = conn.execute(
        "SELECT COUNT(*) FROM thought_embeddings WHERE thought_id = ?", (str(tid),)
    )
    assert cursor.fetchone()[0] == 1


# === mark_embedding_status ===


def test_mark_embedding_status_failed(conn: sqlite3.Connection) -> None:
    tid = _insert(conn, embedding=_zero_vec())
    assert mark_embedding_status(conn, tid, "failed", error_message="model unavailable")
    row = get_thought_row(conn, tid)
    assert row is not None
    assert row["embedding_status"] == "failed"
    assert row["embedding_error"] == "model unavailable"


def test_list_thoughts_with_status(conn: sqlite3.Connection) -> None:
    _insert(conn)  # pending
    _insert(conn, embedding=_zero_vec())  # ok
    _insert(conn)  # pending
    pending = list_thoughts_with_status(conn, "pending")
    assert len(pending) == 2


# === search_thoughts_by_vector ===


def test_search_returns_results(conn: sqlite3.Connection) -> None:
    """Closest-vector wins; pending rows excluded from search."""
    base = datetime.now(UTC)
    far = _insert(conn, embedding=_make_vec(0.0), created_at=base, file_path="lesson/far.md")
    near = _insert(conn, embedding=_make_vec(1.0), created_at=base, file_path="lesson/near.md")
    _insert(conn, file_path="lesson/pending.md")  # pending; must be excluded

    results, total_found = search_thoughts_by_vector(conn, query_vector=_make_vec(1.0), k=10)
    # total_found counts ok-embedding rows in filter scope (here, all = 2).
    assert total_found == 2
    ids = [r[0]["id"] for r in results]
    assert str(near) in ids
    assert str(far) in ids
    # near should rank ahead of far.
    near_idx = ids.index(str(near))
    far_idx = ids.index(str(far))
    assert near_idx < far_idx


def test_search_excludes_pending_rows(conn: sqlite3.Connection) -> None:
    _insert(conn, file_path="lesson/pending.md")  # pending
    _insert(conn, embedding=_make_vec(0.5), file_path="lesson/ok.md")  # ok
    results, total_found = search_thoughts_by_vector(conn, query_vector=_make_vec(0.5), k=10)
    assert total_found == 1  # only the ok one
    assert len(results) == 1


def test_search_with_filter(conn: sqlite3.Connection) -> None:
    _insert(conn, prefix="Lesson", embedding=_make_vec(0.5), file_path="lesson/a.md")
    _insert(conn, prefix="Pattern", embedding=_make_vec(0.5), file_path="pattern/b.md")
    results, total_found = search_thoughts_by_vector(
        conn,
        query_vector=_make_vec(0.5),
        k=10,
        filter_=Filter(prefix="Lesson"),
    )
    assert total_found == 1
    assert len(results) == 1
    assert results[0][0]["prefix"] == "Lesson"


def test_search_k_zero_returns_empty(conn: sqlite3.Connection) -> None:
    _insert(conn, embedding=_make_vec(0.5))
    results, total_found = search_thoughts_by_vector(conn, query_vector=_make_vec(0.5), k=0)
    assert results == []
    assert total_found == 0


def test_search_similarity_in_zero_to_one_range(conn: sqlite3.Connection) -> None:
    _insert(conn, embedding=_make_vec(0.5))
    results, _ = search_thoughts_by_vector(conn, query_vector=_make_vec(0.5), k=10)
    for _row, similarity in results:
        assert 0.0 <= similarity <= 1.0


# === get_stats ===


def test_get_stats_empty_vault(conn: sqlite3.Connection) -> None:
    """B7 / Q3: oldest/newest are None on empty vault."""
    stats = get_stats(conn)
    assert stats["total_count"] == 0
    assert stats["by_prefix"] == {}
    assert stats["by_portability"] == {"portable": 0, "sensitive": 0, "block": 0}
    assert stats["oldest"] is None
    assert stats["newest"] is None


def test_get_stats_populated(conn: sqlite3.Connection) -> None:
    base = datetime.now(UTC)
    _insert(conn, prefix="Lesson", source="alice", created_at=base, embedding=_zero_vec())
    _insert(
        conn,
        prefix="Lesson",
        source="bob",
        created_at=base + timedelta(days=1),
        embedding=_zero_vec(),
    )
    _insert(
        conn,
        prefix="Pattern",
        source="alice",
        portability="sensitive",
        created_at=base + timedelta(days=2),
        embedding=_zero_vec(),
    )
    stats = get_stats(conn)
    assert stats["total_count"] == 3
    assert stats["by_prefix"]["Lesson"] == 2
    assert stats["by_prefix"]["Pattern"] == 1
    assert stats["by_portability"]["portable"] == 2
    assert stats["by_portability"]["sensitive"] == 1
    assert stats["by_source"] == {"alice": 2, "bob": 1}
    assert stats["oldest"] is not None
    assert stats["newest"] is not None


def test_get_stats_includes_pending_rows(conn: sqlite3.Connection) -> None:
    """Stats counts include pending rows so total matches markdown count on disk."""
    _insert(conn)  # pending
    _insert(conn, embedding=_zero_vec())
    stats = get_stats(conn)
    assert stats["total_count"] == 2


# === migrations audit ===


def test_record_migration_lifecycle(conn: sqlite3.Connection) -> None:
    started = datetime.now(UTC)
    rowid = record_migration_start(
        conn,
        source_type="open-brain",
        source_url="https://example.supabase.co/functions/v1/open-brain-mcp",
        started_at=started,
    )
    assert rowid > 0
    record_migration_complete(
        conn,
        rowid,
        completed_at=started + timedelta(minutes=5),
        thought_count=1840,
        error_count=2,
        report_path="/tmp/migration-report.json",
    )
    row = conn.execute(
        "SELECT source_type, thought_count, error_count, report_path FROM migrations WHERE id = ?",
        (rowid,),
    ).fetchone()
    assert row[0] == "open-brain"
    assert row[1] == 1840
    assert row[2] == 2


# === SQL injection resistance ===


def test_filter_values_not_sql_interpreted(conn: sqlite3.Connection) -> None:
    _insert(conn, prefix="Lesson", embedding=_zero_vec())
    _, total = list_thoughts(
        conn,
        filter_=Filter(prefix=["Lesson'; DROP TABLE thoughts; --"]),
    )
    assert total == 0
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE name = 'thoughts'")
    assert cursor.fetchone() is not None  # table still exists


# === parameterized helper interactions ===


def test_insert_thought_with_uuid_object_or_string(conn: sqlite3.Connection) -> None:
    tid_obj = uuid4()
    insert_thought(
        conn,
        thought_id=tid_obj,
        prefix="Lesson",
        portability="portable",
        source="k",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        fingerprint=_FP_BASE,
        file_path="lesson/a.md",
        embedding=_zero_vec(),
    )
    # Read back via str id.
    row = get_thought_row(conn, str(tid_obj))
    assert row is not None
    # And via UUID object.
    row2 = get_thought_row(conn, tid_obj)
    assert row2 is not None
