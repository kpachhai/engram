"""Atomic file writes for the markdown source-of-truth layer.

Pattern:

1. Create a tempfile in the SAME directory as the destination so the eventual
   ``os.replace`` is a same-filesystem rename (atomic on POSIX).
2. Write the bytes; flush; fsync the file. On macOS use ``F_FULLFSYNC`` because
   plain ``fsync`` on APFS does not flush to media.
3. ``chmod`` the tempfile to ``0600`` (per ``06-SECURITY.md`` Boundary B1).
4. ``os.replace`` the tempfile to the destination.
5. fsync the parent directory FD so the rename itself is durable across crash.

If any step fails, the tempfile remains in the target directory and surfaces via
``engram doctor`` (per ``02-TECHNICAL_DESIGN.md`` Frontmatter Schema Drift Handling
edge case A14). ``engram doctor --repair`` removes orphan ``.tmp`` files safely.
"""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
from pathlib import Path

_FILE_MODE = 0o600
_TMP_SUFFIX = ".tmp"

_IS_DARWIN = sys.platform == "darwin"


def _platform_fsync(fd: int) -> None:
    """Flush file ``fd`` to media.

    On macOS uses ``F_FULLFSYNC``; on other platforms uses ``os.fsync``.
    """
    if _IS_DARWIN:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)


def _fsync_directory(dir_path: Path) -> None:
    """Fsync a directory so a recent rename within it survives a crash."""
    fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        _platform_fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically write ``content`` to ``path`` with mode ``0600``.

    Args:
        path: Destination path. The parent directory MUST already exist; this
            function does not auto-create directories.
        content: Bytes to write.

    Raises:
        FileNotFoundError: if ``path.parent`` does not exist.
        OSError: from any underlying I/O error.
    """
    path = Path(path)
    if not path.parent.exists():
        msg = f"parent directory does not exist: {path.parent}"
        raise FileNotFoundError(msg)

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        suffix=_TMP_SUFFIX,
        prefix=f"{path.name}.",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)

    # On any failure between here and os.replace, the tempfile remains for doctor to clean.
    with os.fdopen(tmp_fd, "wb") as fh:
        fh.write(content)
        fh.flush()
        _platform_fsync(fh.fileno())

    # mkstemp creates with mode 0600 already, but be explicit (defense-in-depth against umask).
    os.chmod(tmp_path, _FILE_MODE)  # noqa: PTH101 - tmp_path is a Path; chmod via os is fine

    os.replace(tmp_path, path)  # noqa: PTH105 - patched at this exact path in tests
    _fsync_directory(path.parent)


def atomic_write_text(
    path: Path,
    content: str,
    encoding: str = "utf-8",
) -> None:
    """Atomically write a UTF-8 (by default) text file to ``path`` with mode ``0600``.

    See :func:`atomic_write_bytes` for the underlying durability contract.
    """
    atomic_write_bytes(path, content.encode(encoding))


__all__ = ["atomic_write_bytes", "atomic_write_text"]
