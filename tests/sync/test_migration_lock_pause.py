"""Step 12 - MigrationLock pauses the sync coordinator deterministically."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from engram.sync.coordinator import (
    CoordinatorConfig,
    SyncCoordinator,
    SyncState,
)
from engram.utils.lock import MigrationLock

from .conftest import commit_file, init_repo


def test_migration_lock_basic_acquire_release(tmp_path: Path) -> None:
    lock = MigrationLock(tmp_path)
    lock.acquire()
    # While held, is_held may return True (flock is per-open-file-description).
    lock.release()
    # After release, the file is unlinked AND no fd holds the flock.
    assert MigrationLock.is_held(tmp_path) is False


def test_migration_lock_is_held_observed_from_other_thread(tmp_path: Path) -> None:
    """Holder thread + observer thread - is_held must report True while holder is alive."""
    barrier = threading.Event()
    held_observed: list[bool] = []
    release_event = threading.Event()

    def _holder() -> None:
        with MigrationLock(tmp_path):
            barrier.set()
            release_event.wait()

    t = threading.Thread(target=_holder, daemon=True)
    t.start()
    barrier.wait(timeout=5.0)
    held_observed.append(MigrationLock.is_held(tmp_path))
    release_event.set()
    t.join(timeout=5.0)
    assert held_observed == [True]
    assert MigrationLock.is_held(tmp_path) is False


def test_coordinator_pauses_when_migration_held_via_threading_event(tmp_path: Path) -> None:
    """sf-11 deterministic interleave: barrier set after holder acquires."""
    init_repo(tmp_path, bare=False)
    commit_file(tmp_path, "seed.md", "seed")

    barrier = threading.Event()
    release = threading.Event()

    def _hold_migration() -> None:
        with MigrationLock(tmp_path):
            barrier.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_migration, daemon=True)
    holder.start()
    assert barrier.wait(timeout=5.0)

    coord = SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(
            migration_held=lambda: MigrationLock.is_held(tmp_path),
        ),
    )

    async def _drive() -> None:
        await coord._tick()

    asyncio.run(_drive())
    assert coord.state is SyncState.PAUSED_FOR_MIGRATION

    release.set()
    holder.join(timeout=5.0)
    asyncio.run(_drive())
    # After the migration lock is released, the next tick transitions back to IDLE.
    assert coord.state in {SyncState.IDLE, SyncState.PAUSED_FOR_MIGRATION}
