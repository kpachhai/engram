"""Proxy client: byte shuffler + connect dance + reconnect backoff.

Unit-test scope. End-to-end proxy↔daemon integration (proxy spawns a
real daemon process, sends MCP frames, gets responses) lives in the
hermetic CLI smokes; here we test the helpers with injected streams
and a mock spawn callable.
"""

from __future__ import annotations

import asyncio
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.config.models import DaemonConfig
from engram.daemon.client import (
    _JITTER_MAX_SECONDS,
    _PROXY_RETRY_DELAYS_SECONDS,
    DaemonClient,
    _try_connect,
)
from engram.daemon.socket_paths import resolve_paths
from engram.errors import DaemonConnectionError

# ----- helpers -------------------------------------------------------


class _CapturingWriter:
    """Minimal writer surface that mimics asyncio.StreamWriter for tests."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer.write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _seeded_reader(payload: bytes) -> asyncio.StreamReader:
    """Return an asyncio.StreamReader pre-loaded with ``payload`` + EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Short-path vault so UDS socket fits the 104-byte limit on macOS."""
    with tempfile.TemporaryDirectory(prefix="eng-cli-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        vault.mkdir()
        yield vault


# ----- constants -----------------------------------------------------


def test_retry_schedule_matches_spec() -> None:
    """Spec Section 5.6: 1s, 4s, 16s exponential backoff."""
    assert _PROXY_RETRY_DELAYS_SECONDS == (1.0, 4.0, 16.0)
    assert _JITTER_MAX_SECONDS == 2.0


# ----- _try_connect --------------------------------------------------


@pytest.mark.asyncio
async def test_try_connect_returns_none_for_missing_socket(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    # No daemon ever bound; socket path does not exist.
    assert paths.socket.exists() is False
    result = await _try_connect(paths.socket)
    assert result is None


@pytest.mark.asyncio
async def test_try_connect_succeeds_with_real_listener(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)

    async def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(_accept, path=str(paths.socket))
    try:
        result = await _try_connect(paths.socket)
        assert result is not None
        _reader, writer = result
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


# ----- run_proxy_loop ------------------------------------------------


@pytest.mark.asyncio
async def test_run_proxy_loop_echoes_through_mock_daemon(short_vault: Path) -> None:
    """End-to-end proxy → mock daemon → stdout via the byte shuffler.

    Verifies the bidirectional pump: ``stdin`` payload reaches the
    mock daemon, the mock daemon's echo reaches the proxy's stdout.
    """
    paths = resolve_paths(short_vault)
    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'

    async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.readline()
            if data:
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            with __import__("contextlib").suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(_echo, path=str(paths.socket))
    try:
        stdin = _seeded_reader(payload)
        stdout = _CapturingWriter()
        client = DaemonClient(
            vault_path=short_vault,
            daemon_config=DaemonConfig(),
            stdin_reader=stdin,
            stdout_writer=stdout,
        )
        rc = await asyncio.wait_for(client.run_proxy_loop(), timeout=5.0)
        assert rc == 0
        assert stdout.buffer.getvalue() == payload
    finally:
        server.close()
        await server.wait_closed()


# ----- spawn dance ---------------------------------------------------


@pytest.mark.asyncio
async def test_connect_with_spawn_if_missing_calls_spawn_on_cold(
    short_vault: Path,
) -> None:
    """No socket → spawn callable is invoked once, then second _try_connect succeeds."""
    paths = resolve_paths(short_vault)
    spawn_calls: list[dict[str, object]] = []

    async def _fake_spawn(
        *, vault_path: Path, spawn_timeout_seconds: int, wal_recovery_grace_seconds: int
    ) -> None:
        spawn_calls.append(
            {
                "vault_path": vault_path,
                "spawn_timeout_seconds": spawn_timeout_seconds,
                "wal_recovery_grace_seconds": wal_recovery_grace_seconds,
            }
        )

        # After "spawn" succeeds, start a real listener so the recheck connects.
        async def _accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()
            with __import__("contextlib").suppress(OSError):
                await w.wait_closed()

        server = await asyncio.start_unix_server(_accept, path=str(paths.socket))
        # Store the server on the closure so the test can close it afterwards.
        spawn_calls[-1]["server"] = server

    client = DaemonClient(
        vault_path=short_vault,
        daemon_config=DaemonConfig(),
        spawn_fn=_fake_spawn,
    )
    _reader, writer = await client._connect_with_spawn_if_missing()
    try:
        assert len(spawn_calls) == 1
        assert spawn_calls[0]["vault_path"] == short_vault
    finally:
        writer.close()
        with __import__("contextlib").suppress(OSError):
            await writer.wait_closed()
        server = spawn_calls[0]["server"]
        assert isinstance(server, asyncio.base_events.Server)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_connect_with_spawn_if_missing_skips_spawn_on_warm(
    short_vault: Path,
) -> None:
    """Daemon already listening → spawn callable not invoked."""
    paths = resolve_paths(short_vault)

    async def _accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()
        with __import__("contextlib").suppress(OSError):
            await w.wait_closed()

    server = await asyncio.start_unix_server(_accept, path=str(paths.socket))

    spawn_called = False

    async def _should_not_spawn(**kwargs: object) -> None:
        nonlocal spawn_called
        spawn_called = True

    try:
        client = DaemonClient(
            vault_path=short_vault,
            daemon_config=DaemonConfig(),
            spawn_fn=_should_not_spawn,
        )
        _reader, writer = await client._connect_with_spawn_if_missing()
        writer.close()
        with __import__("contextlib").suppress(OSError):
            await writer.wait_closed()
        assert spawn_called is False
    finally:
        server.close()
        await server.wait_closed()


# ----- reconnect backoff --------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_with_backoff_exhausts_and_raises(short_vault: Path) -> None:
    """Every attempt fails → DaemonConnectionError after 3 retries."""

    async def _always_fail(**kwargs: object) -> None:
        # Simulate spawn returning successfully but the post-spawn connect
        # still fails (socket never appears). DaemonClient surfaces that
        # via DaemonConnectionError at the recheck.
        return None

    # Override the retry schedule with tiny values so the test does not
    # spend ~21 seconds on actual backoff.
    client = DaemonClient(
        vault_path=short_vault,
        daemon_config=DaemonConfig(),
        retry_delays=(0.01, 0.01, 0.01),
        spawn_fn=_always_fail,
    )

    with pytest.raises(DaemonConnectionError) as exc_info:
        await client._reconnect_with_backoff()
    assert "3 retries exhausted" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_reconnect_with_backoff_recovers_on_second_attempt(
    short_vault: Path,
) -> None:
    """First attempt fails, second succeeds → returns the connection."""
    paths = resolve_paths(short_vault)

    attempts = {"n": 0}

    async def _flaky_spawn(**kwargs: object) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return  # leave socket missing → caller's recheck fails

        # Second attempt: actually bind so the recheck succeeds.
        async def _accept(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()
            with __import__("contextlib").suppress(OSError):
                await w.wait_closed()

        await asyncio.start_unix_server(_accept, path=str(paths.socket))

    client = DaemonClient(
        vault_path=short_vault,
        daemon_config=DaemonConfig(),
        retry_delays=(0.01, 0.01, 0.01),
        spawn_fn=_flaky_spawn,
    )
    _reader, writer = await client._reconnect_with_backoff()
    try:
        assert attempts["n"] == 2
    finally:
        writer.close()
        with __import__("contextlib").suppress(OSError):
            await writer.wait_closed()


# ----- mid-session reconnect (the bug that lost MCP tools on daemon restart) -----


@pytest.mark.asyncio
async def test_run_proxy_loop_reconnects_after_daemon_eof(short_vault: Path) -> None:
    """Proxy reconnects when daemon disappears mid-session.

    Regression: prior to the fix the proxy's ``_shuffle_bytes`` returned
    on EITHER stdin EOF or socket EOF without distinguishing which,
    and ``run_proxy_loop`` exited unconditionally. The reconnect helper
    existed as dead code. Result: any daemon restart killed the MCP
    client's tool registry (Claude Code saw its server vanish).

    Test setup: mock daemon accepts a connection, echoes one frame,
    then closes its end. The proxy should reconnect; the mock then
    accepts a second connection. We orchestrate stdin so the proxy
    exits cleanly only after the reconnect has happened.
    """
    import contextlib as _contextlib

    paths = resolve_paths(short_vault)
    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
    accept_count = 0
    reconnect_event = asyncio.Event()

    async def _flaky_daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        if accept_count == 1:
            # First connection: echo one frame then close to simulate
            # the daemon dying mid-session.
            data = await reader.readline()
            if data:
                writer.write(data)
                await writer.drain()
            writer.close()
            with _contextlib.suppress(OSError):
                await writer.wait_closed()
        else:
            # Second connection: tells the test the reconnect happened.
            # Hold the connection open until the test closes stdin so
            # the proxy doesn't exit before we've observed the count.
            reconnect_event.set()
            with _contextlib.suppress(OSError):
                await reader.read()
            writer.close()
            with _contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(_flaky_daemon, path=str(paths.socket))
    try:
        # stdin starts with the payload but NOT at EOF; we feed EOF
        # after the reconnect has been observed so the proxy can exit.
        stdin = asyncio.StreamReader()
        stdin.feed_data(payload)
        stdout = _CapturingWriter()
        client = DaemonClient(
            vault_path=short_vault,
            daemon_config=DaemonConfig(),
            stdin_reader=stdin,
            stdout_writer=stdout,
            retry_delays=(0.01,),  # fast for tests
        )
        proxy_task = asyncio.create_task(client.run_proxy_loop())

        # Wait for the second-connection accept (the reconnect).
        await asyncio.wait_for(reconnect_event.wait(), timeout=5.0)
        assert accept_count == 2

        # Now let the proxy exit cleanly.
        stdin.feed_eof()
        rc = await asyncio.wait_for(proxy_task, timeout=5.0)
        assert rc == 0
        assert stdout.buffer.getvalue() == payload
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_run_proxy_loop_returns_1_when_reconnect_exhausts(
    short_vault: Path,
) -> None:
    """If reconnect backoff is exhausted, the proxy returns 1 (was: 0 on bug)."""
    import contextlib as _contextlib

    paths = resolve_paths(short_vault)
    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'

    async def _one_shot_daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Accept once, echo, close, then become unreachable (the
        # ``finally`` after this handler closes the server below).
        data = await reader.readline()
        if data:
            writer.write(data)
            await writer.drain()
        writer.close()
        with _contextlib.suppress(OSError):
            await writer.wait_closed()

    server = await asyncio.start_unix_server(_one_shot_daemon, path=str(paths.socket))
    accepted_first = asyncio.Event()

    # Close the server after the first connection so reconnect attempts
    # fail (no listener accepting).
    async def _close_after_first() -> None:
        await accepted_first.wait()
        server.close()

    close_task = asyncio.create_task(_close_after_first())
    try:
        stdin = asyncio.StreamReader()
        stdin.feed_data(payload)
        stdout = _CapturingWriter()

        async def _failing_spawn(
            *, vault_path: Path, spawn_timeout_seconds: int, wal_recovery_grace_seconds: int
        ) -> None:
            # Spawn fails: there's nothing listening and we don't
            # actually spawn a daemon in this test.
            from engram.errors import DaemonSpawnError

            msg = "no listener; spawn refused for test"
            raise DaemonSpawnError(msg)

        client = DaemonClient(
            vault_path=short_vault,
            daemon_config=DaemonConfig(),
            stdin_reader=stdin,
            stdout_writer=stdout,
            retry_delays=(0.01, 0.01),  # two fast attempts then give up
            spawn_fn=_failing_spawn,
        )
        proxy_task = asyncio.create_task(client.run_proxy_loop())

        # As soon as the proxy has read the payload and starts shuffling,
        # let the server close so reconnect attempts cannot succeed.
        # Sleep briefly to let the first accept happen.
        await asyncio.sleep(0.2)
        accepted_first.set()
        await close_task

        rc = await asyncio.wait_for(proxy_task, timeout=5.0)
        assert rc == 1, "exhausted reconnect should surface as non-zero exit"
    finally:
        if not server.is_serving():
            pass  # already closed
        else:
            server.close()
        with _contextlib.suppress(Exception):
            await server.wait_closed()
