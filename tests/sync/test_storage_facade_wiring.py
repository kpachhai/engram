"""Step 10 wiring: VaultStorage._post_capture_sync forwards to coordinator."""

from __future__ import annotations

from pathlib import Path

from engram.storage.facade import VaultStorage


class _SpyCoordinator:
    """Drop-in stand-in that records every enqueued path."""

    def __init__(self) -> None:
        self.enqueued: list[Path] = []

    def enqueue(self, path: Path) -> None:
        self.enqueued.append(path)


def test_capture_with_coordinator_attached_enqueues_path(tmp_path: Path) -> None:
    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=384,
    )
    spy = _SpyCoordinator()
    storage.set_sync_coordinator(spy)
    try:
        thought = storage.capture(content="[Lesson] sync wiring works")
        assert spy.enqueued == [thought.file_path]
    finally:
        storage.close()


def test_capture_without_coordinator_is_noop(tmp_path: Path) -> None:
    """Default behavior: no coordinator attached -> no enqueue happens."""
    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=384,
    )
    try:
        # Should not raise even though _sync_coordinator is None.
        storage.capture(content="[Lesson] no coordinator attached")
    finally:
        storage.close()


def test_coordinator_enqueue_failure_does_not_break_capture(tmp_path: Path) -> None:
    """If the coordinator raises, the markdown SoT still wins (Flow A)."""

    class _BrokenCoord:
        def enqueue(self, path: Path) -> None:
            raise RuntimeError("simulated broken coordinator")

    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
        embedding_dim=384,
    )
    storage.set_sync_coordinator(_BrokenCoord())
    try:
        thought = storage.capture(content="[Lesson] coord raised but capture survives")
        # File on disk is the source of truth; capture must succeed.
        assert thought.file_path.exists()
    finally:
        storage.close()
