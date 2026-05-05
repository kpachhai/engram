"""Pure async tool handlers for the 5 MCP tools.

These handlers take an explicit :class:`VaultStorage` and
:class:`EmbeddingProvider` so unit tests can call them directly without
spinning up the FastMCP server. The wiring layer in :mod:`engram.mcp.server`
binds them to FastMCP's tool registration.

Per ``02-TECHNICAL_DESIGN.md`` MCP API Contract, the 5 tools are:

* ``capture_thought`` - write a new thought; embedding may fail (pending status).
* ``search_thoughts`` - sqlite-vec ANN search with metadata filter; pending rows excluded.
* ``list_thoughts`` - filtered + sorted + paginated list including pending rows.
* ``thought_stats`` - aggregates for the entire vault.
* ``fetch`` - lookup by id; returns null thought (NOT an error) when absent.
"""

from __future__ import annotations

import logging
from typing import TypedDict
from uuid import UUID

from engram.embedding.protocol import EmbeddingProvider
from engram.errors import EmbeddingError
from engram.models.frontmatter import Portability
from engram.models.mcp import (
    CaptureInput,
    CaptureOutput,
    FetchInput,
    FetchOutput,
    ListInput,
    ListOutput,
    SearchInput,
    SearchOutput,
    StatsOutput,
)
from engram.storage.facade import VaultStorage


class ResolvedCaptureMetadata(TypedDict):
    """Resolved prefix + portability + source for a capture probe."""

    prefix: str
    portability: Portability
    source: str


_log = logging.getLogger("engram.mcp.tools")


def _relative_file_path(storage: VaultStorage, absolute: object) -> str:
    """Return a vault-relative string path for ``absolute`` (a Path or str)."""
    from pathlib import Path

    p = Path(str(absolute))
    try:
        return str(p.relative_to(storage.thoughts_dir))
    except ValueError:
        return str(p)


def resolve_capture_metadata(
    payload: CaptureInput,
    *,
    default_user: str,
) -> ResolvedCaptureMetadata:
    """Resolve prefix + portability + source for a capture without writing.

    Used by the routing dispatcher (which needs a transient
    :class:`engram.models.thought.Thought` probe to consult portability +
    first-prefix BEFORE the actual write happens). Mirrors the resolution
    logic baked into :meth:`engram.storage.facade.VaultStorage.capture`.
    """
    from engram.models.frontmatter import DEFAULT_PORTABILITY_BY_PREFIX
    from engram.storage.facade import parse_prefix_from_content

    metadata = payload.metadata
    explicit_prefix = metadata.prefix if metadata else None
    resolved_prefix = (
        explicit_prefix
        if explicit_prefix is not None
        else parse_prefix_from_content(payload.content)
    )
    explicit_portability = metadata.portability if metadata else None
    resolved_portability: Portability = (
        explicit_portability
        if explicit_portability is not None
        else DEFAULT_PORTABILITY_BY_PREFIX.get(resolved_prefix, "portable")  # type: ignore[assignment]
    )
    resolved_source = metadata.source if metadata and metadata.source else default_user
    return ResolvedCaptureMetadata(
        prefix=resolved_prefix,
        portability=resolved_portability,
        source=resolved_source,
    )


async def capture_thought_handler(
    storage: VaultStorage,
    embedder: EmbeddingProvider,
    *,
    payload: CaptureInput,
    default_user: str = "engram-user",
    captured_by: str | None = None,
) -> CaptureOutput:
    """Handle the ``capture_thought`` MCP tool.

    Embedding failure is non-fatal: if the embedder raises, the thought is
    still captured with ``embedding_status='pending'`` and the next
    ``engram doctor --repair`` regenerates the vector.

    When the target is a team-write vault, ``captured_by`` carries the
    operator's GPG primary fingerprint (40 hex; canonical upper-case)
    set by the team-vault capture gate before this handler runs.
    """
    embedding = None
    try:
        embedding = await embedder.aembed(payload.content)
    except (EmbeddingError, Exception) as exc:
        _log.warning("capture_thought: embedding failed; capturing as pending. error=%s", exc)

    metadata = payload.metadata
    thought = storage.capture(
        content=payload.content,
        prefix=metadata.prefix if metadata else None,
        portability=metadata.portability if metadata else None,
        source=(metadata.source if metadata and metadata.source else default_user),
        tags=metadata.tags if metadata else None,
        embedding=embedding,
        captured_by=captured_by,
    )
    return CaptureOutput(
        id=thought.id,
        file_path=_relative_file_path(storage, thought.file_path),
        fingerprint=thought.fingerprint,
    )


async def search_thoughts_handler(
    storage: VaultStorage,
    embedder: EmbeddingProvider,
    *,
    payload: SearchInput,
) -> SearchOutput:
    """Handle the ``search_thoughts`` MCP tool.

    ``embedding_status='pending'`` rows are excluded from results (Risk R2).
    Returns the cosine-similarity-ranked top-k plus a true ``total_found``
    over the filter-eligible candidate pool.
    """
    query_vector = await embedder.aembed(payload.query)
    results, total_found = storage.search(
        query_embedding=query_vector,
        k=payload.k,
        filter_=payload.filter,
    )
    return SearchOutput(results=results, total_found=total_found)


async def list_thoughts_handler(
    storage: VaultStorage,
    *,
    payload: ListInput,
) -> ListOutput:
    """Handle the ``list_thoughts`` MCP tool."""
    rows, total = storage.list_thoughts(
        filter_=payload.filter,
        limit=payload.limit,
        offset=payload.offset,
        sort=payload.sort,
    )
    return ListOutput(results=rows, total_count=total)


async def thought_stats_handler(storage: VaultStorage) -> StatsOutput:
    """Handle the ``thought_stats`` MCP tool."""
    return storage.stats()


async def fetch_handler(
    storage: VaultStorage,
    *,
    payload: FetchInput,
) -> FetchOutput:
    """Handle the ``fetch`` MCP tool. Returns ``thought=None`` for unknown ids (NOT an error)."""
    thought_id: UUID = payload.id
    thought = storage.get_by_id(thought_id)
    return FetchOutput(thought=thought)


__all__ = [
    "capture_thought_handler",
    "fetch_handler",
    "list_thoughts_handler",
    "search_thoughts_handler",
    "thought_stats_handler",
]
