"""Daily + size-threshold log rotation with retention sweep.

Wraps stdlib :class:`logging.handlers.RotatingFileHandler` and adds:

- ``umask(0o077)`` around file creation so the initial inode is born
  with mode ``0o600`` (closes deep-plan critique S9 — closes the
  0o644 → chmod window).
- Retention sweep that deletes rotated files older than
  ``retention_days``.
- Mode-preservation across rotations.

``engram daemon logs --follow`` uses ``WatchedFileHandler``-style
inode-reopen logic implemented separately in ``cli/daemon.py`` via a
tail-poll loop (spec Amendment 8).

Spec: ``2026-05-12-engram-daemon-mode-design.md`` Section 13.3 +
Amendments 8 + 9.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from pathlib import Path


class EngramRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler with 0o600 perms + retention sweep."""

    def __init__(
        self,
        path: Path,
        *,
        max_size_mb: int,
        retention_days: int,
    ) -> None:
        """Open the log with restrictive perms; record retention for sweeps."""
        # Tighten umask BEFORE the parent constructor creates the file so
        # the initial inode is born with 0600 perms instead of going
        # through a 0644 → chmod window (closes critique S9).
        prior_umask = os.umask(0o077)
        try:
            super().__init__(
                filename=str(path),
                maxBytes=max_size_mb * 1024 * 1024,
                backupCount=retention_days,
                encoding="utf-8",
            )
        finally:
            os.umask(prior_umask)
        if path.exists():
            # Belt-and-suspenders on top of umask: explicitly chmod the
            # log so an unusual parent-process umask still produces 0600.
            path.chmod(0o600)
        self.retention_days = retention_days
        self._path = path

    def doRollover(self) -> None:  # noqa: N802 - overrides stdlib RotatingFileHandler.doRollover
        """Rotate + restore mode 0600 on each rotated file + sweep retention."""
        super().doRollover()
        base = Path(self.baseFilename)
        if base.exists():
            base.chmod(0o600)
        for i in range(1, self.backupCount + 1):
            rotated = Path(f"{self.baseFilename}.{i}")
            if rotated.exists():
                rotated.chmod(0o600)
        self._sweep_retention()

    def _sweep_retention(self) -> None:
        """Delete rotated files older than ``retention_days``."""
        cutoff = time.time() - self.retention_days * 86400
        for rotated in self._path.parent.glob(f"{self._path.name}.*"):
            try:
                if rotated.stat().st_mtime < cutoff:
                    rotated.unlink(missing_ok=True)
            except OSError:
                # Best-effort: a concurrent rename or permission glitch
                # surfaces as a doctor row, not a crash.
                continue


def configure_log_rotation(
    log_path: Path,
    *,
    max_size_mb: int,
    retention_days: int,
    level: str,
) -> EngramRotatingHandler:
    """Return a configured :class:`EngramRotatingHandler`.

    Caller attaches the returned handler to a logger. The format mirrors
    engram's existing log style: ISO-8601 timestamp + level + logger
    name + message.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = EngramRotatingHandler(
        log_path,
        max_size_mb=max_size_mb,
        retention_days=retention_days,
    )
    handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s"))
    return handler


__all__ = ["EngramRotatingHandler", "configure_log_rotation"]
