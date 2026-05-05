"""Tests for SyncCoordinator persistent push queue integration.

Step 9 verifier: confirm that when the coordinator is constructed with
a persistent push queue, enqueue writes to disk before landing in the
in-memory queue, and start() drains the persistent queue's contents
into the in-memory queue.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from engram.sync.coordinator import CoordinatorConfig, SyncCoordinator
from engram.team.push_queue import PersistentPushQueue


def _config() -> CoordinatorConfig:
    return CoordinatorConfig(
        debounce_window_seconds=10.0,
        max_deferral_seconds=60.0,
        push_retry_count=0,
        push_timeout_seconds=2.0,
        role="primary",
        auto_commit_on_capture=True,
        auto_push_on_capture=False,
    )


def test_enqueue_with_persistent_queue_writes_to_disk(tmp_path: Path) -> None:
    """When push_queue is configured, enqueue writes to disk first."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    coordinator = SyncCoordinator(
        repo_dir=tmp_path,
        config=_config(),
        push_queue=queue,
    )
    tid = uuid4()
    file_path = tmp_path / "thoughts" / "x.md"
    coordinator.enqueue(file_path, thought_id=str(tid))
    pending = queue.iter_pending()
    assert len(pending) == 1
    assert pending[0].thought_id == str(tid)


def test_enqueue_without_persistent_queue_skips_disk(tmp_path: Path) -> None:
    """When push_queue is None, the on-disk queue file is never created."""
    coordinator = SyncCoordinator(
        repo_dir=tmp_path,
        config=_config(),
    )
    file_path = tmp_path / "thoughts" / "x.md"
    coordinator.enqueue(file_path)
    assert coordinator.queue_depth == 1
    assert not (tmp_path / ".engram" / "push-queue.local").exists()


@pytest.mark.asyncio
async def test_start_replays_persistent_queue(tmp_path: Path) -> None:
    """A prior process's queued pushes drain into the in-memory queue at start."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    queue.enqueue(uuid4(), "thoughts/from-prior-run.md")
    coordinator = SyncCoordinator(
        repo_dir=tmp_path,
        config=_config(),
        push_queue=queue,
    )
    assert coordinator.queue_depth == 0
    await coordinator.start()
    try:
        # The replay drained the persistent queue into the in-memory queue.
        assert coordinator.queue_depth == 1
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_start_without_persistent_queue_is_noop(tmp_path: Path) -> None:
    coordinator = SyncCoordinator(
        repo_dir=tmp_path,
        config=_config(),
    )
    await coordinator.start()
    try:
        assert coordinator.queue_depth == 0
    finally:
        await coordinator.stop()
