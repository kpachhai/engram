"""Per-vault path resolution for daemon-mode files.

All daemon-mode artifacts are co-located with the existing ``engram.lock``
under ``<vault>/.indexes/``:

- ``engram.sock``          UDS the daemon binds and listens on
- ``engram.spawn.lock``    flock used to serialize spawn races (brief)
- ``engram.state.json``    PID, started_at, vault_name, hostname, config snapshot
- ``engram.log``           daemon stdout/stderr (rotated)

The resolver enforces the macOS UDS path-length limit (104 bytes for
``sun_path``; Linux is 108) at resolve time so callers fail fast with a
clear remediation hint rather than at ``bind()`` time with a confusing
``OSError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from engram.errors import DaemonError

_INDEXES_SUBDIR = ".indexes"
_SOCKET_FILENAME = "engram.sock"
_SPAWN_LOCK_FILENAME = "engram.spawn.lock"
_STATE_FILENAME = "engram.state.json"
_LOG_FILENAME = "engram.log"

#: macOS ``sun_path`` is 104 bytes including the trailing NUL; Linux is 108.
#: We use the stricter 104 so a vault path that works on Linux also works on
#: macOS.
UDS_PATH_LIMIT_BYTES: Final[int] = 104


@dataclass(frozen=True)
class SocketPaths:
    """Resolved per-vault daemon-mode paths."""

    vault: Path
    indexes_dir: Path
    socket: Path
    spawn_lock: Path
    state_file: Path
    log_file: Path


def resolve_paths(vault: Path) -> SocketPaths:
    """Resolve and validate daemon-mode paths for ``vault``.

    Creates ``<vault>/.indexes/`` if missing so callers can rely on the
    directory existing afterwards. Raises :class:`DaemonError` when the
    resolved socket path exceeds :data:`UDS_PATH_LIMIT_BYTES`.
    """
    vault = Path(vault).expanduser().resolve()
    indexes_dir = vault / _INDEXES_SUBDIR
    indexes_dir.mkdir(parents=True, exist_ok=True)
    socket = (indexes_dir / _SOCKET_FILENAME).resolve()
    spawn_lock = (indexes_dir / _SPAWN_LOCK_FILENAME).resolve()
    state_file = (indexes_dir / _STATE_FILENAME).resolve()
    log_file = (indexes_dir / _LOG_FILENAME).resolve()

    # The kernel measures ``sun_path`` in bytes including the trailing NUL.
    # We compare against the stricter macOS limit (104) for cross-platform
    # safety. The four sibling paths share a prefix; the socket is checked
    # because its name is the shortest, so if it fits, the others fit.
    socket_bytes = str(socket).encode("utf-8")
    if len(socket_bytes) >= UDS_PATH_LIMIT_BYTES:
        msg = (
            f"UDS path too long for macOS (max {UDS_PATH_LIMIT_BYTES} bytes): "
            f"{socket} ({len(socket_bytes)} bytes). "
            f"Workaround: symlink your vault dir into ~/.engram-vaults/<short-name>/"
        )
        raise DaemonError(msg)

    return SocketPaths(
        vault=vault,
        indexes_dir=indexes_dir,
        socket=socket,
        spawn_lock=spawn_lock,
        state_file=state_file,
        log_file=log_file,
    )


__all__ = ["UDS_PATH_LIMIT_BYTES", "SocketPaths", "resolve_paths"]
