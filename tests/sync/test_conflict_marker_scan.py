"""Tests for engram.sync.gitops.conflict_marker_scan."""

from __future__ import annotations

from pathlib import Path

from engram.sync.gitops import conflict_marker_scan


def test_empty_dir_returns_empty_list(tmp_path: Path) -> None:
    assert conflict_marker_scan(tmp_path) == []


def test_clean_files_return_empty_list(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# Heading\n\nbody")
    (tmp_path / "b.md").write_text("plain text")
    assert conflict_marker_scan(tmp_path) == []


def test_marker_in_body_detected(tmp_path: Path) -> None:
    (tmp_path / "conflicted.md").write_text(
        "preamble\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    )
    found = conflict_marker_scan(tmp_path)
    assert len(found) == 1
    assert found[0].name == "conflicted.md"


def test_marker_only_in_frontmatter_detected(tmp_path: Path) -> None:
    (tmp_path / "frontmatter-conflict.md").write_text(
        "---\n<<<<<<< HEAD\ntag: ours\n=======\ntag: theirs\n>>>>>>> branch\n---\nbody\n"
    )
    found = conflict_marker_scan(tmp_path)
    assert len(found) == 1


def test_marker_beyond_8kb_into_file_still_detected(tmp_path: Path) -> None:
    """Whole-file scan: markers anywhere in the file are flagged."""
    padding = "filler line\n" * 1000  # ~12 KB padding
    body = "preamble\n" + padding + "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    (tmp_path / "long-conflict.md").write_text(body)
    assert len(body.encode("utf-8")) > 8 * 1024  # confirm the marker is past 8 KB
    found = conflict_marker_scan(tmp_path)
    assert len(found) == 1


def test_lone_separator_does_not_trigger_false_positive(tmp_path: Path) -> None:
    """``=======`` alone is a markdown horizontal rule; must NOT flag."""
    (tmp_path / "horizontal.md").write_text("# Heading\n\n=======\n\nfollowing paragraph\n")
    assert conflict_marker_scan(tmp_path) == []


def test_only_open_marker_does_not_trigger(tmp_path: Path) -> None:
    """Defense in depth: an opening marker without close is incomplete."""
    (tmp_path / "partial.md").write_text("text\n<<<<<<< HEAD\nstuff\n")
    assert conflict_marker_scan(tmp_path) == []


def test_only_close_marker_does_not_trigger(tmp_path: Path) -> None:
    (tmp_path / "partial2.md").write_text("text\n>>>>>>> branch\nstuff\n")
    assert conflict_marker_scan(tmp_path) == []


def test_nested_subdirectories_walked(tmp_path: Path) -> None:
    nested = tmp_path / "lesson" / "subdir"
    nested.mkdir(parents=True)
    (nested / "deep.md").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    found = conflict_marker_scan(tmp_path)
    assert len(found) == 1
    assert found[0].name == "deep.md"


def test_non_markdown_files_ignored(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> branch\n")
    assert conflict_marker_scan(tmp_path) == []


def test_returns_sorted_paths(tmp_path: Path) -> None:
    (tmp_path / "z.md").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    (tmp_path / "a.md").write_text("<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n")
    found = conflict_marker_scan(tmp_path)
    assert [p.name for p in found] == ["a.md", "z.md"]
