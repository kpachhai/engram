"""Tests for engram.team.policy.TeamVaultPolicy.refuse_or_pass.

Step 5 verifier - exercises each rejection path + the pass-through happy path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from engram.errors import (
    BlockThoughtInTeamVaultDisallowed,
    TeamPolicyViolation,
)
from engram.models.thought import Thought
from engram.team.policy import TeamVaultPolicy


def _make_thought(
    *,
    prefix: str = "Postmortem",
    portability: str = "portable",
    source: str = "engram-test",
) -> Thought:
    now = datetime.now(tz=UTC)
    return Thought(
        id=uuid4(),
        schema_version=1,
        prefix=prefix,
        portability=portability,  # type: ignore[arg-type]
        source=source,
        created_at=now,
        updated_at=now,
        fingerprint="0" * 64,
        tags=[],
        vault="team-x",
        content=f"[{prefix}] policy gate test",
        file_path=Path("thoughts/test.md"),
    )


def _allowlisted_policy() -> TeamVaultPolicy:
    return TeamVaultPolicy(
        allowed_prefixes=["Postmortem", "Decision"],
        allowed_sources=["engram-test", "engram-prod"],
        accept_sensitive=False,
        required_embedding_model="BAAI/bge-small-en-v1.5",
        required_embedding_dim=384,
    )


def test_happy_path_passes() -> None:
    policy = _allowlisted_policy()
    policy.refuse_or_pass(_make_thought(prefix="Postmortem", source="engram-test"))


def test_block_portability_always_refused_at_gate() -> None:
    policy = _allowlisted_policy()
    with pytest.raises(BlockThoughtInTeamVaultDisallowed):
        policy.refuse_or_pass(_make_thought(prefix="Postmortem", portability="block"))


def test_unallowed_prefix_refused() -> None:
    policy = _allowlisted_policy()
    with pytest.raises(TeamPolicyViolation, match="prefix_not_allowed"):
        policy.refuse_or_pass(_make_thought(prefix="Friction"))


def test_unallowed_source_refused() -> None:
    policy = _allowlisted_policy()
    with pytest.raises(TeamPolicyViolation, match="source_not_allowed"):
        policy.refuse_or_pass(_make_thought(source="cli"))


def test_sensitive_without_accept_refused() -> None:
    policy = _allowlisted_policy()
    with pytest.raises(TeamPolicyViolation, match="sensitive"):
        policy.refuse_or_pass(_make_thought(portability="sensitive"))


def test_sensitive_with_accept_passes() -> None:
    policy = TeamVaultPolicy(
        accept_sensitive=True,
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    policy.refuse_or_pass(_make_thought(portability="sensitive"))


def test_none_allowlist_means_any() -> None:
    policy = TeamVaultPolicy(
        allowed_prefixes=None,
        allowed_sources=None,
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    policy.refuse_or_pass(_make_thought(prefix="Friction", source="random"))


def test_empty_allowlist_denies_all() -> None:
    """Per spec: empty list is the explicit deny-all."""
    policy = TeamVaultPolicy(
        allowed_prefixes=[],
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    with pytest.raises(TeamPolicyViolation):
        policy.refuse_or_pass(_make_thought(prefix="Postmortem"))


def test_block_takes_precedence_over_allowlist() -> None:
    """block is refused even when prefix would be allowlisted."""
    policy = TeamVaultPolicy(
        allowed_prefixes=["Postmortem"],
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    with pytest.raises(BlockThoughtInTeamVaultDisallowed):
        policy.refuse_or_pass(_make_thought(prefix="Postmortem", portability="block"))
