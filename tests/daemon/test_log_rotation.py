"""Log rotation policy."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from engram.daemon.log_rotation import EngramRotatingHandler, configure_log_rotation


@pytest.fixture
def isolated_logger(request: pytest.FixtureRequest) -> logging.Logger:
    """Per-test logger so handlers do not leak across tests."""
    logger = logging.getLogger(f"engram.daemon.test_log_rotation.{request.node.name}")
    # Defensive: detach any handlers a previous run may have left attached.
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_rotation_at_size_threshold(tmp_path: Path, isolated_logger: logging.Logger) -> None:
    log_path = tmp_path / "engram.log"
    handler = configure_log_rotation(
        log_path,
        max_size_mb=1,  # 1 MiB threshold
        retention_days=7,
        level="DEBUG",
    )
    isolated_logger.addHandler(handler)

    # Emit > 1 MiB of log lines (~80 bytes each).
    for _ in range(20_000):
        isolated_logger.info("x" * 80)
    handler.flush()

    rotated = list(tmp_path.glob("engram.log.*"))
    assert len(rotated) >= 1, f"expected at least one rotated file, got {rotated}"


def test_retention_deletes_old_files(tmp_path: Path) -> None:
    log_path = tmp_path / "engram.log"
    # Pre-create 10 rotated files, all older than the retention cutoff.
    old_time = time.time() - 30 * 86400  # 30 days ago
    for i in range(10):
        rotated = tmp_path / f"engram.log.{i + 1}"
        rotated.write_text("old")
        # Backdate so the retention sweep can see them as expired.
        import os as _os

        _os.utime(rotated, (old_time, old_time))
    handler = configure_log_rotation(
        log_path,
        max_size_mb=100,
        retention_days=7,
        level="INFO",
    )
    handler._sweep_retention()  # exercise the sweep directly
    surviving = list(tmp_path.glob("engram.log.*"))
    assert surviving == [], f"expected all old rotated files swept, found {surviving}"


def test_handler_creates_log_with_mode_0600(
    tmp_path: Path, isolated_logger: logging.Logger
) -> None:
    log_path = tmp_path / "engram.log"
    handler = configure_log_rotation(
        log_path,
        max_size_mb=100,
        retention_days=7,
        level="INFO",
    )
    isolated_logger.addHandler(handler)
    isolated_logger.info("first record forces file creation")
    handler.flush()
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_handler_subclasses_rotating_file_handler(tmp_path: Path) -> None:
    """Sanity: configure_log_rotation returns our EngramRotatingHandler."""
    handler = configure_log_rotation(
        tmp_path / "engram.log",
        max_size_mb=10,
        retention_days=7,
        level="WARNING",
    )
    assert isinstance(handler, EngramRotatingHandler)
    assert handler.retention_days == 7
