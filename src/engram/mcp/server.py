"""FastMCP server wiring.

Two factories:

* :func:`build_server` - 5-tool single-vault wiring used by
  single-vault deployments.
* :func:`build_multivault_server` - multi-vault wiring that takes a
  :class:`engram.multivault.registry.VaultRegistry` and adds the
  ``summarize_thought`` and ``synthesize_thoughts`` tools alongside
  the stable five.

Each tool is a thin shim around the handlers in
:mod:`engram.mcp.tools` (storage tools) or :mod:`engram.mcp.llm_tools`
(LLM tools). The ``engram serve`` CLI is responsible for acquiring
per-vault locks, starting sync coordinators, and running the server's
stdio loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastmcp import FastMCP

from engram.config.models import UserConfig
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


def _user_config_view_from(deps: HandlerDeps) -> UserConfig:
    """Build a UserConfig view from EffectiveConfig for the routing dispatcher.

    The dispatcher needs ``auto_route``, ``routing_rules``, and ``vaults``
    fields; pulls them from the EffectiveConfig that ``serve`` populated
    at startup. We reconstruct a minimal UserConfig rather than threading
    a separate UserConfig through the call graph.
    """
    return UserConfig.model_construct(
        default_user=deps.config.default_user,
        vaults=list(deps.config.vaults) if deps.config.vaults else [],
        auto_route=deps.config.auto_route,
        routing_rules=list(deps.config.routing_rules),
    )


def build_server(
    storage: VaultStorage,
    embedder: EmbeddingProvider,
    *,
    default_user: str = "engram-user",
    server_name: str = "engram",
) -> FastMCP[Any]:
    """Wire the 5 engram tools to a FastMCP server and return it.

    Single-vault entry point. Multi-vault callers use
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
    """Multi-vault MCP server with routing dispatcher + capture gate.

    Wires the seven engram tools (``capture_thought``,
    ``search_thoughts``, ``list_thoughts``, ``thought_stats``,
    ``fetch``, ``summarize_thought``, ``synthesize_thoughts``) to a
    FastMCP server backed by a :class:`VaultRegistry`. ``capture_thought``
    consults the per-prefix routing dispatcher when no explicit ``vault:``
    metadata is supplied, then runs the team-vault capture gate
    (read-only refusal + member enrollment + policy refuse-or-pass +
    captured_by stamping) before delegating to the storage layer.

    Backwards compatibility: clients that omit ``meta.vault`` and run
    against a config without ``auto_route`` see single-vault primary
    semantics unchanged.
    """
    mcp: FastMCP[Any] = FastMCP(server_name)

    @mcp.tool
    async def capture_thought(
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Capture a thought, routing to the appropriate vault.

        Routing precedence (see :func:`engram.team.routing.resolve_target_vault`):

        1. ``portability=block`` always lands in primary.
        2. Explicit ``meta.vault`` arg wins.
        3. If ``auto_route=true`` and a routing rule matches the first
           prefix, the rule's target_vault wins.
        4. Otherwise -> primary.

        For team-write targets, the capture gate verifies member
        enrollment + policy refuse-or-pass + stamps ``captured_by``
        before the write.
        """
        from engram.mcp.tools import resolve_capture_metadata
        from engram.models.thought import Thought
        from engram.team.capture_gate import gate_team_capture
        from engram.team.routing import resolve_target_vault

        meta = CaptureInputMetadata.model_validate(metadata) if metadata is not None else None
        payload = CaptureInput(content=content, metadata=meta)

        # Build a transient Thought for the routing dispatcher (it only
        # consults portability + content-prefix; the real Thought lands
        # below via the storage facade).
        resolved = resolve_capture_metadata(payload, default_user=default_user)
        from datetime import UTC, datetime
        from uuid import uuid4

        now = datetime.now(tz=UTC)
        probe = Thought(
            id=uuid4(),
            schema_version=1,
            prefix=resolved["prefix"],
            portability=resolved["portability"],
            source=resolved["source"],
            created_at=now,
            updated_at=now,
            fingerprint="0" * 64,
            tags=[],
            vault="probe",
            content=content,
            file_path=Path("probe.md"),
        )

        # Resolve the target vault.
        target_policy_lookup = deps.team_policies if hasattr(deps, "team_policies") else {}
        decision = resolve_target_vault(
            thought=probe,
            explicit_vault=meta.vault if meta is not None else None,
            user_config=_user_config_view_from(deps),
            registry=registry,
            target_policy_lookup=target_policy_lookup,  # type: ignore[arg-type]
        )

        target_storage = registry.get(decision.target_vault)
        if target_storage is None:
            target_storage = registry.primary()
        target_role = registry.role_of(decision.target_vault) or "primary"

        # Run the capture gate (read-only refusal, member enrollment,
        # policy refuse-or-pass, captured_by stamping).
        team_policy = (
            deps.team_policies.get(decision.target_vault)
            if hasattr(deps, "team_policies")
            else None
        )
        team_members = (
            deps.team_members.get(decision.target_vault) if hasattr(deps, "team_members") else None
        )
        gate_team_capture(
            thought=probe,
            role=target_role,
            members=team_members,  # type: ignore[arg-type]
            policy=team_policy,  # type: ignore[arg-type]
            gpg_identity=deps.gpg_identity,  # type: ignore[arg-type]
        )

        captured_by_value = probe.captured_by if target_role == "team-write" else None

        result: CaptureOutput = await capture_thought_handler(
            target_storage,
            embedder,
            payload=payload,
            default_user=default_user,
            captured_by=captured_by_value,
        )
        out = result.model_dump(mode="json")
        out["vault_name"] = decision.target_vault
        out["routing_reason"] = decision.reason
        return out

    @mcp.tool
    async def search_thoughts(
        query: str,
        k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Semantic search.

        ``filter.vault == "*"`` opts into multi-vault search; any other
        value (or absence) routes to the primary only, matching
        single-vault client semantics.
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

        Defaults to the primary vault; explicit vault filter routes to
        the named vault.
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
        """Aggregate counts; targets the primary vault."""
        result: StatsOutput = await thought_stats_handler(registry.primary())
        return result.model_dump(mode="json")

    @mcp.tool
    async def fetch(id: str) -> dict[str, Any]:
        """Lookup a single thought by id (primary vault).

        Targets the primary vault; cross-vault id lookup is not
        currently supported (the composite key (vault, id) makes
        single-id ambiguous when the same UUID exists in two vaults
        which can only happen via id collision - already refused at
        bundle import).
        """
        payload = FetchInput(id=UUID(id))
        result: FetchOutput = await fetch_handler(registry.primary(), payload=payload)
        return result.model_dump(mode="json")

    @mcp.tool
    async def summarize_thought(id: str) -> dict[str, Any]:
        """LLM-mediated summary of a single thought."""
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
        """LLM-mediated cross-vault synthesis."""
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
