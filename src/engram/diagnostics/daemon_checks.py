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
from typing import TYPE_CHECKING, Literal

from engram.daemon.socket_paths import (
    _INDEXES_SUBDIR,
    UDS_PATH_LIMIT_BYTES,
    SocketPaths,
    resolve_paths,
)
from engram.daemon.state import read_state
from engram.diagnostics.check_codes import (
    DAEMON_CONFIG_DRIFTED,
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_RUNNING,
    DAEMON_SOCKET_PATH_TOO_LONG,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_UPTIME_EXCESSIVE,
    DAEMON_VERSION_MATCHES_CLI,
)
from engram.errors import DaemonError

if TYPE_CHECKING:
    from engram.config.models import EffectiveConfig
    from engram.diagnostics.doctor import DoctorReport

#: ``SKIP`` is distinct from ``OK``: a row whose precondition is absent
#: answered nothing, and a report that renders the two alike lets "did not
#: run" read as "passed".
DaemonDoctorStatus = Literal["OK", "INFO", "SKIP", "WARN", "FAIL"]

#: What the socket says about a daemon right now. The state file cannot
#: answer this: :func:`read_state` returns ``None`` for a missing file, an
#: unparseable one and a schema-drifted one alike, so "no state" is not
#: evidence that nothing is running.
DaemonLiveness = Literal["no_socket", "not_listening", "listening"]


def _probe_daemon(socket_path: Path, *, timeout: float = 1.0) -> DaemonLiveness:
    """Non-blocking UDS connect: is a daemon serving on ``socket_path``?

    One predicate for every row that needs the answer, so the report cannot
    say a daemon is listening on one line and not running on the next.
    """
    if not socket_path.exists():
        return "no_socket"
    s = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path))
    except OSError:
        return "not_listening"
    finally:
        with contextlib.suppress(OSError):
            s.close()
    return "listening"


@dataclass(frozen=True)
class DaemonDoctorRow:
    """One row in the daemon portion of the doctor report.

    Mirrors :class:`engram.diagnostics.team_checks.TeamDoctorRow`
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
    liveness = _probe_daemon(paths.socket)
    if liveness == "no_socket":
        return DaemonDoctorRow(
            code=DAEMON_RUNNING,
            status="INFO",
            detail=(
                f"Daemon not running for vault {paths.vault.name}. Run "
                f"`engram daemon start` or open a Claude session "
                f"(`engram serve` auto-spawns by default)."
            ),
        )
    if liveness == "not_listening":
        return DaemonDoctorRow(
            code=DAEMON_RUNNING,
            status="INFO",
            detail=(
                f"Socket file present at {paths.socket} but no daemon "
                f"is listening; consider `engram daemon start`."
            ),
        )

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


def _row_for_unreadable_state(
    code: str,
    paths: SocketPaths,
    *,
    unknown: str,
) -> DaemonDoctorRow:
    """The row for "the state file did not answer".

    ``read_state`` returns ``None`` for a missing file, an unparseable one
    and a schema-drifted one alike, so the file alone cannot say whether a
    daemon is running. The socket can. A daemon serving with no readable
    state is exactly the condition these rows exist to warn about, so it
    must not render as a pass; with nothing listening there is nothing to
    compare against, which is a skip and not a pass either.
    """
    if _probe_daemon(paths.socket) == "listening":
        cause = "is unreadable" if paths.state_file.exists() else "is missing"
        return DaemonDoctorRow(
            code=code,
            status="WARN",
            detail=(
                f"A daemon is listening on {paths.socket} but its state file "
                f"{cause} ({paths.state_file}), so {unknown} is unknown. "
                f"Restart it so it re-records: `engram daemon stop && "
                f"engram daemon start`."
            ),
        )
    return DaemonDoctorRow(
        code=code,
        status="SKIP",
        detail=(
            f"not run: no daemon is listening on {paths.socket}, so there is "
            f"no running daemon to compare against"
        ),
    )


def check_daemon_version_matches_cli(vault_path: Path) -> DaemonDoctorRow:
    """WARN when the running daemon was built from a different engram than the CLI.

    An install replaces the wheel; the daemon keeps serving from the module
    objects it loaded at spawn. Every CLI command, ``engram doctor`` included,
    then reports the new code while ``capture_thought`` and ``search_thoughts``
    still run the old. The state file is the only place the running process
    says what it was built from.
    """
    from engram import __version__

    paths = resolve_paths(vault_path)
    state = read_state(paths.state_file)
    if state is None:
        return _row_for_unreadable_state(
            DAEMON_VERSION_MATCHES_CLI,
            paths,
            unknown="the engram version it is serving from",
        )
    if not state.engram_version:
        return DaemonDoctorRow(
            code=DAEMON_VERSION_MATCHES_CLI,
            status="WARN",
            detail=(
                "Running daemon records no engram version, so it predates this "
                "field and its code is unknown. Restart to confirm: "
                "`engram daemon stop && engram daemon start`."
            ),
        )
    if state.engram_version != __version__:
        return DaemonDoctorRow(
            code=DAEMON_VERSION_MATCHES_CLI,
            status="WARN",
            detail=(
                f"Running daemon was built from engram {state.engram_version}; "
                f"this CLI is {__version__}. The daemon serves MCP traffic from "
                f"the older code until it restarts: "
                f"`engram daemon stop && engram daemon start`."
            ),
        )
    return DaemonDoctorRow(
        code=DAEMON_VERSION_MATCHES_CLI,
        status="OK",
        detail=f"daemon and CLI both engram {__version__}",
    )


def check_daemon_config_drifted(vault_path: Path, config: EffectiveConfig) -> DaemonDoctorRow:
    """WARN when the vault's daemon config no longer matches the running daemon's.

    The daemon records the resolved :class:`DaemonConfig` it started with.
    Nothing read that record until this row: an edit to ``engram.config.yaml``
    lands on disk, is contradicted by the live process, and every other row
    reports healthy.
    """
    paths = resolve_paths(vault_path)
    state = read_state(paths.state_file)
    if state is None:
        return _row_for_unreadable_state(
            DAEMON_CONFIG_DRIFTED,
            paths,
            unknown="the daemon config it is serving with",
        )
    current = config.daemon.model_dump()
    snapshot = state.config_snapshot
    # Compare only keys both sides carry: a daemon from another engram version
    # may know a different key set, and that is a version finding, not a config
    # one - the version row above owns it.
    shared = sorted(set(current) & set(snapshot))
    if not shared:
        return DaemonDoctorRow(
            code=DAEMON_CONFIG_DRIFTED,
            status="WARN",
            detail=(
                "Running daemon's config snapshot shares no keys with the "
                "current daemon config; the comparison could not be made. "
                "Restart the daemon to re-record it."
            ),
        )
    drifted = [key for key in shared if snapshot[key] != current[key]]
    if drifted:
        return DaemonDoctorRow(
            code=DAEMON_CONFIG_DRIFTED,
            status="WARN",
            detail=(
                f"Running daemon started with different values for "
                f"{', '.join(drifted)}; the vault config on disk is not what is "
                f"serving. `engram daemon stop && engram daemon start` to apply."
            ),
        )
    return DaemonDoctorRow(
        code=DAEMON_CONFIG_DRIFTED,
        status="OK",
        detail=f"{len(shared)} daemon config keys match the running daemon",
    )


#: The daemon rows that cannot run when ``resolve_paths`` refuses the vault.
#: Named here so a suppressed sweep reports one SKIP row per check rather
#: than silently returning a shorter report.
_PATH_DEPENDENT_CHECK_CODES: tuple[str, ...] = (
    DAEMON_RUNNING,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_UPTIME_EXCESSIVE,
    DAEMON_VERSION_MATCHES_CLI,
    DAEMON_CONFIG_DRIFTED,
)


def run_daemon_checks(report: DoctorReport, config: EffectiveConfig) -> None:
    """Fold the daemon-mode rows into the doctor report.

    ``INFO`` maps to :attr:`CheckStatus.OK` - the doctor report has no
    info tier and INFO rows are advisory, not degraded.
    """
    from engram.diagnostics.doctor import CheckStatus

    status_map = {
        "OK": CheckStatus.OK,
        "INFO": CheckStatus.OK,
        "SKIP": CheckStatus.SKIP,
        "WARN": CheckStatus.WARN,
        "FAIL": CheckStatus.FAIL,
    }
    rows = [check_daemon_socket_path_too_long(config.vault_path)]
    # resolve_paths refuses over-limit UDS paths with DaemonError; the
    # path-length row above already carries the WARN + remediation, and
    # no daemon can exist there for the other checks to inspect.
    unreachable: str | None = None
    try:
        rows.extend(
            [
                check_daemon_running(config.vault_path),
                check_daemon_socket_permissions(config.vault_path),
                check_daemon_socket_stale(config.vault_path),
                check_daemon_log_rotation_healthy(config.vault_path),
                check_daemon_uptime_excessive(config.vault_path),
                check_daemon_version_matches_cli(config.vault_path),
                check_daemon_config_drifted(config.vault_path, config),
            ]
        )
    except DaemonError as exc:
        # Dropping those rows entirely would shrink the report without
        # saying so, and a shorter all-green report reads like a healthy one.
        # Emit each as SKIP instead, so the row count stays constant and the
        # reason is on the row that did not run.
        unreachable = str(exc)
    for row in rows:
        report.add(row.code, status_map[row.status], row.detail)
    if unreachable is not None:
        for code in _PATH_DEPENDENT_CHECK_CODES:
            report.add(
                code,
                CheckStatus.SKIP,
                f"{code}: not run (daemon paths could not be resolved)",
                detail=unreachable,
            )


__all__ = [
    "DaemonDoctorRow",
    "DaemonDoctorStatus",
    "DaemonLiveness",
    "check_daemon_config_drifted",
    "check_daemon_log_rotation_healthy",
    "check_daemon_running",
    "check_daemon_socket_path_too_long",
    "check_daemon_socket_permissions",
    "check_daemon_socket_stale",
    "check_daemon_uptime_excessive",
    "check_daemon_version_matches_cli",
    "run_daemon_checks",
]
