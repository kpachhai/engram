"""Tests for engram.utils.lock - per-vault advisory lock.

Concurrent-process tests use subprocess.Popen so flock semantics are exercised
across kernel-arbitrated FDs (within a single Python process flock arbitrates
per-FD which would let mocks deceive the test).
"""

from __future__ import annotations

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from engram.errors import LockError
from engram.utils.lock import LOCK_FORMAT_VERSION, VaultLock, serve_lock_metadata


def _make_vault(tmp_path: Path) -> Path:
    """Set up a minimal vault directory with .indexes/."""
    (tmp_path / ".indexes").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_basic_acquire_release(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    lock = VaultLock(vault)
    lock.acquire()
    assert (vault / ".indexes" / "engram.lock").exists()
    lock.release()
    assert not (vault / ".indexes" / "engram.lock").exists()


def test_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    lock_path = vault / ".indexes" / "engram.lock"
    with VaultLock(vault):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_lock_file_contents_have_required_fields(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    with VaultLock(vault):
        contents = json.loads((vault / ".indexes" / "engram.lock").read_text())
    assert contents["pid"] == os.getpid()
    assert contents["hostname"] == socket.gethostname()
    assert "acquired_at" in contents
    assert contents["version"] == LOCK_FORMAT_VERSION


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_lock_file_mode_0600(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    with VaultLock(vault):
        lock_path = vault / ".indexes" / "engram.lock"
        mode = lock_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_acquire_creates_indexes_dir_if_missing(tmp_path: Path) -> None:
    """The .indexes/ directory may not exist yet on first engram serve."""
    # Note: tmp_path exists but .indexes does NOT.
    lock = VaultLock(tmp_path)
    lock.acquire()
    try:
        assert (tmp_path / ".indexes").is_dir()
    finally:
        lock.release()


def test_re_acquire_in_same_instance_raises(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    lock = VaultLock(vault)
    lock.acquire()
    try:
        with pytest.raises(LockError, match="already acquired"):
            lock.acquire()
    finally:
        lock.release()


def test_concurrent_process_acquire_raises_lock_error(tmp_path: Path) -> None:
    """A second process acquiring the same vault must fail with LockError."""
    vault = _make_vault(tmp_path)

    # Helper script: acquire the lock and hold it until parent sends a signal.
    helper_script = textwrap.dedent(f"""
        from engram.utils.lock import VaultLock
        from pathlib import Path
        import sys, time
        with VaultLock(Path({str(vault)!r})):
            sys.stdout.write("acquired\\n")
            sys.stdout.flush()
            time.sleep(30)  # parent will kill us
    """)

    proc = subprocess.Popen(  # noqa: S603 - sys.executable + literal -c is safe
        [sys.executable, "-c", helper_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for child to confirm acquisition.
        ready_line = proc.stdout.readline() if proc.stdout else ""
        assert "acquired" in ready_line, (
            f"helper did not acquire: stderr={proc.stderr.read() if proc.stderr else ''}"
        )

        # Parent now tries to acquire the same lock.
        with pytest.raises(LockError, match=r"already running|engram lock"):
            VaultLock(vault).acquire()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_stale_lock_from_dead_process_can_be_acquired(tmp_path: Path) -> None:
    """When the prior holder died, kernel released flock; new acquire must succeed."""
    vault = _make_vault(tmp_path)
    lock_path = vault / ".indexes" / "engram.lock"

    # Fabricate a stale lock file (no flock held; just JSON content from a dead pid).
    stale = {
        "pid": 999999,  # almost certainly not a live PID
        "hostname": socket.gethostname(),
        "acquired_at": "2020-01-01T00:00:00+00:00",
        "version": LOCK_FORMAT_VERSION,
    }
    lock_path.write_text(json.dumps(stale))

    with VaultLock(vault) as lock:
        # Our acquire overwrote the metadata.
        new_contents = json.loads(lock_path.read_text())
        assert new_contents["pid"] == os.getpid()
        del lock  # silence unused-var warning


def test_force_override_when_flock_fails(tmp_path: Path) -> None:
    """--force lets a user take over even when flock reports the lock is held."""
    vault = _make_vault(tmp_path)
    lock_path = vault / ".indexes" / "engram.lock"

    # Pre-write a "valid-looking" lock file pointing at another host.
    foreign = {
        "pid": 12345,
        "hostname": "other-host.example.com",
        "acquired_at": "2026-05-04T14:23:01+00:00",
        "version": LOCK_FORMAT_VERSION,
    }
    lock_path.write_text(json.dumps(foreign))

    # Simulate flock failure on first try (other-host holds it),
    # then success after we unlink and retry.
    flock_calls = {"count": 0}
    real_flock = __import__("fcntl").flock

    def flaky_flock(fd: int, op: int) -> None:
        flock_calls["count"] += 1
        if flock_calls["count"] == 1:
            raise BlockingIOError(errno.EWOULDBLOCK, "would block")
        real_flock(fd, op)

    with (
        patch("engram.utils.lock.fcntl.flock", side_effect=flaky_flock),
        VaultLock(vault, force=True) as lock,
    ):
        new_contents = json.loads(lock_path.read_text())
        assert new_contents["pid"] == os.getpid()
        assert new_contents["hostname"] == socket.gethostname()
        del lock


def test_busy_message_distinguishes_local_vs_remote_host(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    lock_path = vault / ".indexes" / "engram.lock"

    foreign = {
        "pid": 12345,
        "hostname": "other-host.example.com",
        "acquired_at": "2026-05-04T14:23:01+00:00",
        "version": LOCK_FORMAT_VERSION,
    }
    lock_path.write_text(json.dumps(foreign))

    with patch(
        "engram.utils.lock.fcntl.flock",
        side_effect=BlockingIOError(errno.EWOULDBLOCK, "would block"),
    ):
        with pytest.raises(LockError) as exc_info:
            VaultLock(vault).acquire()
        assert "other-host.example.com" in str(exc_info.value)
        assert "cross-host" in str(exc_info.value)

    # Now same-host scenario.
    same_host = dict(foreign, hostname=socket.gethostname())
    lock_path.write_text(json.dumps(same_host))

    with patch(
        "engram.utils.lock.fcntl.flock",
        side_effect=BlockingIOError(errno.EWOULDBLOCK, "would block"),
    ):
        with pytest.raises(LockError) as exc_info:
            VaultLock(vault).acquire()
        assert "already running" in str(exc_info.value)


def test_release_idempotent(tmp_path: Path) -> None:
    """Calling release twice must not raise."""
    vault = _make_vault(tmp_path)
    lock = VaultLock(vault)
    lock.acquire()
    lock.release()
    lock.release()  # should be a no-op


def test_atexit_cleanup_registered_on_acquire(tmp_path: Path) -> None:
    """atexit handler is registered so a missed __exit__ still cleans up."""
    vault = _make_vault(tmp_path)
    lock = VaultLock(vault)

    registered = []
    real_register = __import__("atexit").register

    def fake_register(func, *args, **kwargs):
        registered.append(func)
        return real_register(func, *args, **kwargs)

    with patch("engram.utils.lock.atexit.register", side_effect=fake_register):
        lock.acquire()
    try:
        assert any(getattr(f, "__name__", "") == "_cleanup" for f in registered)
    finally:
        lock.release()


def test_signal_handlers_restored_on_release(tmp_path: Path) -> None:
    """SIGTERM and SIGINT handlers are restored after release."""
    vault = _make_vault(tmp_path)
    initial_sigterm = signal.getsignal(signal.SIGTERM)
    initial_sigint = signal.getsignal(signal.SIGINT)

    lock = VaultLock(vault)
    lock.acquire()
    try:
        # During lock, our handler is installed.
        assert signal.getsignal(signal.SIGTERM) is not initial_sigterm
    finally:
        lock.release()

    assert signal.getsignal(signal.SIGTERM) is initial_sigterm
    assert signal.getsignal(signal.SIGINT) is initial_sigint


# === serve_lock_metadata ===


def test_serve_lock_metadata_absent_returns_none(tmp_path: Path) -> None:
    """No lock marker -> None (caller knows the vault is idle)."""
    vault = _make_vault(tmp_path)
    assert serve_lock_metadata(vault) is None


def test_serve_lock_metadata_present_returns_dict(tmp_path: Path) -> None:
    """Active VaultLock surfaces as a populated metadata dict."""
    vault = _make_vault(tmp_path)
    lock = VaultLock(vault, install_signal_handlers=False)
    lock.acquire()
    try:
        meta = serve_lock_metadata(vault)
        assert meta is not None
        assert meta.get("pid") == os.getpid()
        # Standard VaultLock metadata fields.
        assert "hostname" in meta
        assert "acquired_at" in meta
        assert meta.get("version") == LOCK_FORMAT_VERSION
    finally:
        lock.release()


def test_serve_lock_metadata_malformed_returns_empty_dict(tmp_path: Path) -> None:
    """Unreadable / non-JSON lock body -> {} (still signals 'held')."""
    vault = _make_vault(tmp_path)
    lock_path = vault / ".indexes" / "engram.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not a json document", encoding="utf-8")
    meta = serve_lock_metadata(vault)
    assert meta == {}
