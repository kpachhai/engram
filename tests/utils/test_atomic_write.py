"""Tests for engram.utils.atomic_write."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engram.utils.atomic_write import atomic_write_bytes, atomic_write_text


def test_basic_write_text(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"
    atomic_write_text(target, "hello world")
    assert target.read_text() == "hello world"


def test_basic_write_bytes(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    atomic_write_bytes(target, b"\x00\x01\x02hello\xff")
    assert target.read_bytes() == b"\x00\x01\x02hello\xff"


def test_no_orphan_tmpfile_after_success(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"
    atomic_write_text(target, "content")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected leftover .tmp files: {leftovers}"


def test_overwrite_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"
    target.write_text("old content")
    atomic_write_text(target, "new content")
    assert target.read_text() == "new content"


def test_parent_directory_must_exist(tmp_path: Path) -> None:
    target = tmp_path / "subdir" / "thought.md"
    with pytest.raises(FileNotFoundError, match="parent directory does not exist"):
        atomic_write_text(target, "content")


def test_orphan_tmp_remains_after_crash_in_replace(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"

    with (
        patch("engram.utils.atomic_write.os.replace", side_effect=OSError("simulated crash")),
        pytest.raises(OSError, match="simulated crash"),
    ):
        atomic_write_text(target, "content")

    leftovers = list(tmp_path.glob("*.tmp"))
    assert len(leftovers) == 1, "expected exactly one orphan .tmp file after crash"
    assert leftovers[0].read_text() == "content"
    assert not target.exists(), "destination must not exist when atomic write failed"


def test_tmp_file_lives_in_target_directory(tmp_path: Path) -> None:
    """The tempfile must share a filesystem with the target so os.replace is atomic."""
    target = tmp_path / "thought.md"

    seen_tmp_paths: list[Path] = []

    def fake_replace(src: str, dst: str) -> None:
        seen_tmp_paths.append(Path(src))
        raise OSError("intercepted")

    with (
        patch("engram.utils.atomic_write.os.replace", side_effect=fake_replace),
        pytest.raises(OSError, match="intercepted"),
    ):
        atomic_write_text(target, "content")

    assert len(seen_tmp_paths) == 1
    assert seen_tmp_paths[0].parent == tmp_path, "tmp file must be in same dir as target"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_file_mode_is_0600(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"
    atomic_write_text(target, "secret")
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600, f"expected mode 0600, got {oct(mode)}"


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is macOS-specific")
def test_full_fsync_on_macos(tmp_path: Path) -> None:
    """On macOS, atomic_write uses F_FULLFSYNC so APFS actually flushes to media."""
    import fcntl
    from typing import Any

    fcntl_calls: list[tuple[Any, ...]] = []
    real_fcntl = fcntl.fcntl

    def spy_fcntl(fd: Any, op: Any, *rest: Any) -> Any:
        fcntl_calls.append((fd, op, *rest))
        return real_fcntl(fd, op, *rest)

    target = tmp_path / "thought.md"
    with patch("engram.utils.atomic_write.fcntl.fcntl", side_effect=spy_fcntl):
        atomic_write_text(target, "content")

    full_fsync_calls = [c for c in fcntl_calls if len(c) > 1 and c[1] == fcntl.F_FULLFSYNC]
    assert len(full_fsync_calls) > 0, "F_FULLFSYNC was not called on macOS"


def test_default_encoding_is_utf8(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"
    atomic_write_text(target, "héllo wörld 日本語")
    assert target.read_bytes() == "héllo wörld 日本語".encode()


def test_explicit_encoding_respected(tmp_path: Path) -> None:
    target = tmp_path / "thought.md"
    atomic_write_text(target, "héllo", encoding="latin-1")
    assert target.read_bytes() == "héllo".encode("latin-1")


@given(content=st.text())
@settings(max_examples=30, deadline=None)
def test_text_roundtrip_property(tmp_path_factory: pytest.TempPathFactory, content: str) -> None:
    target = tmp_path_factory.mktemp("aw_text") / "thought.md"
    atomic_write_text(target, content)
    # Read raw bytes + decode to bypass Path.read_text()'s universal-newline translation
    # (which would turn \r into \n on read even though our writer preserves the byte verbatim).
    assert target.read_bytes().decode("utf-8") == content


@given(content=st.binary())
@settings(max_examples=30, deadline=None)
def test_bytes_roundtrip_property(tmp_path_factory: pytest.TempPathFactory, content: bytes) -> None:
    target = tmp_path_factory.mktemp("aw_bytes") / "blob.bin"
    atomic_write_bytes(target, content)
    assert target.read_bytes() == content
