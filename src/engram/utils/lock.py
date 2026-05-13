"""Per-vault advisory lock for ``engram serve``.

A vault must be served by at most one ``engram serve`` process at a time;
concurrent serves race on SQLite writes, on git commits, and produce
inconsistent state.

Acquisition uses :func:`fcntl.flock` with ``LOCK_EX | LOCK_NB`` so the kernel
arbitrates between processes attempting to acquire the same lock file. The
lock file's JSON contents (``pid``, ``hostname``, ``acquired_at``, ``version``)
are diagnostic only - flock is the actual mutex. This sidesteps the TOCTOU
race that a pure ``O_CREAT|O_EXCL`` + read-and-decide-stale loop would have:
the kernel either grants the lock or it doesn't.

Stale locks self-recover. When a prior holder dies (graceful or SIGKILL), the
kernel releases its flock when the FD closes; the next acquirer's flock call
succeeds and overwrites the diagnostic metadata.

The ``--force`` flag lets a user take over a contested lock by unlinking the
lock file and retrying once. This is a deliberate user-initiated override
(per ``02-TECHNICAL_DESIGN.md`` Concurrent serve and Locking).

Caveat: POSIX file locking on NFS/SMB and on consumer cloud-sync providers
(Dropbox, iCloud) is unreliable; engram's startup-time path-detection check
(implemented in :mod:`engram.cli.serve`) should refuse or warn before
locking.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import json
import os
import signal
import socket
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any, Self

from engram.errors import LockError

LOCK_FORMAT_VERSION = "1"
_LOCK_FILENAME = "engram.lock"
_INDEXES_SUBDIR = ".indexes"
_LOCK_FILE_MODE = 0o600


class VaultLock:
    """Context manager for the per-vault engram lock at ``<vault>/.indexes/engram.lock``."""

    def __init__(
        self,
        vault_path: Path,
        *,
        force: bool = False,
        install_signal_handlers: bool = True,
    ) -> None:
        """Create a (not yet acquired) vault lock handle.

        Args:
            vault_path: Path to the vault directory (the parent of ``.indexes/``).
            force: If ``True``, taking over an apparently-held lock is allowed
                via one unlink-and-retry attempt. Use only when the operator
                has confirmed no other engram serve is running for this vault.
            install_signal_handlers: If ``True`` (default), VaultLock installs
                its own SIGTERM/SIGINT handler that releases the lock on
                signal. The daemon (Phase 5 Layer C) passes ``False`` so it
                can own its own signal handler that drains the coordinator
                + closes storage + releases the lock in the correct order
                (spec Amendment 1).
        """
        self.vault_path = Path(vault_path)
        self.lock_path = self.vault_path / _INDEXES_SUBDIR / _LOCK_FILENAME
        self.force = force
        self.install_signal_handlers = install_signal_handlers
        self._fd: int | None = None
        self._original_sigterm: Any = None
        self._original_sigint: Any = None
        self._signal_handlers_installed = False

    def __enter__(self) -> Self:
        """Acquire the lock and return self."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Release the lock on context exit."""
        self.release()

    def acquire(self) -> None:
        """Acquire the lock. Raises :class:`LockError` on contention."""
        if self._fd is not None:
            msg = f"VaultLock already acquired for {self.vault_path}"
            raise LockError(msg)
        self._acquire_internal(allow_force=self.force)

    def _acquire_internal(self, *, allow_force: bool) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, _LOCK_FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            existing = self._read_existing_metadata()
            os.close(fd)
            if allow_force:
                # User accepts the risk; remove the stale-looking lockfile and retry once.
                self.lock_path.unlink(missing_ok=True)
                self._acquire_internal(allow_force=False)
                return
            msg = self._format_busy_message(existing)
            raise LockError(msg) from exc
        except OSError as exc:
            os.close(fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                msg = self._format_busy_message(self._read_existing_metadata())
                raise LockError(msg) from exc
            raise

        self._write_metadata(fd)
        self._fd = fd
        self._install_cleanup_hooks()

    def _write_metadata(self, fd: int) -> None:
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": datetime.now(UTC).isoformat(),
            "version": LOCK_FORMAT_VERSION,
        }
        payload = json.dumps(metadata).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        os.fsync(fd)
        # File mode set at open time; double-check for umask defense-in-depth.
        os.fchmod(fd, _LOCK_FILE_MODE)

    def _read_existing_metadata(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.lock_path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return {}

    def _format_busy_message(self, existing: dict[str, Any]) -> str:
        pid = existing.get("pid", "?")
        hostname = existing.get("hostname", "?")
        acquired_at = existing.get("acquired_at", "?")
        local_host = socket.gethostname()
        if hostname == local_host:
            return (
                f"engram is already running for vault {self.vault_path} "
                f"(pid {pid} since {acquired_at}); stop the other process "
                f"or pass --force to override"
            )
        return (
            f"engram lock claimed by {hostname} (pid {pid} since {acquired_at}); "
            f"cross-host vault access is not supported by default; pass --force "
            f"to take over the lock if you are sure no other serve is running"
        )

    def _install_cleanup_hooks(self) -> None:
        atexit.register(self._cleanup)
        if not self.install_signal_handlers:
            # Daemon owns its own SIGTERM/SIGINT handler — do not stomp it.
            # The atexit hook above still fires on interpreter shutdown.
            return
        self._original_sigterm = signal.signal(signal.SIGTERM, self._signal_handler)
        self._original_sigint = signal.signal(signal.SIGINT, self._signal_handler)
        self._signal_handlers_installed = True

    def _restore_signal_handlers(self) -> None:
        if not self._signal_handlers_installed:
            return
        if self._original_sigterm is not None:
            signal.signal(signal.SIGTERM, self._original_sigterm)
        if self._original_sigint is not None:
            signal.signal(signal.SIGINT, self._original_sigint)
        self._original_sigterm = None
        self._original_sigint = None
        self._signal_handlers_installed = False

    def release(self) -> None:
        """Release the lock; safe to call multiple times.

        Idempotent: a release after the lock was never acquired or already
        released is a no-op.
        """
        if self._fd is None:
            return
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self.lock_path.unlink(missing_ok=True)
        finally:
            self._fd = None
            self._restore_signal_handlers()

    def _cleanup(self) -> None:
        """Atexit handler: best-effort release on interpreter shutdown."""
        self.release()

    def _signal_handler(self, signum: int, frame: FrameType | None) -> None:
        """SIGTERM/SIGINT handler: release the lock then re-raise the signal default."""
        del frame
        self.release()
        # Restore default disposition and re-raise so the caller sees the standard exit.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


_MIGRATION_LOCK_FILENAME = "migration.lock"


class MigrationLock:
    """Per-vault flock used to pause the sync coordinator during migration.

    Step 12 deliverable. ``engram migrate-from-open-brain`` acquires this
    lock for the duration of migration; the sync coordinator polls it
    before every git invocation and transitions to
    ``paused-for-migration`` when it is held.

    The lock file lives at ``<vault>/.indexes/migration.lock`` (separate
    from :class:`VaultLock` so a serve-loop CAN observe the migration
    holder while it is still in the foreground itself).
    """

    def __init__(self, vault_path: Path) -> None:
        """Create a (not yet acquired) migration lock handle."""
        self.vault_path = Path(vault_path)
        self.lock_path = self.vault_path / _INDEXES_SUBDIR / _MIGRATION_LOCK_FILENAME
        self._fd: int | None = None

    def __enter__(self) -> Self:
        """Acquire the migration lock and return self."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Release on context exit."""
        self.release()

    def acquire(self) -> None:
        """Acquire the migration flock; raises :class:`LockError` on contention."""
        if self._fd is not None:
            msg = f"MigrationLock already held for {self.vault_path}"
            raise LockError(msg)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, _LOCK_FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            msg = f"migration lock at {self.lock_path} is held by another process"
            raise LockError(msg) from exc
        metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(metadata).encode("utf-8"))
        os.fsync(fd)
        self._fd = fd

    def release(self) -> None:
        """Release the migration lock; idempotent."""
        if self._fd is None:
            return
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self.lock_path.unlink(missing_ok=True)
        finally:
            self._fd = None

    @classmethod
    def is_held(cls, vault_path: Path) -> bool:
        """Return True iff another process currently holds the migration lock.

        Implementation: try to acquire-and-release. ``flock`` is the only
        reliable observability primitive; a stat-based check would race
        any in-flight unlink. The acquire-and-release roundtrip is cheap
        (microseconds) and runs once per coordinator tick.
        """
        lock_path = vault_path / _INDEXES_SUBDIR / _MIGRATION_LOCK_FILENAME
        if not lock_path.exists():
            return False
        try:
            fd = os.open(str(lock_path), os.O_RDWR, _LOCK_FILE_MODE)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)


__all__ = ["LOCK_FORMAT_VERSION", "MigrationLock", "VaultLock"]
