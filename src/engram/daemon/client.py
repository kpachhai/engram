"""Proxy client: stdio <-> UDS byte shuffler + spawn dance + crash retry.

Each ``engram serve`` invocation (in default proxy mode) constructs a
:class:`DaemonClient` and runs :meth:`run_proxy_loop`. The proxy does NOT
parse MCP frames — it shuffles bytes between its stdin/stdout (which
Claude Code talks to) and the per-vault daemon's UDS socket.

On mid-session UDS EOF (daemon crashed, restarted, or idle-shut-down),
the proxy retries 3 times with exponential backoff + jitter
(``1s + jitter``, ``4s + jitter``, ``16s + jitter``) before surfacing an
MCP-level error to Claude.

When the WAL file at spawn time exceeds 10 MiB, the proxy extends
the spawn timeout by ``wal_recovery_grace_seconds`` to give the
daemon room to replay before binding.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

from engram.config.models import DaemonConfig
from engram.daemon.socket_paths import resolve_paths
from engram.daemon.spawn import (
    SpawnLockTimeoutError,
    acquire_spawn_lock,
    wait_for_ready,
)
from engram.errors import DaemonConnectionError, DaemonSpawnError

_log = logging.getLogger("engram.daemon.client")

#: Exponential backoff schedule from spec Section 5.6.
#: Worst-case cumulative delay (no jitter) is 21s.
_PROXY_RETRY_DELAYS_SECONDS: Final[tuple[float, ...]] = (1.0, 4.0, 16.0)

#: Per-attempt jitter ceiling; the actual jitter is uniform on
#: ``[0, _JITTER_MAX_SECONDS / attempt]`` so later retries get more
#: spread to break up thundering-herd reconnects.
_JITTER_MAX_SECONDS: Final[float] = 2.0

_BUFFER_SIZE = 64 * 1024

#: After Claude closes stdin, how long to wait for the socket pump
#: to drain any in-flight daemon response before cancelling. Half-
#: closing the UDS writer (write_eof) signals the daemon to finish
#: + close; this budget bounds the wait. Two seconds is generous for
#: a healthy daemon and short enough not to block proxy shutdown.
_STDIN_DRAIN_BUDGET_SECONDS = 2.0
#: Threshold above which the daemon may be replaying a large WAL on
#: startup; we extend the spawn timeout by ``wal_recovery_grace_seconds``
#: when ``engram.db-wal`` exceeds this size at proxy start time.
_WAL_LARGE_THRESHOLD_BYTES = 10 * 1024 * 1024


class _SupportsWrite(Protocol):
    """Minimal writer surface used by the proxy's socket→stdout pump."""

    def write(self, data: bytes, /) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...


class _ShuffleExit(enum.StrEnum):
    """Why ``_shuffle_bytes`` returned.

    ``STDIN_CLOSED`` means the MCP client (Claude Code) closed its side of
    the pipe; the proxy should exit cleanly. ``SOCKET_CLOSED`` means the
    daemon disappeared (crashed, was restarted, or idle-shut-down); the
    proxy should reconnect via :meth:`_reconnect_with_backoff` and resume
    shuffling so the MCP client doesn't see its server vanish.
    """

    STDIN_CLOSED = "stdin_closed"
    SOCKET_CLOSED = "socket_closed"


class DaemonClient:
    """Proxy process: connect to the per-vault daemon and shuffle bytes."""

    def __init__(
        self,
        *,
        vault_path: Path,
        daemon_config: DaemonConfig,
        stdin_reader: asyncio.StreamReader | None = None,
        stdout_writer: _SupportsWrite | None = None,
        retry_delays: Sequence[float] = _PROXY_RETRY_DELAYS_SECONDS,
        spawn_fn: SpawnFn | None = None,
        stdin_drain_budget_seconds: float = _STDIN_DRAIN_BUDGET_SECONDS,
    ) -> None:
        """Construct a proxy client.

        Args:
            vault_path: Path to the vault directory (with ``.indexes/``).
            daemon_config: Per-vault :class:`DaemonConfig` settings.
            stdin_reader: Override for ``sys.stdin`` (tests inject a
                pre-seeded :class:`asyncio.StreamReader`).
            stdout_writer: Override for ``sys.stdout`` (tests inject a
                writer that captures bytes for assertions).
            retry_delays: Reconnect-backoff schedule. Production uses
                the spec-mandated ``(1.0, 4.0, 16.0)``; tests can pass
                small values to keep test wall-clock low.
            spawn_fn: Override for :func:`_spawn_daemon_process`; tests
                pass a mock to avoid ``fork()``.
            stdin_drain_budget_seconds: How long to wait for the socket
                pump to drain pending daemon response after stdin EOFs
                (Claude closed). Default 2s; tests pass small values.
        """
        self.vault_path = vault_path
        self.daemon_config = daemon_config
        self.paths = resolve_paths(vault_path)
        self._stdin_reader = stdin_reader
        self._stdout_writer = stdout_writer
        self._retry_delays = tuple(retry_delays)
        self._spawn_fn: SpawnFn = spawn_fn or _spawn_daemon_process
        self._stdin_drain_budget_seconds = stdin_drain_budget_seconds

    # -- top-level proxy entry ----------------------------------------

    async def run_proxy_loop(self) -> int:
        """Connect (spawn if needed) and shuffle bytes; reconnect across daemon restarts.

        Returns 0 when the proxy's stdin EOFs (Claude Code closed the
        MCP server), 1 if reconnection exhausts the backoff schedule
        after the daemon disappears. On mid-session UDS EOF the proxy
        runs :meth:`_reconnect_with_backoff` and resumes the shuffle so
        the MCP client's tool registry survives daemon restarts.

        Coverage: each helper below is unit-tested directly. The
        top-level orchestration including the reconnect loop is
        exercised by :func:`test_run_proxy_loop_reconnects_after_daemon_eof`
        with mock streams.
        """
        reader, writer = await self._connect_with_spawn_if_missing()
        while True:
            try:
                result = await self._shuffle_bytes(reader, writer)
            finally:
                with contextlib.suppress(OSError):
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()

            if result is _ShuffleExit.STDIN_CLOSED:
                return 0

            # SOCKET_CLOSED: daemon disappeared mid-session. Reconnect
            # so the MCP client doesn't see its server vanish. If the
            # backoff schedule exhausts, surface a non-zero exit so the
            # parent (Claude Code) can show the MCP-server-died state.
            _log.warning(
                "daemon UDS EOF; reconnecting with backoff (vault=%s socket=%s)",
                self.vault_path,
                self.paths.socket,
            )
            try:
                reader, writer = await self._reconnect_with_backoff()
            except DaemonConnectionError as exc:
                _log.error(
                    "proxy failed to reconnect after daemon disappearance: %s",
                    exc,
                )
                return 1

    # -- connection lifecycle -----------------------------------------

    async def _connect_with_spawn_if_missing(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Try-connect; on miss, run the spawn dance, then connect.

        Holds the spawn flock only for the brief duration of the fork-
        and-wait-for-ready dance; releases before the proxy attaches.
        """
        connection = await _try_connect(self.paths.socket)
        if connection is not None:
            return connection

        # Spawn dance — serialized by the spawn flock so two cold
        # proxies racing against an absent daemon do not both fork.
        try:
            with acquire_spawn_lock(
                self.paths.spawn_lock,
                timeout_seconds=self.daemon_config.spawn_lock_timeout_seconds,
            ):
                # Recheck — another spawner may have produced a socket
                # while we waited on the flock.
                connection = await _try_connect(self.paths.socket)
                if connection is not None:
                    return connection
                await self._spawn_fn(
                    vault_path=self.vault_path,
                    spawn_timeout_seconds=self.daemon_config.spawn_timeout_seconds,
                    wal_recovery_grace_seconds=self.daemon_config.wal_recovery_grace_seconds,
                )
        except SpawnLockTimeoutError as exc:
            # Surface as a connection error so the proxy retry loop
            # can decide whether to back off + retry.
            raise DaemonConnectionError(str(exc)) from exc

        # After the lock releases (daemon is ready), attach.
        connection = await _try_connect(self.paths.socket)
        if connection is None:
            msg = f"daemon reported ready but UDS connect failed: {self.paths.socket}"
            raise DaemonConnectionError(msg)
        return connection

    async def _reconnect_with_backoff(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """3-retry exponential backoff per spec Section 5.6.

        Returns the new (reader, writer) on first success. Raises
        :class:`DaemonConnectionError` after the configured schedule
        is exhausted.
        """
        last_error: Exception | None = None
        for attempt, base_delay in enumerate(self._retry_delays, start=1):
            jitter = random.uniform(0.0, _JITTER_MAX_SECONDS / attempt)  # noqa: S311 - non-security
            await asyncio.sleep(base_delay + jitter)
            try:
                return await self._connect_with_spawn_if_missing()
            except (
                DaemonSpawnError,
                DaemonConnectionError,
                SpawnLockTimeoutError,
                OSError,
            ) as exc:
                last_error = exc
                _log.warning(
                    "proxy reconnect attempt %d/%d failed: %s",
                    attempt,
                    len(self._retry_delays),
                    exc,
                )
        msg = (
            f"{len(self._retry_delays)} retries exhausted while reconnecting "
            f"to the engram daemon for vault {self.vault_path}; last error: "
            f"{last_error}"
        )
        raise DaemonConnectionError(msg)

    # -- byte shuffle --------------------------------------------------

    async def _shuffle_bytes(
        self,
        socket_reader: asyncio.StreamReader,
        socket_writer: asyncio.StreamWriter,
    ) -> _ShuffleExit:
        """Bidirectional byte shuffle between stdin/stdout and the UDS.

        Returns ``STDIN_CLOSED`` when Claude closed its side (the proxy
        should exit) or ``SOCKET_CLOSED`` when the daemon disappeared
        (the proxy should reconnect via
        :meth:`_reconnect_with_backoff` and resume).

        The two EOF cases need different handling:

        * Stdin EOF: Claude is shutting down. Half-close the UDS writer
          so the daemon sees EOF and drains its outgoing buffer; let the
          socket pump deliver any pending response with a small budget
          before returning. Without this, an echo-and-close mock daemon
          would lose its echo to a premature pump-cancel.
        * Socket EOF: the daemon died. Cancel the stdin pump (no point
          reading more from Claude when we can't deliver) and return so
          the proxy loop can reconnect.
        """
        stdin = self._stdin_reader or await _wrap_stdin()
        stdout = self._stdout_writer or await _wrap_stdout()

        async def stdin_to_socket() -> _ShuffleExit:
            try:
                while True:
                    data = await stdin.read(_BUFFER_SIZE)
                    if not data:
                        return _ShuffleExit.STDIN_CLOSED
                    socket_writer.write(data)
                    try:
                        await socket_writer.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        return _ShuffleExit.SOCKET_CLOSED
            except OSError:
                return _ShuffleExit.SOCKET_CLOSED

        async def socket_to_stdout() -> _ShuffleExit:
            try:
                while True:
                    data = await socket_reader.read(_BUFFER_SIZE)
                    if not data:
                        return _ShuffleExit.SOCKET_CLOSED
                    stdout.write(data)
                    try:
                        await stdout.drain()
                    except (ConnectionResetError, BrokenPipeError):
                        return _ShuffleExit.STDIN_CLOSED
            except OSError:
                return _ShuffleExit.SOCKET_CLOSED

        stdin_task = asyncio.create_task(stdin_to_socket())
        socket_task = asyncio.create_task(socket_to_stdout())
        done, pending = await asyncio.wait(
            [stdin_task, socket_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        first = done.pop()
        first_result = first.result()

        if first_result is _ShuffleExit.STDIN_CLOSED:
            # Claude closed. Half-close our writer side so the daemon
            # sees EOF and finishes any in-flight response, then drain
            # the socket pump with a small budget before exiting.
            with contextlib.suppress(Exception):
                if socket_writer.can_write_eof():
                    socket_writer.write_eof()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=self._stdin_drain_budget_seconds,
                )
            except TimeoutError:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            return _ShuffleExit.STDIN_CLOSED

        # SOCKET_CLOSED: cancel stdin pump and return so the proxy loop
        # can reconnect. No point waiting on stdin when we have nowhere
        # to deliver bytes.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return _ShuffleExit.SOCKET_CLOSED


# -- module helpers (testable as free functions) -----------------------


class SpawnFn(Protocol):
    """Type for the daemon-spawn callable. Tests substitute a mock."""

    async def __call__(
        self,
        *,
        vault_path: Path,
        spawn_timeout_seconds: int,
        wal_recovery_grace_seconds: int,
    ) -> None:
        """Fork the daemon + wait for readiness; raise DaemonSpawnError on failure."""


async def _try_connect(
    socket_path: Path,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """Try to connect to the UDS; return ``None`` on missing socket or refused.

    Used both for the first connect attempt and the rechecks inside the
    spawn dance (after acquiring the spawn flock another proxy may have
    won the race).
    """
    if not socket_path.exists():
        return None
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        return None
    return reader, writer


async def _spawn_daemon_process(  # pragma: no cover - forks the process; smoke-covered
    *,
    vault_path: Path,
    spawn_timeout_seconds: int,
    wal_recovery_grace_seconds: int,
) -> None:
    """Fork the daemon process + wait for its readiness signal.

    Single-fork: the child execs ``engram daemon start --vault-path
    <path> --readiness-fd <wfd>``. The child performs its own
    double-fork detach inside that subcommand. The parent (the proxy)
    reads the readiness pipe and surfaces error or success.

    Effective timeout is ``spawn_timeout_seconds`` plus
    ``wal_recovery_grace_seconds`` when the WAL file at spawn time
    exceeds 10 MiB — the daemon may need extra time to replay before
    binding.

    Coverage note: ``os.fork()`` inside a pytest worker would clone
    the test runner. End-to-end behavior is exercised by the hermetic
    CLI smoke that spawns the binary in a subprocess.
    """
    rfd, wfd = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(rfd)
        os.close(wfd)
        raise

    if pid == 0:
        # Child. ``sys.executable`` is the running interpreter — no shell
        # involved, so ruff S606 ("starting a process without a shell")
        # is the intended posture for an explicit ``execvpe`` of a
        # known-trusted python binary.
        os.close(rfd)
        try:
            os.execvpe(  # noqa: S606 - explicit no-shell exec of sys.executable
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "engram",
                    "daemon",
                    "start",
                    "--vault-path",
                    str(vault_path),
                    "--readiness-fd",
                    str(wfd),
                ],
                os.environ.copy(),
            )
        except OSError as exc:
            msg = f"error: failed to exec engram daemon start: {exc}\n"
            with contextlib.suppress(OSError):
                os.write(wfd, msg.encode("utf-8"))
            os._exit(1)

    # Parent.
    os.close(wfd)

    effective_timeout = float(spawn_timeout_seconds)
    wal_path = vault_path / ".indexes" / "engram.db-wal"
    try:
        if wal_path.exists() and wal_path.stat().st_size > _WAL_LARGE_THRESHOLD_BYTES:
            effective_timeout += wal_recovery_grace_seconds
    except OSError:
        # WAL stat can race with daemon startup; default to nominal timeout.
        pass

    try:
        result = await wait_for_ready(rfd, timeout_seconds=effective_timeout)
    except TimeoutError as exc:
        msg = (
            f"daemon readiness pipe timed out after {effective_timeout}s; "
            f"check {vault_path}/.indexes/engram.log for the daemon's last words"
        )
        raise DaemonSpawnError(msg) from exc

    if result.is_error:
        msg = f"daemon spawn reported error: {result.message}"
        raise DaemonSpawnError(msg)


async def _wrap_stdin() -> asyncio.StreamReader:  # pragma: no cover - real stdin pipe required
    """Wrap ``sys.stdin`` as an :class:`asyncio.StreamReader`.

    Requires a real pipe-shaped stdin (Claude Code MCP invocation pipes
    stdin/stdout); pytest workers have a TTY-shaped stdin which fails
    ``connect_read_pipe`` with ``OSError: Invalid argument``. Tests
    inject a pre-seeded ``StreamReader`` via the ``DaemonClient``
    constructor instead.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


async def _wrap_stdout() -> asyncio.StreamWriter:  # pragma: no cover - real stdout pipe required
    """Wrap ``sys.stdout`` as an :class:`asyncio.StreamWriter`.

    Same pytest-worker stdio limitation as :func:`_wrap_stdin`. Tests
    inject a writer through the ``DaemonClient`` constructor.
    """
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_write_pipe(
        lambda: asyncio.streams.FlowControlMixin(loop=loop),
        sys.stdout.buffer,
    )
    return asyncio.StreamWriter(transport, protocol, reader, loop)


__all__ = [
    "DaemonClient",
    "SpawnFn",
]
