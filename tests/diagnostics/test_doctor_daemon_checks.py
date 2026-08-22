"""Daemon-mode doctor checks."""

from __future__ import annotations

import os
import socket as socket_module
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.daemon.socket_paths import resolve_paths
from engram.daemon.state import DaemonState, write_state
from engram.diagnostics.check_codes import (
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_RUNNING,
    DAEMON_SOCKET_PATH_TOO_LONG,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_UPTIME_EXCESSIVE,
)
from engram.diagnostics.daemon_checks import (
    DaemonDoctorRow,
    check_daemon_log_rotation_healthy,
    check_daemon_running,
    check_daemon_socket_path_too_long,
    check_daemon_socket_permissions,
    check_daemon_socket_stale,
    check_daemon_uptime_excessive,
)


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Short-path vault so socket_paths.resolve_paths() succeeds on macOS."""
    with tempfile.TemporaryDirectory(prefix="eng-doc-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        vault.mkdir()
        yield vault


# ----- daemon_running ------------------------------------------------


def test_daemon_running_reports_not_running_on_cold_vault(short_vault: Path) -> None:
    row = check_daemon_running(short_vault)
    assert isinstance(row, DaemonDoctorRow)
    assert row.code == DAEMON_RUNNING
    assert row.status == "INFO"
    assert "not running" in row.detail.lower()


def test_daemon_running_reports_running_when_state_and_listener_present(
    short_vault: Path,
) -> None:
    paths = resolve_paths(short_vault)
    # Bind a real listener at the socket path.
    s = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    s.bind(str(paths.socket))
    s.listen(1)
    try:
        write_state(
            paths.state_file,
            DaemonState(
                pid=os.getpid(),
                started_at="2026-05-12T14:20:04+00:00",
                vault_name="personal",
                vault_path=str(paths.vault),
                hostname="test-host",
                config_snapshot={},
            ),
        )
        row = check_daemon_running(short_vault)
        assert row.status == "INFO"
        assert str(os.getpid()) in row.detail
    finally:
        s.close()


# ----- daemon_socket_stale ------------------------------------------


def test_daemon_socket_stale_warns_when_socket_file_no_listener(
    short_vault: Path,
) -> None:
    paths = resolve_paths(short_vault)
    # Plain regular file at the socket path — connect will refuse.
    paths.socket.write_text("")
    row = check_daemon_socket_stale(short_vault)
    assert row.code == DAEMON_SOCKET_STALE
    assert row.status == "WARN"
    assert "stale" in row.detail.lower()


def test_daemon_socket_stale_ok_when_no_socket(short_vault: Path) -> None:
    row = check_daemon_socket_stale(short_vault)
    assert row.status == "OK"


# ----- daemon_socket_permissions -------------------------------------


def test_daemon_socket_permissions_ok_when_0600(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    s = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    s.bind(str(paths.socket))
    s.listen(1)
    try:
        paths.socket.chmod(0o600)
        row = check_daemon_socket_permissions(short_vault)
        assert row.code == DAEMON_SOCKET_PERMISSIONS
        assert row.status == "OK"
    finally:
        s.close()


def test_daemon_socket_permissions_warns_on_non_0600(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    s = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    s.bind(str(paths.socket))
    s.listen(1)
    try:
        paths.socket.chmod(0o644)
        row = check_daemon_socket_permissions(short_vault)
        assert row.status == "WARN"
        assert "0o644" in row.detail
    finally:
        s.close()


# ----- daemon_uptime_excessive --------------------------------------


def test_daemon_uptime_info_after_7d(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    write_state(
        paths.state_file,
        DaemonState(
            pid=os.getpid(),
            started_at="2020-01-01T00:00:00+00:00",
            vault_name="personal",
            vault_path=str(paths.vault),
            hostname=socket_module.gethostname(),
            config_snapshot={},
        ),
    )
    row = check_daemon_uptime_excessive(short_vault)
    assert row.code == DAEMON_UPTIME_EXCESSIVE
    assert row.status == "INFO"
    assert "consider" in row.detail.lower()


def test_daemon_uptime_ok_when_no_state(short_vault: Path) -> None:
    row = check_daemon_uptime_excessive(short_vault)
    assert row.status == "OK"


# ----- daemon_log_rotation_healthy ----------------------------------


def test_daemon_log_rotation_ok_when_under_threshold(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    paths.log_file.write_text("small log")
    row = check_daemon_log_rotation_healthy(short_vault, max_size_mb=100)
    assert row.code == DAEMON_LOG_ROTATION_HEALTHY
    assert row.status == "OK"


def test_daemon_log_rotation_warns_on_oversize_old(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    # 6 MiB > 5 MiB threshold; backdate so age > 24h.
    paths.log_file.write_bytes(b"x" * (6 * 1024 * 1024))
    old_time = time.time() - 86400 * 2
    os.utime(paths.log_file, (old_time, old_time))
    row = check_daemon_log_rotation_healthy(short_vault, max_size_mb=5)
    assert row.status == "WARN"


# ----- daemon_socket_path_too_long ----------------------------------


def test_daemon_socket_path_too_long_ok_for_short_vault(short_vault: Path) -> None:
    row = check_daemon_socket_path_too_long(short_vault)
    assert row.code == DAEMON_SOCKET_PATH_TOO_LONG
    assert row.status == "OK"
    assert "bytes" in row.detail


def test_daemon_socket_path_too_long_warns_for_long_path(short_vault: Path) -> None:
    deep = short_vault
    # Stack ~20 components of 8 chars each → resolve_paths will reject.
    for _ in range(20):
        deep = deep / ("x" * 8)
    deep.mkdir(parents=True)
    row = check_daemon_socket_path_too_long(deep)
    assert row.status == "WARN"
    assert "104" in row.detail
