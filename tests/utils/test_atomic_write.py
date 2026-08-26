"""Tests for engram.utils.atomic_write."""

from __future__ import annotations

import re
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

    f_fullfsync = getattr(fcntl, "F_FULLFSYNC")  # noqa: B009  # Darwin-only attribute
    full_fsync_calls = [c for c in fcntl_calls if len(c) > 1 and c[1] == f_fullfsync]
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


# === the convention has an enforcement point, not only a rule in CLAUDE.md ===

#: Direct ``write_text`` / ``write_bytes`` call sites in ``src/`` that are
#: deliberately exempt from the atomic-write rule, with the count of calls each
#: file is allowed and the reason. A file not listed here, or a listed file that
#: grew another direct write, fails: the point is that adding one is a decision
#: someone makes on purpose rather than a habit that spreads.
#:
#: An allowlist rather than an outright ban because the tree already carries
#: these twelve; a rule that is red on the day it lands gets relaxed instead of
#: obeyed. Paying an entry down means deleting its line, never editing a count up.
_ALLOWED_DIRECT_WRITES: dict[str, tuple[int, str]] = {
    "engram/bundle/importer.py": (
        2,
        "a staging file the importer re-reads and discards, and a run report; "
        "neither is vault state a later run depends on",
    ),
    "engram/cli/clone.py": (
        1,
        "writes an identity template into a directory this command just created, "
        "and only when the file does not already exist",
    ),
    "engram/cli/init.py": (
        3,
        "scaffolds a brand-new vault; the command refuses a non-empty target, so "
        "there is no prior content a partial write could destroy",
    ),
    "engram/cli/team_vault.py": (
        5,
        "team-vault setup scaffolding, each guarded by an exists() check plus a "
        "completion sentinel written last, so an interrupted run resumes",
    ),
    "engram/migration/open_brain.py": (
        1,
        "a migration run report, regenerated by re-running the migration",
    ),
}

_DIRECT_WRITE_RE = re.compile(r"\.write_text\(|\.write_bytes\(")


def _src_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "src"
    assert root.is_dir(), f"source tree not found at {root}"
    return root


def test_file_mutations_go_through_atomic_write() -> None:
    """Every direct write in ``src/`` is either absent or on the allowlist.

    CLAUDE.md states "all file mutations go through atomic_write"; before this
    test nothing checked it, and three sites had drifted - two of them rewriting
    the operator's own config file in place, one a read-modify-write of an audit
    log that truncates on any interruption.
    """
    src = _src_root()
    modules = sorted(path for path in src.rglob("*.py") if path.name != "atomic_write.py")
    scanned = {path.relative_to(src).as_posix() for path in modules}

    # A scan that examined nothing, or that cannot see the files the allowlist
    # already names, is a broken scan and not a clean tree. No threshold here:
    # the allowlist itself is the denominator, so this cannot rot into a number
    # that only the current corpus clears.
    assert modules, f"examined 0 modules under {src} - the scan found no source at all"
    unseen = sorted(name for name in _ALLOWED_DIRECT_WRITES if name not in scanned)
    assert not unseen, (
        f"examined {len(modules)} modules under {src} but never saw {unseen}, "
        f"which the allowlist names - either the scan is broken or those entries "
        f"are stale and should be deleted"
    )

    found: dict[str, int] = {}
    for path in modules:
        hits = len(_DIRECT_WRITE_RE.findall(path.read_text(encoding="utf-8")))
        if hits:
            found[path.relative_to(src).as_posix()] = hits

    expected = {name: count for name, (count, _reason) in _ALLOWED_DIRECT_WRITES.items()}
    assert found == expected, (
        f"examined {len(modules)} modules under {src}; direct write sites are "
        f"{found}, allowlist expects {expected}. A new or grown entry: route the "
        f"write through engram.utils.atomic_write.atomic_write_text / "
        f"atomic_write_bytes, or add it to _ALLOWED_DIRECT_WRITES with the reason "
        f"it is safe. A shrunk entry means the debt was paid - update the count, "
        f"and delete the line when it reaches zero."
    )
