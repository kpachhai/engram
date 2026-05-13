"""Daemon lifecycle integration: idle, reconnect, peer-cred reject."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import pytest

from engram.config.models import DaemonConfig
from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import resolve_paths
from engram.errors import PeerCredRejectError
from tests.integration.conftest import build_runtime


@pytest.mark.asyncio
async def test_idle_shutdown_fires_after_timeout(short_vault: Path) -> None:
    """Daemon armed with idle_shutdown_seconds=1 exits when no proxies connect."""
    runtime = build_runtime(short_vault)
    daemon = DaemonServer(
        runtime=runtime,
        daemon_config=DaemonConfig(idle_shutdown_seconds=1),
    )
    paths = resolve_paths(short_vault)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)

    # Wait beyond the idle timeout.
    await asyncio.wait_for(server_task, timeout=4.0)
    assert not paths.socket.exists(), "socket should be unlinked after idle shutdown"
    assert not paths.state_file.exists(), "state file should be unlinked"


@pytest.mark.asyncio
async def test_two_phase_atomic_shutdown_cancels_on_reconnect(short_vault: Path) -> None:
    """Connecting during the idle countdown cancels the shutdown.

    The daemon must never close the listener
    while a proxy thinks it just attached.
    """
    runtime = build_runtime(short_vault)
    daemon = DaemonServer(
        runtime=runtime,
        daemon_config=DaemonConfig(idle_shutdown_seconds=2),
    )
    paths = resolve_paths(short_vault)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)

    try:
        # Sleep until partway through the idle window.
        await asyncio.sleep(0.5)
        _r, w = await asyncio.open_unix_connection(str(paths.socket))
        await asyncio.sleep(0.1)
        assert daemon.connected_proxies == 1
        # Wait past where the original idle timer would have fired.
        await asyncio.sleep(2.0)
        assert not server_task.done(), "daemon must still be running after reconnect"
        w.close()
        with contextlib.suppress(OSError):
            await w.wait_closed()
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)


@pytest.mark.asyncio
async def test_peer_cred_reject_rejects_foreign_uid(
    short_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If peer_credentials returns a foreign UID, daemon refuses + bumps the counter.

    The actual SO_PEERCRED / getpeereid kernel mechanism is exercised by
    unit tests in ``tests/daemon/test_auth.py``. Here we patch the check
    to simulate a foreign UID so the daemon's reject path is observable
    in a hermetic test.
    """
    from engram.daemon import server as server_module

    def _always_reject(fd: int) -> None:
        raise PeerCredRejectError(f"peer uid=99999 does not match daemon uid={os.getuid()}")

    monkeypatch.setattr(server_module, "check_peer_or_reject", _always_reject)

    runtime = build_runtime(short_vault)
    daemon = DaemonServer(
        runtime=runtime,
        daemon_config=DaemonConfig(idle_shutdown_seconds=0),
    )
    paths = resolve_paths(short_vault)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)

    try:
        # Open a connection; the daemon's accept-loop closes it
        # immediately and bumps the counter.
        _r, w = await asyncio.open_unix_connection(str(paths.socket))
        await asyncio.sleep(0.15)
        assert daemon.peer_cred_rejects >= 1, "reject counter should have incremented"
        assert daemon.connected_proxies == 0, "rejected connection must not be tracked as live"
        w.close()
        with contextlib.suppress(OSError):
            await w.wait_closed()
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)


@pytest.mark.asyncio
async def test_no_daemon_vs_daemon_mutual_exclusion(short_vault: Path) -> None:
    """A second VaultLock acquire while the daemon holds the lock raises LockError.

    Surface: when an operator runs ``engram serve --no-daemon`` against
    a vault whose daemon is already running, the underlying VaultLock
    contention surfaces as LockError. Daemon mode and no-daemon mode
    are mutually exclusive by construction.
    """
    from engram.errors import LockError
    from engram.utils.lock import VaultLock

    runtime = build_runtime(short_vault)
    daemon = DaemonServer(
        runtime=runtime,
        daemon_config=DaemonConfig(idle_shutdown_seconds=0),
    )
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=10.0)
    try:
        contender = VaultLock(short_vault, install_signal_handlers=False)
        with pytest.raises(LockError):
            contender.acquire()
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)
