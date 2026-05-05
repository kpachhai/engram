"""FastMCP server wiring.

:func:`build_server` returns a configured :class:`fastmcp.FastMCP` instance with
the 5 engram tools registered. Each tool is a thin shim around the handlers
in :mod:`engram.mcp.tools`. The CLI ``engram serve`` is responsible for
acquiring the per-vault lock and running the server's stdio loop.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from engram.embedding.protocol import EmbeddingProvider
from engram.mcp.tools import (
    capture_thought_handler,
    fetch_handler,
    list_thoughts_handler,
    search_thoughts_handler,
    thought_stats_handler,
)
from engram.models.mcp import (
    CaptureInput,
    CaptureInputMetadata,
    CaptureOutput,
    FetchInput,
    FetchOutput,
    Filter,
    ListInput,
    ListOutput,
    SearchInput,
    SearchOutput,
    SortOption,
    StatsOutput,
)
from engram.storage.facade import VaultStorage


def build_server(
    storage: VaultStorage,
    embedder: EmbeddingProvider,
    *,
    default_user: str = "engram-user",
    server_name: str = "engram",
) -> FastMCP[Any]:
    """Wire the 5 engram tools to a FastMCP server and return it.

    Args:
        storage: The vault storage facade (already connected to SQLite).
        embedder: Embedding provider (lazy-loaded; first call may take 2-3s).
        default_user: Source identifier to apply when capture_thought metadata
            does not specify one.
        server_name: MCP server name surfaced in client discovery.

    Returns:
        A :class:`FastMCP` instance ready to ``run()`` over stdio.
    """
    mcp: FastMCP[Any] = FastMCP(server_name)

    @mcp.tool
    async def capture_thought(
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a new thought to the vault and return its identity."""
        meta = CaptureInputMetadata.model_validate(metadata) if metadata is not None else None
        payload = CaptureInput(content=content, metadata=meta)
        result: CaptureOutput = await capture_thought_handler(
            storage, embedder, payload=payload, default_user=default_user
        )
        return result.model_dump(mode="json")

    @mcp.tool
    async def search_thoughts(
        query: str,
        k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Semantic search over the vault. Returns top-k by cosine similarity."""
        f = Filter.model_validate(filter) if filter is not None else None
        payload = SearchInput(query=query, k=k, filter=f)
        result: SearchOutput = await search_thoughts_handler(storage, embedder, payload=payload)
        return result.model_dump(mode="json")

    @mcp.tool
    async def list_thoughts(
        limit: int = 50,
        offset: int = 0,
        filter: dict[str, Any] | None = None,
        sort: SortOption = "created_at_desc",
    ) -> dict[str, Any]:
        """Filtered + sorted + paginated list of thoughts."""
        f = Filter.model_validate(filter) if filter is not None else None
        payload = ListInput(limit=limit, offset=offset, filter=f, sort=sort)
        result: ListOutput = await list_thoughts_handler(storage, payload=payload)
        return result.model_dump(mode="json")

    @mcp.tool
    async def thought_stats() -> dict[str, Any]:
        """Aggregate counts and timestamps for the entire vault."""
        result: StatsOutput = await thought_stats_handler(storage)
        return result.model_dump(mode="json")

    @mcp.tool
    async def fetch(id: str) -> dict[str, Any]:
        """Lookup a single thought by id. Returns ``thought: null`` if absent."""
        from uuid import UUID

        payload = FetchInput(id=UUID(id))
        result: FetchOutput = await fetch_handler(storage, payload=payload)
        return result.model_dump(mode="json")

    return mcp


__all__ = ["build_server"]
