"""The reflog gate must fail closed when it has no baseline to compare against.

The gate exists to detect an upstream history rewrite before auto-rebasing and
force-pushing. When the remote-tracking ref cannot be resolved there is no
previous SHA to prove reachability against, so proceeding would auto-rebase onto
an unverified remote head and then force-push the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator, SyncState

from .conftest import commit_file, init_repo, run_git


@pytest.mark.asyncio
async def test_missing_remote_tracking_ref_refuses_auto_rebase(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    ours = tmp_path / "ours"
    assert run_git(["clone", str(bare), str(ours)], tmp_path).returncode == 0
    run_git(["config", "user.email", "b@x"], ours)
    run_git(["config", "user.name", "b"], ours)
    run_git(["config", "commit.gpgsign", "false"], ours)
    commit_file(ours, "seed.md", "seed")
    assert run_git(["push", "-u", "origin", "main"], ours).returncode == 0
    commit_file(ours, "local.md", "local")

    # Drop the remote-tracking ref: the gate now has no baseline SHA.
    run_git(["update-ref", "-d", "refs/remotes/origin/main"], ours)
    assert run_git(["rev-parse", "--verify", "refs/remotes/origin/main"], ours).returncode != 0, (
        "precondition: remote-tracking ref must be absent"
    )

    coord = SyncCoordinator(
        repo_dir=ours,
        config=CoordinatorConfig(
            auto_push_on_capture=True,
            push_retry_count=0,
            push_retry_backoff_seconds=0.0,
            push_timeout_seconds=10.0,
        ),
    )

    outcome = await coord._reflog_gate_and_rebase()

    assert not outcome, "gate must refuse when there is no baseline SHA to verify against"
    assert coord.state is SyncState.MANUAL_RESOLUTION_REQUIRED, coord.state
    notes = " ".join(event.note for event in coord.events)
    assert "baseline" in notes.lower() or "no previous" in notes.lower(), notes
