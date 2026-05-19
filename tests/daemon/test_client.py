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
    _FrameSnooper,
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
    """If reconnect backoff is exhausted, the proxy returns 1 (was: 0 on bug).

    Closes the listener synchronously inside the connection handler — once
    the first connection finishes echoing, the server stops accepting so
    every subsequent proxy reconnect attempt fails (no listener + spawn
    refuses). This is causally ordered with the proxy's first roundtrip,
    so it does not race against ``_PROXY_RETRY_DELAYS_SECONDS[0]`` + the
    up-to-2s jitter on attempt #1 — a sleep-based gate did (see git
    history: CI flake on macOS arm).
    """
    import contextlib as _contextlib

    paths = resolve_paths(short_vault)
    payload = b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'

    server: asyncio.base_events.Server | None = None

    async def _one_shot_daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Accept once, echo, close, then stop the listener so subsequent
        # reconnect attempts fail.
        data = await reader.readline()
        if data:
            writer.write(data)
            await writer.drain()
        writer.close()
        with _contextlib.suppress(OSError):
            await writer.wait_closed()
        if server is not None:
            server.close()

    server = await asyncio.start_unix_server(_one_shot_daemon, path=str(paths.socket))
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

        rc = await asyncio.wait_for(proxy_task, timeout=10.0)
        assert rc == 1, "exhausted reconnect should surface as non-zero exit"
    finally:
        if server is not None and server.is_serving():
            server.close()
        if server is not None:
            with _contextlib.suppress(Exception):
                await server.wait_closed()


# ----- MCP frame snooper (unit) --------------------------------------


def test_snooper_captures_initialize_frame() -> None:
    """A single-line ``initialize`` frame is cached verbatim (newline included)."""
    snooper = _FrameSnooper()
    frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    snooper.feed(frame)
    assert snooper.initialize_frame == frame


def test_snooper_captures_notifications_initialized() -> None:
    """``notifications/initialized`` is cached alongside initialize."""
    snooper = _FrameSnooper()
    frame = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    snooper.feed(frame)
    assert snooper.initialized_frame == frame


def test_snooper_overwrites_on_resent_initialize() -> None:
    """Client may re-send initialize on a transport recovery; cache the latest."""
    snooper = _FrameSnooper()
    first = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"a":1}}\n'
    second = b'{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"a":2}}\n'
    snooper.feed(first)
    snooper.feed(second)
    assert snooper.initialize_frame == second


def test_snooper_ignores_non_protocol_methods() -> None:
    """Regular MCP requests (tools/list, tools/call, ...) are not cached."""
    snooper = _FrameSnooper()
    frame = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    snooper.feed(frame)
    assert snooper.initialize_frame is None
    assert snooper.initialized_frame is None


def test_snooper_handles_chunked_partial_frame() -> None:
    """A frame split across two ``feed`` calls is reassembled at the newline."""
    snooper = _FrameSnooper()
    snooper.feed(b'{"jsonrpc":"2.0","id":1,"method":"initial')
    assert snooper.initialize_frame is None  # not complete yet
    snooper.feed(b'ize","params":{}}\n')
    expected = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    assert snooper.initialize_frame == expected


def test_snooper_handles_multiple_frames_in_one_chunk() -> None:
    """Two frames delivered in a single ``feed`` are split on newline."""
    snooper = _FrameSnooper()
    other = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    init = b'{"jsonrpc":"2.0","id":2,"method":"initialize"}\n'
    snooper.feed(other + init)
    assert snooper.initialize_frame == init


def test_snooper_skips_invalid_json() -> None:
    """Malformed JSON does not crash the snooper or leak into the cache."""
    snooper = _FrameSnooper()
    snooper.feed(b"not json at all\n")
    snooper.feed(b"{not valid either}\n")
    assert snooper.initialize_frame is None
    assert snooper.initialized_frame is None


def test_snooper_drops_buffer_past_cap() -> None:
    """Pathological no-newline stream is bounded; cache stays empty + buffer drops."""
    snooper = _FrameSnooper(max_buffer_bytes=128)
    # 200 bytes with no newline: exceeds cap.
    snooper.feed(b"a" * 200)
    assert snooper.initialize_frame is None
    # Subsequent valid frame still works after the buffer drop.
    frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    snooper.feed(frame)
    assert snooper.initialize_frame == frame


# ----- MCP-aware proxy: replay-on-reconnect (integration) ------------


@pytest.mark.asyncio
async def test_proxy_replays_initialize_on_reconnect(short_vault: Path) -> None:
    """Across daemon restart, proxy transparently replays cached MCP session state.

    Setup: client sends initialize + notifications/initialized + a tools/list.
    First daemon connection echoes the init response, then drops. The proxy
    reconnects to a fresh daemon. The fresh daemon would normally reject
    tools/list with -32602; the proxy must replay initialize first.

    Assertions:
    - Second daemon connection sees the replayed initialize and initialized.
    - The daemon's response to the REPLAYED initialize is swallowed by the
      proxy (Claude already saw the original; a duplicate id=1 response
      would break the MCP client).
    """
    import contextlib as _contextlib

    paths = resolve_paths(short_vault)
    initialize_frame = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
    initialized_frame = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    tools_list_frame = b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    init_response = b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n'
    tools_response = b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}\n'

    accept_count = 0
    frames_per_connection: list[list[bytes]] = []
    second_connection_done = asyncio.Event()

    async def _flaky_daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        frames: list[bytes] = []
        frames_per_connection.append(frames)
        if accept_count == 1:
            # Receive initialize, respond, receive initialized, then drop.
            frames.append(await reader.readline())
            writer.write(init_response)
            await writer.drain()
            frames.append(await reader.readline())
            writer.close()
            with _contextlib.suppress(OSError):
                await writer.wait_closed()
        else:
            # Should receive REPLAYED initialize first; respond (proxy
            # must swallow this). Then initialized. Then the real
            # tools/list, which we answer.
            frames.append(await reader.readline())
            writer.write(init_response)
            await writer.drain()
            frames.append(await reader.readline())
            frames.append(await reader.readline())
            writer.write(tools_response)
            await writer.drain()
            second_connection_done.set()
            # Hold open so the proxy can pump until stdin EOFs.
            with _contextlib.suppress(OSError):
                await reader.read()
            writer.close()
            with _contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(_flaky_daemon, path=str(paths.socket))
    try:
        stdin = asyncio.StreamReader()
        stdin.feed_data(initialize_frame + initialized_frame)
        stdout = _CapturingWriter()
        client = DaemonClient(
            vault_path=short_vault,
            daemon_config=DaemonConfig(),
            stdin_reader=stdin,
            stdout_writer=stdout,
            retry_delays=(0.01,),
        )
        proxy_task = asyncio.create_task(client.run_proxy_loop())

        # Give time for first connection's init exchange + drop.
        await asyncio.sleep(0.3)
        # Now send tools/list; it should land on the SECOND (post-reconnect) daemon.
        stdin.feed_data(tools_list_frame)

        await asyncio.wait_for(second_connection_done.wait(), timeout=5.0)
        stdin.feed_eof()
        rc = await asyncio.wait_for(proxy_task, timeout=5.0)
        assert rc == 0

        # First daemon saw initialize + initialized in order.
        assert frames_per_connection[0][0] == initialize_frame
        assert frames_per_connection[0][1] == initialized_frame

        # Second daemon saw the SAME initialize + initialized (replayed by proxy),
        # then the real tools/list request.
        assert frames_per_connection[1][0] == initialize_frame
        assert frames_per_connection[1][1] == initialized_frame
        assert frames_per_connection[1][2] == tools_list_frame

        # Critical: stdout must contain init_response ONCE (from the first daemon).
        # The second daemon's init_response was discarded by the proxy.
        assert stdout.buffer.getvalue() == init_response + tools_response
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_proxy_caches_latest_initialize_if_client_resends(
    short_vault: Path,
) -> None:
    """When client re-sends initialize, the cached frame updates to the latest.

    Realistic case: client recovers from a transport blip and re-initializes.
    The proxy should replay the *most recent* initialize, not the first.
    """
    import contextlib as _contextlib

    paths = resolve_paths(short_vault)
    first_init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"v":1}}\n'
    second_init = b'{"jsonrpc":"2.0","id":2,"method":"initialize","params":{"v":2}}\n'
    init_response = b'{"jsonrpc":"2.0","id":2,"result":{"capabilities":{}}}\n'

    accept_count = 0
    frames_per_connection: list[list[bytes]] = []
    second_done = asyncio.Event()

    async def _flaky_daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accept_count
        accept_count += 1
        frames: list[bytes] = []
        frames_per_connection.append(frames)
        if accept_count == 1:
            # Receive both initializes, respond once, then drop.
            frames.append(await reader.readline())
            frames.append(await reader.readline())
            writer.write(init_response)
            await writer.drain()
            writer.close()
            with _contextlib.suppress(OSError):
                await writer.wait_closed()
        else:
            # Expect the LATEST initialize replayed.
            frames.append(await reader.readline())
            writer.write(init_response)
            await writer.drain()
            second_done.set()
            with _contextlib.suppress(OSError):
                await reader.read()
            writer.close()
            with _contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(_flaky_daemon, path=str(paths.socket))
    try:
        stdin = asyncio.StreamReader()
        stdin.feed_data(first_init + second_init)
        stdout = _CapturingWriter()
        client = DaemonClient(
            vault_path=short_vault,
            daemon_config=DaemonConfig(),
            stdin_reader=stdin,
            stdout_writer=stdout,
            retry_delays=(0.01,),
        )
        proxy_task = asyncio.create_task(client.run_proxy_loop())

        await asyncio.wait_for(second_done.wait(), timeout=5.0)
        stdin.feed_eof()
        rc = await asyncio.wait_for(proxy_task, timeout=5.0)
        assert rc == 0

        # Second daemon should have received the SECOND init (latest), not the first.
        assert frames_per_connection[1][0] == second_init
    finally:
        server.close()
        await server.wait_closed()
