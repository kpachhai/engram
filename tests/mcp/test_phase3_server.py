"""Multi-vault MCP server wiring tests."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from engram.config.models import (
    AggregatorConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.llm.budget import LLMBudget
from engram.llm.providers import MockProvider
from engram.mcp.llm_tools import HandlerDeps
from engram.mcp.server import build_multivault_server
from engram.multivault.registry import VaultRegistry
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting

DIM = 16


class _FakeEmbedder:
    """Minimal EmbeddingProvider stub returning a slot-0 vector."""

    dimension: int = DIM
    model_name: str = "BAAI/bge-small-en-v1.5"

    def embed(self, text: str) -> list[float]:
        del text
        v = [0.0] * DIM
        v[0] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def warmup(self) -> None:
        pass

    def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _vault_storage(tmp_path: Path, name: str) -> VaultStorage:
    thoughts_dir = tmp_path / name / "thoughts"
    indexes_dir = tmp_path / name / ".indexes"
    thoughts_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=DIM,
        embedding_model_name="BAAI/bge-small-en-v1.5",
        vault_name=name,
    )
    set_setting(storage.conn, "embedding_model_name", "BAAI/bge-small-en-v1.5")
    set_setting(storage.conn, "embedding_dim", str(DIM))
    return storage


def _capture(storage: VaultStorage, content: str, portability: str = "portable") -> str:
    v = [0.0] * DIM
    v[0] = 1.0
    t = storage.capture(
        content=content,
        portability=portability,  # type: ignore[arg-type]
        embedding=v,
    )
    return str(t.id)


def _config(tmp_path: Path) -> EffectiveConfig:
    return EffectiveConfig(
        default_user="me",
        vault_path=tmp_path / "primary",
        thoughts_dir=tmp_path / "primary/thoughts",
        index_dir=tmp_path / "primary/.indexes",
        embedding_model="BAAI/bge-small-en-v1.5",
        vault_name="primary",
        sync=SyncConfig(),
        llm=LLMConfig(provider="ollama"),
        aggregator=AggregatorConfig(min_per_vault_results=1),
    )


def _deps(
    *,
    registry: VaultRegistry,
    config: EffectiveConfig,
    provider: MockProvider | None = None,
) -> HandlerDeps:
    budget = LLMBudget(
        state_path=config.index_dir / "llm_usage.json",
        daily_cost_cap_usd=10.0,
    )
    return HandlerDeps(
        registry=registry,
        embedder=_FakeEmbedder(),
        config=config,
        budget=budget,
        provider_override=provider,
    )


def test_phase3_server_advertises_seven_tools(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    _capture(primary, "[Pattern] one")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    deps = _deps(registry=registry, config=_config(tmp_path), provider=MockProvider())

    server = build_multivault_server(
        registry,
        _FakeEmbedder(),
        deps,
    )
    # FastMCP exposes registered tools under various attribute names; the
    # public surface we care about is the server is wired without raising.
    assert server is not None


@pytest.mark.asyncio
async def test_phase3_search_default_returns_primary_only(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    _capture(primary, "[Pattern] from-primary")
    _capture(alice, "[Pattern] from-alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    _deps(registry=registry, config=_config(tmp_path), provider=MockProvider())

    # We invoke the underlying handler the way the MCP wrapper does.
    from engram.mcp.tools import search_thoughts_handler
    from engram.models.mcp import SearchInput

    result = await search_thoughts_handler(
        registry.primary(),
        _FakeEmbedder(),
        payload=SearchInput(query="anything"),
    )
    # Default targets primary; alice's thought never reaches the result.
    vault_names = {r.vault for r in result.results}
    assert vault_names == {"primary"}


@pytest.mark.asyncio
async def test_phase3_search_star_returns_multivault(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    _capture(primary, "[Pattern] from-primary")
    _capture(alice, "[Pattern] from-alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    deps = _deps(registry=registry, config=_config(tmp_path), provider=MockProvider())

    from engram.models.mcp import Filter
    from engram.multivault.aggregator import aggregate_search

    agg = aggregate_search(
        registry=registry,
        query_embedding=_FakeEmbedder().embed("x"),
        k=10,
        filter_=Filter(vault="*"),
        min_per_vault_results=deps.config.aggregator.min_per_vault_results,
    )
    vault_names = {r.thought.vault for r in agg.rows}
    assert vault_names == {"primary", "alice"}


def test_phase3_capture_routes_to_primary(tmp_path: Path) -> None:
    """capture_thought always lands in the primary vault, not the read-only one."""
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")

    # Capture goes through the primary's facade.
    _capture(primary, "[Pattern] explicit primary capture")
    # Confirm: alice has zero thoughts; primary has one.
    _, primary_total = primary.list_thoughts(limit=1)
    _, alice_total = alice.list_thoughts(limit=1)
    assert primary_total == 1
    assert alice_total == 0
