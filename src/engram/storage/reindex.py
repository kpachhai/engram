"""Reindex flows per ``02-TECHNICAL_DESIGN.md`` Flow D.

Four modes:

* **incremental** (default): walk markdown directory, compare to SQLite. For
  each on-disk thought, insert if missing, re-embed if body fingerprint drifted,
  update SQLite metadata if frontmatter changed. Orphan SQLite rows are
  surfaced but NOT deleted.

* **full**: drop the SQLite index entirely and rebuild from markdown. The
  SoT contract guarantees this is non-destructive (markdown remains intact).

* **repair**: regenerate embeddings for rows in ``embedding_status='pending'``.
  Targeted variant of incremental; doesn't re-walk the whole directory.

* **remove_orphans**: take a snapshot of "now"; delete SQLite rows whose
  markdown file no longer exists AND whose ``updated_at`` is older than the
  snapshot (Risk R11: don't delete a just-captured row that landed on disk
  between the walk and the orphan scan).

Each mode produces a :class:`ReindexReport` that ``engram doctor`` and
``engram reindex`` can display.
"""

from __future__ import annotations

import enum
import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from engram.errors import IndexError as EngramIndexError
from engram.errors import VaultReadOnlyError
from engram.models import Thought
from engram.storage.facade import VaultStorage
from engram.storage.markdown import FrontmatterDrift, read_thought, split_frontmatter
from engram.storage.sqlite_queries import (
    delete_thought,
    insert_thought,
    iter_all_thought_paths,
    list_thoughts_with_status,
    mark_embedding_status,
    update_thought_body,
    update_thought_metadata,
    upsert_embedding,
)

_log = logging.getLogger("engram.storage.reindex")


class ReindexMode(enum.StrEnum):
    """Reindex execution mode."""

    INCREMENTAL = "incremental"
    FULL = "full"
    REPAIR = "repair"
    REMOVE_ORPHANS = "remove_orphans"


@dataclass(slots=True)
class ReindexReport:
    """Summary of a reindex run."""

    mode: ReindexMode
    walked: int = 0
    inserted: int = 0
    body_reindexed: int = 0
    metadata_reindexed: int = 0
    embeddings_repaired: int = 0
    embedding_failures: int = 0
    orphans_detected: int = 0
    orphans_removed: int = 0
    drift_observations: list[FrontmatterDrift] = field(default_factory=list)
    duration_seconds: float = 0.0


def reindex_vault(
    storage: VaultStorage,
    *,
    mode: ReindexMode = ReindexMode.INCREMENTAL,
    embed_fn: Callable[[str], Sequence[float]] | None = None,
    remove_orphans: bool = False,
) -> ReindexReport:
    """Run a reindex pass against ``storage``.

    Args:
        storage: An open :class:`VaultStorage`.
        mode: One of :class:`ReindexMode` values.
        embed_fn: Function mapping content -> embedding vector. Required for
            INCREMENTAL (when new files need embedding), FULL, and REPAIR.
            Optional for REMOVE_ORPHANS.
        remove_orphans: When ``mode == INCREMENTAL`` and this is True, also
            remove SQLite rows whose markdown file is missing (subject to
            the snapshot-timestamp guard for race safety).

    Returns:
        :class:`ReindexReport` summarizing the work done.
    """
    if storage.read_only_role:
        msg = (
            f"vault {storage.vault_name!r} is mounted with role=read-only; "
            f"refusing reindex (mode={mode.value}). Read-only vaults are "
            "regenerable from the originating bundle; re-import to recover."
        )
        raise VaultReadOnlyError(msg)

    started = datetime.now(UTC)
    report = ReindexReport(mode=mode)

    if mode is ReindexMode.FULL:
        if embed_fn is None:
            msg = "reindex --full requires an embed_fn to regenerate embeddings"
            raise EngramIndexError(msg)
        _full_reindex(storage, embed_fn=embed_fn, report=report)
    elif mode is ReindexMode.REPAIR:
        if embed_fn is None:
            msg = "reindex --repair requires an embed_fn to regenerate pending embeddings"
            raise EngramIndexError(msg)
        _repair_pending(storage, embed_fn=embed_fn, report=report)
    elif mode is ReindexMode.REMOVE_ORPHANS:
        _remove_orphans(storage, report=report, snapshot=started)
    else:  # INCREMENTAL
        _incremental_reindex(
            storage,
            embed_fn=embed_fn,
            report=report,
            snapshot=started,
            remove_orphans=remove_orphans,
        )

    report.duration_seconds = (datetime.now(UTC) - started).total_seconds()
    return report


# === implementations ===


def _walk_markdown_files(thoughts_dir: Path) -> list[Path]:
    """Return absolute paths of all .md files under ``thoughts_dir``."""
    return sorted(p.resolve() for p in thoughts_dir.rglob("*.md") if p.is_file())


def _legacy_created_at_from_file(md_path: Path) -> datetime | str | None:
    """Read the optional ``legacy_created_at`` frontmatter field.

    The :class:`Thought` model does not carry this field (it is migration
    metadata consumed by consolidate staleness), so the reindex path reads
    it straight from the file's frontmatter.
    """
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        data = YAML(typ="safe", pure=True).load(io.StringIO(split[0]))
    except YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("legacy_created_at")
    return value if isinstance(value, datetime | str) else None


def _insert_index_row(
    storage: VaultStorage,
    md_path: Path,
    thought: Thought,
    *,
    embedding: Sequence[float] | None,
    legacy_created_at: datetime | str | None,
) -> None:
    """Insert the SQLite row for an on-disk thought without touching markdown.

    Reindex is index-only: the row is keyed on the file's actual on-disk
    path and every frontmatter field is carried verbatim. Routing through
    ``storage.capture()`` here would rewrite the markdown SoT, reset
    ``updated_at``, drop ``captured_by``, and re-derive the path (spawning
    a duplicate file when the slug drifted after a body edit).
    """
    insert_thought(
        storage.conn,
        thought_id=thought.id,
        prefix=thought.prefix,
        portability=thought.portability,
        source=thought.source,
        created_at=thought.created_at,
        updated_at=thought.updated_at,
        fingerprint=thought.fingerprint,
        file_path=str(md_path.relative_to(storage.thoughts_dir)),
        vault_name=thought.vault,
        tags=thought.tags,
        legacy_id=thought.legacy_id,
        legacy_created_at=legacy_created_at,
        schema_version=thought.schema_version,
        embedding=embedding,
        captured_by=thought.captured_by,
    )


def _incremental_reindex(
    storage: VaultStorage,
    *,
    embed_fn: Callable[[str], Sequence[float]] | None,
    report: ReindexReport,
    snapshot: datetime,
    remove_orphans: bool,
) -> None:
    """Walk markdown dir; reconcile SQLite to disk."""
    on_disk = _walk_markdown_files(storage.thoughts_dir)
    seen_ids: set[str] = set()

    for md_path in on_disk:
        report.walked += 1
        result = read_thought(md_path)
        if result is None:
            continue
        thought, drifts = result
        report.drift_observations.extend(drifts)
        if thought is None:
            continue

        seen_ids.add(str(thought.id))
        existing = storage.get_by_id(thought.id)
        if existing is None:
            embedding = embed_fn(thought.content) if embed_fn is not None else None
            try:
                _insert_index_row(
                    storage,
                    md_path,
                    thought,
                    embedding=embedding,
                    legacy_created_at=_legacy_created_at_from_file(md_path),
                )
                report.inserted += 1
            except Exception:
                _log.exception(
                    "incremental reindex insert failed for %s; will retry next run", md_path
                )
                report.embedding_failures += 1
            continue

        # Body drift: fingerprint differs -> re-embed + bump updated_at.
        if existing.fingerprint != thought.fingerprint:
            new_vec = embed_fn(thought.content) if embed_fn is not None else None
            update_thought_body(
                storage.conn,
                thought.id,
                fingerprint=thought.fingerprint,
                updated_at=thought.updated_at,
                embedding=new_vec,
            )
            report.body_reindexed += 1
            continue

        # Metadata drift: any tracked frontmatter field differs from SQLite row.
        if (
            existing.prefix != thought.prefix
            or existing.portability != thought.portability
            or existing.source != thought.source
            or set(existing.tags) != set(thought.tags)
            or existing.vault != thought.vault
        ):
            update_thought_metadata(
                storage.conn,
                thought.id,
                prefix=thought.prefix,
                portability=thought.portability,
                source=thought.source,
                tags=thought.tags,
                vault_name=thought.vault,
                updated_at=thought.updated_at,
            )
            report.metadata_reindexed += 1

    # Orphan detection: SQLite rows referencing files no longer on disk.
    for thought_id, file_path_rel in iter_all_thought_paths(storage.conn):
        if str(thought_id) in seen_ids:
            continue
        abs_path = (storage.thoughts_dir / Path(file_path_rel)).resolve()
        if abs_path.exists():
            continue
        report.orphans_detected += 1
        if remove_orphans:
            row = storage.get_by_id(thought_id)
            if row is None:
                continue
            # R11: only remove rows older than the snapshot to avoid racing
            # a concurrent capture that landed during the walk.
            if row.updated_at < snapshot and delete_thought(storage.conn, thought_id):
                report.orphans_removed += 1


def _full_reindex(
    storage: VaultStorage,
    *,
    embed_fn: Callable[[str], Sequence[float]],
    report: ReindexReport,
) -> None:
    """Drop SQLite content, walk markdown, re-insert everything from scratch."""
    # Files written before engram emitted legacy_created_at to frontmatter
    # carry it only in the index; snapshot those values before the wipe so
    # the rebuild does not lose consolidate's staleness anchors.
    legacy_by_id: dict[str, str] = {
        str(row[0]): row[1]
        for row in storage.conn.execute(
            "SELECT id, legacy_created_at FROM thoughts WHERE legacy_created_at IS NOT NULL"
        )
    }
    storage.conn.execute("DELETE FROM thought_embeddings")
    storage.conn.execute("DELETE FROM thoughts")

    on_disk = _walk_markdown_files(storage.thoughts_dir)
    for md_path in on_disk:
        report.walked += 1
        result = read_thought(md_path)
        if result is None:
            continue
        thought, drifts = result
        report.drift_observations.extend(drifts)
        if thought is None:
            continue
        try:
            embedding = embed_fn(thought.content)
        except Exception:
            _log.exception("embedding failed during --full reindex for %s", md_path)
            embedding = None
            report.embedding_failures += 1

        try:
            _insert_index_row(
                storage,
                md_path,
                thought,
                embedding=embedding,
                legacy_created_at=(
                    _legacy_created_at_from_file(md_path) or legacy_by_id.get(str(thought.id))
                ),
            )
            report.inserted += 1
        except Exception:
            _log.exception("--full reindex insert failed for %s", md_path)
            report.embedding_failures += 1


def _repair_pending(
    storage: VaultStorage,
    *,
    embed_fn: Callable[[str], Sequence[float]],
    report: ReindexReport,
) -> None:
    """Regenerate embeddings for rows in ``embedding_status='pending'``."""
    pending_rows = list_thoughts_with_status(storage.conn, "pending")
    for row in pending_rows:
        thought_id = row["id"]
        thought = storage.get_by_id(thought_id)
        if thought is None:
            continue
        try:
            vec = embed_fn(thought.content)
            upsert_embedding(storage.conn, thought_id, vec)
            report.embeddings_repaired += 1
        except Exception as exc:
            mark_embedding_status(storage.conn, thought_id, "failed", str(exc))
            report.embedding_failures += 1
            _log.warning("embedding repair failed for %s: %s", thought_id, exc)


def _remove_orphans(
    storage: VaultStorage,
    *,
    report: ReindexReport,
    snapshot: datetime,
) -> None:
    """Delete SQLite rows whose markdown file is missing (snapshot-guarded)."""
    for thought_id, file_path_rel in iter_all_thought_paths(storage.conn):
        abs_path = (storage.thoughts_dir / Path(file_path_rel)).resolve()
        if abs_path.exists():
            continue
        report.orphans_detected += 1
        row = storage.get_by_id(thought_id)
        if row is None:
            continue
        if row.updated_at < snapshot and delete_thought(storage.conn, thought_id):
            report.orphans_removed += 1


__all__ = [
    "ReindexMode",
    "ReindexReport",
    "reindex_vault",
]
