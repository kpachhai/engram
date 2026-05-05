"""Tests for engram sync (Steps 15-16)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from engram.cli import app
from tests.sync.conftest import commit_file, init_repo, run_git

runner = CliRunner()


def _write_user_config(tmp_path: Path, vault_path: Path) -> Path:
    """Build a minimal per-user engram config pointing at ``vault_path``."""
    home = tmp_path / "home"
    config_dir = home / ".config" / "engram"
    config_dir.mkdir(parents=True)
    config_file = config_dir / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "default_user": "test",
                "vaults": [{"name": "primary", "path": str(vault_path), "role": "primary"}],
            }
        )
    )
    return home


def _setup_clone(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Bare remote + seeded clone + per-user config home."""
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    seed = tmp_path / "seed"
    cp = run_git(["clone", str(bare), str(seed)], tmp_path)
    assert cp.returncode == 0
    run_git(["config", "user.email", "x@y"], seed)
    run_git(["config", "user.name", "x"], seed)
    run_git(["config", "commit.gpgsign", "false"], seed)
    commit_file(seed, "first.md", "1")
    run_git(["push", "-u", "origin", "main"], seed)

    vault = tmp_path / "vault"
    cp_clone = run_git(["clone", str(bare), str(vault)], tmp_path)
    assert cp_clone.returncode == 0
    run_git(["config", "user.email", "x@y"], vault)
    run_git(["config", "user.name", "x"], vault)
    run_git(["config", "commit.gpgsign", "false"], vault)

    home = _write_user_config(tmp_path, vault)
    # Vault config: minimal.
    (vault / "engram.config.yaml").write_text(
        yaml.safe_dump(
            {
                "vault_name": "primary",
                "thoughts_dir": str(vault / "thoughts"),
                "sync": {"git_remote": "origin", "git_branch": "main"},
            }
        )
    )
    (vault / "thoughts").mkdir(exist_ok=True)
    return vault, bare, home


def test_sync_pull_against_clean_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ENGRAM_DEFAULT_USER", raising=False)
    result = runner.invoke(app, ["sync", "--config", str(vault / "engram.config.yaml"), "--pull"])
    assert result.exit_code == 0, result.stdout + result.stderr


def test_sync_push_with_pending_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    # Make a local commit then sync --push.
    commit_file(vault, "thoughts/x.md", "x")
    result = runner.invoke(app, ["sync", "--config", str(vault / "engram.config.yaml"), "--push"])
    assert result.exit_code == 0, result.stdout + result.stderr


def test_sync_push_read_only_role_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    # Override sync.role to read-only in vault config.
    vault_config = vault / "engram.config.yaml"
    data = yaml.safe_load(vault_config.read_text())
    data["sync"]["role"] = "read-only"
    vault_config.write_text(yaml.safe_dump(data))
    commit_file(vault, "thoughts/x.md", "x")
    result = runner.invoke(app, ["sync", "--config", str(vault / "engram.config.yaml"), "--push"])
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "vault_read_only" in combined or "read-only" in combined


def test_sync_first_push_empty_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First push from a non-bootstrapped clone."""
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)
    vault = tmp_path / "vault"
    init_repo(vault, bare=False)
    run_git(["config", "user.email", "x@y"], vault)
    run_git(["config", "user.name", "x"], vault)
    run_git(["config", "commit.gpgsign", "false"], vault)
    run_git(["remote", "add", "origin", str(bare)], vault)
    (vault / "thoughts").mkdir()
    (vault / "thoughts" / "first.md").write_text("first")
    home = _write_user_config(tmp_path, vault)
    (vault / "engram.config.yaml").write_text(
        yaml.safe_dump(
            {
                "vault_name": "primary",
                "thoughts_dir": str(vault / "thoughts"),
                "sync": {"git_remote": "origin", "git_branch": "main"},
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    result = runner.invoke(
        app,
        ["sync", "--config", str(vault / "engram.config.yaml"), "--first-push"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Bare repo should now have main branch.
    cp_branches = run_git(["branch", "--list"], bare)
    assert "main" in cp_branches.stdout


def test_sync_default_pull_then_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    commit_file(vault, "thoughts/y.md", "y")
    result = runner.invoke(app, ["sync", "--config", str(vault / "engram.config.yaml")])
    assert result.exit_code == 0, result.stdout + result.stderr


def test_sync_refuses_when_vault_lock_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    # Create an artificial lock file to simulate a running serve.
    indexes = vault / ".indexes"
    indexes.mkdir(exist_ok=True)
    (indexes / "engram.lock").write_text('{"pid": 99999, "hostname": "test"}')
    try:
        result = runner.invoke(
            app, ["sync", "--config", str(vault / "engram.config.yaml"), "--pull"]
        )
        assert result.exit_code == 2
        combined = result.stdout + result.stderr
        assert "lock" in combined.lower()
    finally:
        (indexes / "engram.lock").unlink()


def test_sync_compact_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    result = runner.invoke(app, ["sync", "compact", "--config", str(vault / "engram.config.yaml")])
    assert result.exit_code == 0, result.stdout + result.stderr
    # gc.reflogExpire should now be set.
    cp = run_git(["config", "--get", "gc.reflogExpire"], vault)
    assert cp.returncode == 0
    assert "30.days.ago" in cp.stdout


def test_sync_mutually_exclusive_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, _, home = _setup_clone(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    result = runner.invoke(
        app,
        ["sync", "--config", str(vault / "engram.config.yaml"), "--pull", "--push"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in (result.stdout + result.stderr)
