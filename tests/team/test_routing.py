"""Tests for engram.team.routing.resolve_target_vault.

Step 8 verifier: covers each precedence rule (5 paths) + ambiguous +
unmounted refusals + multi-prefix first-wins tie-break + sensitive-
without-accept fall-through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from engram.config.models import RoutingRule, UserConfig, VaultMount
from engram.errors import (
    RoutingRuleAmbiguous,
    RoutingTargetNotMounted,
    TeamPolicyViolation,
)
from engram.models.thought import Thought
from engram.team.policy import TeamVaultPolicy
from engram.team.routing import RoutingDecision, resolve_target_vault


def _thought(
    *,
    portability: str = "portable",
    content: str = "[Postmortem] body",
    prefix: str = "Postmortem",
) -> Thought:
    now = datetime.now(tz=UTC)
    return Thought(
        id=uuid4(),
        schema_version=1,
        prefix=prefix,
        portability=portability,  # type: ignore[arg-type]
        source="engram-test",
        created_at=now,
        updated_at=now,
        fingerprint="0" * 64,
        tags=[],
        vault="default",
        content=content,
        file_path=Path("test.md"),
    )


def _registry(*, primary_name: str = "personal", mounted: list[str] | None = None) -> MagicMock:
    """Build a MagicMock VaultRegistry with the given mounted vaults."""
    registry = MagicMock()
    registry.primary_name.return_value = primary_name
    members = mounted or [primary_name]
    registry.__contains__.side_effect = lambda name: name in members
    return registry


def _config(
    *,
    auto_route: bool = False,
    rules: list[RoutingRule] | None = None,
) -> UserConfig:
    return UserConfig(
        vaults=[VaultMount(name="personal", path=Path("/tmp/p"), role="primary")],
        auto_route=auto_route,
        routing_rules=rules or [],
    )


def _policy(*, accept_sensitive: bool = False) -> TeamVaultPolicy:
    return TeamVaultPolicy(
        accept_sensitive=accept_sensitive,
        required_embedding_model="m",
        required_embedding_dim=1,
    )


# === Rule 1: block portability -> primary ===


def test_block_portability_routes_to_primary() -> None:
    """Pinned invariant 1."""
    decision = resolve_target_vault(
        thought=_thought(portability="block"),
        explicit_vault="team-x",  # ignored by Rule 1
        user_config=_config(),
        registry=_registry(mounted=["personal", "team-x"]),
    )
    assert decision == RoutingDecision(
        target_vault="personal", reason="block_portability_to_primary"
    )


# === Rule 3: explicit arg wins ===


def test_explicit_vault_arg_routes_to_target() -> None:
    """Pinned invariant 2."""
    decision = resolve_target_vault(
        thought=_thought(),
        explicit_vault="team-x",
        user_config=_config(auto_route=True),
        registry=_registry(mounted=["personal", "team-x"]),
    )
    assert decision.target_vault == "team-x"
    assert decision.reason == "explicit_arg"


def test_explicit_vault_to_unmounted_refuses() -> None:
    with pytest.raises(RoutingTargetNotMounted):
        resolve_target_vault(
            thought=_thought(),
            explicit_vault="team-y",
            user_config=_config(),
            registry=_registry(mounted=["personal", "team-x"]),
        )


def test_explicit_vault_overrides_routing_rule() -> None:
    """Even if a routing rule would fire, explicit always wins."""
    decision = resolve_target_vault(
        thought=_thought(),
        explicit_vault="personal",
        user_config=_config(
            auto_route=True,
            rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
        ),
        registry=_registry(mounted=["personal", "team-x"]),
    )
    assert decision.target_vault == "personal"
    assert decision.reason == "explicit_arg"


def test_explicit_vault_sensitive_to_non_accepting_refuses() -> None:
    """Operator surprise: explicit + sensitive against non-accepting policy."""
    with pytest.raises(TeamPolicyViolation, match="sensitive_thought_target_does_not_accept"):
        resolve_target_vault(
            thought=_thought(portability="sensitive"),
            explicit_vault="team-x",
            user_config=_config(),
            registry=_registry(mounted=["personal", "team-x"]),
            target_policy_lookup={"team-x": _policy(accept_sensitive=False)},
        )


def test_explicit_vault_sensitive_to_accepting_passes() -> None:
    decision = resolve_target_vault(
        thought=_thought(portability="sensitive"),
        explicit_vault="team-x",
        user_config=_config(),
        registry=_registry(mounted=["personal", "team-x"]),
        target_policy_lookup={"team-x": _policy(accept_sensitive=True)},
    )
    assert decision.target_vault == "team-x"


# === Rule 4: auto-route + matching rule ===


def test_auto_route_match_routes_to_target() -> None:
    decision = resolve_target_vault(
        thought=_thought(),
        explicit_vault=None,
        user_config=_config(
            auto_route=True,
            rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
        ),
        registry=_registry(mounted=["personal", "team-x"]),
    )
    assert decision.target_vault == "team-x"
    assert decision.reason == "auto_route_match"


def test_auto_route_disabled_falls_back_to_primary() -> None:
    decision = resolve_target_vault(
        thought=_thought(),
        explicit_vault=None,
        user_config=_config(
            auto_route=False,
            rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
        ),
        registry=_registry(mounted=["personal", "team-x"]),
    )
    assert decision.target_vault == "personal"
    assert decision.reason == "fallback_primary"


def test_auto_route_no_matching_rule_falls_back() -> None:
    decision = resolve_target_vault(
        thought=_thought(content="[Friction] body", prefix="Friction"),
        explicit_vault=None,
        user_config=_config(
            auto_route=True,
            rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
        ),
        registry=_registry(mounted=["personal", "team-x"]),
    )
    assert decision.target_vault == "personal"
    assert decision.reason == "fallback_primary"


def test_auto_route_unmounted_target_refuses() -> None:
    with pytest.raises(RoutingTargetNotMounted):
        resolve_target_vault(
            thought=_thought(),
            explicit_vault=None,
            user_config=_config(
                auto_route=True,
                rules=[RoutingRule(prefix="Postmortem", target_vault="team-y")],
            ),
            registry=_registry(mounted=["personal", "team-x"]),
        )


def test_auto_route_sensitive_to_non_accepting_falls_through() -> None:
    """Pinned invariant 1 / Rule 2: silent fall-through for routing-rule path."""
    decision = resolve_target_vault(
        thought=_thought(portability="sensitive"),
        explicit_vault=None,
        user_config=_config(
            auto_route=True,
            rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
        ),
        registry=_registry(mounted=["personal", "team-x"]),
        target_policy_lookup={"team-x": _policy(accept_sensitive=False)},
    )
    assert decision.target_vault == "personal"
    assert decision.reason == "sensitive_target_does_not_accept_to_primary"


def test_auto_route_sensitive_to_accepting_passes() -> None:
    decision = resolve_target_vault(
        thought=_thought(portability="sensitive"),
        explicit_vault=None,
        user_config=_config(
            auto_route=True,
            rules=[RoutingRule(prefix="Postmortem", target_vault="team-x")],
        ),
        registry=_registry(mounted=["personal", "team-x"]),
        target_policy_lookup={"team-x": _policy(accept_sensitive=True)},
    )
    assert decision.target_vault == "team-x"


# === Multi-prefix tie-break ===


def test_multi_prefix_first_wins() -> None:
    """[Postmortem][Decision] -> only Postmortem participates."""
    decision = resolve_target_vault(
        thought=_thought(content="[Postmortem][Decision] body", prefix="Postmortem"),
        explicit_vault=None,
        user_config=_config(
            auto_route=True,
            rules=[
                RoutingRule(prefix="Decision", target_vault="team-y"),
                RoutingRule(prefix="Postmortem", target_vault="team-x"),
            ],
        ),
        registry=_registry(mounted=["personal", "team-x", "team-y"]),
    )
    assert decision.target_vault == "team-x"


def test_ambiguous_rules_refuse_with_priority_unset() -> None:
    """Two rules with the same prefix and same length and no priority refuse."""
    with pytest.raises(RoutingRuleAmbiguous):
        resolve_target_vault(
            thought=_thought(),
            explicit_vault=None,
            user_config=_config(
                auto_route=True,
                rules=[
                    RoutingRule(prefix="Postmortem", target_vault="team-x"),
                    RoutingRule(prefix="Postmortem", target_vault="team-y"),
                ],
            ),
            registry=_registry(mounted=["personal", "team-x", "team-y"]),
        )


def test_priority_breaks_tie() -> None:
    """When two rules tie on prefix-length, higher priority wins."""
    decision = resolve_target_vault(
        thought=_thought(),
        explicit_vault=None,
        user_config=_config(
            auto_route=True,
            rules=[
                RoutingRule(prefix="Postmortem", target_vault="team-x", priority=5),
                RoutingRule(prefix="Postmortem", target_vault="team-y", priority=10),
            ],
        ),
        registry=_registry(mounted=["personal", "team-x", "team-y"]),
    )
    assert decision.target_vault == "team-y"


# === Rule 5: fall-through ===


def test_no_explicit_no_auto_route_falls_through() -> None:
    decision = resolve_target_vault(
        thought=_thought(),
        explicit_vault=None,
        user_config=_config(),
        registry=_registry(mounted=["personal"]),
    )
    assert decision.target_vault == "personal"
    assert decision.reason == "fallback_primary"
