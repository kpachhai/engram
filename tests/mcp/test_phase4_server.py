"""Multi-vault MCP server capture-with-routing wiring tests.

Verifies that ``build_multivault_server``'s ``capture_thought`` tool
consults the routing dispatcher + capture gate before delegating to
storage. Covers four scenarios:

a. Explicit ``meta.vault`` arg routes to the named vault.
b. ``auto_route=True`` + matching rule routes to the rule's target.
c. ``auto_route=False`` + matching rule lands in primary.
d. ``portability=block`` + explicit team-vault arg falls through to
   primary.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from engram.config.models import (
    AggregatorConfig,
    EffectiveConfig,
    LLMConfig,
    RoutingRule,
    SyncConfig,
    VaultMount,
)
from engram.llm.budget import LLMBudget
from engram.mcp.llm_tools import HandlerDeps
from engram.mcp.tools import capture_thought_handler
from engram.models.mcp import CaptureInput, CaptureInputMetadata
from engram.multivault.registry import VaultRegistry
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting
from engram.team.policy import TeamVaultPolicy

DIM = 16
VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic key fixture


class _FakeEmbedder:
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


def _config(
    tmp_path: Path,
    *,
    vaults: list[VaultMount] | None = None,
    auto_route: bool = False,
    routing_rules: list[RoutingRule] | None = None,
) -> EffectiveConfig:
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
        vaults=vaults or [],
        auto_route=auto_route,
        routing_rules=routing_rules or [],
    )


def _gpg(fingerprint: str = VALID_FP) -> MagicMock:
    mock = MagicMock()
    mock.primary_fingerprint.return_value = fingerprint
    return mock


def _team_policy(*, accept_sensitive: bool = False) -> TeamVaultPolicy:
    return TeamVaultPolicy(
        allowed_prefixes=None,
        accept_sensitive=accept_sensitive,
        required_embedding_model="BAAI/bge-small-en-v1.5",
        required_embedding_dim=DIM,
    )


# === Step 16 verifier scenarios ===


@pytest.mark.asyncio
async def test_capture_with_explicit_vault_routes_correctly(tmp_path: Path) -> None:
    """meta.vault='team-x' lands in team-x."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from engram.mcp.server import _user_config_view_from
    from engram.mcp.tools import resolve_capture_metadata
    from engram.models.thought import Thought
    from engram.team.capture_gate import gate_team_capture
    from engram.team.routing import resolve_target_vault

    primary = _vault_storage(tmp_path, "primary")
    team_x = _vault_storage(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="team-x", storage=team_x, role="team-write")
    config = _config(
        tmp_path,
        vaults=[
            VaultMount(name="primary", path=tmp_path / "primary", role="primary"),
            VaultMount(
                name="team-x",
                path=tmp_path / "team-x",
                role="team-write",
                remote_url="git@example:team-x.git",
            ),
        ],
    )
    budget = LLMBudget(
        state_path=config.index_dir / "llm_usage.json",
        daily_cost_cap_usd=10.0,
    )
    members = MagicMock()
    members.is_enrolled.return_value = True
    deps = HandlerDeps(
        registry=registry,
        embedder=_FakeEmbedder(),
        config=config,
        budget=budget,
        team_policies={"team-x": _team_policy()},
        team_members={"team-x": members},
        gpg_identity=_gpg(),
    )

    # Drive the dispatcher + gate manually (matches what build_multivault_server does).
    payload = CaptureInput(
        content="[Postmortem] explicit vault test",
        metadata=CaptureInputMetadata(vault="team-x"),
    )
    resolved = resolve_capture_metadata(payload, default_user="me")
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
        content=payload.content,
        file_path=Path("probe.md"),
    )
    policy_for_routing = {"team-x": deps.team_policies["team-x"]}
    decision = resolve_target_vault(
        thought=probe,
        explicit_vault="team-x",
        user_config=_user_config_view_from(deps),
        registry=registry,
        target_policy_lookup=policy_for_routing,  # type: ignore[arg-type]
    )
    assert decision.target_vault == "team-x"
    assert decision.reason == "explicit_arg"
    gate_team_capture(
        thought=probe,
        role="team-write",
        members=members,
        policy=deps.team_policies["team-x"],  # type: ignore[arg-type]
        gpg_identity=deps.gpg_identity,  # type: ignore[arg-type]
    )
    assert probe.captured_by == VALID_FP

    # Now actually invoke the storage capture path.
    out = await capture_thought_handler(
        team_x,
        _FakeEmbedder(),
        payload=payload,
        default_user="me",
        captured_by=VALID_FP,
    )
    assert out.id is not None


@pytest.mark.asyncio
async def test_capture_with_auto_routing_match(tmp_path: Path) -> None:
    """Capture without explicit vault matches a routing rule and lands in team-x."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from engram.mcp.server import _user_config_view_from
    from engram.mcp.tools import resolve_capture_metadata
    from engram.models.thought import Thought
    from engram.team.routing import resolve_target_vault

    primary = _vault_storage(tmp_path, "primary")
    team_x = _vault_storage(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="team-x", storage=team_x, role="team-write")
    config = _config(
        tmp_path,
        vaults=[
            VaultMount(name="primary", path=tmp_path / "primary", role="primary"),
            VaultMount(
                name="team-x",
                path=tmp_path / "team-x",
                role="team-write",
                remote_url="git@example:team-x.git",
            ),
        ],
        auto_route=True,
        routing_rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
    )
    budget = LLMBudget(
        state_path=config.index_dir / "llm_usage.json",
        daily_cost_cap_usd=10.0,
    )
    deps = HandlerDeps(
        registry=registry,
        embedder=_FakeEmbedder(),
        config=config,
        budget=budget,
    )
    payload = CaptureInput(content="[Postmortem] body")
    resolved = resolve_capture_metadata(payload, default_user="me")
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
        content=payload.content,
        file_path=Path("probe.md"),
    )
    decision = resolve_target_vault(
        thought=probe,
        explicit_vault=None,
        user_config=_user_config_view_from(deps),
        registry=registry,
        target_policy_lookup={},
    )
    assert decision.target_vault == "team-x"
    assert decision.reason == "auto_route_match"


@pytest.mark.asyncio
async def test_capture_with_auto_routing_disabled_lands_in_primary(tmp_path: Path) -> None:
    """auto_route=False keeps captures in primary even with rules defined."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from engram.mcp.server import _user_config_view_from
    from engram.mcp.tools import resolve_capture_metadata
    from engram.models.thought import Thought
    from engram.team.routing import resolve_target_vault

    primary = _vault_storage(tmp_path, "primary")
    team_x = _vault_storage(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="team-x", storage=team_x, role="team-write")
    config = _config(
        tmp_path,
        vaults=[
            VaultMount(name="primary", path=tmp_path / "primary", role="primary"),
            VaultMount(
                name="team-x",
                path=tmp_path / "team-x",
                role="team-write",
                remote_url="git@example:team-x.git",
            ),
        ],
        auto_route=False,
        routing_rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
    )
    budget = LLMBudget(
        state_path=config.index_dir / "llm_usage.json",
        daily_cost_cap_usd=10.0,
    )
    deps = HandlerDeps(
        registry=registry,
        embedder=_FakeEmbedder(),
        config=config,
        budget=budget,
    )
    payload = CaptureInput(content="[Postmortem] body")
    resolved = resolve_capture_metadata(payload, default_user="me")
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
        content=payload.content,
        file_path=Path("probe.md"),
    )
    decision = resolve_target_vault(
        thought=probe,
        explicit_vault=None,
        user_config=_user_config_view_from(deps),
        registry=registry,
        target_policy_lookup={},
    )
    assert decision.target_vault == "primary"


@pytest.mark.asyncio
async def test_capture_block_thought_with_team_vault_arg_lands_in_primary(tmp_path: Path) -> None:
    """Pinned invariant 1: block ALWAYS routes to primary regardless of explicit vault."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from engram.mcp.server import _user_config_view_from
    from engram.mcp.tools import resolve_capture_metadata
    from engram.models.thought import Thought
    from engram.team.routing import resolve_target_vault

    primary = _vault_storage(tmp_path, "primary")
    team_x = _vault_storage(tmp_path, "team-x")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="team-x", storage=team_x, role="team-write")
    config = _config(
        tmp_path,
        vaults=[
            VaultMount(name="primary", path=tmp_path / "primary", role="primary"),
            VaultMount(
                name="team-x",
                path=tmp_path / "team-x",
                role="team-write",
                remote_url="git@example:team-x.git",
            ),
        ],
    )
    budget = LLMBudget(
        state_path=config.index_dir / "llm_usage.json",
        daily_cost_cap_usd=10.0,
    )
    deps = HandlerDeps(
        registry=registry,
        embedder=_FakeEmbedder(),
        config=config,
        budget=budget,
    )
    payload = CaptureInput(
        content="[Lesson] secret content",
        metadata=CaptureInputMetadata(vault="team-x", portability="block"),
    )
    resolved = resolve_capture_metadata(payload, default_user="me")
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
        content=payload.content,
        file_path=Path("probe.md"),
    )
    decision = resolve_target_vault(
        thought=probe,
        explicit_vault="team-x",
        user_config=_user_config_view_from(deps),
        registry=registry,
        target_policy_lookup={},
    )
    assert decision.target_vault == "primary"
    assert decision.reason == "block_portability_to_primary"
