"""Tests for engram.storage.sqlite - connection factory + schema + settings KV."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from engram.errors import IndexError as EngramIndexError
from engram.storage.sqlite import (
    DEFAULT_EMBEDDING_DIM,
    ENGRAM_SCHEMA_VERSION,
    SETTING_EMBEDDING_DIM,
    SETTING_EMBEDDING_MODEL_NAME,
    SETTING_SQLITE_VEC_VERSION,
    get_setting,
    get_user_version,
    open_connection,
    set_setting,
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (name,),
    )
    return cursor.fetchone() is not None


def _index_exists(conn: sqlite3.Connection, name: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    )
    return cursor.fetchone() is not None


# === schema creation ===


def test_open_creates_db_file_and_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "engram.db"
    assert not db_path.exists()

    conn = open_connection(db_path)
    try:
        assert db_path.exists()
        assert _table_exists(conn, "thoughts")
        assert _table_exists(conn, "thought_embeddings")
        assert _table_exists(conn, "migrations")
        assert _table_exists(conn, "engram_settings")
    finally:
        conn.close()


def test_thoughts_indexes_present(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        for index_name in (
            "idx_thoughts_prefix",
            "idx_thoughts_portability",
            "idx_thoughts_source",
            "idx_thoughts_created_at",
            "idx_thoughts_vault_name",
            "idx_thoughts_embedding_status",
        ):
            assert _index_exists(conn, index_name), f"missing index {index_name}"
    finally:
        conn.close()


def test_user_version_set_to_one(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        assert get_user_version(conn) == ENGRAM_SCHEMA_VERSION
    finally:
        conn.close()


def test_reopen_existing_db_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "engram.db"
    open_connection(db_path).close()
    open_connection(db_path).close()
    open_connection(db_path).close()

    conn = open_connection(db_path)
    try:
        assert _table_exists(conn, "thoughts")
        assert get_user_version(conn) == ENGRAM_SCHEMA_VERSION
    finally:
        conn.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_db_file_mode_is_0600_when_created(tmp_path: Path) -> None:
    db_path = tmp_path / "engram.db"
    open_connection(db_path).close()
    mode = db_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_thoughts_table_columns(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        cursor = conn.execute("PRAGMA table_info(thoughts)")
        columns = {row[1] for row in cursor.fetchall()}
        # All required columns from 02-TECHNICAL_DESIGN.md SQLite Schema.
        for col in (
            "id",
            "schema_version",
            "prefix",
            "portability",
            "source",
            "created_at",
            "updated_at",
            "fingerprint",
            "file_path",
            "vault_name",
            "tags",
            "legacy_id",
            "legacy_created_at",
            "embedding_status",
            "embedding_error",
        ):
            assert col in columns, f"missing column: {col}"
    finally:
        conn.close()


# === sqlite-vec virtual table ===


def test_sqlite_vec_loaded_and_virtual_table_works(tmp_path: Path) -> None:
    """Confirm sqlite-vec loaded and the embedding virtual table accepts vectors."""
    conn = open_connection(tmp_path / "engram.db")
    try:
        # Insert a 384-dim vector.
        sample_vec = [0.0] * DEFAULT_EMBEDDING_DIM
        sample_vec[0] = 1.0
        # vec0 takes a JSON-encoded array or raw bytes; either is accepted.
        conn.execute(
            "INSERT INTO thought_embeddings(thought_id, embedding) VALUES (?, ?)",
            ("test-id-1", str(sample_vec)),
        )
        cursor = conn.execute(
            "SELECT thought_id FROM thought_embeddings WHERE thought_id = ?",
            ("test-id-1",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "test-id-1"
    finally:
        conn.close()


def test_custom_embedding_dim_creates_correct_virtual_table(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db", embedding_dim=128)
    try:
        # Inserting a 128-dim vector should succeed; a 384-dim should fail.
        good_vec = [0.0] * 128
        conn.execute(
            "INSERT INTO thought_embeddings(thought_id, embedding) VALUES (?, ?)",
            ("good", str(good_vec)),
        )
        bad_vec = [0.0] * 384
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO thought_embeddings(thought_id, embedding) VALUES (?, ?)",
                ("bad", str(bad_vec)),
            )
    finally:
        conn.close()


# === settings KV ===


def test_set_get_setting_roundtrip(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        set_setting(conn, "test_key", "test_value")
        assert get_setting(conn, "test_key") == "test_value"
    finally:
        conn.close()


def test_get_setting_missing_returns_none(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        assert get_setting(conn, "no_such_key") is None
    finally:
        conn.close()


def test_set_setting_upserts(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        set_setting(conn, "k", "first")
        set_setting(conn, "k", "second")
        assert get_setting(conn, "k") == "second"
    finally:
        conn.close()


def test_open_records_embedding_dim_in_settings(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db", embedding_dim=384)
    try:
        assert get_setting(conn, SETTING_EMBEDDING_DIM) == "384"
    finally:
        conn.close()


def test_open_records_embedding_model_name(tmp_path: Path) -> None:
    conn = open_connection(
        tmp_path / "engram.db",
        embedding_dim=384,
        embedding_model_name="BAAI/bge-small-en-v1.5",
    )
    try:
        assert get_setting(conn, SETTING_EMBEDDING_MODEL_NAME) == "BAAI/bge-small-en-v1.5"
    finally:
        conn.close()


def test_open_records_sqlite_vec_version(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        version = get_setting(conn, SETTING_SQLITE_VEC_VERSION)
        assert version is not None
        assert len(version) > 0
    finally:
        conn.close()


# === dimension / model mismatch detection ===


def test_dimension_mismatch_on_reopen_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "engram.db"
    open_connection(db_path, embedding_dim=384).close()
    with pytest.raises(EngramIndexError, match="embedding dimension mismatch"):
        open_connection(db_path, embedding_dim=768)


def test_model_name_mismatch_on_reopen_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "engram.db"
    open_connection(db_path, embedding_model_name="model-a").close()
    with pytest.raises(EngramIndexError, match="embedding model mismatch"):
        open_connection(db_path, embedding_model_name="model-b")


def test_model_name_unset_then_set_records(tmp_path: Path) -> None:
    """First open without model name; second open records it."""
    db_path = tmp_path / "engram.db"
    open_connection(db_path).close()
    conn = open_connection(db_path, embedding_model_name="BAAI/bge-small-en-v1.5")
    try:
        assert get_setting(conn, SETTING_EMBEDDING_MODEL_NAME) == "BAAI/bge-small-en-v1.5"
    finally:
        conn.close()


# === parameterized queries / SQL injection resistance ===


def test_set_setting_does_not_inject(tmp_path: Path) -> None:
    """Even adversarial values are stored verbatim, not interpreted as SQL."""
    conn = open_connection(tmp_path / "engram.db")
    try:
        adversarial = "'; DROP TABLE thoughts; --"
        set_setting(conn, "evil", adversarial)
        assert get_setting(conn, "evil") == adversarial
        # Confirm the table still exists.
        assert _table_exists(conn, "thoughts")
    finally:
        conn.close()


# === checks (constraints) ===


def test_portability_check_constraint_enforced(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thoughts(id, prefix, portability, source, created_at, "
                "updated_at, fingerprint, file_path, vault_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "id1",
                    "Lesson",
                    "confidential",  # not in allowed set
                    "k",
                    "2026-05-04T00:00:00Z",
                    "2026-05-04T00:00:00Z",
                    "0" * 64,
                    "lesson/a.md",
                    "default",
                ),
            )
    finally:
        conn.close()


def test_embedding_status_check_constraint(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thoughts(id, prefix, portability, source, created_at, "
                "updated_at, fingerprint, file_path, vault_name, embedding_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "id-bad-status",
                    "Lesson",
                    "portable",
                    "k",
                    "2026-05-04T00:00:00Z",
                    "2026-05-04T00:00:00Z",
                    "0" * 64,
                    "lesson/a.md",
                    "default",
                    "weird-status",  # not in {ok, pending, failed}
                ),
            )
    finally:
        conn.close()


def test_id_unique_constraint(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        for path in ("a.md", "b.md"):
            conn.execute(
                "INSERT INTO thoughts(id, prefix, portability, source, created_at, "
                "updated_at, fingerprint, file_path, vault_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "duplicate-id",
                    "Lesson",
                    "portable",
                    "k",
                    "2026-05-04T00:00:00Z",
                    "2026-05-04T00:00:00Z",
                    "0" * 64,
                    f"lesson/{path}",
                    "default",
                ),
            ) if path == "a.md" else None
            if path == "b.md":
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO thoughts(id, prefix, portability, source, created_at, "
                        "updated_at, fingerprint, file_path, vault_name) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "duplicate-id",
                            "Lesson",
                            "portable",
                            "k",
                            "2026-05-04T00:00:00Z",
                            "2026-05-04T00:00:00Z",
                            "0" * 64,
                            f"lesson/{path}",
                            "default",
                        ),
                    )
    finally:
        conn.close()


def test_file_path_unique_constraint(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "engram.db")
    try:
        conn.execute(
            "INSERT INTO thoughts(id, prefix, portability, source, created_at, "
            "updated_at, fingerprint, file_path, vault_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "id-A",
                "Lesson",
                "portable",
                "k",
                "2026-05-04T00:00:00Z",
                "2026-05-04T00:00:00Z",
                "0" * 64,
                "lesson/same.md",
                "default",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO thoughts(id, prefix, portability, source, created_at, "
                "updated_at, fingerprint, file_path, vault_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "id-B",
                    "Lesson",
                    "portable",
                    "k",
                    "2026-05-04T00:00:00Z",
                    "2026-05-04T00:00:00Z",
                    "0" * 64,
                    "lesson/same.md",  # duplicate file_path
                    "default",
                ),
            )
    finally:
        conn.close()
