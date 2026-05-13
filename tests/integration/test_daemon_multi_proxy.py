"""Multi-proxy integration: concurrent UDS connections + MCP initialize handshakes.

These tests bypass DaemonClient and drive the UDS directly so the
assertion surface is on the daemon's per-connection dispatch (the
fastmcp_dispatch shim). DaemonClient is exercised in
``tests/daemon/test_client.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest

from engram.daemon.server import DaemonServer
from engram.daemon.socket_paths import SocketPaths


# Minimal MCP initialize request shape — anything mismatching the
# upstream MCP SDK's expected fields would surface as a JSON-RPC
# error response with the same ``id``, which is fine for assertion
# (the id must round-trip, that is the dispatch-isolation contract).
def _make_initialize(request_id: int | str) -> bytes:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "engram-int-test", "version": "0.5.0"},
                    "capabilities": {},
                },
            }
        )
        + "\n"
    ).encode()


async def _send_then_read_one(
    socket_path: str, payload: bytes, *, timeout: float = 5.0
) -> dict[str, Any] | None:
    """Open UDS, send payload, read one newline-terminated frame back."""
    reader, writer = await asyncio.open_unix_connection(
        socket_path,
        limit=1_048_576,
    )
    try:
        writer.write(payload)
        await writer.drain()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except TimeoutError:
            return None
        if not line:
            return None
        return dict(json.loads(line.decode("utf-8")))
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_initialize_roundtrip_returns_matching_id(
    running_daemon: tuple[DaemonServer, SocketPaths],
) -> None:
    """One proxy: initialize request with id=42 returns a response with id=42."""
    _daemon, paths = running_daemon
    response = await _send_then_read_one(str(paths.socket), _make_initialize(42))
    assert response is not None, "daemon did not respond to initialize"
    assert response["id"] == 42
    # The response carries either ``result`` (success) or ``error`` (rejected
    # input). For dispatch isolation we only need id round-trip; the upstream
    # MCP SDK may or may not happy-path our minimal init params depending on
    # version, and either is fine here.
    assert "result" in response or "error" in response


@pytest.mark.asyncio
async def test_two_concurrent_proxies_distinct_ids_no_crosstalk(
    running_daemon: tuple[DaemonServer, SocketPaths],
) -> None:
    """Two concurrent connections: each gets the response for its own id.

    Bounds the blast radius of a future fastmcp bump that causes response
    cross-talk would fail this assertion loudly.
    """
    _daemon, paths = running_daemon
    proxy_a = _send_then_read_one(str(paths.socket), _make_initialize(101))
    proxy_b = _send_then_read_one(str(paths.socket), _make_initialize(202))
    response_a, response_b = await asyncio.gather(proxy_a, proxy_b)
    assert response_a is not None
    assert response_b is not None
    assert response_a["id"] == 101, response_a
    assert response_b["id"] == 202, response_b


@pytest.mark.asyncio
async def test_five_concurrent_proxies_each_get_own_response(
    running_daemon: tuple[DaemonServer, SocketPaths],
) -> None:
    """N=5 concurrent connections — verifies bandwidth + isolation under load."""
    _daemon, paths = running_daemon
    ids = [11, 22, 33, 44, 55]
    coros = [_send_then_read_one(str(paths.socket), _make_initialize(i)) for i in ids]
    responses = await asyncio.gather(*coros)
    received_ids = sorted(int(r["id"]) for r in responses if r is not None)
    assert received_ids == sorted(ids), responses


@pytest.mark.asyncio
async def test_connected_proxies_counter_tracks_open_connections(
    running_daemon: tuple[DaemonServer, SocketPaths],
) -> None:
    """connected_proxies increments on connect, decrements on disconnect."""
    daemon, paths = running_daemon
    # Open two connections WITHOUT closing them.
    _r1, w1 = await asyncio.open_unix_connection(str(paths.socket))
    _r2, w2 = await asyncio.open_unix_connection(str(paths.socket))
    try:
        await asyncio.sleep(0.05)
        assert daemon.connected_proxies == 2
    finally:
        w1.close()
        w2.close()
        with contextlib.suppress(OSError):
            await w1.wait_closed()
        with contextlib.suppress(OSError):
            await w2.wait_closed()
        # Allow the daemon's accept-loop tasks to observe both disconnects.
        await asyncio.sleep(0.2)
        assert daemon.connected_proxies == 0
