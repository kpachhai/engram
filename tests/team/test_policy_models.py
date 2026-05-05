"""Tests for engram.team.policy + engram.team.members + RoutingRule.

Covers the team-vault models: TeamVaultPolicy, MembersList, RoutingRule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.config.models import RoutingRule
from engram.errors import (
    BlockThoughtInTeamVaultDisallowed,
    TeamPolicyViolation,
)
from engram.models.thought import Thought
from engram.team.members import (
    MemberEntry,
    MembersList,
    is_valid_fingerprint,
    normalize_fingerprint,
)
from engram.team.policy import TeamVaultPolicy

VALID_FP_1 = "1234567890ABCDEF1234567890ABCDEF12345678"
VALID_FP_2 = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"


def _make_thought(
    *,
    prefix: str = "Lesson",
    portability: str = "portable",
    source: str = "engram-test",
    content: str = "[Lesson] hello",
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
        content=content,
        file_path=Path("thoughts/2026-05-05-test.md"),
    )


# === TeamVaultPolicy ===


def test_team_vault_policy_round_trip() -> None:
    """All fields round-trip through model_dump / model_validate."""
    policy = TeamVaultPolicy(
        allowed_prefixes=["Postmortem", "Decision"],
        allowed_sources=None,
        accept_sensitive=False,
        required_embedding_model="BAAI/bge-small-en-v1.5",
        required_embedding_dim=384,
        stewards=[VALID_FP_1],
        min_engram_version="0.4.0",
    )
    redumped = TeamVaultPolicy.model_validate(policy.model_dump())
    assert redumped == policy


def test_team_vault_policy_default_accept_sensitive_is_false() -> None:
    """Pinned invariant 1 default-deny."""
    policy = TeamVaultPolicy(
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    assert policy.accept_sensitive is False


def test_team_vault_policy_empty_allowlist_denies_all() -> None:
    """An empty list denies all (explicit); ``None`` means "any"."""
    policy = TeamVaultPolicy(
        allowed_prefixes=[],
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    thought = _make_thought(prefix="Lesson")
    with pytest.raises(TeamPolicyViolation):
        policy.refuse_or_pass(thought)


def test_team_vault_policy_none_allowlist_means_any() -> None:
    policy = TeamVaultPolicy(
        allowed_prefixes=None,
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    # Should pass through since allowlist is "any".
    policy.refuse_or_pass(_make_thought(prefix="Lesson"))


def test_team_vault_policy_refuses_unallowed_prefix() -> None:
    policy = TeamVaultPolicy(
        allowed_prefixes=["Postmortem"],
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    with pytest.raises(TeamPolicyViolation, match="prefix_not_allowed"):
        policy.refuse_or_pass(_make_thought(prefix="Lesson"))


def test_team_vault_policy_refuses_unallowed_source() -> None:
    policy = TeamVaultPolicy(
        allowed_sources=["engram-prod"],
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    with pytest.raises(TeamPolicyViolation, match="source_not_allowed"):
        policy.refuse_or_pass(_make_thought(source="engram-test"))


def test_team_vault_policy_refuses_sensitive_when_not_accepted() -> None:
    policy = TeamVaultPolicy(
        accept_sensitive=False,
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    with pytest.raises(TeamPolicyViolation, match="sensitive_thought_target_does_not_accept"):
        policy.refuse_or_pass(_make_thought(portability="sensitive"))


def test_team_vault_policy_passes_sensitive_when_accepted() -> None:
    policy = TeamVaultPolicy(
        accept_sensitive=True,
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    policy.refuse_or_pass(_make_thought(portability="sensitive"))


def test_team_vault_policy_block_portability_always_refused() -> None:
    """Defense-in-depth: pinned invariant 1."""
    policy = TeamVaultPolicy(
        accept_sensitive=True,
        required_embedding_model="m",
        required_embedding_dim=1,
    )
    with pytest.raises(BlockThoughtInTeamVaultDisallowed):
        policy.refuse_or_pass(_make_thought(portability="block"))


def test_team_vault_policy_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        TeamVaultPolicy(
            required_embedding_model="m",
            required_embedding_dim=1,
            extra_unknown_field="oops",  # type: ignore[call-arg]
        )


# === MembersList ===


def test_members_list_round_trip() -> None:
    ml = MembersList(
        members=[
            MemberEntry(fingerprint=VALID_FP_1, display_name="alice"),
            MemberEntry(fingerprint=VALID_FP_2, display_name="bob"),
        ],
        revoked=[],
    )
    redumped = MembersList.model_validate(ml.model_dump())
    assert redumped == ml


def test_members_list_is_enrolled() -> None:
    ml = MembersList(members=[MemberEntry(fingerprint=VALID_FP_1)])
    assert ml.is_enrolled(VALID_FP_1)
    assert not ml.is_enrolled(VALID_FP_2)


def test_members_list_revoked_returns_false_for_enrollment() -> None:
    ml = MembersList(
        members=[MemberEntry(fingerprint=VALID_FP_1)],
        revoked=[VALID_FP_1],
    )
    assert not ml.is_enrolled(VALID_FP_1)


def test_members_list_lower_case_fingerprint_normalized() -> None:
    """is_enrolled tolerates lower-case + spaces."""
    ml = MembersList(members=[MemberEntry(fingerprint=VALID_FP_1)])
    assert ml.is_enrolled(VALID_FP_1.lower())


def test_members_list_invalid_fingerprint_refused() -> None:
    with pytest.raises(ValidationError):
        MemberEntry(fingerprint="too-short")


def test_members_list_from_yaml_dict_bare_string() -> None:
    """Hand-edited YAML with bare-string members parses correctly."""
    ml = MembersList.from_yaml_dict({"members": [VALID_FP_1, VALID_FP_2]})
    assert ml.is_enrolled(VALID_FP_1)
    assert ml.is_enrolled(VALID_FP_2)


def test_members_list_display_name_lookup() -> None:
    ml = MembersList(
        members=[MemberEntry(fingerprint=VALID_FP_1, display_name="alice")],
    )
    assert ml.display_name_of(VALID_FP_1) == "alice"
    assert ml.display_name_of(VALID_FP_2) is None


def test_normalize_fingerprint_strips_separators() -> None:
    spaced = "1234 5678 90AB CDEF 1234 5678 90AB CDEF 1234 5678"
    assert normalize_fingerprint(spaced) == VALID_FP_1


def test_is_valid_fingerprint() -> None:
    assert is_valid_fingerprint(VALID_FP_1)
    assert not is_valid_fingerprint("0123")
    assert not is_valid_fingerprint("X" * 40)


# === RoutingRule ===


def test_routing_rule_round_trip() -> None:
    rule = RoutingRule(prefix="Postmortem", target_vault="team-x", priority=10)
    redumped = RoutingRule.model_validate(rule.model_dump())
    assert redumped == rule


def test_routing_rule_default_no_priority() -> None:
    rule = RoutingRule(prefix="Postmortem", target_vault="team-x")
    assert rule.priority is None


def test_routing_rule_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        RoutingRule(prefix="P", target_vault="t", extra_field="x")  # type: ignore[call-arg]
