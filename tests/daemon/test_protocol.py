"""Newline-delimited JSON-RPC framing helpers.

These tests drive the reader directly via
:meth:`asyncio.StreamReader.feed_data` + ``feed_eof`` — this exercises
exactly the same code path that the daemon's accept loop uses, without
any socket plumbing.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from engram.daemon.protocol import (
    DEFAULT_MAX_FRAME_BYTES,
    FrameTooLargeError,
    read_frame,
    write_frame,
)


def _build_reader(
    payload: bytes, *, limit: int = DEFAULT_MAX_FRAME_BYTES * 2
) -> asyncio.StreamReader:
    """Return a StreamReader pre-loaded with ``payload`` and EOF."""
    reader = asyncio.StreamReader(limit=limit)
    reader.feed_data(payload)
    reader.feed_eof()
    return reader


class _SinkWriter:
    """asyncio.StreamWriter-compatible byte sink for testing write_frame."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, data: bytes) -> None:
        self.buffer.write(data)


@pytest.mark.asyncio
async def test_write_then_read_roundtrip() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    sink = _SinkWriter()
    await write_frame(sink, payload)
    reader = _build_reader(sink.buffer.getvalue())
    decoded = await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)
    assert decoded == payload


@pytest.mark.asyncio
async def test_read_frame_returns_none_on_eof() -> None:
    reader = _build_reader(b"")
    assert await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES) is None


@pytest.mark.asyncio
async def test_read_frame_rejects_oversize() -> None:
    big = {"data": "x" * 200_000}
    payload = json.dumps(big).encode() + b"\n"
    # readuntil raises LimitOverrunError when the StreamReader's own limit
    # is below the frame size. Keep the reader's internal buffer limit
    # generous so we exercise read_frame's own ``max_frame_bytes`` check
    # against the post-decode length too.
    reader = _build_reader(payload, limit=1_000_000)
    with pytest.raises(FrameTooLargeError):
        await read_frame(reader, max_frame_bytes=100_000)


@pytest.mark.asyncio
async def test_read_frame_handles_partial_first() -> None:
    """Daemon dies after writing half a frame; reader observes EOF mid-frame."""
    reader = _build_reader(b'{"jsonrpc":"2.0",')  # no trailing newline
    result = await read_frame(reader, max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)
    assert result is None  # incomplete frame treated as EOF


@pytest.mark.asyncio
async def test_write_frame_terminates_with_newline() -> None:
    sink = _SinkWriter()
    await write_frame(sink, {"a": 1})
    out = sink.buffer.getvalue()
    assert out.endswith(b"\n")
    # No spurious whitespace from json.dumps separators.
    assert out == b'{"a":1}\n'
