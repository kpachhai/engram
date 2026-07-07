"""Storage facade: composes markdown SoT + SQLite index under the Flow A atomicity contract.

:class:`VaultStorage` is the single entry-point for the MCP tool layer. It
implements the capture flow per ``02-TECHNICAL_DESIGN.md`` Flow A:

1. Markdown write must succeed first. If it fails, the capture errors and
   nothing else happens.
2. Embedding generation is failure-tolerant. The caller may pass an
   embedding (already computed) or omit it; if omitted, the SQLite row is
   inserted with ``embedding_status='pending'`` and the embedding row
   itself is not written. ``engram doctor --repair`` reconciles later.
3. SQLite row + embedding (if present) wrap in a single transaction so the
   index is never half-written.
4. Git commit/push is delegated to the sync coordinator; this facade leaves a
   stub hook (``_post_capture_sync``) that forwards to the coordinator when
   one is attached and is a no-op otherwise.

Open Question Q1 default applied: content larger than 1 MB is rejected
with a :class:`VaultError`; content larger than 100 KB logs a WARNING
but is still accepted (typical thoughts are <2 KB per NFR1).
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from uuid_extensions import uuid7

from engram.errors import ThoughtNotFoundError, VaultError, VaultReadOnlyError
from engram.models import Thought, ThoughtWithSimilarity
from engram.models.frontmatter import (
    DEFAULT_PORTABILITY_BY_PREFIX,
    Portability,
)
from engram.models.mcp import Filter, PortabilityCounts, SortOption, StatsOutput
from engram.storage.markdown import read_thought, write_thought
from engram.storage.sqlite import (
    DEFAULT_EMBEDDING_DIM,
    open_connection,
)
from engram.storage.sqlite_queries import (
    delete_thought as _q_delete_thought,
)
from engram.storage.sqlite_queries import (
    get_stats as _q_get_stats,
)
from engram.storage.sqlite_queries import (
    get_thought_row as _q_get_thought_row,
)
from engram.storage.sqlite_queries import (
    insert_thought as _q_insert_thought,
)
from engram.storage.sqlite_queries import (
    list_thoughts as _q_list_thoughts,
)
from engram.storage.sqlite_queries import (
    list_thoughts_with_status as _q_list_thoughts_with_status,
)
from engram.storage.sqlite_queries import (
    mark_embedding_status as _q_mark_embedding_status,
)
from engram.storage.sqlite_queries import (
    search_thoughts_by_vector as _q_search,
)
from engram.storage.sqlite_queries import (
    update_thought_body as _q_update_body,
)
from engram.storage.sqlite_queries import (
    update_thought_metadata as _q_update_metadata,
)
from engram.storage.sqlite_queries import (
    upsert_embedding as _q_upsert_embedding,
)
from engram.utils.file_naming import derive_relative_path
from engram.utils.fingerprint import compute_fingerprint

_log = logging.getLogger("engram.storage.facade")

#: Soft warning threshold for capture content size (bytes).
_CAPTURE_WARN_BYTES = 100 * 1024
#: Hard reject threshold for capture content size (bytes). Q1 default.
_CAPTURE_REJECT_BYTES = 1 * 1024 * 1024
_PREFIX_RE = re.compile(r"^\s*\[([^\[\]]+)\]")
_DEFAULT_PREFIX_FALLBACK = "Note"


def parse_prefix_from_content(content: str) -> str:
    """Return the first ``[Word]`` from ``content`` or :data:`_DEFAULT_PREFIX_FALLBACK`.

    Multi-word prefixes (``[Action Item]``, ``[Session Summary]``) are returned
    intact. The bracketed value is whitespace-trimmed but not otherwise normalized;
    callers (the storage facade) apply BYOC default-portability rules separately.
    """
    match = _PREFIX_RE.match(content)
    if match is None:
        return _DEFAULT_PREFIX_FALLBACK
    inner = match.group(1).strip()
    if not inner:
        return _DEFAULT_PREFIX_FALLBACK
    return inner


def _new_uuid7() -> UUID:
    """Mint a fresh UUID-v7 (timestamp prefix + random tail)."""
    # uuid_extensions.uuid7 is typed as Any (no stubs); cast through UUID for mypy strict.
    return UUID(str(uuid7()))


class VaultStorage:
    """Single entry-point for vault read/write operations.

    Wraps a SQLite connection (with sqlite-vec loaded) and the markdown SoT
    layer. Caller is responsible for closing via :meth:`close` (or using as
    a context manager).
    """

    def __init__(
        self,
        *,
        thoughts_dir: Path,
        index_db_path: Path,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        embedding_model_name: str | None = None,
        vault_name: str = "default",
        read_only_role: bool = False,
    ) -> None:
        """Open the SQLite + sqlite-vec connection and ensure the thoughts dir exists.

        ``read_only_role`` is the hard-refusal guard for read-only vaults:
        when True, every public write entry-point (``capture``,
        ``update_metadata``, ``update_body``, ``delete``,
        ``repair_pending_embeddings``) raises
        :class:`engram.errors.VaultReadOnlyError` rather than mutating the
        vault. The attribute is normally set by
        :class:`engram.multivault.registry.VaultRegistry` at mount time
        based on the ``role:`` field; tests may pass it directly.
        """
        self.thoughts_dir = Path(thoughts_dir).resolve()
        self.index_db_path = Path(index_db_path)
        self.vault_name = vault_name
        self.embedding_dim = embedding_dim
        self.read_only_role = read_only_role
        self.thoughts_dir.mkdir(parents=True, exist_ok=True)
        self.conn: sqlite3.Connection = open_connection(
            self.index_db_path,
            embedding_dim=embedding_dim,
            embedding_model_name=embedding_model_name,
        )
        # ``engram serve`` injects the SyncCoordinator via
        # :meth:`set_sync_coordinator` so unit tests stay hermetic. When
        # this attribute is None, ``_post_capture_sync`` is a no-op.
        self._sync_coordinator: object | None = None
        # Branch-drift monitor: snapshot the current branch HEAD at mount
        # time so a side-channel ``git checkout`` between mount and read
        # surfaces as a doctor row rather than silently shifting the
        # vault's view of disk. Best-effort; non-git-repo dirs return None.
        self._mounted_branch_at_init: str | None = self._read_current_branch()

    def _refuse_if_read_only(self, action: str) -> None:
        """Hard-refusal gate for read-only-role vaults."""
        if self.read_only_role:
            msg = (
                f"vault {self.vault_name!r} is mounted with role=read-only; "
                f"refusing {action} (read-only vaults expose read-path only)"
            )
            raise VaultReadOnlyError(msg)

    def set_read_only_role(self, *, read_only: bool) -> None:
        """Update the read-only flag after construction.

        Mostly used by :class:`VaultRegistry.mount` so a single ``VaultStorage``
        can be re-roled across mount/unmount cycles in long-running tests.
        """
        self.read_only_role = read_only

    def _read_current_branch(self) -> str | None:
        """Read the current branch HEAD via ``git symbolic-ref``.

        Returns ``None`` if the thoughts dir is not under a git repo or
        if the branch cannot be resolved (e.g. detached HEAD, missing git
        binary). The storage layer cannot prevent a side-channel
        ``git checkout``; this method is the read-path snapshot used by
        the branch-drift doctor probe.
        """
        try:
            import subprocess

            result = subprocess.run(  # noqa: S603
                ["git", "-C", str(self.thoughts_dir), "symbolic-ref", "--short", "HEAD"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
            if result.returncode != 0:
                return None
            branch = result.stdout.strip()
            return branch or None
        except (OSError, ValueError):
            return None

    def current_branch_drifted(self) -> tuple[bool, str | None, str | None]:
        """Check whether the branch HEAD has changed since mount time.

        Returns ``(drifted, mounted_at, current)`` so the doctor probe
        can surface both values for the operator. ``drifted`` is False
        when the storage was not mounted under a git repo (no comparison
        is possible).
        """
        if self._mounted_branch_at_init is None:
            return False, None, None
        current = self._read_current_branch()
        if current is None:
            return False, self._mounted_branch_at_init, None
        return current != self._mounted_branch_at_init, self._mounted_branch_at_init, current

    def __enter__(self) -> VaultStorage:
        """Return self; storage opened in __init__."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the SQLite connection on context exit."""
        del exc_info
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        # Flush all WAL frames to the main database file before closing so
        # the next session doesn't open a stale WAL that can cause
        # disk I/O errors (SQLITE_IOERR) in WAL-mode shared memory.
        with contextlib.suppress(sqlite3.Error):
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            self.conn.close()
        except sqlite3.Error:
            _log.exception("error closing SQLite connection at %s", self.index_db_path)

    # === capture ===

    def capture(
        self,
        *,
        content: str,
        prefix: str | None = None,
        portability: Portability | None = None,
        source: str = "engram-user",
        tags: Sequence[str] | None = None,
        vault: str | None = None,
        embedding: Sequence[float] | None = None,
        legacy_id: str | None = None,
        legacy_created_at: datetime | None = None,
        thought_id: UUID | None = None,
        created_at: datetime | None = None,
        captured_by: str | None = None,
        on_index_failure: Callable[[Thought, sqlite3.Error], None] | None = None,
        extra_frontmatter: dict[str, Any] | None = None,
    ) -> Thought:
        """Capture a thought: write markdown SoT, then insert SQLite row.

        Per Flow A: markdown write must succeed first. If embedding is provided,
        it lands in the same SQLite transaction as the row. If omitted, the row
        is marked ``embedding_status='pending'`` for later repair.

        ``extra_frontmatter`` emits additional frontmatter fields on the
        markdown file (e.g. ``consolidated_from`` provenance on a merged
        thought). Markdown-only: the SQLite row does not carry them.

        ``on_index_failure`` is an optional callback invoked when the SQLite
        insert raises (``sqlite3.Error``). The capture STILL succeeds: the
        markdown remains the source of truth and ``engram reindex`` recovers
        the row. Callers that need a real-time signal of degraded index state
        (notably the MCP ``capture_thought`` handler) can register a
        callback. Default ``None`` preserves the historical log-and-continue
        behavior. If the callback itself raises, the error is logged and
        swallowed - it must not mask the original capture outcome.

        Raises:
            VaultError: if content exceeds 1 MB (Q1 default) or markdown write fails.
            VaultReadOnlyError: if this storage is mounted with
                ``read_only_role=True`` (hard refusal on read-only vaults).
        """
        self._refuse_if_read_only("capture")
        size_bytes = len(content.encode("utf-8"))
        if size_bytes > _CAPTURE_REJECT_BYTES:
            msg = (
                f"capture content too large: {size_bytes} bytes exceeds the "
                f"{_CAPTURE_REJECT_BYTES}-byte hard limit"
            )
            raise VaultError(msg)
        if size_bytes > _CAPTURE_WARN_BYTES:
            _log.warning(
                "capture content is %d bytes, above the %d-byte soft threshold",
                size_bytes,
                _CAPTURE_WARN_BYTES,
            )

        resolved_prefix = prefix if prefix is not None else parse_prefix_from_content(content)
        resolved_portability: Portability = (
            portability
            if portability is not None
            else (DEFAULT_PORTABILITY_BY_PREFIX.get(resolved_prefix, "portable"))  # type: ignore[assignment]
        )
        now = created_at or datetime.now(UTC)
        tid = thought_id or _new_uuid7()

        fingerprint = compute_fingerprint(content)
        rel_path = derive_relative_path(
            prefix=resolved_prefix,
            body=content,
            created_at=now,
            thought_id=tid,
        )
        absolute_path = (self.thoughts_dir / rel_path).resolve()

        thought = Thought.model_validate(
            {
                "id": tid,
                "schema_version": 1,
                "prefix": resolved_prefix,
                "portability": resolved_portability,
                "source": source,
                "created_at": now,
                "updated_at": now,
                "fingerprint": fingerprint,
                "tags": list(tags) if tags else [],
                "vault": vault or self.vault_name,
                "legacy_id": legacy_id,
                "captured_by": captured_by,
                "content": content,
                "file_path": absolute_path,
            }
        )

        # Step 1: markdown write must succeed before anything else.
        # legacy_created_at is not carried on the Thought model, so it is
        # emitted as a frontmatter extra - the markdown SoT must hold
        # everything a full reindex needs to rebuild the row.
        extras = dict(extra_frontmatter) if extra_frontmatter else {}
        if legacy_created_at is not None and "legacy_created_at" not in extras:
            extras["legacy_created_at"] = legacy_created_at.isoformat()
        write_thought(thought, base_dir=self.thoughts_dir, extra_fields=extras or None)

        # Step 2 + 3: insert SQLite row (with embedding if provided).
        try:
            _q_insert_thought(
                self.conn,
                thought_id=thought.id,
                prefix=thought.prefix,
                portability=thought.portability,
                source=thought.source,
                created_at=thought.created_at,
                updated_at=thought.updated_at,
                fingerprint=thought.fingerprint,
                file_path=str(absolute_path.relative_to(self.thoughts_dir)),
                vault_name=thought.vault,
                tags=thought.tags,
                legacy_id=thought.legacy_id,
                legacy_created_at=legacy_created_at,
                schema_version=thought.schema_version,
                embedding=embedding,
                captured_by=thought.captured_by,
            )
        except sqlite3.Error as exc:
            # Markdown is on disk (SoT); SQLite is out of sync. Doctor will reconcile.
            # Per Flow A step 3 commentary: log and continue; capture still succeeds.
            _log.exception(
                "SQLite insert failed for capture %s; markdown SoT preserved at %s; "
                "run `engram doctor --repair` to reconcile",
                thought.id,
                absolute_path,
            )
            if on_index_failure is not None:
                try:
                    on_index_failure(thought, exc)
                except Exception:
                    # The callback's failure must not mask the original outcome.
                    _log.exception(
                        "on_index_failure callback raised for capture %s",
                        thought.id,
                    )

        # Step 4: git commit/push hook (forwards to sync coordinator if attached).
        self._post_capture_sync(thought)
        return thought

    def set_sync_coordinator(self, coordinator: object | None) -> None:
        """Attach (or detach) the sync coordinator.

        Called once by :func:`engram.cli.serve` after the coordinator is
        constructed. Tests typically leave this unset so capture is fully
        hermetic.
        """
        self._sync_coordinator = coordinator

    def _post_capture_sync(self, thought: Thought) -> None:
        """Forward ``thought.file_path`` to the sync coordinator, if attached."""
        coordinator = self._sync_coordinator
        if coordinator is None:
            return
        try:
            coordinator.enqueue(thought.file_path)  # type: ignore[attr-defined]
        except Exception:
            _log.exception(
                "sync coordinator enqueue failed for %s; capture remains on disk",
                thought.file_path,
            )

    # === read ===

    def get_by_id(self, thought_id: UUID | str) -> Thought | None:
        """Return the thought, or None if absent."""
        row = _q_get_thought_row(self.conn, thought_id)
        if row is None:
            return None
        return self._row_to_thought(row)

    def list_thoughts(
        self,
        *,
        filter_: Filter | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: SortOption = "created_at_desc",
    ) -> tuple[list[Thought], int]:
        """List thoughts with filter + pagination + true total_count."""
        rows, total = _q_list_thoughts(
            self.conn,
            filter_=filter_,
            limit=limit,
            offset=offset,
            sort=sort,
        )
        return [self._row_to_thought(row) for row in rows], total

    def search(
        self,
        *,
        query_embedding: Sequence[float],
        k: int = 10,
        filter_: Filter | None = None,
    ) -> tuple[list[ThoughtWithSimilarity], int]:
        """ANN search; pending-embedding rows are excluded."""
        rows, total_found = _q_search(
            self.conn,
            query_vector=query_embedding,
            k=k,
            filter_=filter_,
        )
        results: list[ThoughtWithSimilarity] = []
        for row, similarity in rows:
            base = self._row_to_thought(row)
            results.append(
                ThoughtWithSimilarity(
                    **base.model_dump(),
                    similarity=similarity,
                )
            )
        return results, total_found

    # === update ===

    def update_metadata(
        self,
        thought_id: UUID | str,
        *,
        prefix: str | None = None,
        portability: Portability | None = None,
        source: str | None = None,
        tags: Sequence[str] | None = None,
        vault: str | None = None,
        updated_at: datetime | None = None,
    ) -> bool:
        """Patch metadata-only fields. Returns True if updated.

        Raises:
            VaultReadOnlyError: if mounted with ``read_only_role=True``.
        """
        self._refuse_if_read_only("update_metadata")
        if not _q_update_metadata(
            self.conn,
            thought_id,
            prefix=prefix,
            portability=portability,
            source=source,
            tags=tags,
            vault_name=vault,
            updated_at=updated_at,
        ):
            return False
        thought = self.get_by_id(thought_id)
        if thought is not None:
            write_thought(thought, base_dir=self.thoughts_dir)
        return True

    def update_body(
        self,
        thought_id: UUID | str,
        *,
        new_content: str,
        embedding: Sequence[float] | None = None,
    ) -> bool:
        """Body changed: refresh fingerprint, advance updated_at, re-embed.

        Raises:
            VaultReadOnlyError: if mounted with ``read_only_role=True``.
        """
        self._refuse_if_read_only("update_body")
        existing = self.get_by_id(thought_id)
        if existing is None:
            return False
        new_fp = compute_fingerprint(new_content)
        new_ts = datetime.now(UTC)
        if not _q_update_body(
            self.conn,
            thought_id,
            fingerprint=new_fp,
            updated_at=new_ts,
            embedding=embedding,
        ):
            return False
        updated = existing.model_copy(
            update={"content": new_content, "fingerprint": new_fp, "updated_at": new_ts}
        )
        write_thought(updated, base_dir=self.thoughts_dir)
        return True

    def delete(self, thought_id: UUID | str, *, source: str = "api") -> Thought:
        """Remove a thought from both markdown SoT and SQLite.

        Returns the deleted :class:`Thought` so callers (CLI, MCP handler)
        can emit a confirmation that includes prefix + portability without
        a separate lookup.

        Args:
            thought_id: UUID of the thought to delete.
            source: Audit-log tag identifying the call site (``mcp``,
                ``cli``, or default ``api`` for direct programmatic use).

        Raises:
            VaultReadOnlyError: if mounted with ``read_only_role=True``.
            ThoughtNotFoundError: if no thought with this id exists.
        """
        self._refuse_if_read_only("delete")
        existing = self.get_by_id(thought_id)
        if existing is None:
            msg = f"no thought with id={thought_id!r}"
            raise ThoughtNotFoundError(msg)
        # SQLite first: if this fails, the markdown is still on disk and
        # the row is still in the index - clean transaction failure, no
        # half-deleted state.
        if not _q_delete_thought(self.conn, thought_id):
            msg = f"sqlite delete failed for id={thought_id!r}"
            raise ThoughtNotFoundError(msg)
        try:
            existing.file_path.unlink(missing_ok=True)
        except OSError:
            _log.exception(
                "SQLite row deleted but markdown unlink failed for %s; manual cleanup required",
                existing.file_path,
            )
        _log.info(
            "thought_deleted id=%s prefix=%s portability=%s fingerprint=%s vault=%s source=%s",
            existing.id,
            existing.prefix,
            existing.portability,
            existing.fingerprint,
            existing.vault,
            source,
        )
        self._post_capture_sync(existing)
        return existing

    # === stats + doctor support ===

    def stats(self) -> StatsOutput:
        """Aggregate counts + timestamps for ``thought_stats`` MCP tool."""
        raw = _q_get_stats(self.conn)
        oldest = self._coerce_dt(raw["oldest"])
        newest = self._coerce_dt(raw["newest"])
        index_size = self.index_db_path.stat().st_size if self.index_db_path.exists() else 0
        return StatsOutput(
            total_count=raw["total_count"],
            by_prefix=raw["by_prefix"],
            by_portability=PortabilityCounts(**raw["by_portability"]),
            by_source=raw["by_source"],
            by_vault=raw["by_vault"],
            oldest=oldest,
            newest=newest,
            index_size_bytes=index_size,
            vault_paths=[str(self.thoughts_dir.parent)],
        )

    def repair_pending_embeddings(
        self,
        embed_fn: Callable[[str], Sequence[float]],
    ) -> int:
        """Walk pending-embedding rows, regenerate via ``embed_fn``, mark ok.

        Returns the count of rows successfully repaired. Failures are logged
        and the row remains pending for a later doctor run.

        Raises:
            VaultReadOnlyError: if mounted with ``read_only_role=True``.
                Doctor catches this and reports a "skipped N pending
                embeddings on read-only vault X" INFO row.
        """
        self._refuse_if_read_only("repair_pending_embeddings")
        pending = _q_list_thoughts_with_status(self.conn, "pending")
        repaired = 0
        for row in pending:
            thought = self._row_to_thought(row)
            try:
                vector = embed_fn(thought.content)
                _q_upsert_embedding(self.conn, thought.id, vector)
                repaired += 1
            except Exception as exc:
                _log.warning(
                    "embedding repair failed for %s: %s",
                    thought.id,
                    exc,
                )
                _q_mark_embedding_status(self.conn, thought.id, "failed", str(exc))
        return repaired

    # === helpers ===

    def _row_to_thought(self, row: dict[str, Any]) -> Thought:
        rel_path = Path(row["file_path"])
        abs_path = (self.thoughts_dir / rel_path).resolve()
        body = ""
        if abs_path.exists():
            read_result = read_thought(abs_path)
            if read_result is not None:
                read_thought_obj, _ = read_result
                if read_thought_obj is not None:
                    body = read_thought_obj.content
        return Thought.model_validate(
            {
                "id": UUID(row["id"]) if isinstance(row["id"], str) else row["id"],
                "schema_version": row["schema_version"],
                "prefix": row["prefix"],
                "portability": row["portability"],
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "fingerprint": row["fingerprint"],
                "tags": row["tags"],
                "vault": row["vault_name"],
                "legacy_id": row["legacy_id"],
                "captured_by": row.get("captured_by"),
                "content": body,
                "file_path": abs_path,
            }
        )

    @staticmethod
    def _coerce_dt(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None


__all__ = [
    "VaultStorage",
    "parse_prefix_from_content",
]
