"""Tests for ``engram team-vault setup`` CLI command (Step 12).

Covers the canonical-files written, idempotency, resume-after-partial,
and the min_engram_version + steward fingerprint recordings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from engram.cli.team_vault import setup_cmd
from engram.errors import TeamVaultAlreadyInitialized, VaultError

VALID_FP = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic key fixture


def _yaml_load(path: Path) -> dict[str, object]:
    yaml_safe = YAML(typ="safe", pure=True)
    return dict(yaml_safe.load(path.read_text(encoding="utf-8")) or {})


def test_setup_init_empty_writes_canonical_files(tmp_path: Path) -> None:
    written = setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        init_empty=True,
        steward_fingerprint=VALID_FP,
    )
    # Five canonical files.
    assert (tmp_path / "engram.config.yaml").exists()
    assert (tmp_path / ".engram" / "team-policy.yaml").exists()
    assert (tmp_path / ".engram" / "members.yaml").exists()
    assert (tmp_path / ".gitignore").exists()
    assert (tmp_path / ".engram" / "setup_complete").exists()
    assert "engram.config.yaml" in written


def test_setup_records_min_engram_version(tmp_path: Path) -> None:
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    config = _yaml_load(tmp_path / "engram.config.yaml")
    assert "min_engram_version" in config
    policy = _yaml_load(tmp_path / ".engram" / "team-policy.yaml")
    assert "min_engram_version" in policy


def test_setup_records_steward_fingerprints(tmp_path: Path) -> None:
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    policy = _yaml_load(tmp_path / ".engram" / "team-policy.yaml")
    stewards = policy["stewards"]
    assert isinstance(stewards, list)
    assert VALID_FP in stewards
    members = _yaml_load(tmp_path / ".engram" / "members.yaml")
    member_list = members["members"]
    assert isinstance(member_list, list)
    assert any(m.get("fingerprint") == VALID_FP for m in member_list)


def test_setup_derives_vault_id(tmp_path: Path) -> None:
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    config = _yaml_load(tmp_path / "engram.config.yaml")
    assert "vault_id" in config
    vault_id = config["vault_id"]
    assert isinstance(vault_id, str)
    assert len(vault_id) == 16


def test_setup_uses_path_basename_as_default_vault_name(tmp_path: Path) -> None:
    target = tmp_path / "my-team-vault"
    setup_cmd(
        target,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    config = _yaml_load(target / "engram.config.yaml")
    assert config["vault_name"] == "my-team-vault"


def test_setup_explicit_vault_name(tmp_path: Path) -> None:
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        vault_name="custom-name",
        steward_fingerprint=VALID_FP,
    )
    config = _yaml_load(tmp_path / "engram.config.yaml")
    assert config["vault_name"] == "custom-name"


def test_setup_refuses_overwrite_when_complete(tmp_path: Path) -> None:
    """Second setup against a fully-initialized vault refuses."""
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    with pytest.raises(TeamVaultAlreadyInitialized):
        setup_cmd(
            tmp_path,
            remote_url="git@example:team-x.git",
            steward_fingerprint=VALID_FP,
        )


def test_setup_resume_after_partial(tmp_path: Path) -> None:
    """A crash after engram.config.yaml writes but before members.yaml resumes cleanly."""
    # Simulate partial setup: write only engram.config.yaml.
    (tmp_path / "engram.config.yaml").write_text("vault_name: x\n", encoding="utf-8")
    # Resume should fill in the missing files (no sentinel = not "complete").
    written = setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    # The sentinel was just-written; setup completed.
    assert (tmp_path / ".engram" / "setup_complete").exists()
    assert ".engram/team-policy.yaml" in written  # newly written
    assert "engram.config.yaml" not in written  # already existed, not overwritten


def test_setup_refuses_init_empty_and_adopt_existing_together(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="mutually exclusive"):
        setup_cmd(
            tmp_path,
            remote_url="git@example:team-x.git",
            init_empty=True,
            adopt_existing=True,
            steward_fingerprint=VALID_FP,
        )


def test_setup_refuses_without_steward_fingerprint(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="steward GPG fingerprint"):
        setup_cmd(
            tmp_path,
            remote_url="git@example:team-x.git",
            steward_fingerprint=None,
        )


def test_setup_writes_gitignore_with_indexes_entry(tmp_path: Path) -> None:
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".indexes/" in gitignore
    assert ".engram/identity.local" in gitignore
    assert ".engram/push-queue.local" in gitignore


def test_setup_recorded_remote_url(tmp_path: Path) -> None:
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=VALID_FP,
    )
    config = _yaml_load(tmp_path / "engram.config.yaml")
    assert config["remote_url"] == "git@example:team-x.git"
