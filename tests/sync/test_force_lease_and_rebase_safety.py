"""Push-lease and rebase-target safety against concurrent remote advances.

A bare ``--force-with-lease`` leases against whatever the remote-tracking ref
currently says, so any unrelated fetch between verification and push re-arms
the lease and lets the push clobber commits this machine never saw. Pinning the
lease to the SHA the coordinator actually verified is what makes the lease a
guard rather than a formality.

Likewise, rebasing must land on the SHA that passed the ancestor + signature
gates, not on whatever a second fetch discovers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.sync import gitops
from engram.sync.gitops import GitErrorClass

from .conftest import commit_file, init_repo, run_git


def _head(repo: Path) -> str:
    cp = run_git(["rev-parse", "HEAD"], repo)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


@pytest.mark.asyncio
async def test_lease_pinned_to_verified_sha_refuses_stale_overwrite(tmp_path: Path) -> None:
    """A lease pinned to an older SHA must refuse once the remote has advanced.

    This is the data-loss guard: the remote-tracking ref is deliberately
    refreshed (as a concurrent ``git fetch`` would) so a bare lease would
    happily overwrite the newer remote commit.
    """
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    author = tmp_path / "author"
    cp_clone = run_git(["clone", str(bare), str(author)], tmp_path)
    assert cp_clone.returncode == 0, cp_clone.stderr
    run_git(["config", "user.email", "a@x"], author)
    run_git(["config", "user.name", "a"], author)
    run_git(["config", "commit.gpgsign", "false"], author)
    commit_file(author, "first.md", "1")
    assert run_git(["push", "-u", "origin", "main"], author).returncode == 0
    base_sha = _head(author)

    # Our machine clones at base_sha and builds a local commit on top.
    ours = tmp_path / "ours"
    assert run_git(["clone", str(bare), str(ours)], tmp_path).returncode == 0
    run_git(["config", "user.email", "b@x"], ours)
    run_git(["config", "user.name", "b"], ours)
    run_git(["config", "commit.gpgsign", "false"], ours)
    commit_file(ours, "ours.md", "ours")

    # Another machine pushes a commit we have never seen.
    commit_file(author, "theirs.md", "theirs")
    assert run_git(["push", "origin", "main"], author).returncode == 0
    theirs_sha = _head(author)
    assert theirs_sha != base_sha

    # A concurrent fetch re-arms a bare lease by advancing our tracking ref.
    assert run_git(["fetch", "origin"], ours).returncode == 0

    result = await gitops.push(
        ours,
        "origin",
        "main",
        force_with_lease=True,
        lease_expect=base_sha,
    )

    assert result.error_class is not GitErrorClass.OK, (
        "push must be refused: the lease was pinned to the verified SHA, "
        "but the remote has since advanced"
    )
    # The other machine's commit must still be on the remote.
    cp_remote = run_git(["rev-parse", "refs/heads/main"], bare)
    assert cp_remote.stdout.strip() == theirs_sha, "remote commit was clobbered"


@pytest.mark.asyncio
async def test_rebase_onto_targets_exact_sha_without_refetching(tmp_path: Path) -> None:
    """``rebase_onto`` must rebase onto the SHA it is given, not re-fetch.

    Re-fetching is the TOCTOU hole: the ancestor and signature gates validate
    one remote state, and a second fetch can rebase onto a different one.
    """
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    author = tmp_path / "author"
    assert run_git(["clone", str(bare), str(author)], tmp_path).returncode == 0
    run_git(["config", "user.email", "a@x"], author)
    run_git(["config", "user.name", "a"], author)
    run_git(["config", "commit.gpgsign", "false"], author)
    commit_file(author, "first.md", "1")
    assert run_git(["push", "-u", "origin", "main"], author).returncode == 0

    ours = tmp_path / "ours"
    assert run_git(["clone", str(bare), str(ours)], tmp_path).returncode == 0
    run_git(["config", "user.email", "b@x"], ours)
    run_git(["config", "user.name", "b"], ours)
    run_git(["config", "commit.gpgsign", "false"], ours)
    commit_file(ours, "ours.md", "ours")

    # Remote advances twice; we verify the FIRST of the two.
    commit_file(author, "verified.md", "verified")
    assert run_git(["push", "origin", "main"], author).returncode == 0
    assert run_git(["fetch", "origin"], ours).returncode == 0
    verified_sha = run_git(["rev-parse", "refs/remotes/origin/main"], ours).stdout.strip()

    commit_file(author, "unverified.md", "unverified")
    assert run_git(["push", "origin", "main"], author).returncode == 0
    unverified_sha = _head(author)
    assert verified_sha != unverified_sha

    result = await gitops.rebase_onto(ours, verified_sha)

    assert result.error_class is GitErrorClass.OK, result.stderr
    # The verified commit is an ancestor; the unverified one was never pulled in.
    assert run_git(["merge-base", "--is-ancestor", verified_sha, "HEAD"], ours).returncode == 0
    assert run_git(["merge-base", "--is-ancestor", unverified_sha, "HEAD"], ours).returncode != 0
    assert not (ours / "unverified.md").exists()


@pytest.mark.asyncio
async def test_failed_rebase_leaves_no_rebase_in_progress(tmp_path: Path) -> None:
    """A conflicting rebase must be aborted, not left mid-flight.

    A half-finished rebase leaves conflict markers in the markdown source of
    truth and a detached HEAD while the daemon keeps serving the vault.
    """
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    author = tmp_path / "author"
    assert run_git(["clone", str(bare), str(author)], tmp_path).returncode == 0
    run_git(["config", "user.email", "a@x"], author)
    run_git(["config", "user.name", "a"], author)
    run_git(["config", "commit.gpgsign", "false"], author)
    commit_file(author, "shared.md", "base\n")
    assert run_git(["push", "-u", "origin", "main"], author).returncode == 0

    ours = tmp_path / "ours"
    assert run_git(["clone", str(bare), str(ours)], tmp_path).returncode == 0
    run_git(["config", "user.email", "b@x"], ours)
    run_git(["config", "user.name", "b"], ours)
    run_git(["config", "commit.gpgsign", "false"], ours)

    # Divergent edits to the same line guarantee a rebase conflict.
    commit_file(ours, "shared.md", "ours\n")
    commit_file(author, "shared.md", "theirs\n")
    assert run_git(["push", "origin", "main"], author).returncode == 0
    assert run_git(["fetch", "origin"], ours).returncode == 0
    target = run_git(["rev-parse", "refs/remotes/origin/main"], ours).stdout.strip()

    result = await gitops.rebase_onto(ours, target)

    assert result.error_class is not GitErrorClass.OK, "conflicting rebase should report failure"
    assert not (ours / ".git" / "rebase-merge").exists(), "rebase left in progress"
    assert not (ours / ".git" / "rebase-apply").exists(), "rebase left in progress"
    # HEAD is back on a real branch, not detached mid-rebase.
    cp_branch = run_git(["symbolic-ref", "--quiet", "HEAD"], ours)
    assert cp_branch.returncode == 0, "HEAD left detached after failed rebase"
