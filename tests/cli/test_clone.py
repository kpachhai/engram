"""Tests for engram clone-vault (Step 14)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from engram.cli import app
from tests.sync.conftest import commit_file, init_repo, run_git

runner = CliRunner()


def _seed_remote(tmp_path: Path) -> Path:
    """Create a bare remote with one initial commit pushed from a seed clone."""
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)
    seed = tmp_path / "seed-push"
    cp_clone = run_git(["clone", str(bare), str(seed)], tmp_path)
    assert cp_clone.returncode == 0
    run_git(["config", "user.email", "x@y"], seed)
    run_git(["config", "user.name", "x"], seed)
    run_git(["config", "commit.gpgsign", "false"], seed)
    commit_file(seed, "first.md", "1")
    cp_push = run_git(["push", "-u", "origin", "main"], seed)
    assert cp_push.returncode == 0
    return bare


def test_clone_vault_succeeds(tmp_path: Path) -> None:
    """Basic clone succeeds and writes the identity template."""
    source = _seed_remote(tmp_path)
    target = tmp_path / "clone"
    result = runner.invoke(app, ["clone-vault", str(source), str(target)])
    assert result.exit_code == 0, result.stdout
    assert (target / ".git").exists()
    assert (target / ".engram" / "identity.local").exists()
    # Hooks dir was nuked then re-created (empty).
    hooks_dir = target / ".git" / "hooks"
    assert hooks_dir.exists()
    assert not any(hooks_dir.iterdir())


def test_clone_vault_security_property_post_checkout_hook_does_not_fire(
    tmp_path: Path,
) -> None:
    """R-H1: even with hooks pre-staged in .git, ``clone-vault`` deletes them
    BEFORE checkout so the malicious hook never runs."""
    source = _seed_remote(tmp_path)

    # Plant a malicious hook directly in the bare repo's hooks dir.
    sentinel = tmp_path / "hook-sentinel.txt"
    bare_hooks_dir = source / "hooks"
    bare_hook_path = bare_hooks_dir / "post-checkout"
    bare_hook_path.write_text(f"#!/bin/sh\necho fired > {sentinel}\n")
    bare_hook_path.chmod(0o755)

    target = tmp_path / "clone"
    result = runner.invoke(app, ["clone-vault", str(source), str(target)])
    assert result.exit_code == 0, result.stdout
    # The defining property: regardless of whether git would run hooks from
    # the bare repo, our coordinator deletes the cloned hooks dir before
    # checkout, so the only way for sentinel to exist is via a path engram
    # explicitly mitigates against. Therefore sentinel must NOT exist.
    assert not sentinel.exists(), "post-checkout hook should never fire under clone-vault"


def test_clone_vault_refuses_non_empty_target(tmp_path: Path) -> None:
    source = _seed_remote(tmp_path)

    target = tmp_path / "target"
    target.mkdir()
    (target / "existing.md").write_text("not empty")

    result = runner.invoke(app, ["clone-vault", str(source), str(target)])
    assert result.exit_code == 2
    combined = (result.stdout + result.stderr).lower()
    assert "non-empty" in combined or "non empty" in combined


def test_clone_vault_invalid_url_fails(tmp_path: Path) -> None:
    target = tmp_path / "fail-target"
    bad = tmp_path / "does-not-exist.git"
    result = runner.invoke(app, ["clone-vault", str(bad), str(target)])
    assert result.exit_code == 2
