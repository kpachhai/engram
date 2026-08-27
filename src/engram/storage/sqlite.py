"""SQLite + sqlite-vec connection factory and schema management.

The engram index has three core tables (``thoughts``,
``thought_embeddings`` virtual, and ``migrations``) plus an
implementation-detail table (``engram_settings``)
that stores model name + dimension so a startup mismatch can be detected and
reported via ``engram doctor``.

The SQLite schema version is tracked via ``PRAGMA user_version``. The schema
currently ships at version ``1``; later schema changes must increment this
and provide a migration step in :func:`_apply_migrations`.

A common deployment landmine: Python's stdlib ``sqlite3`` module is sometimes
built without ``--enable-loadable-sqlite-extensions``; on such builds
``sqlite-vec`` cannot load and engram cannot operate. The connection factory
raises a clear :class:`engram.errors.IndexError` with a remediation pointer
(use ``uv``-managed Python) when this is detected.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from pathlib import Path
from typing import Final

import sqlite_vec

from engram.errors import IndexError as EngramIndexError

#: Engram's own schema version (independent of sqlite-vec or sqlite engine versions).
ENGRAM_SCHEMA_VERSION: Final[int] = 1

#: Default vector dimension; matches ``BAAI/bge-small-en-v1.5``.
DEFAULT_EMBEDDING_DIM: Final[int] = 384

#: SQLite database file mode (owner-only).
_DB_FILE_MODE: Final[int] = 0o600

#: Settings KV keys reserved at this layer.
SETTING_EMBEDDING_MODEL_NAME: Final[str] = "embedding_model_name"
SETTING_EMBEDDING_DIM: Final[str] = "embedding_dim"
SETTING_SQLITE_VEC_VERSION: Final[str] = "sqlite_vec_version"


_CREATE_THOUGHTS_TABLE = """
CREATE TABLE IF NOT EXISTS thoughts (
  id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL DEFAULT 1,
  prefix TEXT NOT NULL,
  portability TEXT NOT NULL CHECK (portability IN ('portable', 'sensitive', 'block')),
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  file_path TEXT NOT NULL UNIQUE,
  vault_name TEXT NOT NULL,
  tags TEXT,
  legacy_id TEXT,
  legacy_created_at TEXT,
  embedding_status TEXT NOT NULL DEFAULT 'ok'
    CHECK (embedding_status IN ('ok', 'pending', 'failed')),
  embedding_error TEXT,
  captured_by TEXT
)
"""

_CREATE_THOUGHTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_thoughts_prefix ON thoughts(prefix)",
    "CREATE INDEX IF NOT EXISTS idx_thoughts_portability ON thoughts(portability)",
    "CREATE INDEX IF NOT EXISTS idx_thoughts_source ON thoughts(source)",
    "CREATE INDEX IF NOT EXISTS idx_thoughts_created_at ON thoughts(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_thoughts_vault_name ON thoughts(vault_name)",
    "CREATE INDEX IF NOT EXISTS idx_thoughts_embedding_status ON thoughts(embedding_status)",
)

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS migrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_type TEXT NOT NULL,
  source_url TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  thought_count INTEGER,
  error_count INTEGER,
  report_path TEXT
)
"""

_CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS engram_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)
"""


def _probe_extension_support() -> None:
    """Verify the running Python's sqlite3 module supports loadable extensions.

    Raises:
        IndexError: if extensions are not enabled (with a remediation pointer).
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.enable_load_extension(True)
    except (AttributeError, sqlite3.NotSupportedError) as exc:
        msg = (
            "Your Python's sqlite3 module was built without "
            "--enable-loadable-sqlite-extensions, so engram cannot load sqlite-vec. "
            "Use uv-managed Python (`uv python install 3.11`) or rebuild Python "
            "with the flag enabled."
        )
        raise EngramIndexError(msg) from exc
    finally:
        probe.close()


def open_connection(
    db_path: Path,
    *,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    embedding_model_name: str | None = None,
) -> sqlite3.Connection:
    """Open a SQLite connection at ``db_path`` with sqlite-vec loaded and schema present.

    Idempotent: re-opening an existing database is a no-op other than re-loading
    sqlite-vec. The caller is responsible for closing the returned connection.

    Args:
        db_path: Path to the SQLite database file. Parent directory will be
            created if missing.
        embedding_dim: Vector dimension for the ``thought_embeddings`` virtual
            table. Default 384 (BAAI/bge-small-en-v1.5). The dimension is
            persisted to the settings table; later opens with a different
            dimension raise unless an explicit reindex is performed.
        embedding_model_name: Optional model identifier to record in the
            settings table; ``engram doctor`` compares this against the
            current configured model.

    Raises:
        IndexError: if sqlite-vec cannot load, the embedding dimension
            disagrees with a previously-recorded value, or the SQLite engine
            lacks loadable extension support.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not db_path.exists()

    _probe_extension_support()

    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        # Ride out brief SQLITE_BUSY windows (concurrent writer / WAL
        # checkpoint contention) before raising. Five seconds covers
        # typical contention; persistent failures still surface as
        # sqlite3.OperationalError on the caller's path.
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
        except (sqlite3.OperationalError, OSError) as exc:
            msg = f"failed to load sqlite-vec extension: {exc}"
            raise EngramIndexError(msg) from exc

        _create_schema(conn, embedding_dim=embedding_dim)
        _record_or_verify_settings(
            conn,
            embedding_dim=embedding_dim,
            embedding_model_name=embedding_model_name,
        )
    except Exception:
        conn.close()
        raise

    if is_new and sys.platform != "win32":
        with contextlib.suppress(OSError):
            db_path.chmod(_DB_FILE_MODE)

    return conn


def open_connection_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an EXISTING index read-only (URI ``mode=ro``) with sqlite-vec loaded.

    Used by consolidation report mode so it can run safely beside a live
    daemon: a read-only connection can never become the second WAL writer.
    No schema creation or settings verification happens on this path.

    Raises:
        IndexError: if the database does not exist, sqlite-vec cannot load,
            or the open/probe fails - notably when a leftover ``-wal`` needs
            recovery the read-only connection cannot perform (unclean daemon
            exit shape, SQLITE_READONLY_CANTINIT class).
    """
    if not db_path.exists():
        msg = (
            f"index database does not exist at {db_path}; "
            "run `engram serve` or `engram reindex --full` to build it"
        )
        raise EngramIndexError(msg)

    _probe_extension_support()

    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
    except sqlite3.Error as exc:
        msg = f"failed to open {db_path} read-only: {exc}"
        raise EngramIndexError(msg) from exc
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            conn.enable_load_extension(True)
            try:
                sqlite_vec.load(conn)
            finally:
                conn.enable_load_extension(False)
        except (sqlite3.OperationalError, OSError) as exc:
            msg = f"failed to load sqlite-vec extension: {exc}"
            raise EngramIndexError(msg) from exc
        # Probe eagerly so corruption / WAL-recovery failures surface here
        # with a remediation message instead of at the first caller query.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        msg = (
            f"cannot read {db_path} read-only: {exc}. The index may be "
            "corrupted or hold write-ahead-log state needing recovery "
            "(daemon exited uncleanly?); run `engram doctor`, or "
            "`engram reindex --full` to rebuild from markdown"
        )
        raise EngramIndexError(msg) from exc
    except Exception:
        conn.close()
        raise

    return conn


def _create_schema(conn: sqlite3.Connection, *, embedding_dim: int) -> None:
    """Create all engram tables idempotently."""
    conn.execute(_CREATE_THOUGHTS_TABLE)
    for ddl in _CREATE_THOUGHTS_INDEXES:
        conn.execute(ddl)
    # Virtual table dimension is part of the DDL; can't be parameterized.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS thought_embeddings USING vec0("
        f"thought_id TEXT PRIMARY KEY, embedding FLOAT[{embedding_dim}])"
    )
    conn.execute(_CREATE_MIGRATIONS_TABLE)
    conn.execute(_CREATE_SETTINGS_TABLE)
    # Ensure captured_by column exists on databases created before team-write support.
    _ensure_captured_by_column(conn)
    conn.execute(f"PRAGMA user_version = {ENGRAM_SCHEMA_VERSION}")


def _ensure_captured_by_column(conn: sqlite3.Connection) -> None:
    """Add ``captured_by`` column to existing thoughts tables that lack it.

    Online migration: SQLite ALTER TABLE ADD COLUMN is fast (metadata-only;
    no row rewrite) and the new column defaults to NULL for rows captured
    before team-write support shipped. Idempotent: a fresh schema already
    has the column from the CREATE TABLE DDL above, so this no-ops.
    """
    cursor = conn.execute("PRAGMA table_info(thoughts)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if "captured_by" not in existing_columns:
        conn.execute("ALTER TABLE thoughts ADD COLUMN captured_by TEXT")


def _record_or_verify_settings(
    conn: sqlite3.Connection,
    *,
    embedding_dim: int,
    embedding_model_name: str | None,
) -> None:
    """Record embedding model + dim on first open; verify equality on subsequent opens."""
    existing_dim = get_setting(conn, SETTING_EMBEDDING_DIM)
    if existing_dim is None:
        set_setting(conn, SETTING_EMBEDDING_DIM, str(embedding_dim))
    elif int(existing_dim) != embedding_dim:
        msg = (
            f"embedding dimension mismatch: index recorded {existing_dim}, "
            f"requested {embedding_dim}. Run `engram reindex --full` after changing model."
        )
        raise EngramIndexError(msg)

    if embedding_model_name is not None:
        existing_name = get_setting(conn, SETTING_EMBEDDING_MODEL_NAME)
        if existing_name is None:
            set_setting(conn, SETTING_EMBEDDING_MODEL_NAME, embedding_model_name)
        elif existing_name != embedding_model_name:
            msg = (
                f"embedding model mismatch: index recorded {existing_name!r}, "
                f"requested {embedding_model_name!r}. Set `embedding.model` in the "
                f"vault config to {embedding_model_name!r}, then run `engram reindex --full`."
            )
            raise EngramIndexError(msg)

    sv_version = sqlite_vec.__version__ if hasattr(sqlite_vec, "__version__") else "unknown"
    set_setting(conn, SETTING_SQLITE_VEC_VERSION, sv_version)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    """Look up a single value from ``engram_settings``; return ``None`` when absent."""
    cursor = conn.execute("SELECT value FROM engram_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return None if row is None else str(row[0])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert a single ``engram_settings`` row."""
    conn.execute(
        "INSERT INTO engram_settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_user_version(conn: sqlite3.Connection) -> int:
    """Return the engram schema version from ``PRAGMA user_version``."""
    cursor = conn.execute("PRAGMA user_version")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "ENGRAM_SCHEMA_VERSION",
    "SETTING_EMBEDDING_DIM",
    "SETTING_EMBEDDING_MODEL_NAME",
    "SETTING_SQLITE_VEC_VERSION",
    "get_setting",
    "get_user_version",
    "open_connection",
    "open_connection_readonly",
    "set_setting",
]
