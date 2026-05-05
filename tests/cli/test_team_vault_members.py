"""Tests for ``engram team-vault add-member`` and ``revoke-key`` (Layer F).

Covers add-member happy path, idempotency, steward-only refusal,
revoke-key happy path, and revoke-key shadowing of enrollment.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from engram.cli.team_vault import add_member_cmd, revoke_key_cmd
from engram.errors import TeamMemberNotEnrolled, VaultError
from engram.team.members import MembersList

STEWARD = "1234567890ABCDEF1234567890ABCDEF12345678"
NEW_MEMBER = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"
NON_STEWARD = "9999999988887777666655554444333322221111"


def _initial_members_yaml(path: Path) -> None:
    path.write_text(
        f"members:\n  - {STEWARD}\nrevoked: []\n",
        encoding="utf-8",
    )


def _load_members(path: Path) -> MembersList:
    yaml_safe = YAML(typ="safe", pure=True)
    data = yaml_safe.load(path.read_text(encoding="utf-8")) or {}
    return MembersList.from_yaml_dict(data)


# === add-member ===


def test_add_member_happy_path(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    add_member_cmd(
        members_path,
        fingerprint=NEW_MEMBER,
        display_name="alice",
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    loaded = _load_members(members_path)
    assert loaded.is_enrolled(NEW_MEMBER)
    assert loaded.display_name_of(NEW_MEMBER) == "alice"


def test_add_member_idempotent(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    add_member_cmd(
        members_path,
        fingerprint=NEW_MEMBER,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    # Second call should be a no-op.
    add_member_cmd(
        members_path,
        fingerprint=NEW_MEMBER,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    loaded = _load_members(members_path)
    fingerprints = [m.fingerprint for m in loaded.members]
    assert fingerprints.count(NEW_MEMBER) == 1


def test_add_member_non_steward_refuses(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    with pytest.raises(TeamMemberNotEnrolled, match="not a steward"):
        add_member_cmd(
            members_path,
            fingerprint=NEW_MEMBER,
            caller_fingerprint=NON_STEWARD,
            stewards=[STEWARD],
        )


def test_add_member_invalid_fingerprint_refuses(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    with pytest.raises(VaultError, match="invalid fingerprint"):
        add_member_cmd(
            members_path,
            fingerprint="too-short",
            caller_fingerprint=STEWARD,
            stewards=[STEWARD],
        )


def test_add_member_against_missing_yaml_creates_one(tmp_path: Path) -> None:
    """If members.yaml doesn't exist yet, add-member creates it."""
    members_path = tmp_path / ".engram" / "members.yaml"
    add_member_cmd(
        members_path,
        fingerprint=NEW_MEMBER,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    assert members_path.exists()
    loaded = _load_members(members_path)
    assert loaded.is_enrolled(NEW_MEMBER)


# === revoke-key ===


def test_revoke_key_happy_path(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    add_member_cmd(
        members_path,
        fingerprint=NEW_MEMBER,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    revoke_key_cmd(
        members_path,
        fingerprint=NEW_MEMBER,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
        reason="left the team",
    )
    loaded = _load_members(members_path)
    assert NEW_MEMBER in loaded.revoked
    # is_enrolled returns False for revoked.
    assert not loaded.is_enrolled(NEW_MEMBER)


def test_revoke_key_non_steward_refuses(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    with pytest.raises(TeamMemberNotEnrolled, match="not a steward"):
        revoke_key_cmd(
            members_path,
            fingerprint=NEW_MEMBER,
            caller_fingerprint=NON_STEWARD,
            stewards=[STEWARD],
        )


def test_revoke_key_idempotent(tmp_path: Path) -> None:
    members_path = tmp_path / "members.yaml"
    _initial_members_yaml(members_path)
    revoke_key_cmd(
        members_path,
        fingerprint=STEWARD,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    revoke_key_cmd(
        members_path,
        fingerprint=STEWARD,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
    )
    loaded = _load_members(members_path)
    assert loaded.revoked.count(STEWARD) == 1


def test_revoke_key_against_missing_yaml_refuses(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="not found"):
        revoke_key_cmd(
            tmp_path / "missing" / "members.yaml",
            fingerprint=STEWARD,
            caller_fingerprint=STEWARD,
            stewards=[STEWARD],
        )
