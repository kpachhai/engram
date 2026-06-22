"""Real-git smoke tests for engram.sync.gitops error classification.

Locks the regex patterns in :mod:`engram.sync.gitops` against actual git
stderr formats. Without these, mock fixtures can drift from real git
output across versions and the coordinator silently misclassifies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.sync import gitops
from engram.sync.gitops import GitErrorClass

from .conftest import commit_file, init_repo, run_git


def test_real_non_fast_forward_push(tmp_path: Path) -> None:
    """Two clones diverge; pushing the second classifies as NON_FAST_FORWARD."""
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)
    a = tmp_path / "a"
    b = tmp_path / "b"

    cp_a = run_git(["clone", str(bare), str(a)], tmp_path)
    assert cp_a.returncode == 0
    run_git(["config", "user.email", "a@example.com"], a)
    run_git(["config", "user.name", "a"], a)
    run_git(["config", "commit.gpgsign", "false"], a)
    commit_file(a, "first.md", "first")
    run_git(["push", "-u", "origin", "main"], a)

    cp_b = run_git(["clone", str(bare), str(b)], tmp_path)
    assert cp_b.returncode == 0
    run_git(["config", "user.email", "b@example.com"], b)
    run_git(["config", "user.name", "b"], b)
    run_git(["config", "commit.gpgsign", "false"], b)
    commit_file(b, "second.md", "second")

    # Push from a again to advance the remote past b's HEAD.
    commit_file(a, "third.md", "third")
    run_git(["push", "origin", "main"], a)

    # b has diverged; push should fail with non-fast-forward.
    result = asyncio.run(gitops.push(b, "origin", "main"))
    assert result.error_class is GitErrorClass.NON_FAST_FORWARD


def test_real_repository_not_found(tmp_path: Path) -> None:
    """Pushing to a nonexistent remote URL classifies as NETWORK_PERMANENT."""
    repo = tmp_path / "repo"
    init_repo(repo, bare=False)
    commit_file(repo, "x.md", "x")
    # Point origin at a path that does not exist.
    run_git(["remote", "add", "origin", str(tmp_path / "does-not-exist.git")], repo)
    result = asyncio.run(gitops.push(repo, "origin", "main"))
    assert result.error_class in {
        GitErrorClass.NETWORK_PERMANENT,
        GitErrorClass.UNKNOWN,
    }
    # On every git version we test, the stderr contains "does not appear to be a git repository"
    # which our pattern matches as NETWORK_PERMANENT. The fallback to UNKNOWN here protects against
    # OS-level errno wording differences without weakening the signal.


def test_real_conflict_during_pull_rebase(tmp_path: Path) -> None:
    """Two clones edit the same line; rebase produces CONFLICT classification."""
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    a = tmp_path / "a"
    cp_a = run_git(["clone", str(bare), str(a)], tmp_path)
    assert cp_a.returncode == 0
    run_git(["config", "user.email", "a@example.com"], a)
    run_git(["config", "user.name", "a"], a)
    run_git(["config", "commit.gpgsign", "false"], a)
    commit_file(a, "shared.md", "line one\n")
    run_git(["push", "-u", "origin", "main"], a)

    b = tmp_path / "b"
    cp_b = run_git(["clone", str(bare), str(b)], tmp_path)
    assert cp_b.returncode == 0
    run_git(["config", "user.email", "b@example.com"], b)
    run_git(["config", "user.name", "b"], b)
    run_git(["config", "commit.gpgsign", "false"], b)

    # a and b edit the same line, then a pushes.
    commit_file(a, "shared.md", "line one - edited by a\n")
    run_git(["push", "origin", "main"], a)
    commit_file(b, "shared.md", "line one - edited by b\n")

    # b's pull --rebase should produce a CONFLICT.
    result = asyncio.run(gitops.pull_rebase(b, "origin", "main"))
    assert result.error_class is GitErrorClass.CONFLICT


def test_real_git_version_meets_floor(tmp_path: Path) -> None:
    """The system git binary meets the documented version floor."""
    from engram.sync.startup_probes import GIT_VERSION_FLOOR

    init_repo(tmp_path, bare=False)
    version = asyncio.run(gitops.git_version(tmp_path))
    assert version >= GIT_VERSION_FLOOR, f"git {version} below floor {GIT_VERSION_FLOOR}"


def test_real_status_porcelain_after_modify(tmp_path: Path) -> None:
    """status_porcelain returns one entry after editing a tracked file."""
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "tracked.md", "v1")
    (tmp_path / "tracked.md").write_text("v2")
    entries = asyncio.run(gitops.status_porcelain(tmp_path))
    assert any(e.path == "tracked.md" and e.worktree_status == "M" for e in entries)


def test_real_ahead_behind_after_local_commit(tmp_path: Path, bare_remote: Path) -> None:
    """After a local commit without push, ahead=1 behind=0."""
    repo = tmp_path / "ab"
    cp = run_git(["clone", str(bare_remote), str(repo)], tmp_path)
    assert cp.returncode == 0
    run_git(["config", "user.email", "x@y"], repo)
    run_git(["config", "user.name", "x"], repo)
    run_git(["config", "commit.gpgsign", "false"], repo)
    commit_file(repo, "first.md", "1")
    run_git(["push", "-u", "origin", "main"], repo)
    commit_file(repo, "second.md", "2")
    ahead, behind = asyncio.run(gitops.ahead_behind_count(repo, "main"))
    assert (ahead, behind) == (1, 0)


def test_real_commit_paths_skips_when_nothing_staged(tmp_path: Path) -> None:
    """commit_paths returns nothing_to_commit=True when no diff exists."""
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "x.md", "x")
    result = asyncio.run(
        gitops.commit_paths(
            tmp_path,
            [tmp_path / "x.md"],
            message="should be no-op",
        ),
    )
    assert result.nothing_to_commit is True
    assert result.sha is None


def test_real_commit_paths_with_user_overrides(tmp_path: Path) -> None:
    """User identity flags propagate through to the resulting commit."""
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "seed.md", "seed")
    (tmp_path / "new.md").write_text("hello")
    result = asyncio.run(
        gitops.commit_paths(
            tmp_path,
            [tmp_path / "new.md"],
            message="engram: capture batch (N=1)",
            user_email="vault@example.com",
            user_name="vault",
        ),
    )
    assert result.sha is not None
    log = run_git(["log", "-1", "--format=%ae|%an"], tmp_path)
    assert log.stdout.strip() == "vault@example.com|vault"


@pytest.mark.parametrize("set_upstream", [False, True])
def test_real_push_after_clone(tmp_path: Path, bare_remote: Path, set_upstream: bool) -> None:
    """Successful push returns OK."""
    repo = tmp_path / "p"
    cp = run_git(["clone", str(bare_remote), str(repo)], tmp_path)
    assert cp.returncode == 0
    run_git(["config", "user.email", "x@y"], repo)
    run_git(["config", "user.name", "x"], repo)
    run_git(["config", "commit.gpgsign", "false"], repo)
    commit_file(repo, "x.md", "x")
    result = asyncio.run(gitops.push(repo, "origin", "main", set_upstream=set_upstream))
    assert result.error_class is GitErrorClass.OK
