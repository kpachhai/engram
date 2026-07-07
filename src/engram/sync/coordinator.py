"""Sync coordinator state machine.

The coordinator owns the post-capture queue, debounces commits, performs
fetch/pull/push with bounded retry, detects conflicts, and exposes a
manual ``engram sync`` entry point. State transitions are validated at
runtime against :data:`ALLOWED_TRANSITIONS`; any disallowed transition
raises :class:`engram.errors.SyncError` rather than silently advancing.

Threading model: the coordinator runs as one asyncio task per
``engram serve`` process. All git invocations route through
:mod:`engram.sync.gitops` (which uses :func:`asyncio.to_thread` to keep
the loop responsive). One :class:`asyncio.Lock` serializes git work so
two concurrent enqueues cannot race the same repo state.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from engram.errors import SyncError
from engram.sync import gitops
from engram.sync.gitops import (
    GitErrorClass,
    PullResult,
    PushResult,
)
from engram.utils.run_command import run_git

_log = logging.getLogger("engram.sync.coordinator")

#: Cap on event-log retention; older events fall off the ring.
EVENT_BUFFER_SIZE = 256


class SyncState(enum.StrEnum):
    """Explicit coordinator states.

    The 10-element set is closed: any new state requires updating
    :data:`ALLOWED_TRANSITIONS`. ``committed_not_pushed`` is the durable
    persisted-locally-but-not-replicated state for resume-on-startup;
    ``manual-resolution-required`` is a terminal error state (the operator
    must intervene with ``engram sync --resume`` or manual git work).
    """

    IDLE = "idle"
    DEBOUNCING = "debouncing"
    COMMITTING = "committing"
    COMMITTED_NOT_PUSHED = "committed_not_pushed"
    FETCHING = "fetching"
    PUSHING = "pushing"
    PAUSED_FOR_MIGRATION = "paused_for_migration"
    AUTH_REQUIRED = "auth_required"
    MANUAL_RESOLUTION_REQUIRED = "manual_resolution_required"
    DISABLED = "disabled"


#: Allowed forward transitions. ``DISABLED`` is reachable from anywhere
#: when the kill-switch is flipped (handled separately).
ALLOWED_TRANSITIONS: dict[SyncState, frozenset[SyncState]] = {
    SyncState.IDLE: frozenset(
        {
            SyncState.DEBOUNCING,
            SyncState.COMMITTING,  # explicit sync --push when nothing queued
            SyncState.FETCHING,  # startup pull / sync --pull
            SyncState.PUSHING,  # explicit sync --push when commit already done
            SyncState.PAUSED_FOR_MIGRATION,
            SyncState.DISABLED,
        }
    ),
    SyncState.DEBOUNCING: frozenset(
        {
            SyncState.DEBOUNCING,  # re-enqueue resets the timer (no-op transition)
            SyncState.COMMITTING,
            SyncState.PAUSED_FOR_MIGRATION,
            SyncState.DISABLED,
            SyncState.IDLE,  # cancellation / drain-on-shutdown
        }
    ),
    SyncState.COMMITTING: frozenset(
        {
            SyncState.IDLE,
            SyncState.PUSHING,
            SyncState.COMMITTED_NOT_PUSHED,
            SyncState.MANUAL_RESOLUTION_REQUIRED,
            SyncState.DISABLED,
        }
    ),
    SyncState.PUSHING: frozenset(
        {
            SyncState.IDLE,
            SyncState.COMMITTED_NOT_PUSHED,
            SyncState.AUTH_REQUIRED,
            SyncState.FETCHING,  # NON_FAST_FORWARD -> fetch + rebase + retry
            SyncState.MANUAL_RESOLUTION_REQUIRED,
            SyncState.DISABLED,
        }
    ),
    SyncState.FETCHING: frozenset(
        {
            SyncState.IDLE,
            SyncState.PUSHING,  # rebase + retry push
            SyncState.MANUAL_RESOLUTION_REQUIRED,
            SyncState.DISABLED,
        }
    ),
    SyncState.COMMITTED_NOT_PUSHED: frozenset(
        {
            SyncState.IDLE,  # nothing else to do this cycle
            SyncState.PUSHING,  # resume push attempt
            SyncState.PAUSED_FOR_MIGRATION,
            SyncState.DISABLED,
        }
    ),
    SyncState.PAUSED_FOR_MIGRATION: frozenset(
        {
            SyncState.IDLE,  # migration finished
            SyncState.DISABLED,
        }
    ),
    SyncState.AUTH_REQUIRED: frozenset(
        {
            SyncState.IDLE,  # only after manual resolution + sync --resume
            SyncState.DISABLED,
        }
    ),
    SyncState.MANUAL_RESOLUTION_REQUIRED: frozenset(
        {
            SyncState.IDLE,  # only after manual resolution
            SyncState.DISABLED,
        }
    ),
    SyncState.DISABLED: frozenset({SyncState.IDLE}),  # re-enable via config reload
}


@dataclass(frozen=True, slots=True)
class SyncEvent:
    """One entry of the in-memory ring buffer."""

    timestamp: float
    from_state: SyncState
    to_state: SyncState
    note: str


@dataclass
class CoordinatorConfig:
    """Tunables passed by ``engram serve`` from :class:`SyncConfig`."""

    debounce_window_seconds: float = 60.0
    max_deferral_seconds: float = 300.0
    push_retry_count: int = 3
    push_retry_backoff_seconds: float = 1.0
    push_timeout_seconds: float = 60.0
    git_remote: str = "origin"
    git_branch: str = "main"
    role: str = "primary"
    auto_commit_on_capture: bool = True
    auto_push_on_capture: bool = False
    use_no_verify: bool = True
    user_email: str | None = None
    user_name: str | None = None
    #: Optional callback returning True when the migration lock is held.
    migration_held: object = field(default=None)


def _safe_path_arg(path: Path, *, base: Path) -> str:
    """Return ``path`` as repo-relative when possible, else absolute."""
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


class SyncCoordinator:
    """Drive the multi-machine sync state machine for one vault.

    The coordinator does not start any background tasks until
    :meth:`start` is called; the ``engram serve`` startup ordering puts
    the doctor probes + ``VaultLock`` ahead of this so a misconfigured
    vault never enters the run loop.
    """

    def __init__(
        self,
        *,
        repo_dir: Path,
        config: CoordinatorConfig,
        push_queue: object | None = None,
    ) -> None:
        """Construct a coordinator bound to ``repo_dir``.

        Args:
            repo_dir: Vault root containing the ``.git/`` directory.
            config: Coordinator config.
            push_queue: Optional :class:`engram.team.push_queue.PersistentPushQueue`.
                When supplied, the coordinator uses it for durable
                enqueue (so an engram restart replays pending pushes)
                + orphans on auth-failure. Personal vaults typically
                pass None; team-write vaults pass a queue rooted at
                ``<vault>/.engram/push-queue.local``.
        """
        self.repo_dir = repo_dir
        self.config = config
        self._state: SyncState = SyncState.IDLE
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._git_lock: asyncio.Lock = asyncio.Lock()
        self._events: deque[SyncEvent] = deque(maxlen=EVENT_BUFFER_SIZE)
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._max_deferral_handle: asyncio.TimerHandle | None = None
        self._first_enqueue_at: float | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._push_queue = push_queue
        # Drain any persistent queue from a prior process at construction;
        # the actual replay into the in-memory queue happens at start()
        # so the coordinator's logging is fully wired.
        self._needs_replay = push_queue is not None

    # === public API ===

    @property
    def state(self) -> SyncState:
        """Current state. Read-only - use :meth:`_transition` to change it."""
        return self._state

    @property
    def queue_depth(self) -> int:
        """Pending captures awaiting a commit (size of the asyncio queue)."""
        return self._queue.qsize()

    @property
    def events(self) -> tuple[SyncEvent, ...]:
        """Snapshot of the ring buffer for doctor inspection."""
        return tuple(self._events)

    def enqueue(self, path: Path, *, thought_id: str | None = None) -> None:
        """Append ``path`` to the commit queue and arm the debounce timer.

        Synchronous so :meth:`engram.storage.facade.VaultStorage.capture`
        can call from a non-async context. The actual commit work happens
        on the loop tick driven by :meth:`run`.

        When a persistent push queue is configured (team-write vaults),
        the path is written to disk BEFORE landing in the in-memory queue
        so a crash between capture and push doesn't silently drop work.
        """
        if self._push_queue is not None:
            try:
                relative = path.relative_to(self.repo_dir) if path.is_absolute() else path
            except ValueError:
                relative = path
            tid = thought_id or str(relative)
            # Method probe is duck-typed so unit tests can pass a stub.
            enqueue_method = getattr(self._push_queue, "enqueue", None)
            if enqueue_method is not None:
                enqueue_method(tid, str(relative))
        self._queue.put_nowait(path)
        if self._first_enqueue_at is None:
            self._first_enqueue_at = time.monotonic()
        self._arm_debounce_timer()
        self._wake_event.set()

    def force_flush(self) -> None:
        """Cancel debounce timers and wake the loop immediately.

        Used by ``engram sync --push`` and the drain-on-shutdown path so
        pending captures are committed/pushed without waiting out the
        debounce window.
        """
        self._cancel_timers()
        self._wake_event.set()

    async def start(self) -> None:
        """Spawn the background task; idempotent.

        On first start, drain any prior process's persistent push queue
        (when one is configured) so a restart replays pending pushes
        rather than silently dropping them.
        """
        if self._loop_task is not None and not self._loop_task.done():
            return
        self._stopped.clear()
        if self._needs_replay and self._push_queue is not None:
            try:
                iter_method = getattr(self._push_queue, "iter_pending", None)
                if iter_method is not None:
                    pending = iter_method()
                    for entry in pending:
                        # Re-enqueue into the in-memory queue (without
                        # re-writing the persistent file).
                        rel = getattr(entry, "relative_path", None)
                        if rel is not None:
                            absolute = (self.repo_dir / rel).resolve()
                            self._queue.put_nowait(absolute)
                            if self._first_enqueue_at is None:
                                self._first_enqueue_at = time.monotonic()
            finally:
                self._needs_replay = False
        self._loop_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Drain pending work, run final commit/push, then exit cleanly."""
        if self._loop_task is None:
            return
        self.force_flush()
        self._stopped.set()
        try:
            await asyncio.wait_for(self._loop_task, timeout=self.config.push_timeout_seconds + 5.0)
        except TimeoutError:
            self._loop_task.cancel()
        self._loop_task = None

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                await self._tick()
                if self._stopped.is_set():
                    break
                self._wake_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
            # Drain phase on shutdown.
            if self._queue.qsize() > 0 and self.config.auto_commit_on_capture:
                await self._commit_cycle()
                if self.config.auto_push_on_capture and self.config.role == "primary":
                    await self._push_cycle()
        except Exception:
            _log.exception(
                "sync coordinator loop crashed; transitioning to MANUAL_RESOLUTION_REQUIRED"
            )
            self._transition(
                SyncState.MANUAL_RESOLUTION_REQUIRED,
                note="loop crashed (see logs)",
                allow_from_any=True,
            )

    async def _tick(self) -> None:
        if self._is_migration_held():
            if self._state is not SyncState.PAUSED_FOR_MIGRATION:
                self._transition(SyncState.PAUSED_FOR_MIGRATION, note="migration lock held")
            return
        if self._state is SyncState.PAUSED_FOR_MIGRATION and not self._is_migration_held():
            self._transition(SyncState.IDLE, note="migration lock released")
        if self._state in {
            SyncState.AUTH_REQUIRED,
            SyncState.MANUAL_RESOLUTION_REQUIRED,
            SyncState.DISABLED,
        }:
            return
        if self._state is SyncState.COMMITTED_NOT_PUSHED:
            if self.config.role == "primary":
                await self._push_cycle()
            return
        if self._should_fire_commit():
            await self._commit_cycle()
            if (
                self._state is SyncState.COMMITTING
                and self.config.auto_push_on_capture
                and self.config.role == "primary"
            ):
                # commit_cycle already transitioned to IDLE; auto-push if requested.
                pass
            if self.config.auto_push_on_capture and self.config.role == "primary":
                await self._push_cycle()

    # === state transitions ===

    def _transition(
        self,
        new: SyncState,
        *,
        note: str = "",
        allow_from_any: bool = False,
    ) -> None:
        previous = self._state
        if new is previous:
            return
        if not allow_from_any and new not in ALLOWED_TRANSITIONS[previous]:
            msg = (
                f"sync coordinator illegal transition {previous.value!r} -> {new.value!r}: "
                f"{note or 'no note'}"
            )
            raise SyncError(msg)
        self._state = new
        self._events.append(
            SyncEvent(
                timestamp=time.time(),
                from_state=previous,
                to_state=new,
                note=note,
            )
        )
        _log.debug("sync state %s -> %s (%s)", previous.value, new.value, note)

    # === debounce / max-deferral ===

    def _arm_debounce_timer(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop yet: enqueue happened outside an async context
            # (e.g., a unit test calling capture_thought directly). Defer timer
            # arming until the run loop starts; the queue still holds the work.
            if self._state is SyncState.IDLE:
                with contextlib.suppress(SyncError):
                    self._transition(SyncState.DEBOUNCING, note="enqueue (loopless)")
            return
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        self._debounce_handle = loop.call_later(
            self.config.debounce_window_seconds,
            self._on_debounce_fire,
        )
        if self._max_deferral_handle is None:
            self._max_deferral_handle = loop.call_later(
                self.config.max_deferral_seconds,
                self._on_max_deferral_fire,
            )
        if self._state is SyncState.IDLE:
            with contextlib.suppress(SyncError):
                self._transition(SyncState.DEBOUNCING, note="enqueue (debounce armed)")

    def _on_debounce_fire(self) -> None:
        self._debounce_handle = None
        self._wake_event.set()

    def _on_max_deferral_fire(self) -> None:
        self._max_deferral_handle = None
        self._wake_event.set()

    def _cancel_timers(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        if self._max_deferral_handle is not None:
            self._max_deferral_handle.cancel()
            self._max_deferral_handle = None

    def _should_fire_commit(self) -> bool:
        if self._queue.qsize() == 0:
            return False
        if not self.config.auto_commit_on_capture:
            return False
        # Both timers cancel themselves once they fire; if both are None and we
        # have queue content, the debounce window has elapsed.
        return self._debounce_handle is None or self._max_deferral_handle is None

    def _is_migration_held(self) -> bool:
        callback = self.config.migration_held
        if callback is None:
            return False
        try:
            return bool(callback())  # type: ignore[operator]
        except Exception:
            _log.exception("migration_held callback raised; assuming not held")
            return False

    # === commit cycle (Step 8) ===

    async def _commit_cycle(self) -> None:
        async with self._git_lock:
            paths: list[Path] = []
            seen: set[str] = set()
            while not self._queue.empty():
                p = self._queue.get_nowait()
                key = str(p)
                if key not in seen:
                    paths.append(p)
                    seen.add(key)
            if not paths:
                self._cancel_timers()
                self._first_enqueue_at = None
                if self._state is SyncState.DEBOUNCING:
                    self._transition(SyncState.IDLE, note="empty queue at commit time")
                return

            # Detached HEAD refusal (edge 5/45).
            current = await gitops.current_branch(self.repo_dir)
            if current is None:
                self._transition(
                    SyncState.MANUAL_RESOLUTION_REQUIRED,
                    note="detached HEAD; refuse auto-commit",
                    allow_from_any=True,
                )
                return

            self._transition(SyncState.COMMITTING, note=f"commit {len(paths)} path(s)")
            relative_paths = [_safe_path_arg(p, base=self.repo_dir) for p in paths]
            message = f"engram: capture batch (N={len(paths)})"
            try:
                result = await gitops.commit_paths(
                    self.repo_dir,
                    relative_paths,
                    message=message,
                    user_email=self.config.user_email,
                    user_name=self.config.user_name,
                    no_verify=self.config.use_no_verify,
                )
            except Exception as exc:
                _log.exception("commit_paths raised: %s", exc)
                self._transition(
                    SyncState.MANUAL_RESOLUTION_REQUIRED,
                    note=f"commit raised: {exc}",
                )
                return

            self._cancel_timers()
            self._first_enqueue_at = None
            if result.nothing_to_commit:
                self._transition(SyncState.IDLE, note="nothing staged")
                return
            if result.failed:
                # Re-enqueue the drained batch so a later resume retries it;
                # falling through to IDLE here would silently drop the
                # captures from the sync pipeline for the whole session.
                for p in paths:
                    self._queue.put_nowait(p)
                self._transition(
                    SyncState.MANUAL_RESOLUTION_REQUIRED,
                    note=f"commit failed: {result.stderr.strip()[:200]}",
                )
                return
            self._transition(
                SyncState.IDLE, note=f"committed {result.sha[:8] if result.sha else '?'}"
            )

    # === push cycle (Step 9) ===

    async def _push_cycle(self) -> None:
        if self.config.role != "primary":
            # Read-only roles never push.
            return
        async with self._git_lock:
            previous_state = self._state
            try:
                self._transition(
                    SyncState.PUSHING,
                    note="push attempt",
                    allow_from_any=False,
                )
            except SyncError:
                # Transition disallowed (e.g. already pushing); skip.
                return

            backoff = self.config.push_retry_backoff_seconds
            attempt = 0
            last_result: PushResult | None = None
            while attempt <= self.config.push_retry_count:
                last_result = await gitops.push(
                    self.repo_dir,
                    self.config.git_remote,
                    self.config.git_branch,
                    timeout=self.config.push_timeout_seconds,
                )
                if last_result.error_class is GitErrorClass.OK:
                    self._transition(SyncState.IDLE, note="push ok")
                    return
                if last_result.error_class is GitErrorClass.AUTH:
                    self._transition(
                        SyncState.AUTH_REQUIRED,
                        note=f"auth: {last_result.stderr.strip()[:200]}",
                    )
                    return
                if last_result.error_class is GitErrorClass.NETWORK_PERMANENT:
                    self._transition(
                        SyncState.COMMITTED_NOT_PUSHED,
                        note=f"network permanent: {last_result.stderr.strip()[:200]}",
                    )
                    return
                if last_result.error_class is GitErrorClass.NON_FAST_FORWARD:
                    handled = await self._reflog_gate_and_rebase()
                    if not handled:
                        return
                    # Successful rebase; loop to retry the push (force_with_lease).
                    self._transition(
                        SyncState.PUSHING,
                        note="post-rebase push retry",
                        allow_from_any=True,
                    )
                    last_result = await gitops.push(
                        self.repo_dir,
                        self.config.git_remote,
                        self.config.git_branch,
                        force_with_lease=True,
                        timeout=self.config.push_timeout_seconds,
                    )
                    if last_result.error_class is GitErrorClass.OK:
                        self._transition(SyncState.IDLE, note="push ok after rebase")
                        return
                    self._transition(
                        SyncState.MANUAL_RESOLUTION_REQUIRED,
                        note=(
                            "post-rebase push still failed: "
                            f"{last_result.error_class.value}: "
                            f"{last_result.stderr.strip()[:200]}"
                        ),
                    )
                    return
                if last_result.error_class is GitErrorClass.NETWORK_TRANSIENT:
                    if attempt >= self.config.push_retry_count:
                        self._transition(
                            SyncState.COMMITTED_NOT_PUSHED,
                            note="network transient; out of retries",
                        )
                        return
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    attempt += 1
                    continue
                if last_result.error_class is GitErrorClass.LOCK_HELD:
                    if attempt >= self.config.push_retry_count:
                        self._transition(
                            SyncState.COMMITTED_NOT_PUSHED,
                            note="lock held; out of retries",
                        )
                        return
                    await asyncio.sleep(min(backoff, 2.0))
                    attempt += 1
                    continue
                # CONFLICT / UNKNOWN -> manual resolution.
                self._transition(
                    SyncState.MANUAL_RESOLUTION_REQUIRED,
                    note=(
                        f"unhandled push class {last_result.error_class.value}: "
                        f"{last_result.stderr.strip()[:200]}"
                    ),
                )
                return

            # Out of retry budget without a clear classification.
            del previous_state
            self._transition(
                SyncState.COMMITTED_NOT_PUSHED,
                note="exhausted push retries",
            )

    async def _reflog_gate_and_rebase(self) -> bool:
        """Run the reflog gate and (if safe) attempt rebase.

        Returns True when rebase succeeded and the caller should retry the push.
        Returns False when the gate refused or rebase failed; the caller should
        return without retrying.
        """
        # Capture the previous remote ref BEFORE fetching.
        prev_sha = await self._rev_parse(
            f"refs/remotes/{self.config.git_remote}/{self.config.git_branch}"
        )
        # Fetch.
        fetch_result = await gitops.fetch(
            self.repo_dir,
            self.config.git_remote,
            timeout=self.config.push_timeout_seconds,
        )
        if fetch_result.error_class is not GitErrorClass.OK:
            self._transition(
                SyncState.MANUAL_RESOLUTION_REQUIRED,
                note=(
                    "fetch during reflog gate failed: "
                    f"{fetch_result.error_class.value}: "
                    f"{fetch_result.stderr.strip()[:200]}"
                ),
            )
            return False
        # Reflog reachability check.
        if prev_sha:
            new_sha = await self._rev_parse(
                f"refs/remotes/{self.config.git_remote}/{self.config.git_branch}"
            )
            if new_sha and not await self._is_ancestor(prev_sha, new_sha):
                self._transition(
                    SyncState.MANUAL_RESOLUTION_REQUIRED,
                    note=(
                        "reflog gate: previous origin SHA unreachable from new origin "
                        "(force-push detected upstream); refusing auto-rebase"
                    ),
                    allow_from_any=True,
                )
                return False
        self._transition(SyncState.FETCHING, note="rebase begin", allow_from_any=True)
        pull_result: PullResult = await gitops.pull_rebase(
            self.repo_dir,
            self.config.git_remote,
            self.config.git_branch,
            timeout=self.config.push_timeout_seconds,
        )
        if pull_result.error_class is not GitErrorClass.OK:
            self._transition(
                SyncState.MANUAL_RESOLUTION_REQUIRED,
                note=(
                    "rebase failed: "
                    f"{pull_result.error_class.value}: "
                    f"{pull_result.stderr.strip()[:200]}"
                ),
            )
            return False
        return True

    async def _rev_parse(self, ref: str) -> str | None:
        cp = await asyncio.to_thread(
            run_git, ["rev-parse", "--verify", ref], cwd=self.repo_dir, check=False
        )
        if cp.returncode != 0:
            return None
        return cp.stdout.strip() or None

    async def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        cp = await asyncio.to_thread(
            run_git,
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=self.repo_dir,
            check=False,
        )
        return cp.returncode == 0

    # === explicit operations (engram sync CLI) ===

    async def explicit_pull(self) -> PullResult:
        """Run a one-shot pull --rebase outside the auto loop."""
        async with self._git_lock:
            return await gitops.pull_rebase(
                self.repo_dir,
                self.config.git_remote,
                self.config.git_branch,
                timeout=self.config.push_timeout_seconds,
            )

    async def explicit_push(self) -> PushResult:
        """Run a one-shot push outside the auto loop."""
        if self.config.role != "primary":
            # Read-only role refuses; surface as auth-style failure with explicit message.
            return PushResult(
                error_class=GitErrorClass.UNKNOWN,
                stderr="vault role is read-only; refusing push",
            )
        async with self._git_lock:
            return await gitops.push(
                self.repo_dir,
                self.config.git_remote,
                self.config.git_branch,
                timeout=self.config.push_timeout_seconds,
            )

    async def has_uncommitted_changes(self) -> bool:
        """True when ``git status --porcelain`` shows any rows."""
        entries = await gitops.status_porcelain(self.repo_dir)
        return bool(entries)


def filter_engram_paths(paths: Iterable[Path], thoughts_dir: Path) -> list[Path]:
    """Return only paths under ``thoughts_dir`` (defense-in-depth filter)."""
    base = thoughts_dir.resolve()
    out: list[Path] = []
    for p in paths:
        try:
            p_resolved = p.resolve()
        except OSError:
            continue
        try:
            p_resolved.relative_to(base)
        except ValueError:
            continue
        out.append(p_resolved)
    return out


__all__ = [
    "ALLOWED_TRANSITIONS",
    "EVENT_BUFFER_SIZE",
    "CoordinatorConfig",
    "SyncCoordinator",
    "SyncEvent",
    "SyncState",
    "filter_engram_paths",
]
