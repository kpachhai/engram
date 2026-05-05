"""Shared fixtures for the sync test suite.

Step 18 (Layer G) deliverable. Used by every test that needs a real git
working tree, including the gitops smoke tests in Layer B.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

#: Non-interactive env for every git invocation in the test suite.
TEST_GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_MERGE_AUTOEDIT": "no",
    "GIT_ASKPASS": "true",
    "GIT_LFS_SKIP_SMUDGE": "1",
    # Force consistent identity even on CI where global config is absent.
    "GIT_AUTHOR_NAME": "engram-test",
    "GIT_AUTHOR_EMAIL": "engram-test@example.com",
    "GIT_COMMITTER_NAME": "engram-test",
    "GIT_COMMITTER_EMAIL": "engram-test@example.com",
}


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Wrapper that always passes the non-interactive env + capture_output."""
    return subprocess.run(  # noqa: S603 - test-only helper; args are static literals
        ["git", *args],  # noqa: S607 - test fixtures rely on $PATH
        cwd=str(cwd),
        env=TEST_GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def init_repo(repo: Path, *, bare: bool = False) -> None:
    """Initialize a fresh repo at ``repo`` (bare or non-bare).

    Sets ``init.defaultBranch=main`` and configures user identity so commit
    invocations succeed without depending on the test machine's global git
    config.
    """
    repo.mkdir(parents=True, exist_ok=True)
    args = ["init", "--initial-branch=main"]
    if bare:
        args.append("--bare")
    cp = run_git(args, repo)
    assert cp.returncode == 0, cp.stderr
    if not bare:
        run_git(["config", "user.email", "engram-test@example.com"], repo)
        run_git(["config", "user.name", "engram-test"], repo)
        run_git(["config", "commit.gpgsign", "false"], repo)


def commit_file(
    repo: Path,
    relative_path: str,
    content: str,
    *,
    message: str | None = None,
) -> None:
    """Write a file under ``repo`` and commit it.

    Creates parent directories as needed; commits with a deterministic
    message defaulting to ``"add <relative_path>"``.
    """
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    cp_add = run_git(["add", "--", relative_path], repo)
    assert cp_add.returncode == 0, cp_add.stderr
    msg = message or f"add {relative_path}"
    cp_commit = run_git(["commit", "-m", msg, "--no-verify"], repo)
    assert cp_commit.returncode == 0, cp_commit.stderr


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """Create a fresh bare git repo at ``tmp_path / 'remote.git'``."""
    repo = tmp_path / "remote.git"
    init_repo(repo, bare=True)
    return repo


@pytest.fixture
def working_repo(tmp_path: Path) -> Path:
    """Initialize a non-bare repo at ``tmp_path / 'work'`` with one seed commit."""
    repo = tmp_path / "work"
    init_repo(repo, bare=False)
    commit_file(repo, "README.md", "seed\n", message="initial commit")
    return repo


@pytest.fixture
def linked_clones(tmp_path: Path, bare_remote: Path) -> tuple[Path, Path]:
    """Two non-bare clones of ``bare_remote`` named ``vault_a`` and ``vault_b``.

    Both clones receive a seed commit pushed to the remote so subsequent
    fetches see a non-empty history.
    """
    # Seed: push from vault_a, then clone vault_b.
    vault_a = tmp_path / "vault_a"
    cp_clone_a = run_git(["clone", str(bare_remote), str(vault_a)], tmp_path)
    assert cp_clone_a.returncode == 0, cp_clone_a.stderr
    run_git(["config", "user.email", "engram-test@example.com"], vault_a)
    run_git(["config", "user.name", "engram-test"], vault_a)
    run_git(["config", "commit.gpgsign", "false"], vault_a)
    commit_file(vault_a, "thoughts/.gitkeep", "")
    cp_push = run_git(["push", "-u", "origin", "main"], vault_a)
    assert cp_push.returncode == 0, cp_push.stderr

    vault_b = tmp_path / "vault_b"
    cp_clone_b = run_git(["clone", str(bare_remote), str(vault_b)], tmp_path)
    assert cp_clone_b.returncode == 0, cp_clone_b.stderr
    run_git(["config", "user.email", "engram-test@example.com"], vault_b)
    run_git(["config", "user.name", "engram-test"], vault_b)
    run_git(["config", "commit.gpgsign", "false"], vault_b)

    return (vault_a, vault_b)
