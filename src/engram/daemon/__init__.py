"""engram daemon-mode subpackage.

The daemon listens on a per-vault Unix Domain Socket and accepts N
concurrent ``engram serve`` proxy connections sharing one vault. This
subpackage holds:

- :mod:`engram.daemon.server` — accept loop, per-connection task,
  idle-shutdown timer, graceful drain.
- :mod:`engram.daemon.client` — proxy process: stdio ↔ UDS byte
  shuffler with auto-spawn-on-miss + reconnect backoff.
- :mod:`engram.daemon.spawn` — spawn-lock + readiness-pipe + double-fork.
- :mod:`engram.daemon.protocol` — newline-delimited JSON-RPC framing.
- :mod:`engram.daemon.auth` — ``SO_PEERCRED`` / ``getpeereid``
  same-UID check.
- :mod:`engram.daemon.socket_paths` — per-vault path resolution with
  macOS UDS ``sun_path`` limit enforcement.
- :mod:`engram.daemon.state` — daemon state file (PID + hostname +
  config snapshot).
- :mod:`engram.daemon.log_rotation` — rotating handler with 0o600
  perms + retention sweep.

See ``docs/DAEMON_MODE.md`` for the operator guide and
``docs/adr/008-daemon-mode.md`` for the design rationale.
"""

from __future__ import annotations

from engram.daemon.socket_paths import (
    UDS_PATH_LIMIT_BYTES,
    SocketPaths,
    resolve_paths,
)

__all__ = ["UDS_PATH_LIMIT_BYTES", "SocketPaths", "resolve_paths"]
