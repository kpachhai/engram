"""Tests for engram.team.capture_gate.gate_team_capture.

Step 11 verifier: each branch + the happy path through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from engram.errors import (
    BlockThoughtInTeamVaultDisallowed,
    TeamMemberNotEnrolled,
    TeamPolicyViolation,
    VaultReadOnlyError,
)
from engram.models.thought import Thought
from engram.team.capture_gate import gate_team_capture
from engram.team.members import MemberEntry, MembersList
from engram.team.policy import TeamVaultPolicy

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"
OTHER_FP = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"


def _thought(*, prefix: str = "Postmortem", portability: str = "portable") -> Thought:
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
        vault="team-x",
        content=f"[{prefix}] body",
        file_path=Path("test.md"),
    )


def _members() -> MembersList:
    return MembersList(members=[MemberEntry(fingerprint=VALID_FP)])


def _policy() -> TeamVaultPolicy:
    return TeamVaultPolicy(
        allowed_prefixes=["Postmortem", "Decision"],
        required_embedding_model="m",
        required_embedding_dim=1,
    )


def _gpg(fingerprint: str | None = VALID_FP) -> MagicMock:
    mock = MagicMock()
    mock.primary_fingerprint.return_value = fingerprint
    return mock


# === Happy paths ===


def test_team_write_happy_path_stamps_captured_by() -> None:
    thought = _thought()
    gated = gate_team_capture(
        thought=thought,
        role="team-write",
        members=_members(),
        policy=_policy(),
        gpg_identity=_gpg(),
    )
    assert gated.captured_by == VALID_FP


def test_primary_role_skips_team_gate() -> None:
    """primary captures don't hit team gate logic; captured_by stays None."""
    thought = _thought()
    gated = gate_team_capture(
        thought=thought,
        role="primary",
        members=None,
        policy=None,
        gpg_identity=None,
    )
    assert gated.captured_by is None


# === Read-only refusal ===


def test_read_only_role_refuses() -> None:
    with pytest.raises(VaultReadOnlyError, match="vault_read_only"):
        gate_team_capture(
            thought=_thought(),
            role="read-only",
            members=None,
            policy=None,
            gpg_identity=None,
        )


# === Member enrollment ===


def test_team_write_unenrolled_fingerprint_refuses() -> None:
    with pytest.raises(TeamMemberNotEnrolled):
        gate_team_capture(
            thought=_thought(),
            role="team-write",
            members=_members(),
            policy=_policy(),
            gpg_identity=_gpg(fingerprint=OTHER_FP),
        )


def test_team_write_no_gpg_key_refuses() -> None:
    with pytest.raises(TeamMemberNotEnrolled, match="enroll-key"):
        gate_team_capture(
            thought=_thought(),
            role="team-write",
            members=_members(),
            policy=_policy(),
            gpg_identity=_gpg(fingerprint=None),
        )


# === Policy gate ===


def test_team_write_disallowed_prefix_refuses() -> None:
    with pytest.raises(TeamPolicyViolation, match="prefix_not_allowed"):
        gate_team_capture(
            thought=_thought(prefix="Friction"),
            role="team-write",
            members=_members(),
            policy=_policy(),
            gpg_identity=_gpg(),
        )


def test_team_write_block_portability_defense_in_depth() -> None:
    """Policy gate's block check is defense-in-depth (routing catches upstream)."""
    with pytest.raises(BlockThoughtInTeamVaultDisallowed):
        gate_team_capture(
            thought=_thought(portability="block"),
            role="team-write",
            members=_members(),
            policy=_policy(),
            gpg_identity=_gpg(),
        )


# === Misuse ===


def test_team_write_with_none_members_raises_value_error() -> None:
    """Programmer error: team-write capture without all required args."""
    with pytest.raises(ValueError, match="non-None"):
        gate_team_capture(
            thought=_thought(),
            role="team-write",
            members=None,
            policy=_policy(),
            gpg_identity=_gpg(),
        )
