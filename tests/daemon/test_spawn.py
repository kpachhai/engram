"""Spawn-lock acquisition + readiness pipe.

Spec: ``2026-05-12-engram-daemon-mode-design.md`` Section 5.2 step 4 +
Amendment 1 (daemon startup ordering).

``double_fork_detach`` is exercised at the daemon process boundary
(Layer C hermetic CLI smoke); spawn-time forks inside the test process
would orphan PIDs into the pytest runner, so we cover that behavior at
the Layer G smoke level rather than here.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.daemon.socket_paths import resolve_paths
from engram.daemon.spawn import (
    SpawnLockTimeoutError,
    SpawnReadiness,
    acquire_spawn_lock,
    wait_for_ready,
)


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Short-path vault so socket_paths.resolve_paths() succeeds on macOS."""
    with tempfile.TemporaryDirectory(prefix="eng-spawn-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        vault.mkdir()
        yield vault


def test_acquire_spawn_lock_exclusive(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)

    with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=1.0) as locked:
        assert locked is True
        # Second attempt blocks then times out (we hold the lock above).
        with (
            pytest.raises(SpawnLockTimeoutError),
            acquire_spawn_lock(paths.spawn_lock, timeout_seconds=0.3),
        ):
            pass


def test_acquire_spawn_lock_releases_on_exit(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)

    with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=1.0):
        pass  # released at __exit__
    # New acquirer succeeds because the lock was released.
    with acquire_spawn_lock(paths.spawn_lock, timeout_seconds=1.0) as locked:
        assert locked is True


@pytest.mark.asyncio
async def test_wait_for_ready_success() -> None:
    """Simulate the forked daemon writing 'ready\\n' to a pipe."""
    rfd, wfd = os.pipe()
    os.write(wfd, b"ready\n")
    os.close(wfd)
    result = await wait_for_ready(rfd, timeout_seconds=2.0)
    assert result.is_ready
    assert result == SpawnReadiness.ready()


@pytest.mark.asyncio
async def test_wait_for_ready_timeout() -> None:
    rfd, wfd = os.pipe()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await wait_for_ready(rfd, timeout_seconds=0.3)
    finally:
        # wait_for_ready closes rfd internally on timeout via the transport;
        # close wfd here.
        os.close(wfd)


@pytest.mark.asyncio
async def test_wait_for_ready_error_message() -> None:
    rfd, wfd = os.pipe()
    os.write(wfd, b"error: vault locked by pid 12345\n")
    os.close(wfd)
    result = await wait_for_ready(rfd, timeout_seconds=2.0)
    assert result.is_error
    assert "vault locked by pid 12345" in result.message
