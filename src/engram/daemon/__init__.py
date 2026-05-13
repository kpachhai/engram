"""engram daemon mode (Phase 5).

Spec: ``docs/superpowers/specs/2026-05-12-engram-daemon-mode-design.md``.
Plan: ``docs/PHASE_5_PLAN.md``.

This subpackage hosts the daemon server (Layer C), proxy client (Layer D),
spawn-lock + readiness-pipe helpers (Layer B), per-vault path resolution
(Layer A), and the UDS framing protocol (Layer B). Today (Layer A only),
only ``socket_paths`` is populated.
"""

from __future__ import annotations

from engram.daemon.socket_paths import (
    UDS_PATH_LIMIT_BYTES,
    SocketPaths,
    resolve_paths,
)

__all__ = ["UDS_PATH_LIMIT_BYTES", "SocketPaths", "resolve_paths"]
