"""MCP server layer for engram.

* :mod:`engram.mcp.tools` exposes pure async tool handlers that take a
  :class:`VaultStorage` and :class:`EmbeddingProvider` plus typed input
  models and return typed output models. Unit tests call these directly.

* :mod:`engram.mcp.server` wires the tool handlers to FastMCP for stdio
  protocol. ``engram.cli.serve`` is the entry point that spins this up
  inside the per-vault advisory lock.
"""

from __future__ import annotations

from engram.mcp.server import build_server
from engram.mcp.tools import (
    capture_thought_handler,
    fetch_handler,
    list_thoughts_handler,
    search_thoughts_handler,
    thought_stats_handler,
)

__all__ = [
    "build_server",
    "capture_thought_handler",
    "fetch_handler",
    "list_thoughts_handler",
    "search_thoughts_handler",
    "thought_stats_handler",
]
