"""Daemon-mode doctor checks."""

from __future__ import annotations

import os
import socket as socket_module
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import engram
from engram.config.models import EffectiveConfig, LLMConfig, SyncConfig
from engram.daemon.socket_paths import resolve_paths
from engram.daemon.state import DaemonState, write_state
from engram.diagnostics.check_codes import (
    ALL_DAEMON_CHECK_CODES,
    DAEMON_CONFIG_DRIFTED,
    DAEMON_LOG_ROTATION_HEALTHY,
    DAEMON_RUNNING,
    DAEMON_SOCKET_PATH_TOO_LONG,
    DAEMON_SOCKET_PERMISSIONS,
    DAEMON_SOCKET_STALE,
    DAEMON_UPTIME_EXCESSIVE,
    DAEMON_VERSION_MATCHES_CLI,
)
from engram.diagnostics.daemon_checks import (
    DaemonDoctorRow,
    check_daemon_config_drifted,
    check_daemon_log_rotation_healthy,
    check_daemon_running,
    check_daemon_socket_path_too_long,
    check_daemon_socket_permissions,
    check_daemon_socket_stale,
    check_daemon_uptime_excessive,
    check_daemon_version_matches_cli,
    run_daemon_checks,
)
from engram.diagnostics.doctor import CheckStatus, DoctorReport


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


# ----- daemon_version_matches_cli + daemon_config_drifted ------------


def _config_for(vault: Path) -> EffectiveConfig:
    return EffectiveConfig(
        default_user="t",
        vault_path=vault,
        thoughts_dir=vault / "thoughts",
        index_dir=vault / ".indexes",
        embedding_model="BAAI/bge-small-en-v1.5",
        vault_name="t",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )


def _listen_on(socket_path: Path) -> socket_module.socket:
    """Bind a real UDS listener, so the socket probe sees a live daemon."""
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    return listener


def _write_state_for(
    vault: Path,
    *,
    engram_version: str,
    config_snapshot: dict[str, object] | None = None,
) -> None:
    paths = resolve_paths(vault)
    write_state(
        paths.state_file,
        DaemonState(
            pid=os.getpid(),
            started_at="2026-05-12T14:20:04+00:00",
            vault_name="personal",
            vault_path=str(paths.vault),
            hostname="test-host",
            config_snapshot=config_snapshot if config_snapshot is not None else {},
            engram_version=engram_version,
        ),
    )


def test_daemon_version_ok_when_state_matches_this_cli(short_vault: Path) -> None:
    _write_state_for(short_vault, engram_version=engram.__version__)
    row = check_daemon_version_matches_cli(short_vault)
    assert row.status == "OK"
    assert engram.__version__ in row.detail


def test_daemon_version_warns_when_daemon_predates_the_installed_cli(
    short_vault: Path,
) -> None:
    """An upgrade replaces the wheel; the running daemon keeps the old code."""
    _write_state_for(short_vault, engram_version="0.0.1")
    row = check_daemon_version_matches_cli(short_vault)
    assert row.status == "WARN"
    assert "0.0.1" in row.detail
    assert "daemon stop" in row.detail


def test_daemon_version_warns_when_the_daemon_recorded_nothing(short_vault: Path) -> None:
    """An empty field is an unknown answer, not a matching one."""
    _write_state_for(short_vault, engram_version="")
    row = check_daemon_version_matches_cli(short_vault)
    assert row.status == "WARN"
    # Assert the branch, not just the verdict: the mismatch branch below also
    # WARNs and also says "restart", so a looser assertion here would pass with
    # this branch deleted.
    assert "records no engram version" in row.detail
    assert "unknown" in row.detail


def test_daemon_version_skips_when_no_daemon_is_listening(short_vault: Path) -> None:
    """Nothing is serving, so there is no running version to compare: a skip."""
    row = check_daemon_version_matches_cli(short_vault)
    assert row.status == "SKIP"
    assert "not run" in row.detail


def test_daemon_version_warns_when_a_listening_daemon_has_no_state_file(
    short_vault: Path,
) -> None:
    """A daemon is serving and its version is unknown - the row's whole subject."""
    paths = resolve_paths(short_vault)
    listener = _listen_on(paths.socket)
    try:
        row = check_daemon_version_matches_cli(short_vault)
        assert row.status == "WARN"
        assert "listening" in row.detail
        assert "missing" in row.detail
    finally:
        listener.close()


def test_daemon_version_warns_when_a_listening_daemon_has_an_unreadable_state_file(
    short_vault: Path,
) -> None:
    """read_state answers None for corruption too, and None is not "not running"."""
    paths = resolve_paths(short_vault)
    paths.state_file.write_text("{not json")
    listener = _listen_on(paths.socket)
    try:
        row = check_daemon_version_matches_cli(short_vault)
        assert row.status == "WARN"
        assert "unreadable" in row.detail
    finally:
        listener.close()


def test_daemon_config_skips_when_no_daemon_is_listening(short_vault: Path) -> None:
    """Nothing is serving, so there is no running config to compare: a skip."""
    row = check_daemon_config_drifted(short_vault, _config_for(short_vault))
    assert row.status == "SKIP"
    assert "not run" in row.detail


def test_daemon_config_warns_when_a_listening_daemon_has_no_state_file(
    short_vault: Path,
) -> None:
    """A daemon is serving under a config nothing can read."""
    paths = resolve_paths(short_vault)
    listener = _listen_on(paths.socket)
    try:
        row = check_daemon_config_drifted(short_vault, _config_for(short_vault))
        assert row.status == "WARN"
        assert "listening" in row.detail
        assert "missing" in row.detail
    finally:
        listener.close()


def test_daemon_config_warns_when_a_listening_daemon_has_an_unreadable_state_file(
    short_vault: Path,
) -> None:
    """Corruption is an unanswered question, not a matching config."""
    paths = resolve_paths(short_vault)
    paths.state_file.write_text("{not json")
    listener = _listen_on(paths.socket)
    try:
        row = check_daemon_config_drifted(short_vault, _config_for(short_vault))
        assert row.status == "WARN"
        assert "unreadable" in row.detail
    finally:
        listener.close()


def test_daemon_config_ok_when_snapshot_matches_vault_config(short_vault: Path) -> None:
    config = _config_for(short_vault)
    _write_state_for(
        short_vault,
        engram_version=engram.__version__,
        config_snapshot=config.daemon.model_dump(),
    )
    row = check_daemon_config_drifted(short_vault, config)
    assert row.status == "OK"
    assert "match" in row.detail


def test_daemon_config_warns_when_the_vault_config_changed_under_the_daemon(
    short_vault: Path,
) -> None:
    """The snapshot was written and read by nothing until this row existed."""
    config = _config_for(short_vault)
    stale = config.daemon.model_dump()
    stale["idle_shutdown_seconds"] = config.daemon.idle_shutdown_seconds + 999
    _write_state_for(
        short_vault,
        engram_version=engram.__version__,
        config_snapshot=stale,
    )
    row = check_daemon_config_drifted(short_vault, config)
    assert row.status == "WARN"
    assert "idle_shutdown_seconds" in row.detail
    assert "daemon stop" in row.detail


def test_daemon_config_warns_when_no_keys_can_be_compared(short_vault: Path) -> None:
    """An empty snapshot is an absent answer, and absent is never a pass."""
    config = _config_for(short_vault)
    _write_state_for(short_vault, engram_version=engram.__version__, config_snapshot={})
    row = check_daemon_config_drifted(short_vault, config)
    assert row.status == "WARN"
    assert "no keys" in row.detail


# ----- the sweep reports what it could not run -----------------------


def test_daemon_sweep_reports_every_code_on_a_cold_vault(short_vault: Path) -> None:
    """One row per daemon code; the two needing a live daemon say they did not run."""
    report = DoctorReport()
    run_daemon_checks(report, _config_for(short_vault))
    names = [c.name for c in report.checks]
    assert sorted(names) == sorted(ALL_DAEMON_CHECK_CODES)
    skipped = {c.name for c in report.checks if c.status is CheckStatus.SKIP}
    assert skipped == {DAEMON_VERSION_MATCHES_CLI, DAEMON_CONFIG_DRIFTED}


def test_daemon_sweep_skips_nothing_when_a_daemon_is_live(short_vault: Path) -> None:
    """The negative control: SKIP is not permanently on.

    With a daemon listening and a state file it can read, every row runs.
    Without this, a row hard-coded to skip passes every red test above.
    """
    config = _config_for(short_vault)
    paths = resolve_paths(short_vault)
    listener = _listen_on(paths.socket)
    try:
        _write_state_for(
            short_vault,
            engram_version=engram.__version__,
            config_snapshot=config.daemon.model_dump(),
        )
        report = DoctorReport()
        run_daemon_checks(report, config)
        assert sorted(c.name for c in report.checks) == sorted(ALL_DAEMON_CHECK_CODES)
        assert [c.name for c in report.checks if c.status is CheckStatus.SKIP] == []
    finally:
        listener.close()


def test_daemon_sweep_marks_unreachable_rows_skipped_rather_than_dropping_them(
    short_vault: Path,
) -> None:
    """An over-limit UDS path used to shrink the report in silence.

    resolve_paths refuses the vault, so every row except the path-length
    one that detected it cannot run. Dropping those rows left a report with
    nothing saying so, and a shorter all-green report reads like a healthy
    one. Each unrunnable check gets its own SKIP row instead. The counts
    live in ALL_DAEMON_CHECK_CODES, which the assertions below derive from
    rather than restate.
    """
    deep = short_vault
    for _ in range(20):
        deep = deep / ("x" * 8)
    deep.mkdir(parents=True)

    report = DoctorReport()
    run_daemon_checks(report, _config_for(deep))

    names = [c.name for c in report.checks]
    assert sorted(names) == sorted(ALL_DAEMON_CHECK_CODES), names
    skipped = {c.name for c in report.checks if c.status is CheckStatus.SKIP}
    # Everything except the path-length row itself, which is what detected the
    # problem. Derived from the canonical tuple rather than restated, so a new
    # daemon check is covered here the day it is declared.
    assert skipped == set(ALL_DAEMON_CHECK_CODES) - {DAEMON_SOCKET_PATH_TOO_LONG}
    assert all("not run" in c.message for c in report.checks if c.status is CheckStatus.SKIP)
