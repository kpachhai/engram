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
from uuid import UUID

from engram.embedding.protocol import EmbeddingProvider
from engram.errors import EmbeddingError
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

_log = logging.getLogger("engram.mcp.tools")


def _relative_file_path(storage: VaultStorage, absolute: object) -> str:
    """Return a vault-relative string path for ``absolute`` (a Path or str)."""
    from pathlib import Path

    p = Path(str(absolute))
    try:
        return str(p.relative_to(storage.thoughts_dir))
    except ValueError:
        return str(p)


async def capture_thought_handler(
    storage: VaultStorage,
    embedder: EmbeddingProvider,
    *,
    payload: CaptureInput,
    default_user: str = "engram-user",
) -> CaptureOutput:
    """Handle the ``capture_thought`` MCP tool.

    Embedding failure is non-fatal: if the embedder raises, the thought is
    still captured with ``embedding_status='pending'`` and the next
    ``engram doctor --repair`` regenerates the vector.
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
