"""DaemonServer accept loop + idle shutdown + drain.

Unit-test scope only. The full MCP-handshake-against-real-FastMCP
roundtrip is covered by the integration tests in
``tests/integration/test_daemon_multi_proxy.py`` and the hermetic CLI
smoke in ``tests/test_phase5_cli_smoke.py``.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from engram.cli.serve import ServeRuntime
from engram.config.models import (
    AggregatorConfig,
    DaemonConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import resolve_paths
from engram.mcp.server import build_server
from engram.storage.facade import VaultStorage
from engram.utils.lock import VaultLock


class _FakeEmbedder:
    """Hermetic embedder for daemon tests; mirrors the pattern in serve_multivault tests."""

    dimension: int = 16
    model_name: str = "BAAI/bge-small-en-v1.5"

    def embed(self, text: str) -> list[float]:
        del text
        v = [0.0] * self.dimension
        v[0] = 1.0
        return v

    async def aembed(self, text: str) -> list[float]:
        return self.embed(text)

    def warmup(self) -> None:
        pass

    def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Short-path vault so UDS socket fits the 104-byte limit on macOS."""
    with tempfile.TemporaryDirectory(prefix="eng-srv-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / ".indexes").mkdir(parents=True)
        yield vault


def _build_runtime(vault: Path) -> ServeRuntime:
    """Construct a hermetic ServeRuntime: real storage + FakeEmbedder + real FastMCP."""
    embedder = _FakeEmbedder()
    storage = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        embedding_dim=embedder.dimension,
        embedding_model_name=embedder.model_name,
        vault_name="test",
    )
    vault_lock = VaultLock(vault, install_signal_handlers=False)
    vault_lock.acquire()
    fastmcp_server = build_server(
        storage,
        embedder,
        default_user="testuser",
        server_name="engram",
    )
    config = EffectiveConfig(
        default_user="testuser",
        vault_path=vault,
        thoughts_dir=vault / "thoughts",
        index_dir=vault / ".indexes",
        embedding_model=embedder.model_name,
        vault_name="test",
        sync=SyncConfig(),
        llm=LLMConfig(),
        aggregator=AggregatorConfig(),
    )
    return ServeRuntime(
        config=config,
        vault_lock=vault_lock,
        storage=storage,
        coordinator=None,
        embedder=embedder,
        fastmcp_server=fastmcp_server,
    )


async def _start_daemon(
    vault: Path, daemon_config: DaemonConfig
) -> tuple[DaemonServer, asyncio.Task[None]]:
    """Launch a daemon in a task; return (daemon, task) after readiness."""
    runtime = _build_runtime(vault)
    daemon = DaemonServer(runtime=runtime, daemon_config=daemon_config)
    server_task = asyncio.create_task(daemon.serve_forever())
    await daemon.wait_until_ready(timeout=5.0)
    return daemon, server_task


@pytest.mark.asyncio
async def test_serve_forever_binds_socket_and_writes_state(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    daemon, server_task = await _start_daemon(short_vault, DaemonConfig(idle_shutdown_seconds=0))
    try:
        assert paths.socket.exists(), "UDS socket should be present after readiness"
        mode = paths.socket.stat().st_mode & 0o777
        assert mode == 0o600, f"socket should be 0o600 perms, got {oct(mode)}"
        assert paths.state_file.exists(), "state.json should be present after readiness"
        snapshot = json.loads(paths.state_file.read_text())
        assert snapshot["vault_name"] == "test"
        assert snapshot["pid"] == __import__("os").getpid()
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)
        assert not paths.socket.exists()
        assert not paths.state_file.exists()


@pytest.mark.asyncio
async def test_uds_connection_accepted_and_tracked(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    daemon, server_task = await _start_daemon(short_vault, DaemonConfig(idle_shutdown_seconds=0))
    try:
        _reader, writer = await asyncio.open_unix_connection(str(paths.socket))
        # Connection accepted; peer-cred check passes (same UID).
        # Wait briefly for the daemon's accept-loop task to register the
        # connection in its counter.
        await asyncio.sleep(0.05)
        assert daemon.connected_proxies == 1
        writer.close()
        await writer.wait_closed()
        # Give the daemon a tick to register the disconnect.
        await asyncio.sleep(0.1)
        assert daemon.connected_proxies == 0
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)


@pytest.mark.asyncio
async def test_idle_shutdown_after_last_proxy_disconnects(short_vault: Path) -> None:
    """Daemon exits after idle_shutdown_seconds with no connections."""
    paths = resolve_paths(short_vault)
    # 0.5s idle so the test completes quickly.
    _daemon, server_task = await _start_daemon(
        short_vault,
        DaemonConfig(idle_shutdown_seconds=1),
    )
    try:
        # The daemon armed an idle timer on startup since 0 proxies are
        # connected; wait for it to fire + listener to close + drain to run.
        await asyncio.wait_for(server_task, timeout=4.0)
    finally:
        # serve_forever returned; assert cleanup happened.
        assert not paths.socket.exists()
        assert not paths.state_file.exists()


@pytest.mark.asyncio
async def test_oversize_frame_closes_connection(short_vault: Path) -> None:
    """A frame > max_frame_bytes triggers a disconnect on the daemon side.

    Once the daemon closes, the proxy side may observe either a clean
    EOF (``reader.read()`` returns ``b""``) or a ``BrokenPipeError`` /
    ``ConnectionResetError`` depending on whether the proxy's
    ``writer.drain()`` was mid-flight when the daemon closed. Either
    outcome proves the daemon refused the frame.
    """
    import contextlib

    paths = resolve_paths(short_vault)
    daemon, server_task = await _start_daemon(
        short_vault,
        DaemonConfig(idle_shutdown_seconds=0, max_frame_bytes=65536),
    )
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(paths.socket),
            limit=1_000_000,
        )
        # ``reader`` is used to detect the daemon's close below; suppress
        # ruff's unused-var heuristic via direct usage.
        # Send 200 KB — exceeds the 64 KB cap.
        big = json.dumps({"data": "x" * 200_000}).encode() + b"\n"
        writer.write(big)
        connection_torn_down = False
        with contextlib.suppress(ConnectionResetError, BrokenPipeError):
            await writer.drain()
        try:
            tail = await asyncio.wait_for(reader.read(), timeout=3.0)
        except (BrokenPipeError, ConnectionResetError):
            connection_torn_down = True
        else:
            connection_torn_down = tail == b""
        assert connection_torn_down, "daemon should have closed the oversize-frame connection"
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)


@pytest.mark.asyncio
async def test_request_shutdown_returns_serve_forever(short_vault: Path) -> None:
    """request_shutdown() causes serve_forever() to return cleanly."""
    daemon, server_task = await _start_daemon(short_vault, DaemonConfig(idle_shutdown_seconds=0))
    daemon.request_shutdown()
    await asyncio.wait_for(server_task, timeout=5.0)


@pytest.mark.asyncio
async def test_two_phase_idle_shutdown_cancelled_on_reconnect(short_vault: Path) -> None:
    """Connecting between idle-timer fire and listener-close cancels the shutdown.

    Strategy: start daemon with idle=2s; wait 0.5s (timer is armed); connect a
    proxy (cancels the timer); verify daemon is still alive; disconnect;
    request explicit shutdown to end the test.
    """
    paths = resolve_paths(short_vault)
    daemon, server_task = await _start_daemon(short_vault, DaemonConfig(idle_shutdown_seconds=2))
    try:
        await asyncio.sleep(0.5)
        _reader, writer = await asyncio.open_unix_connection(str(paths.socket))
        await asyncio.sleep(0.1)
        assert daemon.connected_proxies == 1, "proxy should be tracked"
        # Wait past where the original idle would have fired.
        await asyncio.sleep(2.0)
        assert not server_task.done(), "daemon should still be running"
        writer.close()
        with __import__("contextlib").suppress(OSError):
            await writer.wait_closed()
    finally:
        daemon.request_shutdown()
        await asyncio.wait_for(server_task, timeout=5.0)
