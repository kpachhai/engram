"""Tests for engram.team.identity - GPG identity wrapper + member-enrollment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from engram.errors import TeamMemberNotEnrolled
from engram.team.identity import (
    GpgError,
    GpgIdentity,
    GpgKey,
    _parse_colon_output,
    assert_member_enrolled,
)
from engram.team.members import MemberEntry, MembersList

VALID_FP_PRIMARY = "1234567890ABCDEF1234567890ABCDEF12345678"
VALID_FP_SUB = "9999999988887777666655554444333322221111"
VALID_FP_OTHER = "ABCDEFABCDEFABCDEFABCDEFABCDEFABCDEFABCD"

# Sample gpg --list-secret-keys --with-colons output covering one primary +
# one signing subkey + one uid.
SAMPLE_COLON_OUTPUT = f"""sec:u:255:22:1234567890ABCDEF:1700000000:::u:::scESC:::+:::ed25519:::0:
fpr:::::::::{VALID_FP_PRIMARY}:
grp:::::::::SOME_GRIP_HERE:
uid:u::::1700000000::ABCDEF::Alice <alice@example.com>::::::::::0:
ssb:u:255:22:9999999988887777:1700000000::::::s:::+:::ed25519:::0:
fpr:::::::::{VALID_FP_SUB}:
grp:::::::::ANOTHER_GRIP:
"""


@dataclass
class _MockResult:
    returncode: int
    stdout: str
    stderr: str = ""


def _make_run_command(
    *,
    returncode: int = 0,
    stdout: str = SAMPLE_COLON_OUTPUT,
    stderr: str = "",
):
    def _runner(
        cmd: Sequence[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        check: bool = False,
        timeout: float | None = None,
    ) -> _MockResult:
        return _MockResult(returncode=returncode, stdout=stdout, stderr=stderr)

    return _runner


# === colon-output parser ===


def test_parse_colon_output_extracts_primary_and_subkey() -> None:
    keys = _parse_colon_output(SAMPLE_COLON_OUTPUT)
    assert len(keys) == 1
    key = keys[0]
    assert key.primary_fingerprint == VALID_FP_PRIMARY
    assert key.subkey_fingerprints == (VALID_FP_SUB,)
    assert key.user_id == "Alice <alice@example.com>"


def test_parse_colon_output_empty_returns_empty() -> None:
    assert _parse_colon_output("") == []


def test_parse_colon_output_two_secrets() -> None:
    other_fp = "FFFFEEEEDDDDCCCCBBBBAAAA9999888877776666"
    text = (
        SAMPLE_COLON_OUTPUT
        + f"""sec:u:255:22:OTHEROTHER:1700000000:::u:::scESC:::+:::ed25519:::0:
fpr:::::::::{other_fp}:
"""
    )
    keys = _parse_colon_output(text)
    assert len(keys) == 2
    assert keys[0].primary_fingerprint == VALID_FP_PRIMARY
    assert keys[1].primary_fingerprint == other_fp


# === GpgIdentity ===


def test_primary_fingerprint_returns_first_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/gpg")
    identity = GpgIdentity(run_command=_make_run_command())
    assert identity.primary_fingerprint() == VALID_FP_PRIMARY


def test_primary_fingerprint_returns_none_when_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/gpg")
    identity = GpgIdentity(run_command=_make_run_command(stdout=""))
    assert identity.primary_fingerprint() is None


def test_primary_for_subkey_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/gpg")
    identity = GpgIdentity(run_command=_make_run_command())
    assert identity.primary_for_subkey(VALID_FP_SUB) == VALID_FP_PRIMARY


def test_primary_for_subkey_returns_primary_when_passed_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/gpg")
    identity = GpgIdentity(run_command=_make_run_command())
    assert identity.primary_for_subkey(VALID_FP_PRIMARY) == VALID_FP_PRIMARY


def test_primary_for_subkey_returns_none_when_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/gpg")
    identity = GpgIdentity(run_command=_make_run_command())
    assert identity.primary_for_subkey(VALID_FP_OTHER) is None


def test_gpg_not_installed_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    identity = GpgIdentity(run_command=_make_run_command())
    with pytest.raises(GpgError, match="not found on PATH"):
        identity.list_secret_keys()


def test_gpg_returns_nonzero_surfaces_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/fake/gpg")
    identity = GpgIdentity(
        run_command=_make_run_command(returncode=2, stdout="", stderr="permission denied"),
    )
    with pytest.raises(GpgError, match="exited 2"):
        identity.list_secret_keys()


# === assert_member_enrolled ===


def test_assert_member_enrolled_passes_for_enrolled() -> None:
    members = MembersList(members=[MemberEntry(fingerprint=VALID_FP_PRIMARY)])
    assert_member_enrolled(members, VALID_FP_PRIMARY)


def test_assert_member_enrolled_refuses_for_unenrolled() -> None:
    members = MembersList(members=[MemberEntry(fingerprint=VALID_FP_PRIMARY)])
    with pytest.raises(TeamMemberNotEnrolled):
        assert_member_enrolled(members, VALID_FP_OTHER)


def test_assert_member_enrolled_refuses_for_none_fingerprint() -> None:
    members = MembersList(members=[MemberEntry(fingerprint=VALID_FP_PRIMARY)])
    with pytest.raises(TeamMemberNotEnrolled, match="enroll-key"):
        assert_member_enrolled(members, None)


def test_assert_member_enrolled_refuses_for_revoked() -> None:
    members = MembersList(
        members=[MemberEntry(fingerprint=VALID_FP_PRIMARY)],
        revoked=[VALID_FP_PRIMARY],
    )
    with pytest.raises(TeamMemberNotEnrolled):
        assert_member_enrolled(members, VALID_FP_PRIMARY)


def test_gpg_key_dataclass_round_trip() -> None:
    """GpgKey is hashable / frozen; document the contract."""
    key = GpgKey(
        primary_fingerprint=VALID_FP_PRIMARY,
        subkey_fingerprints=(VALID_FP_SUB,),
        user_id="alice",
    )
    assert key.primary_fingerprint == VALID_FP_PRIMARY
    assert key.user_id == "alice"
