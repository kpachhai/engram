"""Daemon-mode doctor checks.

Each ``check_*`` function inspects a vault's daemon-mode artifacts
(``engram.sock``, ``engram.state.json``, ``engram.log``) and returns
exactly one :class:`DaemonDoctorRow` so ``engram doctor`` can fold
the rows into its output.

The check codes themselves are declared in
:mod:`engram.diagnostics.check_codes`.
"""

from __future__ import annotations

import contextlib
import os
import socket as socket_module
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from engram.daemon.socket_paths import (
    _INDEXES_SUBDIR,
    UDS_PATH_LIMIT_BYTES,
    resolve_paths,
)
from engram.daemon.state import read_state
from engram.diagnostics.check_codes import (
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_RUNNING,
    DAEMON_SOCKET_PATH_TOO_LONG,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_UPTIME_EXCESSIVE,
)
from engram.errors import DaemonError

DaemonDoctorStatus = Literal["OK", "INFO", "WARN", "FAIL"]


@dataclass(frozen=True)
class DaemonDoctorRow:
    """One row in the daemon portion of the doctor report.

    Mirrors :class:`engram.diagnostics.phase4_checks.Phase4DoctorRow`
    so the top-level doctor command can fold all rows into a uniform
    output.
    """

    code: str
    status: DaemonDoctorStatus
    detail: str


def check_daemon_running(vault_path: Path) -> DaemonDoctorRow:
    """INFO row: is a daemon running for this vault?

    The check is best-effort: it tries a non-blocking UDS connect.
    Failure to connect (file missing, refused, timeout) is reported
    as ``INFO not-running`` — not-running is a normal state, not an
    error.
    """
    paths = resolve_paths(vault_path)
    if not paths.socket.exists():
        return DaemonDoctorRow(
            code=DAEMON_RUNNING,
            status="INFO",
            detail=(
                f"Daemon not running for vault {paths.vault.name}. Run "
                f"`engram daemon start` or open a Claude session "
                f"(`engram serve` auto-spawns by default)."
            ),
        )

    s = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(paths.socket))
    except OSError:
        return DaemonDoctorRow(
            code=DAEMON_RUNNING,
            status="INFO",
            detail=(
                f"Socket file present at {paths.socket} but no daemon "
                f"is listening; consider `engram daemon start`."
            ),
        )
    finally:
        with contextlib.suppress(OSError):
            s.close()

    state = read_state(paths.state_file)
    if state is None:
        return DaemonDoctorRow(
            code=DAEMON_RUNNING,
            status="INFO",
            detail=(
                f"Daemon listening on {paths.socket} but state file "
                f"missing; the daemon may be mid-startup."
            ),
        )
    return DaemonDoctorRow(
        code=DAEMON_RUNNING,
        status="INFO",
        detail=f"Daemon running (PID {state.pid}, started {state.started_at})",
    )


def check_daemon_socket_permissions(vault_path: Path) -> DaemonDoctorRow:
    """WARN when the socket file is not mode 0o600 or not owned by this UID."""
    paths = resolve_paths(vault_path)
    if not paths.socket.exists():
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_PERMISSIONS,
            status="OK",
            detail="no daemon socket present (nothing to check)",
        )
    st = paths.socket.stat()
    mode = st.st_mode & 0o777
    if mode != 0o600:
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_PERMISSIONS,
            status="WARN",
            detail=(
                f"Socket permissions are {oct(mode)} (expected 0o600). "
                f"Run `engram daemon stop && engram daemon start` to "
                f"recreate the socket with correct perms."
            ),
        )
    if st.st_uid != os.getuid():
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_PERMISSIONS,
            status="WARN",
            detail=(
                f"Socket owner uid={st.st_uid} differs from current "
                f"uid={os.getuid()}; this may indicate tampering."
            ),
        )
    return DaemonDoctorRow(
        code=DAEMON_SOCKET_PERMISSIONS,
        status="OK",
        detail="socket mode 0o600, owned by current UID",
    )


def check_daemon_socket_stale(vault_path: Path) -> DaemonDoctorRow:
    """WARN when a socket file exists but no listener responds."""
    paths = resolve_paths(vault_path)
    if not paths.socket.exists():
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_STALE,
            status="OK",
            detail="no socket file present (nothing to clean up)",
        )

    s = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(paths.socket))
    except OSError:
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_STALE,
            status="WARN",
            detail=(
                f"Stale socket file at {paths.socket} from a crashed "
                f"daemon; `engram daemon start` will unlink + respawn."
            ),
        )
    else:
        with contextlib.suppress(OSError):
            s.close()
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_STALE,
            status="OK",
            detail="socket file backed by a live daemon",
        )


def check_daemon_log_rotation_healthy(
    vault_path: Path,
    *,
    max_size_mb: int = 100,
) -> DaemonDoctorRow:
    """WARN when the log file exceeds the rotation threshold and is over 24h old."""
    paths = resolve_paths(vault_path)
    if not paths.log_file.exists():
        return DaemonDoctorRow(
            code=DAEMON_LOG_ROTATION_HEALTHY,
            status="OK",
            detail="no log file present",
        )
    st = paths.log_file.stat()
    size_mb = st.st_size / (1024 * 1024)
    if size_mb <= max_size_mb:
        return DaemonDoctorRow(
            code=DAEMON_LOG_ROTATION_HEALTHY,
            status="OK",
            detail=f"log file {size_mb:.1f} MB (under threshold {max_size_mb} MB)",
        )
    age_hours = (time.time() - st.st_mtime) / 3600
    if age_hours > 24:
        return DaemonDoctorRow(
            code=DAEMON_LOG_ROTATION_HEALTHY,
            status="WARN",
            detail=(
                f"Log file is {size_mb:.1f} MB and has not rotated in "
                f"{age_hours:.0f}h; check `daemon.log_max_size_mb` + "
                f"`daemon.log_retention_days`."
            ),
        )
    return DaemonDoctorRow(
        code=DAEMON_LOG_ROTATION_HEALTHY,
        status="INFO",
        detail=(f"log file {size_mb:.1f} MB; will rotate at next write past {max_size_mb} MB."),
    )


def check_daemon_uptime_excessive(vault_path: Path) -> DaemonDoctorRow:
    """INFO when daemon uptime exceeds 7 days (suggest a restart)."""
    paths = resolve_paths(vault_path)
    state = read_state(paths.state_file)
    if state is None:
        return DaemonDoctorRow(
            code=DAEMON_UPTIME_EXCESSIVE,
            status="OK",
            detail="no daemon state file (daemon not running)",
        )
    try:
        started = datetime.fromisoformat(state.started_at)
    except ValueError:
        return DaemonDoctorRow(
            code=DAEMON_UPTIME_EXCESSIVE,
            status="WARN",
            detail=f"daemon state.json has malformed started_at: {state.started_at}",
        )
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    uptime_days = (datetime.now(UTC) - started).total_seconds() / 86400
    if uptime_days > 7:
        return DaemonDoctorRow(
            code=DAEMON_UPTIME_EXCESSIVE,
            status="INFO",
            detail=(
                f"Daemon uptime is {uptime_days:.1f} days; consider "
                f"`engram daemon stop && engram daemon start` to pick "
                f"up recent updates and reset memory state."
            ),
        )
    return DaemonDoctorRow(
        code=DAEMON_UPTIME_EXCESSIVE,
        status="OK",
        detail=f"daemon uptime {uptime_days:.2f} days",
    )


def check_daemon_socket_path_too_long(vault_path: Path) -> DaemonDoctorRow:
    """WARN when the prospective UDS socket path exceeds 104 bytes (macOS limit).

    Calls :func:`resolve_paths` which raises :class:`DaemonError` for
    long paths; we convert that to a WARN row so the operator sees the
    remediation hint rather than a stack trace.
    """
    try:
        resolve_paths(vault_path)
    except DaemonError as exc:
        return DaemonDoctorRow(
            code=DAEMON_SOCKET_PATH_TOO_LONG,
            status="WARN",
            detail=str(exc),
        )

    # Even when resolve_paths succeeds, surface the byte count so the
    # operator can see how much headroom they have.
    socket_path = Path(vault_path).expanduser().resolve() / _INDEXES_SUBDIR / "engram.sock"
    used = len(str(socket_path).encode("utf-8"))
    return DaemonDoctorRow(
        code=DAEMON_SOCKET_PATH_TOO_LONG,
        status="OK",
        detail=f"UDS path uses {used}/{UDS_PATH_LIMIT_BYTES} bytes",
    )


__all__ = [
    "DaemonDoctorRow",
    "DaemonDoctorStatus",
    "check_daemon_log_rotation_healthy",
    "check_daemon_running",
    "check_daemon_socket_path_too_long",
    "check_daemon_socket_permissions",
    "check_daemon_socket_stale",
    "check_daemon_uptime_excessive",
]
