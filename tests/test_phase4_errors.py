"""Tests for engram.errors team-vault additions - team-vault + routing + push queue."""

from __future__ import annotations

import pytest

from engram.errors import (
    AttributionCommitterMismatch,
    BlockThoughtInTeamVaultDisallowed,
    ConfigError,
    EmbeddingModelMismatch,
    EngramError,
    PushQueuePersistenceFailed,
    RoutingRuleAmbiguous,
    RoutingTargetNotMounted,
    SyncError,
    TeamMemberNotEnrolled,
    TeamMembershipRevoked,
    TeamPolicyViolation,
    TeamVaultAlreadyInitialized,
    TeamVaultEmbeddingMismatch,
    TeamWriteRequiresRemote,
    VaultError,
)

PHASE_4_EXPECTED_CODES: dict[type[EngramError], str] = {
    TeamMemberNotEnrolled: "team_member_not_enrolled",
    TeamPolicyViolation: "team_policy_violation",
    RoutingRuleAmbiguous: "routing_rule_ambiguous",
    RoutingTargetNotMounted: "routing_target_not_mounted",
    BlockThoughtInTeamVaultDisallowed: "block_thought_in_team_vault_disallowed",
    TeamVaultEmbeddingMismatch: "team_vault_embedding_mismatch",
    TeamMembershipRevoked: "team_membership_revoked",
    AttributionCommitterMismatch: "attribution_committer_mismatch",
    TeamWriteRequiresRemote: "team_write_requires_remote",
    TeamVaultAlreadyInitialized: "team_vault_already_initialized",
    PushQueuePersistenceFailed: "push_queue_persistence_failed",
}


@pytest.mark.parametrize(("cls", "expected"), list(PHASE_4_EXPECTED_CODES.items()))
def test_phase_4_error_code_constants(cls: type[EngramError], expected: str) -> None:
    assert cls.error_code == expected


def test_phase_4_inheritance_relationships() -> None:
    """Team-vault errors thread under correct base classes."""
    assert issubclass(TeamMemberNotEnrolled, VaultError)
    assert issubclass(TeamPolicyViolation, VaultError)
    assert issubclass(RoutingRuleAmbiguous, ConfigError)
    assert issubclass(RoutingTargetNotMounted, ConfigError)
    assert issubclass(BlockThoughtInTeamVaultDisallowed, VaultError)
    # TeamVaultEmbeddingMismatch refines EmbeddingModelMismatch.
    assert issubclass(TeamVaultEmbeddingMismatch, EmbeddingModelMismatch)
    assert issubclass(TeamMembershipRevoked, VaultError)
    # AttributionCommitterMismatch is a sync-time enforcement error.
    assert issubclass(AttributionCommitterMismatch, SyncError)
    assert issubclass(TeamWriteRequiresRemote, ConfigError)
    assert issubclass(TeamVaultAlreadyInitialized, VaultError)
    # PushQueuePersistenceFailed sits directly under EngramError.
    assert issubclass(PushQueuePersistenceFailed, EngramError)


def test_team_vault_embedding_mismatch_is_caught_by_base() -> None:
    """TeamVaultEmbeddingMismatch is a refinement of EmbeddingModelMismatch."""
    with pytest.raises(EmbeddingModelMismatch):
        raise TeamVaultEmbeddingMismatch("team policy pins different model")


@pytest.mark.parametrize("cls", list(PHASE_4_EXPECTED_CODES))
def test_phase_4_each_inherits_from_engram_error(cls: type[EngramError]) -> None:
    assert issubclass(cls, EngramError)
    assert issubclass(cls, Exception)


@pytest.mark.parametrize("cls", list(PHASE_4_EXPECTED_CODES))
def test_phase_4_each_can_be_raised_and_caught(cls: type[EngramError]) -> None:
    with pytest.raises(cls):
        raise cls("boom")


def test_phase_4_error_codes_are_unique() -> None:
    codes = [cls.error_code for cls in PHASE_4_EXPECTED_CODES]
    assert len(codes) == len(set(codes))
