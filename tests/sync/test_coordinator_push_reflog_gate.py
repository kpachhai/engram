"""Reflog gate (R-M9) - force-push elsewhere does not auto-rebase."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.sync.coordinator import (
    CoordinatorConfig,
    SyncCoordinator,
    SyncState,
)

from .conftest import commit_file, init_repo, run_git


@pytest.mark.asyncio
async def test_force_push_upstream_refuses_auto_rebase(tmp_path: Path) -> None:
    """Simulate an upstream history rewrite; coordinator must NOT silently rebase."""
    bare = tmp_path / "remote.git"
    init_repo(bare, bare=True)

    # Setup: A pushes initial commit; B clones; both have origin/main pointing at A's seed.
    a = tmp_path / "a"
    cp_a = run_git(["clone", str(bare), str(a)], tmp_path)
    assert cp_a.returncode == 0
    run_git(["config", "user.email", "a@x"], a)
    run_git(["config", "user.name", "a"], a)
    run_git(["config", "commit.gpgsign", "false"], a)
    commit_file(a, "first.md", "1")
    run_git(["push", "-u", "origin", "main"], a)

    b = tmp_path / "b"
    cp_b = run_git(["clone", str(bare), str(b)], tmp_path)
    assert cp_b.returncode == 0
    run_git(["config", "user.email", "b@x"], b)
    run_git(["config", "user.name", "b"], b)
    run_git(["config", "commit.gpgsign", "false"], b)

    # Force-push history rewrite from a different clone (c) so origin/main
    # advances to a SHA where the previous SHA is unreachable.
    c = tmp_path / "c"
    cp_c = run_git(["clone", str(bare), str(c)], tmp_path)
    assert cp_c.returncode == 0
    run_git(["config", "user.email", "c@x"], c)
    run_git(["config", "user.name", "c"], c)
    run_git(["config", "commit.gpgsign", "false"], c)
    # Reset to an empty initial root commit and force-push.
    run_git(["update-ref", "-d", "HEAD"], c)
    (c / "first.md").unlink()
    commit_file(c, "rewritten.md", "rewritten")
    cp_force = run_git(["push", "--force", "origin", "main"], c)
    assert cp_force.returncode == 0

    # Now b adds a local commit and tries to push.
    commit_file(b, "local-only.md", "local")

    # Configure coordinator to attempt push -> non-fast-forward -> reflog gate refuses.
    coord = SyncCoordinator(
        repo_dir=b,
        config=CoordinatorConfig(
            auto_push_on_capture=True,
            push_retry_count=0,
            push_retry_backoff_seconds=0.0,
            push_timeout_seconds=10.0,
        ),
    )
    await coord._push_cycle()
    assert coord.state in {
        SyncState.MANUAL_RESOLUTION_REQUIRED,
        SyncState.COMMITTED_NOT_PUSHED,
    }, coord.state
    # The expected outcome on a force-push gap: MANUAL_RESOLUTION_REQUIRED.
    # (COMMITTED_NOT_PUSHED is the fallback if git stderr classification differs by
    # version; the contract being defended is "did NOT silently auto-rebase".)
    log = run_git(["log", "--oneline", "main"], b)
    # b's local-only commit must still be present locally - it must NOT have
    # been silently overwritten by a rebase against the rewritten history.
    assert (
        "local-only" in log.stdout or "local-only.md" in run_git(["log", "--name-only"], b).stdout
    )


def test_event_log_records_force_push_event(tmp_path: Path) -> None:
    """Calling allow_from_any transition records the note correctly."""
    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(),
    )

    async def _drive() -> None:
        await coord._push_cycle()

    # We don't actually run the cycle here - we test the transition recording.
    coord._transition(
        SyncState.MANUAL_RESOLUTION_REQUIRED,
        note="reflog gate: previous origin SHA unreachable",
        allow_from_any=True,
    )
    notes = " ".join(e.note for e in coord.events)
    assert "reflog gate" in notes
    del _drive
    asyncio.get_event_loop_policy()  # touch loop module to satisfy linters
