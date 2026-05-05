"""Tests for engram.sync.serve_hooks.maybe_startup_pull."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.config.models import SyncConfig
from engram.sync.gitops import GitErrorClass
from engram.sync.serve_hooks import maybe_startup_pull

from .conftest import commit_file, init_repo, run_git


def test_startup_pull_no_remote_returns_none(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "x.md", "x")
    config = SyncConfig()
    result = asyncio.run(maybe_startup_pull(tmp_path, config))
    assert result is None


def test_startup_pull_disabled_returns_none(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    config = SyncConfig(disabled=True)
    result = asyncio.run(maybe_startup_pull(tmp_path, config))
    assert result is None


def test_startup_pull_auto_pull_off_returns_none(tmp_path: Path, bare_remote: Path) -> None:
    repo = tmp_path / "repo"
    cp = run_git(["clone", str(bare_remote), str(repo)], tmp_path)
    assert cp.returncode == 0
    config = SyncConfig(auto_pull_on_startup=False)
    result = asyncio.run(maybe_startup_pull(repo, config))
    assert result is None


def test_startup_pull_succeeds_after_clone(tmp_path: Path, bare_remote: Path) -> None:
    """A clean clone should pull successfully (even when nothing to pull)."""
    # Seed the bare remote first so pull has something to fetch.
    seed = tmp_path / "seed"
    cp_clone = run_git(["clone", str(bare_remote), str(seed)], tmp_path)
    assert cp_clone.returncode == 0
    run_git(["config", "user.email", "x@y"], seed)
    run_git(["config", "user.name", "x"], seed)
    run_git(["config", "commit.gpgsign", "false"], seed)
    commit_file(seed, "first.md", "1")
    cp_push = run_git(["push", "-u", "origin", "main"], seed)
    assert cp_push.returncode == 0

    repo = tmp_path / "repo"
    cp_clone2 = run_git(["clone", str(bare_remote), str(repo)], tmp_path)
    assert cp_clone2.returncode == 0
    run_git(["config", "user.email", "x@y"], repo)
    run_git(["config", "user.name", "x"], repo)
    run_git(["config", "commit.gpgsign", "false"], repo)
    config = SyncConfig(startup_pull_timeout_seconds=10.0)
    result = asyncio.run(maybe_startup_pull(repo, config))
    assert result is not None
    assert result.error_class is GitErrorClass.OK


@pytest.mark.parametrize("timeout_seconds", [10.0])
def test_startup_pull_completes_within_budget(
    tmp_path: Path,
    bare_remote: Path,
    timeout_seconds: float,
) -> None:
    repo = tmp_path / "repo"
    run_git(["clone", str(bare_remote), str(repo)], tmp_path)
    config = SyncConfig(startup_pull_timeout_seconds=timeout_seconds)
    result = asyncio.run(maybe_startup_pull(repo, config))
    assert result is None or result.error_class in {
        GitErrorClass.OK,
        GitErrorClass.UNKNOWN,
        GitErrorClass.NETWORK_TRANSIENT,
    }
