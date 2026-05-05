"""PersistentPushQueue - durable per-vault queue surviving engram restart.

Multi-writer team vaults expose remote-down / auth-failure / disk-full
transients that can occur mid-debounce. The push queue persists pending
captures to ``<vault>/.engram/push-queue.local`` so a restart of engram
replays them rather than silently losing them.

On-disk format (one line per pending push):

    <unix-ts> <thought-id> <relative-path>

* ``<unix-ts>`` - integer seconds since the epoch when enqueue happened.
* ``<thought-id>`` - UUID, exactly 36 chars (hyphens included).
* ``<relative-path>`` - path relative to the vault's thoughts dir; may
  contain spaces (the line splits on the FIRST two whitespace runs to
  recover the path verbatim).

Disk-full at enqueue raises ``PushQueuePersistenceFailed`` and propagates
back to capture as a refusal so the user knows the thought was NOT
durably enqueued.

Auth-failure during push moves the affected thought file to an orphan
tarball under ``<personal>/.engram/orphans/`` so the operator's
``engram orphan-recover`` flow can triage them.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from engram.errors import PushQueuePersistenceFailed

_log = logging.getLogger("engram.team.push_queue")


@dataclass(frozen=True)
class PendingPush:
    """One queued push waiting to be flushed to the team-vault remote."""

    enqueued_at: int
    thought_id: str
    relative_path: str


class PersistentPushQueue:
    """A durable per-vault queue persisted to the vault's ``.engram/`` dir.

    The queue file is appended to (one line per enqueue) + fsync'd; reads
    happen via ``iter_pending`` which tolerates a partial trailing line
    from a SIGKILL mid-write (the partial line is dropped + a doctor
    INFO row surfaces; the prior already-fsync'd lines remain readable).

    Concurrency: external file locking (the per-vault flock provided by
    the sync layer) serializes enqueue / iter_pending / mark_pushed across
    processes.
    """

    def __init__(
        self,
        *,
        vault_path: Path,
        orphans_dir: Path | None = None,
    ) -> None:
        """Construct a queue rooted at ``<vault_path>/.engram/push-queue.local``.

        Args:
            vault_path: The team-vault root directory (the dir holding
                the `.engram/` subtree).
            orphans_dir: Where ``mark_failed_auth`` moves orphan
                tarballs. When None, derives a sibling
                ``<vault_path>/.engram/orphans/`` dir.
        """
        self._engram_dir = vault_path / ".engram"
        self._queue_file = self._engram_dir / "push-queue.local"
        self._orphans_dir = orphans_dir or (self._engram_dir / "orphans")

    @property
    def queue_file(self) -> Path:
        """Path to the on-disk queue file."""
        return self._queue_file

    def enqueue(
        self,
        thought_id: UUID | str,
        relative_path: str | Path,
        *,
        now: int | None = None,
    ) -> None:
        """Append a pending push to the queue.

        Args:
            thought_id: The thought's UUID (canonical 36-char form).
            relative_path: Path to the thought file relative to the
                vault's thoughts dir.
            now: Optional unix timestamp override (for testing).

        Raises:
            PushQueuePersistenceFailed: if the disk write fails (e.g.
                ``OSError(ENOSPC)`` for disk full). Capture-time callers
                should propagate this as a capture refusal.
        """
        ts = now if now is not None else int(time.time())
        tid = str(thought_id)
        rel = str(relative_path)
        line = f"{ts} {tid} {rel}\n"
        self._engram_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self._queue_file.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            # fsync the parent directory so the new file's directory
            # entry survives an immediate power loss too.
            self._fsync_parent_dir()
        except OSError as exc:
            msg = (
                f"push_queue_persistence_failed: failed to enqueue {tid} -> "
                f"{self._queue_file}: {exc}"
            )
            raise PushQueuePersistenceFailed(msg) from exc

    def iter_pending(self) -> list[PendingPush]:
        """Return the currently queued pushes, oldest first.

        Tolerates a partial trailing line: if the very last line lacks a
        terminating newline (caller crashed mid-append), it's dropped
        and a doctor INFO row surfaces so the operator knows.
        """
        if not self._queue_file.exists():
            return []
        try:
            raw = self._queue_file.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("failed to read push queue %s: %s", self._queue_file, exc)
            return []
        pending: list[PendingPush] = []
        if not raw:
            return pending
        # Parse all complete lines (terminated by '\n'). A partial
        # trailing line (no '\n') is dropped + logged.
        if not raw.endswith("\n"):
            _log.info(
                "push queue %s has a partial trailing line; ignoring it "
                "(doctor: push_queue_partial_line_dropped)",
                self._queue_file,
            )
            raw = raw.rsplit("\n", 1)[0] + "\n" if "\n" in raw else ""
        for line_no, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            parts = line.split(" ", 2)
            if len(parts) != 3:
                _log.warning(
                    "push queue line %d malformed; skipping: %r",
                    line_no,
                    line,
                )
                continue
            ts_str, tid, rel = parts
            try:
                ts = int(ts_str)
            except ValueError:
                _log.warning("push queue line %d has non-int ts; skipping", line_no)
                continue
            pending.append(PendingPush(enqueued_at=ts, thought_id=tid, relative_path=rel))
        return pending

    def mark_pushed(self, thought_id: UUID | str) -> None:
        """Remove the queue entry for ``thought_id`` after a successful push.

        Idempotent: if the entry is absent (already pushed, or never
        enqueued), no-op. Re-writes the queue file atomically.
        """
        target = str(thought_id)
        pending = self.iter_pending()
        kept = [p for p in pending if p.thought_id != target]
        self._rewrite_atomic(kept)

    def mark_failed_auth(
        self,
        thought_id: UUID | str,
        *,
        thought_files: list[Path] | None = None,
    ) -> Path | None:
        """Orphan the entry on auth-failure.

        Creates an orphan tarball under ``<orphans-dir>/team-vault-orphan-
        <thought-id>.tar.gz`` containing the affected thought's markdown
        file (when ``thought_files`` is supplied), then removes the
        entry from the queue.

        Returns:
            The path to the orphan tarball (or None if no thought_files
            were supplied).
        """
        target = str(thought_id)
        pending = self.iter_pending()
        kept = [p for p in pending if p.thought_id != target]

        orphan_path: Path | None = None
        if thought_files:
            self._orphans_dir.mkdir(parents=True, exist_ok=True)
            orphan_path = self._orphans_dir / f"team-vault-orphan-{target}.tar.gz"
            with tarfile.open(orphan_path, "w:gz") as tar:
                for f in thought_files:
                    if f.exists():
                        tar.add(str(f), arcname=f.name)

        self._rewrite_atomic(kept)
        return orphan_path

    def clear(self) -> None:
        """Drop all queue entries. Used by tests + operator recovery."""
        self._rewrite_atomic([])

    def _rewrite_atomic(self, pending: list[PendingPush]) -> None:
        """Atomically rewrite the queue with ``pending``."""
        self._engram_dir.mkdir(parents=True, exist_ok=True)
        if not pending:
            with contextlib.suppress(FileNotFoundError):
                self._queue_file.unlink()
            return
        tmp = self._queue_file.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for entry in pending:
                    fh.write(f"{entry.enqueued_at} {entry.thought_id} {entry.relative_path}\n")
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self._queue_file)
            self._fsync_parent_dir()
        except OSError as exc:
            msg = f"push_queue_persistence_failed: rewrite failed: {exc}"
            raise PushQueuePersistenceFailed(msg) from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()

    def _fsync_parent_dir(self) -> None:
        """Best-effort fsync of the queue file's parent directory."""
        try:
            fd = os.open(self._engram_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Some filesystems (notably tmpfs on certain kernels) don't
            # support directory fsync. We log + continue; the file's
            # own fsync gets us close enough for the local-FS case.
            _log.debug("fsync of %s skipped (filesystem unsupported)", self._engram_dir)


__all__ = ["PendingPush", "PersistentPushQueue"]
