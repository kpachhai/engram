r"""Newline-delimited JSON-RPC framing for daemon <-> proxy IPC over UDS.

Each frame is one JSON object terminated by ``\n``. SOCK_STREAM provides
flow control; the only protocol-level concern is the frame-size limit
that prevents an OOM from a buggy or malicious peer.

The frame-size limit defaults to 16 MiB (the per-connection
:class:`asyncio.StreamReader` is constructed with the matching
``limit``) so a buggy or malicious peer cannot OOM the daemon by
streaming an unbounded line.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final, Protocol

#: 16 MiB default; the matching :class:`engram.config.models.DaemonConfig`
#: field is ``max_frame_bytes`` with the same default.
DEFAULT_MAX_FRAME_BYTES: Final[int] = 16 * 1024 * 1024


class FrameTooLargeError(Exception):
    """A frame exceeded ``max_frame_bytes``; caller should close the connection."""


class _SupportsWrite(Protocol):
    def write(self, data: bytes, /) -> None: ...


async def read_frame(
    reader: asyncio.StreamReader,
    *,
    max_frame_bytes: int,
) -> dict[str, Any] | None:
    """Read one newline-delimited JSON-RPC frame.

    Returns the parsed dict, or ``None`` on clean EOF / incomplete frame.
    Raises :class:`FrameTooLargeError` if the frame exceeds
    ``max_frame_bytes``. Raises :class:`json.JSONDecodeError` if the line
    is not valid JSON; the caller is expected to translate to a JSON-RPC
    ``-32700`` parse error on the wire.
    """
    try:
        line = await reader.readuntil(separator=b"\n")
    except asyncio.IncompleteReadError:
        # Connection closed mid-frame; treat as clean EOF.
        return None
    except asyncio.LimitOverrunError as exc:
        msg = f"Frame exceeds max_frame_bytes={max_frame_bytes}: {exc}"
        raise FrameTooLargeError(msg) from exc

    if len(line) > max_frame_bytes:
        msg = f"Frame size {len(line)} exceeds max_frame_bytes={max_frame_bytes}"
        raise FrameTooLargeError(msg)

    decoded = json.loads(line[:-1])  # strip trailing newline
    if not isinstance(decoded, dict):
        msg = f"JSON-RPC frame must be a JSON object, got {type(decoded).__name__}"
        raise ValueError(msg)
    return decoded


async def write_frame(writer: _SupportsWrite, payload: dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC frame.

    Caller awaits ``writer.drain()`` separately when backpressure matters
    (we keep this helper sync-on-write so an in-memory sink works too).
    """
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    writer.write(encoded + b"\n")


__all__ = [
    "DEFAULT_MAX_FRAME_BYTES",
    "FrameTooLargeError",
    "read_frame",
    "write_frame",
]
