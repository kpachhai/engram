"""Tests for engram.storage.markdown - YAML frontmatter SoT layer."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engram.storage.markdown import (
    DriftReason,
    FrontmatterDrift,
    read_thought,
    split_frontmatter,
    write_thought,
)

_NOW = datetime(2026, 5, 5, 14, 23, 1, tzinfo=UTC)
_GOOD_FP = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def _frontmatter_yaml(
    *,
    schema_version: int = 1,
    thought_id: str | None = None,
    prefix: str = "Lesson",
    portability: str = "portable",
    source: str = "kpachhai",
    created_at: str = "2026-05-05T14:23:01+00:00",
    updated_at: str = "2026-05-05T14:23:01+00:00",
    fingerprint: str = _GOOD_FP,
    extras: dict[str, object] | None = None,
) -> str:
    tid = thought_id or str(uuid4())
    lines = [
        "---",
        f"schema_version: {schema_version}",
        f"id: {tid}",
        f"prefix: {prefix}",
        f"portability: {portability}",
        f"source: {source}",
        f"created_at: {created_at}",
        f"updated_at: {updated_at}",
        f"fingerprint: {fingerprint}",
    ]
    if extras:
        for k, v in extras.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _make_md(body: str = "[Lesson] when sqlite-vec returns fewer results", **kwargs) -> str:
    return _frontmatter_yaml(**kwargs) + body + "\n"


# === split_frontmatter ===


def test_split_frontmatter_basic():
    content = "---\nfoo: bar\n---\nbody text\n"
    result = split_frontmatter(content)
    assert result is not None
    fm, body = result
    assert fm.strip() == "foo: bar"
    assert body == "body text\n"


def test_split_frontmatter_empty_body():
    content = "---\nfoo: bar\n---\n"
    result = split_frontmatter(content)
    assert result is not None
    fm, body = result
    assert fm.strip() == "foo: bar"
    assert body == ""


def test_split_frontmatter_no_opening_fence_returns_none():
    assert split_frontmatter("no frontmatter here\n") is None
    assert split_frontmatter("body without fence\n---\nfoo\n") is None


def test_split_frontmatter_unclosed_fence_returns_none():
    assert split_frontmatter("---\nfoo: bar\nbody\n") is None


def test_split_frontmatter_body_contains_triple_dash():
    """A4: body containing literal --- mid-document round-trips intact."""
    content = "---\nfoo: bar\n---\nfirst paragraph\n\n---\n\nsecond paragraph\n"
    result = split_frontmatter(content)
    assert result is not None
    fm, body = result
    assert fm.strip() == "foo: bar"
    assert "first paragraph" in body
    assert "second paragraph" in body
    assert "---" in body  # the inner --- is preserved


# === read_thought happy path ===


def test_read_thought_round_trip(tmp_path: Path):
    tid = uuid4()
    file_path = tmp_path / "lesson" / "20260505142301-test-deadbeef0123.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        _make_md(
            body="[Lesson] body content here",
            thought_id=str(tid),
        )
    )

    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is not None
    assert thought.id == tid
    assert thought.prefix == "Lesson"
    assert thought.portability == "portable"
    assert thought.content == "[Lesson] body content here\n"
    assert thought.file_path == file_path
    assert drifts == []


def test_read_thought_missing_file_returns_none(tmp_path: Path):
    assert read_thought(tmp_path / "no-such-file.md") is None


def test_read_thought_no_frontmatter_returns_none(tmp_path: Path):
    file_path = tmp_path / "x.md"
    file_path.write_text("just a body, no frontmatter\n")
    result = read_thought(file_path)
    # File has no frontmatter; we return None per Schema Drift table row
    # "File has no frontmatter at all".
    assert result is None


# === schema drift cases ===


def test_read_thought_missing_schema_version_defaults_to_1(tmp_path: Path):
    """NFR5 EXCEPTION: missing schema_version is treated as 1 and the file IS indexed."""
    tid = uuid4()
    fm_lines = [
        "---",
        f"id: {tid}",
        "prefix: Lesson",
        "portability: portable",
        "source: kpachhai",
        "created_at: 2026-05-05T14:23:01+00:00",
        "updated_at: 2026-05-05T14:23:01+00:00",
        f"fingerprint: {_GOOD_FP}",
        "---",
    ]
    file_path = tmp_path / "x.md"
    file_path.write_text("\n".join(fm_lines) + "\nbody\n")
    result = read_thought(file_path)
    assert result is not None
    thought, _ = result
    assert thought is not None
    assert thought.schema_version == 1


def test_read_thought_missing_required_field_returns_drift(tmp_path: Path):
    """Missing id (a required field) -> WARN drift; file NOT indexed (None thought)."""
    fm_lines = [
        "---",
        "schema_version: 1",
        # id deliberately missing
        "prefix: Lesson",
        "portability: portable",
        "source: kpachhai",
        "created_at: 2026-05-05T14:23:01+00:00",
        "updated_at: 2026-05-05T14:23:01+00:00",
        f"fingerprint: {_GOOD_FP}",
        "---",
    ]
    file_path = tmp_path / "x.md"
    file_path.write_text("\n".join(fm_lines) + "\nbody\n")
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is None
    assert any(d.reason == DriftReason.MISSING_REQUIRED_FIELD for d in drifts)


def test_read_thought_malformed_yaml_returns_drift(tmp_path: Path):
    file_path = tmp_path / "x.md"
    file_path.write_text("---\n: : : malformed : :\n---\nbody\n")
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is None
    assert any(d.reason == DriftReason.YAML_PARSE_ERROR for d in drifts)


def test_read_thought_invalid_portability_returns_drift(tmp_path: Path):
    file_path = tmp_path / "x.md"
    file_path.write_text(_make_md(portability="confidential"))
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is None
    assert any(d.reason == DriftReason.SCHEMA_VIOLATION for d in drifts)


def test_read_thought_unknown_prefix_indexed_with_warning(tmp_path: Path):
    """Unknown prefix value -> WARN drift, but file IS indexed with the literal prefix."""
    file_path = tmp_path / "x.md"
    file_path.write_text(_make_md(prefix="Brainstorm"))
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is not None
    assert thought.prefix == "Brainstorm"
    assert any(d.reason == DriftReason.UNKNOWN_PREFIX for d in drifts)


def test_read_thought_unknown_extra_field_preserved_with_info_drift(tmp_path: Path):
    """Unknown extra field -> INFO-level drift; file IS indexed; extra field preserved."""
    file_path = tmp_path / "x.md"
    file_path.write_text(_make_md(extras={"future_field": "preserved-value"}))
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is not None
    assert any(d.reason == DriftReason.UNKNOWN_EXTRA_FIELD for d in drifts)


def test_read_thought_non_utf8_returns_drift(tmp_path: Path):
    file_path = tmp_path / "x.md"
    file_path.write_bytes(b"---\nfoo: bar\n---\n\xff\xfe garbage \xff\n")
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is None
    assert any(d.reason == DriftReason.NOT_UTF8 for d in drifts)


# === write_thought ===


def test_write_thought_creates_file_with_frontmatter(tmp_path: Path):
    from engram.models import Thought

    thoughts_dir = tmp_path / "thoughts"
    thoughts_dir.mkdir()
    target = thoughts_dir / "lesson" / "test.md"

    thought = Thought.model_validate(
        {
            "id": uuid4(),
            "schema_version": 1,
            "prefix": "Lesson",
            "portability": "portable",
            "source": "kpachhai",
            "created_at": _NOW,
            "updated_at": _NOW,
            "fingerprint": _GOOD_FP,
            "tags": ["debugging"],
            "vault": "default",
            "content": "[Lesson] body line one\nbody line two\n",
            "file_path": target,
        }
    )
    write_thought(thought, base_dir=thoughts_dir)

    assert target.exists()
    text = target.read_text()
    assert text.startswith("---\n")
    assert "schema_version: 1" in text
    assert "prefix: Lesson" in text
    assert "[Lesson] body line one" in text
    assert "body line two" in text


def test_write_thought_round_trip_through_read(tmp_path: Path):
    from engram.models import Thought

    thoughts_dir = tmp_path / "thoughts"
    thoughts_dir.mkdir()
    target = thoughts_dir / "lesson" / "rt.md"
    tid = uuid4()
    body = "[Lesson] round-trip test content\nwith multiple lines\n"

    thought = Thought.model_validate(
        {
            "id": tid,
            "schema_version": 1,
            "prefix": "Lesson",
            "portability": "portable",
            "source": "kpachhai",
            "created_at": _NOW,
            "updated_at": _NOW,
            "fingerprint": _GOOD_FP,
            "content": body,
            "file_path": target,
        }
    )
    write_thought(thought, base_dir=thoughts_dir)

    result = read_thought(target)
    assert result is not None
    read_back, drifts = result
    assert read_back is not None
    assert read_back.id == tid
    assert read_back.content == body
    assert drifts == []


def test_write_thought_preserves_unknown_extra_field_round_trip(tmp_path: Path):
    """A10: unknown extra field present on read must round-trip on write."""
    file_path = tmp_path / "x.md"
    file_path.write_text(_make_md(extras={"future_field": "preserved-value", "another": "value-2"}))

    # Read, modify a known field, write back, read again.
    result = read_thought(file_path)
    assert result is not None
    thought, _ = result
    assert thought is not None

    # Write back to a NEW file in a new tree so the test is hermetic.
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    new_target = out_dir / "rewritten.md"
    thought_for_write = thought.model_copy(update={"file_path": new_target})
    write_thought(thought_for_write, base_dir=out_dir, preserve_extras_from=file_path)

    rewritten_text = new_target.read_text()
    assert "future_field: preserved-value" in rewritten_text
    assert "another: value-2" in rewritten_text


def test_write_thought_normalizes_crlf_to_lf(tmp_path: Path):
    """NFR4: written files use LF only."""
    from engram.models import Thought

    thoughts_dir = tmp_path / "thoughts"
    thoughts_dir.mkdir()
    target = thoughts_dir / "lesson" / "x.md"
    body_with_crlf = "[Lesson] line one\r\nline two\r\n"
    thought = Thought.model_validate(
        {
            "id": uuid4(),
            "prefix": "Lesson",
            "portability": "portable",
            "source": "kpachhai",
            "created_at": _NOW,
            "updated_at": _NOW,
            "fingerprint": _GOOD_FP,
            "content": body_with_crlf,
            "file_path": target,
        }
    )
    write_thought(thought, base_dir=thoughts_dir)

    raw_bytes = target.read_bytes()
    assert b"\r\n" not in raw_bytes
    assert b"line one\n" in raw_bytes


def test_write_thought_body_with_inner_triple_dash_round_trip(tmp_path: Path):
    """A4 round-trip: body containing a literal --- mid-document survives write+read."""
    from engram.models import Thought

    thoughts_dir = tmp_path / "thoughts"
    thoughts_dir.mkdir()
    target = thoughts_dir / "lesson" / "tricky.md"
    body = "[Lesson] before\n\n---\n\nafter the dashes\n"

    thought = Thought.model_validate(
        {
            "id": uuid4(),
            "prefix": "Lesson",
            "portability": "portable",
            "source": "kpachhai",
            "created_at": _NOW,
            "updated_at": _NOW,
            "fingerprint": _GOOD_FP,
            "content": body,
            "file_path": target,
        }
    )
    write_thought(thought, base_dir=thoughts_dir)

    result = read_thought(target)
    assert result is not None
    read_back, _ = result
    assert read_back is not None
    assert read_back.content == body


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_write_thought_file_mode_is_0600(tmp_path: Path):
    from engram.models import Thought

    thoughts_dir = tmp_path / "thoughts"
    thoughts_dir.mkdir()
    target = thoughts_dir / "lesson" / "mode.md"
    thought = Thought.model_validate(
        {
            "id": uuid4(),
            "prefix": "Lesson",
            "portability": "portable",
            "source": "kpachhai",
            "created_at": _NOW,
            "updated_at": _NOW,
            "fingerprint": _GOOD_FP,
            "content": "x",
            "file_path": target,
        }
    )
    write_thought(thought, base_dir=thoughts_dir)
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


# === FrontmatterDrift dataclass behavior ===


def test_drift_has_path_and_reason():
    drift = FrontmatterDrift(
        path=Path("/tmp/x.md"),
        reason=DriftReason.MISSING_REQUIRED_FIELD,
        detail="missing id",
    )
    assert drift.path == Path("/tmp/x.md")
    assert drift.reason == DriftReason.MISSING_REQUIRED_FIELD
    assert "id" in drift.detail


# === property-based: write+read round-trip preserves Thought ===


_YAML_LINE_BREAKS = ("\n", "\r", "\x85", "\u2028", "\u2029")


def _tag_is_yaml_safe(t: str) -> bool:
    """Reject tag strings containing any character YAML treats as a line break.

    YAML 1.2 treats LF, CR, NEL (U+0085), LINE SEPARATOR (U+2028) and PARAGRAPH
    SEPARATOR (U+2029) as line breaks in a quoted scalar; any of them collapses
    to a space on round-trip and breaks string equality.
    """
    if any(ch in t for ch in _YAML_LINE_BREAKS):
        return False
    return t.strip() == t and "\x00" not in t


@given(
    body=st.text(min_size=0, max_size=5000),
    tags=st.lists(
        st.text(min_size=1, max_size=20).filter(_tag_is_yaml_safe),
        max_size=8,
    ),
)
@settings(max_examples=20, deadline=None)
def test_write_read_roundtrip_property(
    tmp_path_factory: pytest.TempPathFactory, body: str, tags: list[str]
):
    """Any well-formed Thought round-trips identically through write+read."""
    from engram.models import Thought

    work = tmp_path_factory.mktemp("md_rt")
    target = work / "lesson" / "rt.md"
    tid = uuid4()
    thought = Thought.model_validate(
        {
            "id": tid,
            "prefix": "Lesson",
            "portability": "portable",
            "source": "kpachhai",
            "created_at": _NOW,
            "updated_at": _NOW,
            "fingerprint": _GOOD_FP,
            "tags": tags,
            "content": body,
            "file_path": target,
        }
    )
    write_thought(thought, base_dir=work)
    result = read_thought(target)
    assert result is not None
    read_back, _ = result
    assert read_back is not None
    assert read_back.id == tid
    # Apply the same normalization the writer does so the comparison is meaningful:
    # CRLF/CR -> LF, then ensure a trailing LF when body is non-empty.
    expected = body.replace("\r\n", "\n").replace("\r", "\n")
    if expected and not expected.endswith("\n"):
        expected = expected + "\n"
    assert read_back.content == expected
    assert read_back.tags == tags
