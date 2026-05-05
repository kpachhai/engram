"""Tests for engram.diagnostics.check_codes team-vault additions."""

from __future__ import annotations

import re

from engram.diagnostics.check_codes import (
    ALL_PHASE_2_CHECK_CODES,
    ALL_PHASE_3_CHECK_CODES,
    ALL_PHASE_4_CHECK_CODES,
    GIT_BRANCH_DRIFTED,
    MULTIPLE_TEAM_WRITE_VAULTS_OK,
    ROUTING_RULE_PRIORITY_COLLISION,
    SERVE_CONFIG_STALE,
    TEAM_MEMBER_NOT_ENROLLED,
    TEAM_MEMBERSHIP_REVOKED,
    TEAM_PENDING_PUSHES,
    TEAM_POLICY_VIOLATION_QUARANTINED,
    TEAM_VAULT_EMBEDDING_MISMATCH,
)

PHASE_4_NEW_CODES = (
    MULTIPLE_TEAM_WRITE_VAULTS_OK,
    TEAM_MEMBER_NOT_ENROLLED,
    TEAM_PENDING_PUSHES,
    TEAM_MEMBERSHIP_REVOKED,
    TEAM_POLICY_VIOLATION_QUARANTINED,
    SERVE_CONFIG_STALE,
    ROUTING_RULE_PRIORITY_COLLISION,
    TEAM_VAULT_EMBEDDING_MISMATCH,
    GIT_BRANCH_DRIFTED,
)


def test_phase_4_extends_phase_3_superset() -> None:
    """ALL_PHASE_4_CHECK_CODES is a strict superset of ALL_PHASE_3_CHECK_CODES."""
    assert set(ALL_PHASE_3_CHECK_CODES).issubset(set(ALL_PHASE_4_CHECK_CODES))
    # All sync codes also propagate.
    assert set(ALL_PHASE_2_CHECK_CODES).issubset(set(ALL_PHASE_4_CHECK_CODES))


def test_phase_4_codes_are_unique() -> None:
    assert len(ALL_PHASE_4_CHECK_CODES) == len(set(ALL_PHASE_4_CHECK_CODES))


def test_phase_4_adds_nine_new_codes() -> None:
    new_codes = set(ALL_PHASE_4_CHECK_CODES) - set(ALL_PHASE_3_CHECK_CODES)
    assert new_codes == set(PHASE_4_NEW_CODES)


def test_all_codes_are_snake_case() -> None:
    snake_re = re.compile(r"^[a-z][a-z0-9_]*[a-z0-9]$")
    for code in ALL_PHASE_4_CHECK_CODES:
        assert snake_re.fullmatch(code), f"non-snake_case code: {code!r}"
