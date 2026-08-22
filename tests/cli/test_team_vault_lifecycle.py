"""Tests for the team-vault lifecycle commands beyond setup + add-member.

Covers join_cmd (skip-clone path), unmount_cmd, rebind_cmd,
orphan_recover_cmd, redact_history_cmd.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from engram.cli.team_vault import (
    join_cmd,
    orphan_recover_cmd,
    rebind_cmd,
    redact_history_cmd,
    setup_cmd,
    unmount_cmd,
)
from engram.errors import (
    TeamMemberNotEnrolled,
    TeamVaultEmbeddingMismatch,
    VaultError,
)

STEWARD = "1234567890ABCDEF1234567890ABCDEF12345678"  # pii-allow: synthetic key fixture
NON_STEWARD = "9999999988887777666655554444333322221111"  # pii-allow: synthetic key fixture


def _seed_team_vault_files(tmp_path: Path) -> Path:
    """Run setup_cmd to populate canonical files; returns the path."""
    setup_cmd(
        tmp_path,
        remote_url="git@example:team-x.git",
        steward_fingerprint=STEWARD,
    )
    return tmp_path


# === join_cmd ===


def test_join_with_skip_clone_validates_canonical_files(tmp_path: Path) -> None:
    """skip_clone path verifies canonical files but doesn't run git."""
    vault = tmp_path / "team-x"
    _seed_team_vault_files(vault)
    outcome = join_cmd(
        vault,
        remote_url="git@example:team-x.git",
        local_alias="team-x",
        skip_clone=True,
    )
    assert outcome["alias"] == "team-x"
    assert isinstance(outcome["vault_id"], str)
    assert len(outcome["vault_id"]) == 16


def test_join_refuses_target_without_canonical_files(tmp_path: Path) -> None:
    """A skip_clone target lacking engram.config.yaml refuses."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    (empty_dir / "README.md").write_text("not an engram vault\n", encoding="utf-8")
    with pytest.raises(VaultError, match=r"engram\.config\.yaml"):
        join_cmd(
            empty_dir,
            remote_url="git@example:team-x.git",
            skip_clone=True,
        )


def test_join_refuses_embedding_mismatch(tmp_path: Path) -> None:
    """When local model differs from team's, refuses with TeamVaultEmbeddingMismatch."""
    vault = tmp_path / "team-x"
    _seed_team_vault_files(vault)
    with pytest.raises(TeamVaultEmbeddingMismatch):
        join_cmd(
            vault,
            remote_url="git@example:team-x.git",
            skip_clone=True,
            expected_embedding_model="OTHER/different-model",
        )


def test_join_passes_when_embedding_matches(tmp_path: Path) -> None:
    vault = tmp_path / "team-x"
    _seed_team_vault_files(vault)
    outcome = join_cmd(
        vault,
        remote_url="git@example:team-x.git",
        skip_clone=True,
        expected_embedding_model="BAAI/bge-small-en-v1.5",
    )
    assert outcome["alias"] == "team-x"


# === unmount_cmd ===


def test_unmount_removes_alias(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "vaults:\n"
        "  - name: personal\n"
        "    path: ~/p\n"
        "    role: primary\n"
        "  - name: team-x\n"
        "    path: ~/x\n"
        "    role: team-write\n"
        "    remote_url: git@example:team-x.git\n",
        encoding="utf-8",
    )
    outcome = unmount_cmd(
        vault_alias="team-x",
        user_config_path=cfg_path,
    )
    assert outcome["alias"] == "team-x"
    assert outcome["removed_local"] == "no"
    assert "team-x" not in cfg_path.read_text(encoding="utf-8")


def test_unmount_with_remove_local_deletes_directory(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "vaults:\n  - name: team-x\n    path: ~/x\n    role: team-write\n    "
        "remote_url: git@example:team-x.git\n",
        encoding="utf-8",
    )
    local = tmp_path / "team-x-local"
    local.mkdir()
    (local / "README.md").write_text("body", encoding="utf-8")
    outcome = unmount_cmd(
        vault_alias="team-x",
        user_config_path=cfg_path,
        remove_local=True,
        local_path=local,
    )
    assert outcome["removed_local"] == "yes"
    assert not local.exists()


def test_unmount_refuses_unknown_alias(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("vaults: []\n", encoding="utf-8")
    with pytest.raises(VaultError, match="not found"):
        unmount_cmd(
            vault_alias="nonexistent",
            user_config_path=cfg_path,
        )


# === rebind_cmd ===


def test_rebind_updates_remote_url(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "vaults:\n  - name: team-x\n    path: ~/x\n    role: team-write\n    "
        "remote_url: git@old:team-x.git\n",
        encoding="utf-8",
    )
    outcome = rebind_cmd(
        vault_alias="team-x",
        user_config_path=cfg_path,
        new_remote_url="git@new:team-x.git",
    )
    assert outcome["new_remote_url"] == "git@new:team-x.git"
    assert "git@new:team-x.git" in cfg_path.read_text(encoding="utf-8")


def test_rebind_refuses_unknown_alias(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("vaults: []\n", encoding="utf-8")
    with pytest.raises(VaultError):
        rebind_cmd(
            vault_alias="missing",
            user_config_path=cfg_path,
            new_remote_url="git@new:x.git",
        )


# === orphan_recover_cmd ===


def test_orphan_recover_extracts_files(tmp_path: Path) -> None:
    orphan_path = tmp_path / "team-vault-orphan-abc.tar.gz"
    seed_file = tmp_path / "thought.md"
    seed_file.write_text("body", encoding="utf-8")
    with tarfile.open(orphan_path, "w:gz") as tar:
        tar.add(str(seed_file), arcname="2026/05/thought.md")

    target_vault = tmp_path / "personal"
    target_vault.mkdir()
    outcome = orphan_recover_cmd(
        orphan_path=orphan_path,
        target_vault_path=target_vault,
    )
    assert outcome["discarded"] is False
    files = outcome["recovered_files"]
    assert isinstance(files, list)
    assert len(files) == 1
    assert (target_vault / "thoughts" / "2026" / "05" / "thought.md").exists()


def test_orphan_recover_discard_deletes_tarball(tmp_path: Path) -> None:
    orphan_path = tmp_path / "team-vault-orphan-abc.tar.gz"
    with tarfile.open(orphan_path, "w:gz") as tar:
        seed = tmp_path / "x.md"
        seed.write_text("body", encoding="utf-8")
        tar.add(str(seed), arcname="x.md")
    outcome = orphan_recover_cmd(orphan_path=orphan_path, discard=True)
    assert outcome["discarded"] is True
    assert not orphan_path.exists()


def test_orphan_recover_refuses_path_traversal(tmp_path: Path) -> None:
    """Tarballs with path-traversal entries are silently dropped."""
    orphan_path = tmp_path / "team-vault-orphan-evil.tar.gz"
    seed = tmp_path / "x.md"
    seed.write_text("body", encoding="utf-8")
    with tarfile.open(orphan_path, "w:gz") as tar:
        # Add a benign one + a malicious one.
        tar.add(str(seed), arcname="legit.md")
        info = tarfile.TarInfo(name="../escapee.md")
        info.size = 4
        import io as _io

        tar.addfile(info, _io.BytesIO(b"evil"))

    target_vault = tmp_path / "personal"
    target_vault.mkdir()
    outcome = orphan_recover_cmd(
        orphan_path=orphan_path,
        target_vault_path=target_vault,
    )
    files = outcome["recovered_files"]
    assert isinstance(files, list)
    assert "legit.md" in files
    assert "../escapee.md" not in files


# === redact_history_cmd ===


def test_redact_history_records_log_entry(tmp_path: Path) -> None:
    outcome = redact_history_cmd(
        vault_path=tmp_path,
        caller_fingerprint=STEWARD,
        stewards=[STEWARD],
        reason="committed AWS key by mistake",
        confirm_history_rewrite=True,
    )
    log_path = Path(str(outcome["log_path"]))
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert STEWARD in content
    assert "committed AWS key by mistake" in content


def test_redact_history_refuses_non_steward(tmp_path: Path) -> None:
    with pytest.raises(TeamMemberNotEnrolled):
        redact_history_cmd(
            vault_path=tmp_path,
            caller_fingerprint=NON_STEWARD,
            stewards=[STEWARD],
            reason="r",
            confirm_history_rewrite=True,
        )


def test_redact_history_refuses_without_confirmation(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="confirm"):
        redact_history_cmd(
            vault_path=tmp_path,
            caller_fingerprint=STEWARD,
            stewards=[STEWARD],
            reason="r",
            confirm_history_rewrite=False,
        )
