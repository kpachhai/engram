"""Query helpers for the SQLite + sqlite-vec index.

Operations exposed:

* :func:`insert_thought` - insert a thoughts row + (optional) embedding row in
  one transaction.
* :func:`get_thought_row` - fetch a single thoughts row by id.
* :func:`list_thoughts` - filtered, sorted, paginated list. Returns rows and
  the true ``total_count`` (Risk R23: the ``total_count`` field is the actual
  count of filter matches before pagination, not ``len(results)``).
* :func:`search_thoughts_by_vector` - sqlite-vec ANN search with metadata
  filter. ``embedding_status='pending'`` rows are excluded from search results
  (Risk R2). Returns rows + true ``total_found`` (count of filter-eligible
  thoughts that have embeddings).
* :func:`update_thought_metadata` - patch frontmatter-only fields.
* :func:`update_thought_body` - body changed; bump fingerprint, ``updated_at``,
  and re-embed.
* :func:`delete_thought` - remove a thoughts row + its embedding row.
* :func:`upsert_embedding` - insert or replace the embedding row, preserving
  the thought row.
* :func:`mark_embedding_status` - move a row between ``ok``/``pending``/``failed``.
* :func:`get_stats` - aggregates for ``thought_stats``: counts by prefix,
  portability, source, vault, plus oldest/newest timestamps.
* :func:`record_migration_start` / :func:`record_migration_complete` - audit
  trail for migration runs.

All queries are parameterized; no string-concatenated SQL.

Tag filtering uses SQLite's ``json_each`` to avoid the substring false-match
trap that ``LIKE '%"x"%"`` would have (Risk R24).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from engram.models.frontmatter import Portability
from engram.models.mcp import Filter, SortOption

#: Phase 1 vault name for thoughts captured without an explicit vault override.
DEFAULT_VAULT_NAME = "default"


def _serialize_tags(tags: Sequence[str] | None) -> str:
    return json.dumps(list(tags) if tags else [])


def _deserialize_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def _serialize_vector(vector: Sequence[float]) -> str:
    """sqlite-vec accepts a JSON array literal as the vector input."""
    return json.dumps([float(x) for x in vector])


def _normalize_id(thought_id: UUID | str) -> str:
    if isinstance(thought_id, UUID):
        return str(thought_id)
    return thought_id


def _normalize_dt(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def insert_thought(
    conn: sqlite3.Connection,
    *,
    thought_id: UUID | str,
    prefix: str,
    portability: Portability,
    source: str,
    created_at: datetime | str,
    updated_at: datetime | str,
    fingerprint: str,
    file_path: Path | str,
    vault_name: str = DEFAULT_VAULT_NAME,
    tags: Sequence[str] | None = None,
    legacy_id: str | None = None,
    legacy_created_at: datetime | str | None = None,
    schema_version: int = 1,
    embedding: Sequence[float] | None = None,
    embedding_status: Literal["ok", "pending", "failed"] | None = None,
    embedding_error: str | None = None,
    captured_by: str | None = None,
) -> None:
    """Insert a new thoughts row and optionally its embedding, in one transaction.

    When ``embedding`` is provided, ``embedding_status`` defaults to ``"ok"``.
    When ``embedding`` is omitted, the status defaults to ``"pending"`` and the
    embedding row is NOT inserted; the caller can later call
    :func:`upsert_embedding` once the embedding is available.

    Phase 4: ``captured_by`` is the GPG primary fingerprint of the
    capturing user (40 hex; canonical upper-case form). NULL for personal
    captures and Phase 1+2+3 thoughts.
    """
    if embedding is not None:
        resolved_status: Literal["ok", "pending", "failed"] = embedding_status or "ok"
    else:
        resolved_status = embedding_status or "pending"

    with conn:
        conn.execute(
            "INSERT INTO thoughts("
            "id, schema_version, prefix, portability, source, "
            "created_at, updated_at, fingerprint, file_path, vault_name, "
            "tags, legacy_id, legacy_created_at, embedding_status, embedding_error, "
            "captured_by"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _normalize_id(thought_id),
                schema_version,
                prefix,
                portability,
                source,
                _normalize_dt(created_at),
                _normalize_dt(updated_at),
                fingerprint,
                str(file_path),
                vault_name,
                _serialize_tags(tags),
                legacy_id,
                _normalize_dt(legacy_created_at) if legacy_created_at else None,
                resolved_status,
                embedding_error,
                captured_by,
            ),
        )
        if embedding is not None:
            conn.execute(
                "INSERT INTO thought_embeddings(thought_id, embedding) VALUES (?, ?)",
                (_normalize_id(thought_id), _serialize_vector(embedding)),
            )


def get_thought_row(conn: sqlite3.Connection, thought_id: UUID | str) -> dict[str, Any] | None:
    """Fetch a single thoughts row by id; returns a dict, or ``None`` if absent."""
    cursor = conn.execute(
        "SELECT id, schema_version, prefix, portability, source, "
        "created_at, updated_at, fingerprint, file_path, vault_name, "
        "tags, legacy_id, legacy_created_at, embedding_status, embedding_error, "
        "captured_by "
        "FROM thoughts WHERE id = ?",
        (_normalize_id(thought_id),),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": row[0],
        "schema_version": row[1],
        "prefix": row[2],
        "portability": row[3],
        "source": row[4],
        "created_at": row[5],
        "updated_at": row[6],
        "fingerprint": row[7],
        "file_path": row[8],
        "vault_name": row[9],
        "tags": _deserialize_tags(row[10]),
        "legacy_id": row[11],
        "legacy_created_at": row[12],
        "embedding_status": row[13],
        "embedding_error": row[14],
    }
    # Phase 4: row may be 16 cols (with captured_by) or 15 (legacy SELECT).
    if len(row) > 15:
        base["captured_by"] = row[15]
    return base


def _build_where_clause(
    filter_: Filter | None,
    *,
    require_embedding: bool = False,
) -> tuple[str, list[Any]]:
    """Translate a Filter object plus optional embedding-presence clause into SQL.

    Returns the WHERE-suffix string (starts with " WHERE " or empty) and a list
    of bound parameters in positional order.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if filter_ is not None:
        if filter_.prefix is not None:
            values = [filter_.prefix] if isinstance(filter_.prefix, str) else list(filter_.prefix)
            clauses.append(f"prefix IN ({','.join('?' * len(values))})")
            params.extend(values)
        if filter_.portability is not None:
            values = (
                [filter_.portability]
                if isinstance(filter_.portability, str)
                else list(filter_.portability)
            )
            clauses.append(f"portability IN ({','.join('?' * len(values))})")
            params.extend(values)
        if filter_.source is not None:
            values = [filter_.source] if isinstance(filter_.source, str) else list(filter_.source)
            clauses.append(f"source IN ({','.join('?' * len(values))})")
            params.extend(values)
        if filter_.tags:
            # Match if ANY listed tag is present on the thought.
            # Use json_each so we don't fall into the LIKE-substring false-match trap.
            # placeholders is just "?,?,?"; the tag values themselves bind via params.
            placeholders = ",".join("?" * len(filter_.tags))
            tag_clause = (
                f"id IN (SELECT t.id FROM thoughts t, json_each(t.tags) je "  # noqa: S608
                f"WHERE je.value IN ({placeholders}))"
            )
            clauses.append(tag_clause)
            params.extend(filter_.tags)
        if filter_.vault is not None:
            values = [filter_.vault] if isinstance(filter_.vault, str) else list(filter_.vault)
            clauses.append(f"vault_name IN ({','.join('?' * len(values))})")
            params.extend(values)
        if filter_.created_after is not None:
            clauses.append("created_at > ?")
            params.append(_normalize_dt(filter_.created_after))
        if filter_.created_before is not None:
            clauses.append("created_at < ?")
            params.append(_normalize_dt(filter_.created_before))

    if require_embedding:
        clauses.append("embedding_status = 'ok'")

    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _sort_order_clause(sort: SortOption) -> str:
    if sort == "created_at_desc":
        return "ORDER BY created_at DESC, id DESC"
    if sort == "created_at_asc":
        return "ORDER BY created_at ASC, id ASC"
    return "ORDER BY updated_at DESC, id DESC"


def list_thoughts(
    conn: sqlite3.Connection,
    *,
    filter_: Filter | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: SortOption = "created_at_desc",
) -> tuple[list[dict[str, Any]], int]:
    """Filtered, sorted, paginated list. Returns ``(rows, total_count)``.

    ``total_count`` is the true count of filter matches BEFORE pagination,
    NOT ``len(rows)`` (Risk R23). Pending-embedding rows are INCLUDED in
    list output (per spec; they are excluded only from search results).
    """
    where_sql, params = _build_where_clause(filter_, require_embedding=False)

    # where_sql is built from `?` placeholders + literal column names; user values are bound.
    count_cursor = conn.execute(
        f"SELECT COUNT(*) FROM thoughts{where_sql}",  # noqa: S608 - parameterized; see above.
        params,
    )
    total_count = int(count_cursor.fetchone()[0])

    if limit == 0:
        return [], total_count

    sort_sql = _sort_order_clause(sort)  # whitelisted literal from a Literal type.
    _columns = (
        "id, schema_version, prefix, portability, source, created_at, updated_at, "
        "fingerprint, file_path, vault_name, tags, legacy_id, legacy_created_at, "
        "embedding_status, embedding_error, captured_by"
    )
    # Parameterized; where_sql + sort_sql are controlled, user values bind below.
    list_sql = f"SELECT {_columns} FROM thoughts{where_sql} {sort_sql} LIMIT ? OFFSET ?"  # noqa: S608
    cursor = conn.execute(list_sql, [*params, limit, offset])
    rows = [_row_to_dict(row) for row in cursor.fetchall()]
    return rows, total_count


def search_thoughts_by_vector(
    conn: sqlite3.Connection,
    *,
    query_vector: Sequence[float],
    k: int = 10,
    filter_: Filter | None = None,
) -> tuple[list[tuple[dict[str, Any], float]], int]:
    """Run an ANN search over the embedding virtual table with metadata filter.

    Pending-embedding rows are excluded (Risk R2). Returns
    ``(list of (row_dict, similarity), total_found)``. ``total_found`` is the
    number of filter-eligible thoughts that have an ``ok`` embedding (the
    candidate pool size, NOT capped to k).

    The implementation runs an inner KNN search via sqlite-vec to get top-k
    candidates by cosine distance, then joins back to the thoughts table
    applying the metadata filter. The filter is applied AFTER the KNN search,
    so very restrictive filters may return fewer than k results.
    """
    if k <= 0:
        return [], 0

    where_sql, params = _build_where_clause(filter_, require_embedding=True)
    count_sql = f"SELECT COUNT(*) FROM thoughts{where_sql}"  # noqa: S608 - parameterized.
    total_found = int(conn.execute(count_sql, params).fetchone()[0])

    sql = (
        "SELECT t.id, t.schema_version, t.prefix, t.portability, t.source, "
        "t.created_at, t.updated_at, t.fingerprint, t.file_path, t.vault_name, "
        "t.tags, t.legacy_id, t.legacy_created_at, t.embedding_status, t.embedding_error, "
        "t.captured_by, knn.distance "
        "FROM (SELECT thought_id, distance FROM thought_embeddings "
        "      WHERE embedding MATCH ? AND k = ?) knn "
        "JOIN thoughts t ON t.id = knn.thought_id"
    )
    if where_sql:
        # Replace the leading " WHERE " with " AND " since we already have a JOIN clause.
        and_clause = " AND " + where_sql.removeprefix(" WHERE ")
        sql = sql + and_clause
    sql = sql + " ORDER BY knn.distance ASC"

    cursor = conn.execute(sql, [_serialize_vector(query_vector), k, *params])
    rows = cursor.fetchall()
    results: list[tuple[dict[str, Any], float]] = []
    for row in rows:
        thought_dict = _row_to_dict(row[:16])
        # sqlite-vec returns cosine DISTANCE in the default metric (lower is closer).
        # Convert to similarity in [0, 1] via 1 - distance (clamped).
        distance = float(row[16])
        similarity = max(0.0, min(1.0, 1.0 - distance))
        results.append((thought_dict, similarity))
    return results, total_found


def update_thought_metadata(
    conn: sqlite3.Connection,
    thought_id: UUID | str,
    *,
    prefix: str | None = None,
    portability: Portability | None = None,
    source: str | None = None,
    tags: Sequence[str] | None = None,
    vault_name: str | None = None,
    updated_at: datetime | str | None = None,
) -> bool:
    """Patch metadata-only fields on an existing thought. Returns True if updated."""
    sets: list[str] = []
    params: list[Any] = []
    if prefix is not None:
        sets.append("prefix = ?")
        params.append(prefix)
    if portability is not None:
        sets.append("portability = ?")
        params.append(portability)
    if source is not None:
        sets.append("source = ?")
        params.append(source)
    if tags is not None:
        sets.append("tags = ?")
        params.append(_serialize_tags(tags))
    if vault_name is not None:
        sets.append("vault_name = ?")
        params.append(vault_name)
    if updated_at is not None:
        sets.append("updated_at = ?")
        params.append(_normalize_dt(updated_at))

    if not sets:
        return False
    params.append(_normalize_id(thought_id))
    # `sets` is built from a closed list of literal "col = ?" strings.
    update_sql = f"UPDATE thoughts SET {', '.join(sets)} WHERE id = ?"  # noqa: S608
    with conn:
        cursor = conn.execute(update_sql, params)
    return cursor.rowcount > 0


def update_thought_body(
    conn: sqlite3.Connection,
    thought_id: UUID | str,
    *,
    fingerprint: str,
    updated_at: datetime | str,
    embedding: Sequence[float] | None = None,
) -> bool:
    """Body changed: refresh fingerprint, advance updated_at, re-embed.

    When ``embedding`` is None, the row's ``embedding_status`` is set to
    ``pending`` and the embedding row is removed; the next reindex will
    regenerate it.
    """
    sid = _normalize_id(thought_id)
    with conn:
        cursor = conn.execute(
            "UPDATE thoughts SET fingerprint = ?, updated_at = ?, "
            "embedding_status = ?, embedding_error = NULL "
            "WHERE id = ?",
            (
                fingerprint,
                _normalize_dt(updated_at),
                "ok" if embedding is not None else "pending",
                sid,
            ),
        )
        if cursor.rowcount == 0:
            return False
        conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (sid,))
        if embedding is not None:
            conn.execute(
                "INSERT INTO thought_embeddings(thought_id, embedding) VALUES (?, ?)",
                (sid, _serialize_vector(embedding)),
            )
    return True


def delete_thought(conn: sqlite3.Connection, thought_id: UUID | str) -> bool:
    """Remove a thought row + its embedding row. Returns True if deleted."""
    sid = _normalize_id(thought_id)
    with conn:
        conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (sid,))
        cursor = conn.execute("DELETE FROM thoughts WHERE id = ?", (sid,))
    return cursor.rowcount > 0


def upsert_embedding(
    conn: sqlite3.Connection,
    thought_id: UUID | str,
    embedding: Sequence[float],
) -> None:
    """Insert or replace the embedding row; mark the thought row's status as ``ok``."""
    sid = _normalize_id(thought_id)
    with conn:
        conn.execute("DELETE FROM thought_embeddings WHERE thought_id = ?", (sid,))
        conn.execute(
            "INSERT INTO thought_embeddings(thought_id, embedding) VALUES (?, ?)",
            (sid, _serialize_vector(embedding)),
        )
        conn.execute(
            "UPDATE thoughts SET embedding_status = 'ok', embedding_error = NULL WHERE id = ?",
            (sid,),
        )


def mark_embedding_status(
    conn: sqlite3.Connection,
    thought_id: UUID | str,
    status: Literal["ok", "pending", "failed"],
    error_message: str | None = None,
) -> bool:
    """Update the embedding_status (and optional error message) for a thought."""
    sid = _normalize_id(thought_id)
    with conn:
        cursor = conn.execute(
            "UPDATE thoughts SET embedding_status = ?, embedding_error = ? WHERE id = ?",
            (status, error_message, sid),
        )
    return cursor.rowcount > 0


def list_thoughts_with_status(
    conn: sqlite3.Connection,
    status: Literal["ok", "pending", "failed"],
) -> list[dict[str, Any]]:
    """Helper for ``engram doctor --repair``: enumerate rows in a given status."""
    cursor = conn.execute(
        "SELECT id, schema_version, prefix, portability, source, created_at, updated_at, "
        "fingerprint, file_path, vault_name, tags, legacy_id, legacy_created_at, "
        "embedding_status, embedding_error, captured_by FROM thoughts WHERE embedding_status = ?",
        (status,),
    )
    return [_row_to_dict(row) for row in cursor.fetchall()]


def get_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Aggregate counts and timestamps for ``thought_stats``.

    Returns a dict with: ``total_count``, ``by_prefix``, ``by_portability``,
    ``by_source``, ``by_vault``, ``oldest`` (or None if empty), ``newest``,
    ``index_size_bytes`` (sum of WAL + main DB; computed by caller via Path).

    ``total_count`` includes pending-embedding rows so it matches the markdown
    file count on disk; search excludes pending rows separately.
    """
    total = int(conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0])

    by_prefix: dict[str, int] = {
        row[0]: int(row[1])
        for row in conn.execute("SELECT prefix, COUNT(*) FROM thoughts GROUP BY prefix")
    }
    by_portability_raw: dict[str, int] = {
        row[0]: int(row[1])
        for row in conn.execute("SELECT portability, COUNT(*) FROM thoughts GROUP BY portability")
    }
    by_portability = {
        "portable": by_portability_raw.get("portable", 0),
        "sensitive": by_portability_raw.get("sensitive", 0),
        "block": by_portability_raw.get("block", 0),
    }
    by_source: dict[str, int] = {
        row[0]: int(row[1])
        for row in conn.execute("SELECT source, COUNT(*) FROM thoughts GROUP BY source")
    }
    by_vault: dict[str, int] = {
        row[0]: int(row[1])
        for row in conn.execute("SELECT vault_name, COUNT(*) FROM thoughts GROUP BY vault_name")
    }

    oldest_row = conn.execute(
        "SELECT created_at FROM thoughts ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    newest_row = conn.execute(
        "SELECT created_at FROM thoughts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    return {
        "total_count": total,
        "by_prefix": by_prefix,
        "by_portability": by_portability,
        "by_source": by_source,
        "by_vault": by_vault,
        "oldest": oldest_row[0] if oldest_row else None,
        "newest": newest_row[0] if newest_row else None,
    }


def record_migration_start(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_url: str | None,
    started_at: datetime | str,
) -> int:
    """Insert a row in ``migrations`` for a new run; return its rowid."""
    with conn:
        cursor = conn.execute(
            "INSERT INTO migrations(source_type, source_url, started_at) VALUES (?, ?, ?)",
            (source_type, source_url, _normalize_dt(started_at)),
        )
    return int(cursor.lastrowid or 0)


def record_migration_complete(
    conn: sqlite3.Connection,
    migration_rowid: int,
    *,
    completed_at: datetime | str,
    thought_count: int,
    error_count: int,
    report_path: Path | str | None = None,
) -> None:
    """Update an existing ``migrations`` row with completion state."""
    with conn:
        conn.execute(
            "UPDATE migrations SET completed_at = ?, thought_count = ?, error_count = ?, "
            "report_path = ? WHERE id = ?",
            (
                _normalize_dt(completed_at),
                thought_count,
                error_count,
                str(report_path) if report_path is not None else None,
                migration_rowid,
            ),
        )


def iter_all_thought_paths(conn: sqlite3.Connection) -> Iterable[tuple[str, str]]:
    """Yield ``(id, file_path)`` for all thoughts; used by reindex orphan detection."""
    cursor = conn.execute("SELECT id, file_path FROM thoughts")
    yield from cursor.fetchall()


__all__ = [
    "DEFAULT_VAULT_NAME",
    "delete_thought",
    "get_stats",
    "get_thought_row",
    "insert_thought",
    "iter_all_thought_paths",
    "list_thoughts",
    "list_thoughts_with_status",
    "mark_embedding_status",
    "record_migration_complete",
    "record_migration_start",
    "search_thoughts_by_vector",
    "update_thought_body",
    "update_thought_metadata",
    "upsert_embedding",
]
