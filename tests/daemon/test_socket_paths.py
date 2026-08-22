"""Path resolver + UDS length-limit enforcement for daemon-mode paths.

The vault-root fixture deliberately rents a short directory under ``/tmp``
(which on macOS resolves to ``/private/tmp/...``, ~5 bytes of overhead)
instead of pytest's default ``tmp_path`` (``/private/var/folders/.../pytest-of-USER/.../``,
~80 bytes of overhead). The resolver itself enforces a 104-byte UDS cap,
which the real-world pytest tmp directory comfortably exceeds before any
test content gets appended — so we sidestep it for the happy-path checks
and exercise the cap explicitly in ``test_resolve_paths_rejects_long_uds_path``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from engram.daemon.socket_paths import (
    UDS_PATH_LIMIT_BYTES,
    SocketPaths,
    resolve_paths,
)
from engram.errors import DaemonError


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """Tmp directory under ``/tmp`` so the resolved path fits the 104-byte UDS cap."""
    with tempfile.TemporaryDirectory(prefix="eng-uds-", dir="/tmp") as root:
        yield Path(root)


def test_resolve_paths_returns_co_located(short_tmp_path: Path) -> None:
    vault = short_tmp_path / "personal"
    (vault / ".indexes").mkdir(parents=True)
    paths = resolve_paths(vault)
    assert isinstance(paths, SocketPaths)
    assert paths.socket == (vault / ".indexes" / "engram.sock").resolve()
    assert paths.spawn_lock == (vault / ".indexes" / "engram.spawn.lock").resolve()
    assert paths.state_file == (vault / ".indexes" / "engram.state.json").resolve()
    assert paths.log_file == (vault / ".indexes" / "engram.log").resolve()


def test_resolve_paths_creates_indexes_dir_if_missing(short_tmp_path: Path) -> None:
    vault = short_tmp_path / "personal"
    vault.mkdir()
    # No .indexes/ yet.
    paths = resolve_paths(vault)
    assert (vault / ".indexes").exists()
    assert paths.socket.parent == (vault / ".indexes").resolve()


def test_resolve_paths_rejects_long_uds_path(short_tmp_path: Path) -> None:
    long_name = "x" * 120
    vault = short_tmp_path / long_name / "personal"
    vault.mkdir(parents=True)
    with pytest.raises(DaemonError) as exc_info:
        resolve_paths(vault)
    assert "UDS path too long" in str(exc_info.value)
    assert str(UDS_PATH_LIMIT_BYTES) in str(exc_info.value)


def test_uds_path_limit_byte_constant() -> None:
    # macOS has 104; Linux has 108; we use the stricter to be portable.
    assert UDS_PATH_LIMIT_BYTES == 104


def test_resolve_paths_resolves_symlinks(short_tmp_path: Path) -> None:
    real_vault = short_tmp_path / "real"
    real_vault.mkdir()
    link_vault = short_tmp_path / "link"
    link_vault.symlink_to(real_vault)
    paths = resolve_paths(link_vault)
    # The resolved path should follow the symlink to the real directory.
    assert paths.socket.parent == (real_vault / ".indexes").resolve()
