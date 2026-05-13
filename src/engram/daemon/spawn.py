"""Spawn-lock acquisition + double-fork daemon detach + readiness pipe.

The spawn-lock (separate from :class:`engram.utils.lock.VaultLock`)
serializes concurrent ``engram serve`` invocations attempting to spawn
a daemon for the same vault. It is held briefly — only for the duration
of the fork + wait-for-ready dance.

Spec: ``2026-05-12-engram-daemon-mode-design.md`` Section 5.2 step 4 +
Amendment 1 (startup ordering: signal-handlers BEFORE VaultLock BEFORE
unlink BEFORE bind BEFORE ready).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import fcntl
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from engram.errors import DaemonSpawnError


class SpawnLockTimeoutError(DaemonSpawnError):
    """Acquiring the spawn lock timed out (another spawner is mid-dance)."""


class _ReadinessKind(enum.Enum):
    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True)
class SpawnReadiness:
    """Parsed result of :func:`wait_for_ready`.

    Equality is by ``kind`` so callers can write ``result == SpawnReadiness.ready()``
    when they don't care about the error message attached to a failure.
    """

    kind: _ReadinessKind
    message: str = field(default="")

    @property
    def is_ready(self) -> bool:
        """``True`` when the daemon reported a successful spawn."""
        return self.kind == _ReadinessKind.READY

    @property
    def is_error(self) -> bool:
        """``True`` when the daemon reported an explicit error message."""
        return self.kind == _ReadinessKind.ERROR

    def __eq__(self, other: object) -> bool:
        """Equal iff ``other`` is a SpawnReadiness with the same ``kind``."""
        if isinstance(other, SpawnReadiness):
            return self.kind == other.kind
        return NotImplemented

    def __hash__(self) -> int:
        """Hash by ``kind`` to match the equality contract."""
        return hash(self.kind)

    @classmethod
    def ready(cls) -> SpawnReadiness:
        """Sentinel for a successful spawn (used in equality checks)."""
        return cls(kind=_ReadinessKind.READY)

    @classmethod
    def error(cls, message: str = "") -> SpawnReadiness:
        """Sentinel for a daemon-reported error (optionally with message)."""
        return cls(kind=_ReadinessKind.ERROR, message=message)


@contextlib.contextmanager
def acquire_spawn_lock(
    lock_path: Path,
    *,
    timeout_seconds: float,
) -> Iterator[bool]:
    """Acquire the spawn flock; release on context-exit.

    Polls ``fcntl.flock(LOCK_EX | LOCK_NB)`` until it succeeds or
    ``timeout_seconds`` elapses. Raises :class:`SpawnLockTimeoutError` on
    timeout. The yielded value is always ``True`` so callers may write
    ``with acquire_spawn_lock(...) as locked:`` for symmetry.
    """
    deadline = time.monotonic() + timeout_seconds
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    msg = f"spawn lock {lock_path} contended for > {timeout_seconds}s"
                    raise SpawnLockTimeoutError(msg) from exc
                time.sleep(0.05)
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


async def wait_for_ready(
    read_fd: int,
    *,
    timeout_seconds: float,
) -> SpawnReadiness:
    r"""Wait for the spawned daemon to write ``ready\n`` or ``error: <msg>\n``.

    The forked daemon writes a single line to the write-end of a pipe;
    we hold the read-end and parse exactly one line.

    Returns :class:`SpawnReadiness` with ``kind=READY`` on success or
    ``kind=ERROR`` (carrying the daemon's message) on a daemon-reported
    failure. Raises :class:`asyncio.TimeoutError` if neither arrives
    within ``timeout_seconds``; raises :class:`DaemonSpawnError` if the
    line cannot be parsed.

    Owns ``read_fd`` and closes it on exit (success or failure).
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(read_fd, "rb", buffering=0)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
    finally:
        transport.close()

    text = line.decode("utf-8", errors="replace").rstrip("\n")
    if text == "ready":
        return SpawnReadiness.ready()
    if text.startswith("error:"):
        return SpawnReadiness.error(text[len("error:") :].strip())
    msg = f"unexpected readiness payload from spawn pipe: {text!r}"
    raise DaemonSpawnError(msg)


def double_fork_detach() -> None:
    """Standard Unix double-fork detach.

    The caller is the parent before invoking; on return the caller is
    the grandchild process with no controlling terminal. Stdin/stdout/
    stderr are redirected to ``/dev/null`` so the daemon does not write
    to the proxy's stdio.

    Layer C wires this into the daemon-spawn helper; Layer G smoke
    exercises the full fork dance against the installed binary.
    """
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    # Grandchild now: no controlling terminal, child of init.
    os.chdir("/")
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    finally:
        os.close(devnull)


__all__ = [
    "SpawnLockTimeoutError",
    "SpawnReadiness",
    "acquire_spawn_lock",
    "double_fork_detach",
    "wait_for_ready",
]
