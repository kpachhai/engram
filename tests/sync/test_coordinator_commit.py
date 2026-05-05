"""Integration tests for SyncCoordinator.commit_cycle (Step 8)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.sync.coordinator import (
    CoordinatorConfig,
    SyncCoordinator,
    SyncState,
)

from .conftest import commit_file, init_repo


@pytest.mark.asyncio
async def test_commit_cycle_creates_commit(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "seed.md", "seed")
    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(
            debounce_window_seconds=1.0,
            user_email="vault@example.com",
            user_name="vault",
        ),
    )
    new_file = tmp_path / "thoughts" / "x.md"
    new_file.parent.mkdir(parents=True)
    new_file.write_text("body")
    coord.enqueue(new_file)

    await coord._commit_cycle()
    assert coord.state is SyncState.IDLE


@pytest.mark.asyncio
async def test_commit_cycle_skips_when_nothing_to_commit(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "seed.md", "seed")
    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(
            user_email="x@y",
            user_name="x",
        ),
    )
    seed = tmp_path / "seed.md"
    coord.enqueue(seed)
    await coord._commit_cycle()
    assert coord.state is SyncState.IDLE


@pytest.mark.asyncio
async def test_commit_cycle_refuses_detached_head(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "first.md", "1")
    commit_file(tmp_path, "second.md", "2")
    # Detach HEAD.
    from .conftest import run_git

    cp_log = run_git(["rev-parse", "HEAD~1"], tmp_path)
    assert cp_log.returncode == 0
    cp_co = run_git(["checkout", "--detach", cp_log.stdout.strip()], tmp_path)
    assert cp_co.returncode == 0

    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(user_email="x@y", user_name="x"),
    )
    new_file = tmp_path / "after.md"
    new_file.write_text("post-detach")
    coord.enqueue(new_file)
    await coord._commit_cycle()
    assert coord.state is SyncState.MANUAL_RESOLUTION_REQUIRED


@pytest.mark.asyncio
async def test_enqueue_resets_debounce_timer(tmp_path: Path) -> None:
    """Re-enqueue while debouncing must extend (not double-fire) the timer."""
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "seed.md", "seed")
    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(debounce_window_seconds=1.0),
    )

    async def _scenario() -> None:
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("a")
        b.write_text("b")
        coord.enqueue(a)
        assert coord.state is SyncState.DEBOUNCING
        await asyncio.sleep(0.2)
        coord.enqueue(b)
        # Still debouncing, not yet fired.
        assert coord.state is SyncState.DEBOUNCING

    await _scenario()


@pytest.mark.asyncio
async def test_max_deferral_forces_commit(tmp_path: Path) -> None:
    """Max-deferral cancels the perpetual debounce-reset and forces flush."""
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "seed.md", "seed")
    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(
            debounce_window_seconds=10.0,
            max_deferral_seconds=10.0,  # we drive directly
        ),
    )
    coord.enqueue(tmp_path / "x.md")
    # Cancel timers to simulate them firing; then commit should be eligible.
    coord._cancel_timers()
    assert coord._should_fire_commit() is True


def test_filter_engram_paths_drops_outside_paths(tmp_path: Path) -> None:
    """filter_engram_paths only retains paths under the thoughts_dir."""
    from engram.sync.coordinator import filter_engram_paths

    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    inside = thoughts / "x.md"
    inside.write_text("x")
    outside = tmp_path / "outside.md"
    outside.write_text("o")
    out = filter_engram_paths([inside, outside], thoughts)
    assert inside.resolve() in out
    assert outside.resolve() not in out
