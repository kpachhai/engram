"""Tests for engram.sync.identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.errors import ConfigError
from engram.sync.identity import (
    IDENTITY_FILE_RELATIVE,
    Match,
    Mismatch,
    MissingIdentity,
    check_identity,
    load_identity,
)


def _write_identity(vault: Path, body: str) -> Path:
    target = vault / IDENTITY_FILE_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return target


def test_load_identity_missing_returns_none(tmp_path: Path) -> None:
    assert load_identity(tmp_path) is None


def test_load_identity_valid_minimal(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "vault_id: personal\nexpected_remote_pattern: '^git@github\\.com:owner/.*\\.git$'\n",
    )
    identity = load_identity(tmp_path)
    assert identity is not None
    assert identity.vault_id == "personal"
    assert identity.user_email is None


def test_load_identity_with_user_overrides(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        (
            "vault_id: work\n"
            "expected_remote_pattern: '^git@enterprise\\.example\\.com:.*\\.git$'\n"
            "user_email: dev@work.example\n"
            "user_name: Dev User\n"
        ),
    )
    identity = load_identity(tmp_path)
    assert identity is not None
    assert identity.user_email == "dev@work.example"
    assert identity.user_name == "Dev User"


def test_load_identity_invalid_yaml_raises(tmp_path: Path) -> None:
    _write_identity(tmp_path, "not: valid: yaml: with: too: many: colons:\n")
    with pytest.raises(ConfigError):
        load_identity(tmp_path)


def test_load_identity_top_level_not_mapping_raises(tmp_path: Path) -> None:
    _write_identity(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_identity(tmp_path)


def test_load_identity_unknown_field_rejected(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "vault_id: x\nexpected_remote_pattern: foo\nphase_3_thing: true\n",
    )
    with pytest.raises(ConfigError):
        load_identity(tmp_path)


def test_load_identity_missing_required_field_rejected(tmp_path: Path) -> None:
    _write_identity(tmp_path, "vault_id: x\n")
    with pytest.raises(ConfigError):
        load_identity(tmp_path)


def test_check_identity_match(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "vault_id: personal\nexpected_remote_pattern: '^git@github\\.com:owner/.*\\.git$'\n",
    )
    result = check_identity(tmp_path, "git@github.com:owner/personal.git")
    assert isinstance(result, Match)
    assert result.matched_url == "git@github.com:owner/personal.git"


def test_check_identity_mismatch_simulates_cross_vault_contamination(tmp_path: Path) -> None:
    pattern = "^git@github\\.com:owner/.*-personal\\.git$"
    _write_identity(
        tmp_path,
        f"vault_id: personal\nexpected_remote_pattern: '{pattern}'\n",
    )
    # Remote is the WORK URL but identity says personal -> mismatch.
    result = check_identity(tmp_path, "git@github.com:enterprise/internal-work.git")
    assert isinstance(result, Mismatch)
    assert result.actual_url == "git@github.com:enterprise/internal-work.git"


def test_check_identity_missing_file(tmp_path: Path) -> None:
    result = check_identity(tmp_path, "git@github.com:owner/repo.git")
    assert isinstance(result, MissingIdentity)
    assert result.vault_path == tmp_path


def test_check_identity_no_remote_with_identity_present_is_mismatch(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "vault_id: personal\nexpected_remote_pattern: '^git@github\\.com:owner/.*\\.git$'\n",
    )
    result = check_identity(tmp_path, None)
    assert isinstance(result, Mismatch)
    assert result.actual_url == ""


def test_check_identity_invalid_pattern_raises(tmp_path: Path) -> None:
    _write_identity(
        tmp_path,
        "vault_id: x\nexpected_remote_pattern: '['\n",
    )
    with pytest.raises(ConfigError):
        check_identity(tmp_path, "git@github.com:owner/repo.git")
