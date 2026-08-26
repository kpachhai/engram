"""Daemon server process: UDS accept loop + per-connection MCP sessions.

The daemon owns one primary vault's resources (``VaultLock``,
``VaultStorage``, ``SyncCoordinator``, ``FastEmbedProvider``,
``FastMCP``) and accepts ``N`` concurrent UDS connections from proxy
processes (``engram serve`` in proxy mode). Each connection drives one
MCP session via :func:`engram.daemon.fastmcp_dispatch.serve_session`.

Construction is dependency-injected: callers pass a pre-built
:class:`engram.cli.serve.ServeRuntime` (acquired via
:func:`engram.cli.serve._init_serve_runtime` with
``install_signal_handlers=False``) plus a :class:`SocketPaths` and a
:class:`DaemonConfig`. The ``engram daemon start`` CLI entrypoint
wires these.

Startup ordering is enforced by the caller's wiring:

1. Caller installs daemon signal handlers BEFORE constructing this server.
2. Caller's ``_init_serve_runtime(install_signal_handlers=False)``
   acquires ``VaultLock``, opens storage, builds the FastMCP server.
3. :meth:`serve_forever` unlinks any stale socket, binds, chmods 0600,
   writes the state file, then sets the readiness event.

Two-phase atomic idle shutdown is implemented via ``_shutdown_lock``
so a proxy that reconnects between timer fire and listener close is
never silently dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import stat
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from engram import __version__
from engram.config.models import DaemonConfig
from engram.daemon.auth import check_peer_or_reject
from engram.daemon.fastmcp_dispatch import serve_session
from engram.daemon.socket_paths import SocketPaths, resolve_paths
from engram.daemon.state import DaemonState, write_state
from engram.errors import PeerCredRejectError

if TYPE_CHECKING:
    from engram.cli.serve import ServeRuntime

_log = logging.getLogger("engram.daemon.server")


class DaemonServer:
    """Per-vault daemon process owning the UDS listener + shared singletons."""

    def __init__(
        self,
        *,
        runtime: ServeRuntime,
        daemon_config: DaemonConfig,
        paths: SocketPaths | None = None,
        clock: Callable[[], float] | None = None,
        readiness_fd: int | None = None,
    ) -> None:
        r"""Construct against a pre-built runtime.

        Args:
            runtime: The :class:`ServeRuntime` from
                :func:`engram.cli.serve._init_serve_runtime` (with
                ``install_signal_handlers=False`` so the daemon can
                wire its own SIGTERM/SIGINT handler). Owns the
                ``VaultLock``, ``VaultStorage``, ``SyncCoordinator``,
                embedder, and the built FastMCP server. The daemon
                takes ownership and tears it down during
                :meth:`_drain_and_exit`.
            daemon_config: Resolved :class:`DaemonConfig`. Typically
                ``runtime.config.daemon``.
            paths: Pre-resolved :class:`SocketPaths`; defaults to
                ``resolve_paths(runtime.config.vault_path)``.
            clock: Optional monotonic-time callable for tests. Defaults
                to :func:`asyncio.get_event_loop().time` when running.
            readiness_fd: When the daemon was spawned by a proxy, this
                is the write-end FD of a pipe the proxy reads from to
                detect successful spawn. The daemon writes ``ready\n``
                after binding and closes the FD. On a startup error
                the ``engram daemon start`` entrypoint writes
                ``error: <msg>\n`` instead.
        """
        self.runtime = runtime
        self.daemon_config = daemon_config
        self.paths: SocketPaths = paths or resolve_paths(runtime.config.vault_path)
        self._clock = clock
        self._readiness_fd = readiness_fd

        self._ready_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self._connected_proxies = 0
        self._idle_timer_task: asyncio.Task[None] | None = None
        self._server: asyncio.base_events.Server | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._signal_handlers_installed = False

        # Diagnostics counters surfaced via ``engram daemon status``.
        self._requests_total = 0
        self._requests_error = 0
        self._peer_cred_rejects = 0
        self._connect_during_drain = 0
        self._last_request_at: str | None = None

    # -- public API ----------------------------------------------------

    async def serve_forever(self) -> None:
        """Main daemon entrypoint (binds the UDS, runs the accept loop, drains).

        The caller's spawn helper has already done steps 1-6 (signal
        handlers, ``VaultLock``, probes, storage, coordinator, FastMCP).
        This method does:

        7. ``unlink`` stale socket inode.
        8. ``bind`` the UDS via ``asyncio.start_unix_server``.
        9. ``chmod 0o600`` the socket.
        10. ``write_state`` to ``engram.state.json``.
        11. Set the readiness event.
        12. Accept loop until ``shutdown_event`` fires, then drain.
        """
        self._install_async_signal_handlers()

        # 7. Unlink any stale inode at the socket path.
        with contextlib.suppress(FileNotFoundError):
            self.paths.socket.unlink()

        # 8. Bind UDS. ``limit`` controls the StreamReader buffer size
        # so a single frame up to ``max_frame_bytes`` can be read.
        self._server = await asyncio.start_unix_server(
            self._handle_connection,
            path=str(self.paths.socket),
            limit=self.daemon_config.max_frame_bytes,
        )

        # 9. chmod 0o600 as belt-and-suspenders against odd umask state.
        self.paths.socket.chmod(0o600)

        # 10. Write state.json with PID + hostname for cross-machine sanity.
        write_state(
            self.paths.state_file,
            DaemonState(
                pid=os.getpid(),
                started_at=datetime.now(UTC).isoformat(),
                vault_name=self.runtime.config.vault_name,
                vault_path=str(self.paths.vault),
                hostname=socket.gethostname(),
                config_snapshot=self.daemon_config.model_dump(),
                engram_version=__version__,
            ),
        )

        # 11. Signal readiness to the spawning proxy.
        self._ready_event.set()
        if self._readiness_fd is not None:
            # Defensive: if the proxy passed a stale fd number (e.g. exec
            # closed the pipe via FD_CLOEXEC), the kernel may have reused
            # that number for SQLite's engram.db. Writing + closing it
            # would corrupt and close the main-db fd. Verify it's a pipe
            # first; if not, skip and warn — never write to an unknown fd.
            try:
                st = os.fstat(self._readiness_fd)
                is_pipe = stat.S_ISFIFO(st.st_mode)
            except OSError:
                is_pipe = False
            if is_pipe:
                with contextlib.suppress(OSError):
                    os.write(self._readiness_fd, b"ready\n")
                    os.close(self._readiness_fd)
            else:
                _log.warning(
                    "readiness_fd=%d is not a pipe; skipping write+close "
                    "to avoid clobbering a reused fd (likely caller did not "
                    "set os.set_inheritable on the pipe write end)",
                    self._readiness_fd,
                )
            self._readiness_fd = None
        _log.info(
            "engram daemon ready: vault=%s pid=%s socket=%s",
            self.runtime.config.vault_name,
            os.getpid(),
            self.paths.socket,
        )

        # If idle-shutdown is enabled AND we start with zero connections,
        # arm the timer immediately so an unused daemon eventually exits.
        if self.daemon_config.idle_shutdown_seconds > 0:
            self._arm_idle_timer()

        # 12. Accept loop blocks until shutdown is requested. When the
        # event fires, we proactively cancel in-flight connection
        # handlers BEFORE exiting the ``async with`` context. The
        # context manager's ``__aexit__`` calls ``Server.wait_closed()``
        # which would otherwise block indefinitely waiting for the
        # handlers to finish - and they cannot finish on their own
        # because they are blocked in ``await reader.read()`` against a
        # proxy stream that hasn't closed. Cancelling the tasks lets
        # the streams unwind, ``wait_closed`` returns immediately, and
        # ``_drain_and_exit`` runs the budgeted shutdown sequence.
        # Periodic pump: a 1-second no-op timer guarantees the selector
        # loop returns from its blocking call regularly. Pending Python
        # signal handlers fire when the eval loop regains control after a
        # syscall returns; without periodic returns, signal delivery can
        # be deferred until an unrelated event wakes the loop (observed
        # on macOS with multiple in-flight UDS connections: SIGTERM
        # arrival blocks until a proxy keepalive ~60s later wakes the
        # loop). The pump keeps wake-up latency at 1s worst-case.
        pump = asyncio.create_task(self._signal_wakeup_pump())
        try:
            async with self._server:
                await self._shutdown_event.wait()
                for task in list(self._in_flight):
                    task.cancel()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await self._drain_and_exit()

    async def wait_until_ready(self, *, timeout: float) -> None:
        """Block until :meth:`serve_forever` has bound the socket + signaled ready."""
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def request_shutdown(self) -> None:
        """Trigger graceful shutdown (callable from signal handlers and tests)."""
        self._shutdown_event.set()

    async def _signal_wakeup_pump(self) -> None:
        """Wake the asyncio selector every second so pending signals fire promptly.

        Without this, an idle daemon's selector can block on ``kevent``
        for many seconds before SIGTERM is routed through to its Python
        handler (observed on macOS with multiple idle UDS connections
        registered in the kqueue). The pump caps the worst-case
        shutdown latency at ``~1s`` regardless of selector state.
        """
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return

    @property
    def connected_proxies(self) -> int:
        """Current count of accepted, non-rejected, non-closed connections."""
        return self._connected_proxies

    @property
    def peer_cred_rejects(self) -> int:
        """Counter of connections rejected by the peer-cred check."""
        return self._peer_cred_rejects

    @property
    def connect_during_drain(self) -> int:
        """Counter of ``accept()`` attempts after the listener close (drain contract)."""
        return self._connect_during_drain

    # -- internals -----------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """One asyncio task per accepted connection."""
        sock = writer.get_extra_info("socket")
        peer_fd = sock.fileno() if sock is not None else -1

        # SO_PEERCRED / getpeereid same-UID check.
        try:
            if peer_fd >= 0:
                check_peer_or_reject(peer_fd)
        except PeerCredRejectError as exc:
            _log.warning("peer cred reject: %s", exc)
            self._peer_cred_rejects += 1
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return

        # Refuse new connections during drain (two-phase atomic shutdown contract).
        async with self._shutdown_lock:
            if self._shutdown_event.is_set():
                self._connect_during_drain += 1
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                return
            self._connected_proxies += 1
            self._cancel_idle_timer()

        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)
        try:
            await serve_session(
                self.runtime.fastmcp_server,
                uds_reader=reader,
                uds_writer=writer,
                max_frame_bytes=self.daemon_config.max_frame_bytes,
            )
        except Exception:
            self._requests_error += 1
            _log.exception("connection handler raised")
        finally:
            async with self._shutdown_lock:
                self._connected_proxies -= 1
                if (
                    self._connected_proxies == 0
                    and self.daemon_config.idle_shutdown_seconds > 0
                    and not self._shutdown_event.is_set()
                ):
                    self._arm_idle_timer()
            if task is not None:
                self._in_flight.discard(task)

    def _arm_idle_timer(self) -> None:
        """Start (or restart) the idle-shutdown timer."""
        self._cancel_idle_timer()
        self._idle_timer_task = asyncio.create_task(self._idle_timer_loop())

    def _cancel_idle_timer(self) -> None:
        """Cancel a pending idle-shutdown timer task, if any."""
        if self._idle_timer_task is not None and not self._idle_timer_task.done():
            self._idle_timer_task.cancel()
        self._idle_timer_task = None

    async def _idle_timer_loop(self) -> None:
        """Two-phase atomic idle shutdown (two-phase atomic shutdown contract)."""
        try:
            await asyncio.sleep(self.daemon_config.idle_shutdown_seconds)
        except asyncio.CancelledError:
            return

        async with self._shutdown_lock:
            if self._connected_proxies > 0:
                # A proxy reconnected between fire and lock acquire; the
                # disconnect path will re-arm the timer when the count
                # returns to zero.
                return
            # Second phase: close the listener atomically under the lock so
            # any concurrent ``accept()`` either has succeeded already
            # (and bumped ``_connected_proxies``) or fails after this
            # point.
            if self._server is not None:
                self._server.close()
            self._shutdown_event.set()

    async def _drain_and_exit(self) -> None:
        """Graceful shutdown drain — runs in a fixed order with explicit budgets.

        1. Stop accepting (listener is already closed by the shutdown path).
        2. Wait for in-flight tasks OR force-cancel after
           ``shutdown_drain_seconds``.
        3. ``coordinator.force_flush`` within ``coordinator_flush_seconds``.
        4. Close storage.
        5. Release vault lock.
        6. Unlink socket file.
        7. Unlink state file.
        """
        # 0. Cancel any pending idle-shutdown timer so asyncio doesn't
        # log "Task was destroyed but it is pending!" on loop close.
        self._cancel_idle_timer()

        # 1. Listener close (idempotent — may have already been closed by
        # the idle-shutdown phase 2 or the asyncio context manager exit).
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.close()
                with contextlib.suppress(Exception):
                    await self._server.wait_closed()

        # 2. In-flight drain or force-cancel.
        if self._in_flight:
            in_flight = list(self._in_flight)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*in_flight, return_exceptions=True),
                    timeout=self.daemon_config.shutdown_drain_seconds,
                )
            except TimeoutError:
                _log.warning(
                    "drain timeout after %ss; cancelling %d in-flight tasks",
                    self.daemon_config.shutdown_drain_seconds,
                    len(in_flight),
                )
                for t in in_flight:
                    t.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.gather(*in_flight, return_exceptions=True)

        # 3. Coordinator force-flush (its own budget).
        coordinator = self.runtime.coordinator
        if coordinator is not None:
            stop_fn = getattr(coordinator, "stop", None)
            if callable(stop_fn):
                try:
                    maybe = stop_fn()
                    if isinstance(maybe, Awaitable):
                        await asyncio.wait_for(
                            maybe,
                            timeout=self.daemon_config.coordinator_flush_seconds,
                        )
                except TimeoutError:
                    _log.warning(
                        "coordinator force-flush timed out after %ss",
                        self.daemon_config.coordinator_flush_seconds,
                    )
                except Exception:
                    _log.exception("coordinator stop raised during drain")

        # 4. Close storage.
        with contextlib.suppress(Exception):
            self.runtime.storage.close()

        # 5. Release vault lock.
        with contextlib.suppress(Exception):
            self.runtime.vault_lock.release()

        # 6 + 7. Unlink socket + state file.
        with contextlib.suppress(FileNotFoundError):
            self.paths.socket.unlink()
        with contextlib.suppress(FileNotFoundError):
            self.paths.state_file.unlink()

        _log.info("engram daemon stopped")

    def _install_async_signal_handlers(self) -> None:
        """Install SIGTERM/SIGINT handlers, with a synchronous fallback.

        The daemon owns its own signal handling; the caller
        passes ``install_signal_handlers=False`` to ``VaultLock`` so we
        do not stomp these handlers from the lock side.

        Two layers:

        1. ``loop.add_signal_handler`` is the canonical asyncio path. It
           wires up the wakeup-fd and registers the Python callback to be
           dispatched from the selector loop. When it works, shutdown
           completes in milliseconds.

        2. ``signal.signal`` is installed AFTER step 1 and overrides the
           noop Python handler asyncio installs. On macOS, when the
           daemon has multiple in-flight UDS connections registered with
           the kqueue selector, the asyncio-side dispatch of the
           wakeup-fd byte for SIGTERM is observably delayed by ~60s.
           A Python-level handler fires reliably during the
           pending-signals check that runs when the eval loop returns
           from a blocking syscall, and ``call_soon_threadsafe`` lets
           it cross back into the loop. Step 1 is still required because
           it sets up the global wakeup-fd; without it the C-level
           wrapper has nowhere to write and the eval loop never gets
           control on signal arrival.

        Reproduced under Python 3.12.13 + anyio 4.x + 4 idle proxy
        connections; verified that direct write to wakeup-fd triggers
        shutdown in <50ms while ``kill -TERM`` through asyncio alone
        hangs ~60s. The two-layer install drops time-to-stop to <1s.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Should never happen — serve_forever is called from inside a loop.
            return

        def _shutdown_handler(signum: int, frame: object | None) -> None:
            del signum, frame
            # Signal handlers run on the main thread between bytecode
            # instructions; setting the event directly is safe and
            # immediate. ``call_soon_threadsafe`` also writes to the
            # asyncio self-pipe so any blocking selector returns.
            self._shutdown_event.set()
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(self.request_shutdown)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                # Layer 1: asyncio path — wires up the wakeup-fd.
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, ValueError):
                # add_signal_handler is unavailable in worker threads
                # (pytest event loops); not fatal for a non-main loop.
                continue
            try:
                # Layer 2: synchronous fallback — fires from the Python
                # eval loop's pending-signals check, robust against
                # selector-dispatch delays seen with many fds on macOS.
                signal.signal(sig, _shutdown_handler)
            except (ValueError, OSError):
                continue
        self._signal_handlers_installed = True


__all__ = ["DaemonServer"]
