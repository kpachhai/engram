"""Tests for engram.team.push_queue.PersistentPushQueue.

Step 7 verifier - persist-and-reload round trip; orphan-on-auth-failure;
disk-full surfaces refusal; partial-line tolerated on reload.
"""

from __future__ import annotations

import tarfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from engram.errors import PushQueuePersistenceFailed
from engram.team.push_queue import PendingPush, PersistentPushQueue


def test_enqueue_and_iter_round_trip(tmp_path: Path) -> None:
    """Enqueued entries surface in iter_pending in insertion order."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    tid_a = uuid4()
    tid_b = uuid4()
    queue.enqueue(tid_a, "thoughts/2026/01/a.md", now=1000)
    queue.enqueue(tid_b, "thoughts/2026/01/b.md", now=1001)
    pending = queue.iter_pending()
    assert len(pending) == 2
    assert pending[0].thought_id == str(tid_a)
    assert pending[0].enqueued_at == 1000
    assert pending[0].relative_path == "thoughts/2026/01/a.md"
    assert pending[1].thought_id == str(tid_b)


def test_enqueue_persists_across_reopen(tmp_path: Path) -> None:
    """A new queue object reads the prior queue's state."""
    tid = uuid4()
    PersistentPushQueue(vault_path=tmp_path).enqueue(tid, "x.md", now=42)
    pending = PersistentPushQueue(vault_path=tmp_path).iter_pending()
    assert [p.thought_id for p in pending] == [str(tid)]


def test_mark_pushed_removes_entry(tmp_path: Path) -> None:
    queue = PersistentPushQueue(vault_path=tmp_path)
    tid_a = uuid4()
    tid_b = uuid4()
    queue.enqueue(tid_a, "a.md")
    queue.enqueue(tid_b, "b.md")
    queue.mark_pushed(tid_a)
    pending = queue.iter_pending()
    assert [p.thought_id for p in pending] == [str(tid_b)]


def test_mark_pushed_idempotent(tmp_path: Path) -> None:
    """Removing an already-removed entry is a no-op."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    queue.mark_pushed(uuid4())  # never enqueued
    assert queue.iter_pending() == []


def test_mark_failed_auth_creates_orphan(tmp_path: Path) -> None:
    queue = PersistentPushQueue(vault_path=tmp_path)
    tid = uuid4()
    file_path = tmp_path / "file.md"
    file_path.write_text("body", encoding="utf-8")
    queue.enqueue(tid, "file.md")
    orphan = queue.mark_failed_auth(tid, thought_files=[file_path])
    assert orphan is not None
    assert orphan.exists()
    assert orphan.suffix == ".gz"
    # Verify the tarball contains the file.
    with tarfile.open(orphan) as tar:
        assert any(member.name == "file.md" for member in tar.getmembers())
    # The queue entry was removed.
    assert queue.iter_pending() == []


def test_mark_failed_auth_no_files_returns_none(tmp_path: Path) -> None:
    """mark_failed_auth with no thought_files clears entry without orphaning."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    tid = uuid4()
    queue.enqueue(tid, "file.md")
    orphan = queue.mark_failed_auth(tid, thought_files=None)
    assert orphan is None
    assert queue.iter_pending() == []


def test_partial_line_tolerated_on_reload(tmp_path: Path) -> None:
    """A SIGKILL-induced partial trailing line is silently dropped."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    tid = uuid4()
    queue.enqueue(tid, "valid.md", now=100)
    # Corrupt the queue file: append a partial trailing line (no \n).
    with queue.queue_file.open("a", encoding="utf-8") as fh:
        fh.write("999 partial-trailing-line-without-newline-or-fields-")
    pending = queue.iter_pending()
    # Original entry survives; partial line dropped.
    assert len(pending) == 1
    assert pending[0].thought_id == str(tid)


def test_disk_full_on_enqueue_surfaces_refusal(tmp_path: Path) -> None:
    """A disk-full error at enqueue raises PushQueuePersistenceFailed."""
    queue = PersistentPushQueue(vault_path=tmp_path)

    def _explode(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")  # ENOSPC

    with (
        patch("pathlib.Path.open", side_effect=_explode),
        pytest.raises(
            PushQueuePersistenceFailed,
            match="push_queue_persistence_failed",
        ),
    ):
        queue.enqueue(uuid4(), "x.md")


def test_clear_removes_queue_file(tmp_path: Path) -> None:
    queue = PersistentPushQueue(vault_path=tmp_path)
    queue.enqueue(uuid4(), "x.md")
    assert queue.queue_file.exists()
    queue.clear()
    assert not queue.queue_file.exists()
    assert queue.iter_pending() == []


def test_iter_pending_when_no_queue_file(tmp_path: Path) -> None:
    queue = PersistentPushQueue(vault_path=tmp_path)
    assert queue.iter_pending() == []


def test_relative_path_with_spaces_preserved(tmp_path: Path) -> None:
    """A path containing spaces round-trips correctly."""
    queue = PersistentPushQueue(vault_path=tmp_path)
    tid = uuid4()
    rel = "thoughts/2026 ad-hoc folder/note.md"
    queue.enqueue(tid, rel)
    pending = queue.iter_pending()
    assert pending[0].relative_path == rel


def test_pending_push_dataclass_round_trip() -> None:
    p = PendingPush(enqueued_at=42, thought_id="tid", relative_path="p")
    assert p.enqueued_at == 42
    assert p.thought_id == "tid"
