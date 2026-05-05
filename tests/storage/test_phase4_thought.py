"""Tests for Phase 4 Step 4 - Thought.captured_by + SQLite migration + frontmatter round-trip."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from engram.models.frontmatter import Frontmatter
from engram.models.thought import Thought
from engram.storage.markdown import (
    _serialize_frontmatter,
    read_thought,
    write_thought,
)
from engram.storage.sqlite import _ensure_captured_by_column, open_connection
from engram.storage.sqlite_queries import (
    get_thought_row,
    insert_thought,
)

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"


def _make_thought(*, captured_by: str | None = None) -> Thought:
    now = datetime.now(tz=UTC)
    return Thought(
        id=uuid4(),
        schema_version=1,
        prefix="Lesson",
        portability="portable",
        source="engram-test",
        created_at=now,
        updated_at=now,
        fingerprint="0" * 64,
        tags=[],
        vault="team-x",
        captured_by=captured_by,
        content="[Lesson] phase 4 captured_by round-trip",
        file_path=Path("thoughts/2026/05/test.md"),
    )


# === Thought model ===


def test_thought_captured_by_default_is_none() -> None:
    thought = _make_thought()
    assert thought.captured_by is None


def test_thought_captured_by_round_trip() -> None:
    thought = _make_thought(captured_by=VALID_FP)
    redumped = Thought.model_validate(thought.model_dump())
    assert redumped.captured_by == VALID_FP


# === Frontmatter model ===


def test_frontmatter_captured_by_default_is_none() -> None:
    now = datetime.now(tz=UTC)
    fm = Frontmatter(
        id=uuid4(),
        prefix="Lesson",
        portability="portable",
        source="src",
        created_at=now,
        updated_at=now,
        fingerprint="a" * 64,
    )
    assert fm.captured_by is None


def test_frontmatter_captured_by_round_trip() -> None:
    now = datetime.now(tz=UTC)
    fm = Frontmatter(
        id=uuid4(),
        prefix="Lesson",
        portability="portable",
        source="src",
        created_at=now,
        updated_at=now,
        fingerprint="a" * 64,
        captured_by=VALID_FP,
    )
    redumped = Frontmatter.model_validate(fm.model_dump())
    assert redumped.captured_by == VALID_FP


# === Markdown round-trip ===


def test_markdown_omits_captured_by_when_none() -> None:
    """Personal-vault thoughts keep the Phase 1+2+3 frontmatter shape."""
    thought = _make_thought(captured_by=None)
    yaml_text = _serialize_frontmatter(thought)
    assert "captured_by" not in yaml_text


def test_markdown_emits_captured_by_when_populated() -> None:
    """Team-vault thoughts emit captured_by in frontmatter."""
    thought = _make_thought(captured_by=VALID_FP)
    yaml_text = _serialize_frontmatter(thought)
    assert "captured_by" in yaml_text
    assert VALID_FP in yaml_text


def test_write_then_read_round_trips_captured_by(tmp_path: Path) -> None:
    """A thought written with captured_by reads back with the same value."""
    thought = _make_thought(captured_by=VALID_FP)
    written = write_thought(thought, base_dir=tmp_path)
    read_result = read_thought(written)
    assert read_result is not None
    read_thought_obj, drifts = read_result
    assert read_thought_obj is not None
    assert read_thought_obj.captured_by == VALID_FP
    assert drifts == []


def test_phase3_thought_without_captured_by_still_loads(tmp_path: Path) -> None:
    """Phase 1+2+3 thoughts (no captured_by frontmatter) still parse."""
    thought = _make_thought(captured_by=None)
    written = write_thought(thought, base_dir=tmp_path)
    read_result = read_thought(written)
    assert read_result is not None
    read_thought_obj, _ = read_result
    assert read_thought_obj is not None
    assert read_thought_obj.captured_by is None


# === SQLite migration ===


def test_phase4_schema_creates_captured_by_column(tmp_path: Path) -> None:
    """Fresh schema includes captured_by column."""
    conn = open_connection(tmp_path / "test.db", embedding_dim=384)
    try:
        cursor = conn.execute("PRAGMA table_info(thoughts)")
        column_names = {row[1] for row in cursor.fetchall()}
        assert "captured_by" in column_names
    finally:
        conn.close()


def test_phase4_migration_adds_captured_by_to_pre_phase4_db(tmp_path: Path) -> None:
    """A pre-Phase-4 database with no captured_by column gets the column added."""
    db_path = tmp_path / "legacy.db"
    legacy_conn = sqlite3.connect(str(db_path))
    try:
        # Create the pre-Phase-4 thoughts table (no captured_by).
        legacy_conn.execute(
            """
            CREATE TABLE thoughts (
              id TEXT PRIMARY KEY,
              schema_version INTEGER NOT NULL DEFAULT 1,
              prefix TEXT NOT NULL,
              portability TEXT NOT NULL,
              source TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              file_path TEXT NOT NULL UNIQUE,
              vault_name TEXT NOT NULL,
              tags TEXT,
              legacy_id TEXT,
              legacy_created_at TEXT,
              embedding_status TEXT NOT NULL DEFAULT 'ok',
              embedding_error TEXT
            )
            """
        )
        cursor = legacy_conn.execute("PRAGMA table_info(thoughts)")
        before = {row[1] for row in cursor.fetchall()}
        assert "captured_by" not in before
        # Run the migration.
        _ensure_captured_by_column(legacy_conn)
        cursor = legacy_conn.execute("PRAGMA table_info(thoughts)")
        after = {row[1] for row in cursor.fetchall()}
        assert "captured_by" in after
    finally:
        legacy_conn.close()


def test_phase4_migration_idempotent(tmp_path: Path) -> None:
    """Re-running the migration on an already-migrated DB is a no-op."""
    conn = open_connection(tmp_path / "test.db", embedding_dim=384)
    try:
        # First migration ran during open_connection.
        _ensure_captured_by_column(conn)  # Should not raise.
        cursor = conn.execute("PRAGMA table_info(thoughts)")
        captured_by_count = sum(1 for row in cursor.fetchall() if row[1] == "captured_by")
        assert captured_by_count == 1
    finally:
        conn.close()


def test_phase4_insert_thought_with_captured_by(tmp_path: Path) -> None:
    """insert_thought + get_thought_row round-trip captured_by."""
    conn = open_connection(tmp_path / "test.db", embedding_dim=384)
    try:
        thought_id = uuid4()
        now = datetime.now(tz=UTC)
        insert_thought(
            conn,
            thought_id=thought_id,
            prefix="Lesson",
            portability="portable",
            source="engram-test",
            created_at=now,
            updated_at=now,
            fingerprint="a" * 64,
            file_path="thoughts/test.md",
            vault_name="team-x",
            captured_by=VALID_FP,
        )
        row = get_thought_row(conn, thought_id)
        assert row is not None
        assert row["captured_by"] == VALID_FP
    finally:
        conn.close()


def test_phase4_insert_thought_default_null_captured_by(tmp_path: Path) -> None:
    """Phase 1+2+3 insert with no captured_by produces NULL row."""
    conn = open_connection(tmp_path / "test.db", embedding_dim=384)
    try:
        thought_id = uuid4()
        now = datetime.now(tz=UTC)
        insert_thought(
            conn,
            thought_id=thought_id,
            prefix="Lesson",
            portability="portable",
            source="engram-test",
            created_at=now,
            updated_at=now,
            fingerprint="a" * 64,
            file_path="thoughts/test.md",
            vault_name="default",
        )
        row = get_thought_row(conn, thought_id)
        assert row is not None
        assert row["captured_by"] is None
    finally:
        conn.close()
