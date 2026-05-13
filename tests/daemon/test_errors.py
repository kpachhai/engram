"""Test the daemon error family inherits EngramError and has unique error codes."""

from __future__ import annotations

from engram.errors import (
    DaemonConnectionError,
    DaemonError,
    DaemonNotRunningError,
    DaemonSpawnError,
    EngramError,
    PeerCredRejectError,
)


def test_all_daemon_errors_inherit_engram_error() -> None:
    for cls in (
        DaemonError,
        DaemonSpawnError,
        DaemonConnectionError,
        DaemonNotRunningError,
        PeerCredRejectError,
    ):
        assert issubclass(cls, EngramError)


def test_error_codes_unique_and_named() -> None:
    codes = {
        DaemonError.error_code,
        DaemonSpawnError.error_code,
        DaemonConnectionError.error_code,
        DaemonNotRunningError.error_code,
        PeerCredRejectError.error_code,
    }
    assert len(codes) == 5
    assert codes == {
        "daemon_error",
        "daemon_spawn_error",
        "daemon_connection_error",
        "daemon_not_running_error",
        "peer_cred_reject_error",
    }


def test_subtypes_inherit_daemon_error() -> None:
    for cls in (
        DaemonSpawnError,
        DaemonConnectionError,
        DaemonNotRunningError,
        PeerCredRejectError,
    ):
        assert issubclass(cls, DaemonError)


def test_message_preserved() -> None:
    err = DaemonSpawnError("ready signal timed out")
    assert "ready signal timed out" in str(err)
