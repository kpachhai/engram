"""Daemon state file at ``<vault>/.indexes/engram.state.json``."""

from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.daemon.socket_paths import resolve_paths
from engram.daemon.state import DaemonState, read_state, write_state


@pytest.fixture
def short_vault() -> Iterator[Path]:
    """Short-path vault so socket_paths.resolve_paths() succeeds on macOS."""
    with tempfile.TemporaryDirectory(prefix="eng-state-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        vault.mkdir()
        yield vault


def test_write_then_read_roundtrip(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    state = DaemonState(
        pid=os.getpid(),
        started_at="2026-05-12T14:20:04Z",
        vault_name="memex",
        vault_path=str(paths.vault),
        hostname=socket.gethostname(),
        config_snapshot={"idle_shutdown_seconds": 3600},
    )
    write_state(paths.state_file, state)
    loaded = read_state(paths.state_file)
    assert loaded == state


def test_read_state_missing_returns_none(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    # state file not written yet
    assert read_state(paths.state_file) is None


def test_read_state_corrupt_returns_none(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    paths.state_file.write_text("not json{")
    assert read_state(paths.state_file) is None


def test_state_file_mode_0600(short_vault: Path) -> None:
    paths = resolve_paths(short_vault)
    state = DaemonState(
        pid=1234,
        started_at="2026-05-12T14:20:04Z",
        vault_name="memex",
        vault_path=str(paths.vault),
        hostname="testhost",
        config_snapshot={},
    )
    write_state(paths.state_file, state)
    mode = paths.state_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_read_state_schema_drift_returns_none(short_vault: Path) -> None:
    """Future-proof: an unexpected extra key in the on-disk JSON is treated as corruption."""
    paths = resolve_paths(short_vault)
    # Hand-write a JSON blob that's syntactically valid but doesn't match
    # the current DaemonState shape.
    paths.state_file.write_text('{"unexpected": "shape"}')
    assert read_state(paths.state_file) is None
