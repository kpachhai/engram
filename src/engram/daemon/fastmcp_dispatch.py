"""FastMCP per-connection dispatch shim — Option A from Audit 2.

The daemon (:class:`engram.daemon.server.DaemonServer`) listens on a UDS
and accepts N concurrent proxy connections, each one a logical MCP
session. FastMCP itself only exposes loop-owning entrypoints
(``run_stdio_async`` etc); for per-connection dispatch we reach into
:attr:`fastmcp.FastMCP._mcp_server` (the upstream MCP-SDK
``LowLevelServer``) and drive it with anyio in-memory streams.

This shim is the single point that touches the underscore-prefixed
``_mcp_server`` attribute. A future fastmcp bump that renames the
attribute or changes ``LowLevelServer.run``'s signature breaks here
loudly; ``tests/daemon/test_dispatch_isolation.py`` asserts the
expected contract so the failure mode points directly at this file.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import anyio
from mcp.server.lowlevel.server import (
    NotificationOptions,
)
from mcp.server.lowlevel.server import (
    Server as LowLevelServer,
)
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from engram.daemon.protocol import FrameTooLargeError, read_frame, write_frame

if TYPE_CHECKING:
    from fastmcp import FastMCP


def get_low_level_server(fastmcp_server: FastMCP[Any]) -> LowLevelServer:
    """Return the underlying upstream-MCP ``LowLevelServer``.

    Touches ``FastMCP._mcp_server`` — the single MEDIUM-confidence
    point per the FastMCP audit. All daemon-side access goes through
    this helper so a future fastmcp bump has exactly one place to fix.
    """
    inner = getattr(fastmcp_server, "_mcp_server", None)
    if not isinstance(inner, LowLevelServer):
        msg = (
            "fastmcp_server._mcp_server is not a LowLevelServer — fastmcp may "
            "have changed its internal API. See docs/adr/008-daemon-mode.md "
            "(decision D6) for the upgrade procedure."
        )
        raise TypeError(msg)
    return inner


async def serve_session(  # pragma: no cover - exercised by integration tests
    fastmcp_server: FastMCP[Any],
    *,
    uds_reader: asyncio.StreamReader,
    uds_writer: asyncio.StreamWriter,
    max_frame_bytes: int,
) -> None:
    """Drive one MCP session over a UDS connection.

    Spawns three concurrent tasks:

    * ``pump_in``: read newline-delimited JSON-RPC frames from ``uds_reader``
      and forward them as ``SessionMessage`` instances into an in-memory
      ``MemoryObjectSendStream`` consumed by ``LowLevelServer.run``.
    * ``pump_out``: receive ``SessionMessage`` instances from
      ``LowLevelServer.run`` and serialize each back over ``uds_writer``.
    * ``LowLevelServer.run``: the upstream MCP request/response loop.

    Exits when any task finishes (typically the proxy disconnecting closes
    the UDS read side, which propagates EOF through ``pump_in`` and tears
    down the task group). Closes ``uds_writer`` on exit so the proxy
    observes the FIN.

    Raises :class:`FrameTooLargeError` only when a single frame exceeds
    ``max_frame_bytes``; the caller closes the connection in that case
    after logging.
    """
    low_level = get_low_level_server(fastmcp_server)
    init_options = low_level.create_initialization_options(
        notification_options=NotificationOptions(tools_changed=True),
    )

    # One memory stream pair per connection. LowLevelServer.run consumes
    # ``c2s_recv`` and produces into ``s2c_send``; we pump bytes in/out
    # of those streams to/from the UDS.
    c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage | Exception](64)
    s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage](64)

    async def pump_in() -> None:
        """UDS bytes → SessionMessage into ``c2s_send``.

        On a protocol-level error (oversize frame, malformed JSON, bad
        JSON-RPC shape), close the stream silently. LowLevelServer
        observes EOF on its read side and exits cleanly without trying
        to write a log-message reply that the broken wire cannot carry.
        """
        try:
            while True:
                try:
                    frame = await read_frame(uds_reader, max_frame_bytes=max_frame_bytes)
                except (FrameTooLargeError, ValueError):
                    break
                if frame is None:
                    break  # clean EOF from proxy
                msg = JSONRPCMessage.model_validate(frame)
                await c2s_send.send(SessionMessage(message=msg))
        finally:
            await c2s_send.aclose()

    async def pump_out() -> None:
        """SessionMessage out of ``s2c_recv`` → UDS bytes."""
        try:
            async for session_msg in s2c_recv:
                payload = session_msg.message.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
                if not isinstance(payload, dict):
                    continue
                await write_frame(uds_writer, payload)
                try:
                    await uds_writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            uds_writer.close()
            with contextlib.suppress(OSError, asyncio.CancelledError):
                await uds_writer.wait_closed()

    async def run_low_level() -> None:
        """Drive the upstream MCP session.

        Catches the cleanup-time exception group raised when the
        session's internal task group has streams torn down underneath
        it (e.g. the connection drops while it is trying to emit a
        log notification). These are not real failures — the connection
        is already over by the time they fire.
        """
        try:
            await low_level.run(c2s_recv, s2c_send, init_options)
        except BaseExceptionGroup as group:
            real = [
                exc
                for exc in group.exceptions
                if not isinstance(exc, anyio.ClosedResourceError | BaseExceptionGroup)
            ]
            # Anyio nests groups arbitrarily; inspect leaves too.
            for exc in group.exceptions:
                if isinstance(exc, BaseExceptionGroup):
                    real.extend(
                        e for e in exc.exceptions if not isinstance(e, anyio.ClosedResourceError)
                    )
            if real:
                raise BaseExceptionGroup("daemon session ended with errors", real) from group

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(pump_in)
            tg.start_soon(pump_out)
            tg.start_soon(run_low_level)
    finally:
        # Idempotent close on the writer side guards against an early
        # task-group cancellation that did not run pump_out's ``finally``.
        if not uds_writer.is_closing():
            uds_writer.close()


__all__ = ["get_low_level_server", "serve_session"]
