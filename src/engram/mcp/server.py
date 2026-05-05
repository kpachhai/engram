"""FastMCP server wiring (Phase 1 single-vault + Phase 3 multi-vault).

Two factories:

* :func:`build_server` - the original 5-tool single-vault wiring kept
  for backward compatibility. Phase 1 + 2 callers continue to use this.
* :func:`build_multivault_server` - Phase 3 wiring that takes a
  :class:`engram.multivault.registry.VaultRegistry` and adds the
  ``summarize_thought`` and ``synthesize_thoughts`` tools alongside
  the stable five (R-L8 - additive change; ``listChanged`` notifies
  clients of the new tool surface).

Each tool is a thin shim around the handlers in
:mod:`engram.mcp.tools` (Phase 1+2) or
:mod:`engram.mcp.llm_tools` (Phase 3 LLM tools). The ``engram serve``
CLI is responsible for acquiring per-vault locks, starting sync
coordinators, and running the server's stdio loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastmcp import FastMCP

from engram.mcp.llm_tools import (
    HandlerDeps,
    SummarizeInput,
    SynthesizeInput,
    summarize_thought_handler,
    synthesize_thoughts_handler,
)
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

if TYPE_CHECKING:
    from engram.embedding.protocol import EmbeddingProvider
    from engram.multivault.registry import VaultRegistry
    from engram.storage.facade import VaultStorage


def build_server(
    storage: VaultStorage,
    embedder: EmbeddingProvider,
    *,
    default_user: str = "engram-user",
    server_name: str = "engram",
) -> FastMCP[Any]:
    """Wire the 5 engram tools to a FastMCP server and return it.

    Phase 1 single-vault entry point. Kept for backwards compatibility
    with existing tests + Phase 1/2 callers; Phase 3 callers use
    :func:`build_multivault_server`.
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
        payload = FetchInput(id=UUID(id))
        result: FetchOutput = await fetch_handler(storage, payload=payload)
        return result.model_dump(mode="json")

    return mcp


def build_multivault_server(
    registry: VaultRegistry,
    embedder: EmbeddingProvider,
    deps: HandlerDeps,
    *,
    default_user: str = "engram-user",
    server_name: str = "engram",
) -> FastMCP[Any]:
    """Phase 3 wiring with the registry routing + LLM-mediated tools.

    Phase 1 + 2 client semantics: ``capture_thought`` always targets
    the primary; ``search_thoughts`` defaults to the primary unless
    ``filter.vault == "*"``. Per the plan R-L2, this preserves
    backwards compatibility with existing clients.

    Phase 3 additions: ``summarize_thought`` (wraps
    :func:`engram.mcp.llm_tools.summarize_thought_handler`) and
    ``synthesize_thoughts`` (wraps
    :func:`engram.mcp.llm_tools.synthesize_thoughts_handler`).
    """
    mcp: FastMCP[Any] = FastMCP(server_name)

    @mcp.tool
    async def capture_thought(
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a new thought to the primary vault.

        Phase 3: capture is always routed to the primary vault per
        R-L2; explicit vault filters that target a read-only vault
        are refused at the storage layer with VaultReadOnlyError.
        """
        primary = registry.primary()
        meta = CaptureInputMetadata.model_validate(metadata) if metadata is not None else None
        payload = CaptureInput(content=content, metadata=meta)
        result: CaptureOutput = await capture_thought_handler(
            primary, embedder, payload=payload, default_user=default_user
        )
        return result.model_dump(mode="json")

    @mcp.tool
    async def search_thoughts(
        query: str,
        k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Semantic search.

        Phase 3: ``filter.vault == "*"`` opts into multi-vault search;
        any other value (or absence) routes to the primary only,
        matching Phase 1+2 client semantics (R-L2).
        """
        f = Filter.model_validate(filter) if filter is not None else None
        wants_multivault = (f is not None and f.vault == "*") or (
            f is not None and isinstance(f.vault, list) and len(f.vault) > 1
        )
        if not wants_multivault:
            payload = SearchInput(query=query, k=k, filter=f)
            result: SearchOutput = await search_thoughts_handler(
                registry.primary(), embedder, payload=payload
            )
            return result.model_dump(mode="json")
        # Cross-vault: use the aggregator via synthesize-style assembly
        # but return raw rows (no LLM call). Reuses synthesize machinery
        # for consistent portability + floor handling.
        from engram.multivault.aggregator import aggregate_search

        query_emb = embedder.embed(query)
        agg = aggregate_search(
            registry=registry,
            query_embedding=query_emb,
            k=k,
            filter_=f,
            include_sensitive=False,
            min_per_vault_results=deps.config.aggregator.min_per_vault_results,
            aggregate_timeout_seconds=deps.config.aggregator.aggregate_timeout_seconds,
            force_sequential=deps.config.aggregator.force_sequential,
        )
        result_rows = [r.thought for r in agg.rows]
        return {
            "results": [t.model_dump(mode="json") for t in result_rows],
            "total_found": agg.total_found,
            "degraded_vaults": list(agg.degraded_vaults),
            "mode_used": agg.mode_used.value,
        }

    @mcp.tool
    async def list_thoughts(
        limit: int = 50,
        offset: int = 0,
        filter: dict[str, Any] | None = None,
        sort: SortOption = "created_at_desc",
    ) -> dict[str, Any]:
        """Filtered + sorted + paginated list.

        Phase 3: defaults to the primary vault; explicit vault filter
        routes to the named vault.
        """
        f = Filter.model_validate(filter) if filter is not None else None
        target_storage = registry.primary()
        if f is not None and isinstance(f.vault, str) and f.vault != "*":
            named = registry.get(f.vault)
            if named is not None:
                target_storage = named
        payload = ListInput(limit=limit, offset=offset, filter=f, sort=sort)
        result: ListOutput = await list_thoughts_handler(target_storage, payload=payload)
        return result.model_dump(mode="json")

    @mcp.tool
    async def thought_stats() -> dict[str, Any]:
        """Aggregate counts; primary vault for now (Phase 4 will roll up)."""
        result: StatsOutput = await thought_stats_handler(registry.primary())
        return result.model_dump(mode="json")

    @mcp.tool
    async def fetch(id: str) -> dict[str, Any]:
        """Lookup a single thought by id (primary vault).

        Phase 3 keeps this targeting primary; cross-vault id lookup
        is deferred to Phase 4 (the composite key (vault, id) makes
        single-id ambiguous when the same UUID exists in two vaults
        which can only happen via id collision - already refused at
        bundle import).
        """
        payload = FetchInput(id=UUID(id))
        result: FetchOutput = await fetch_handler(registry.primary(), payload=payload)
        return result.model_dump(mode="json")

    @mcp.tool
    async def summarize_thought(id: str) -> dict[str, Any]:
        """LLM-mediated summary of a single thought (Phase 3)."""
        payload = SummarizeInput(id=UUID(id))
        result = await summarize_thought_handler(deps, payload=payload)
        return result.model_dump(mode="json")

    @mcp.tool
    async def synthesize_thoughts(
        query: str,
        k: int = 10,
        filter: dict[str, Any] | None = None,
        include_sensitive: bool = False,
        include_friend_vaults: bool = False,
    ) -> dict[str, Any]:
        """LLM-mediated cross-vault synthesis (Phase 3)."""
        f = Filter.model_validate(filter) if filter is not None else None
        payload = SynthesizeInput(
            query=query,
            k=k,
            filter=f,
            include_sensitive=include_sensitive,
            include_friend_vaults=include_friend_vaults,
        )
        result = await synthesize_thoughts_handler(deps, payload=payload)
        return result.model_dump(mode="json")

    return mcp


__all__ = ["build_multivault_server", "build_server"]
