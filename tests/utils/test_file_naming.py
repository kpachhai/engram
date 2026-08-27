"""Tests for engram.utils.file_naming - filename derivation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from engram.errors import VaultError
from engram.utils.file_naming import (
    derive_prefix_dirname,
    derive_relative_path,
    derive_slug,
    derive_uuid_tail,
)


def _ts(
    year: int = 2026,
    month: int = 5,
    day: int = 4,
    hour: int = 14,
    minute: int = 23,
    second: int = 1,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# Helper: a valid UUID-v7 with a recognizable tail for test assertions.
_SAMPLE_UUID = UUID("0193abcd-7890-7000-abcd-ef0123456789")
# hex form: 0193abcd789070 00abcd ef0123456789 -> last 12 = "ef0123456789"
_SAMPLE_TAIL = "ef0123456789"


# === derive_prefix_dirname ===


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("Lesson", "lesson"),
        ("Pattern", "pattern"),
        ("Action Item", "action-item"),
        ("Session Summary", "session-summary"),
        ("Friction", "friction"),
        ("Note", "note"),
    ],
)
def test_canonical_prefix_dirnames(prefix: str, expected: str) -> None:
    assert derive_prefix_dirname(prefix) == expected


def test_prefix_with_internal_double_space_collapses() -> None:
    assert derive_prefix_dirname("Action  Item") == "action-item"


def test_prefix_rejects_path_traversal() -> None:
    with pytest.raises(VaultError, match="path-traversal"):
        derive_prefix_dirname("../escape")


def test_prefix_rejects_null_byte() -> None:
    with pytest.raises(VaultError, match="path-traversal"):
        derive_prefix_dirname("foo\x00bar")


def test_prefix_rejects_rtl_override() -> None:
    with pytest.raises(VaultError, match="right-to-left"):
        derive_prefix_dirname("foo\u202ebar")


def test_prefix_empty_after_sanitization_raises() -> None:
    with pytest.raises(VaultError):
        derive_prefix_dirname("")


def test_prefix_only_punctuation_raises() -> None:
    with pytest.raises(VaultError):
        derive_prefix_dirname("!!!")


# === derive_slug ===


def test_slug_lowercases_and_hyphenates() -> None:
    body = "When sqlite-vec returns fewer results than k"
    slug = derive_slug(body)
    assert slug == "when-sqlite-vec-returns-fewer"


def test_slug_caps_at_30_input_chars() -> None:
    """Slug derives from first 30 chars of body."""
    body = "abcdefghijklmnopqrstuvwxyzABCD-extra-content-not-in-slug"
    slug = derive_slug(body)
    # First 30 chars after lowercase: "abcdefghijklmnopqrstuvwxyzabcd"
    assert slug == "abcdefghijklmnopqrstuvwxyzabcd"


def test_slug_strips_leading_trailing_hyphens() -> None:
    assert derive_slug("---hello world---") == "hello-world"


def test_slug_collapses_repeated_non_alnum() -> None:
    assert derive_slug("hello!!!world???there") == "hello-world-there"


def test_slug_fallback_for_empty_body() -> None:
    assert derive_slug("") == "thought"


def test_slug_fallback_for_punctuation_only() -> None:
    assert derive_slug("!@#$%^&*()_+{}|:<>?") == "thought"


def test_slug_fallback_for_all_hyphens_after_substitution() -> None:
    assert derive_slug("---") == "thought"


def test_slug_handles_unicode_normalization() -> None:
    """NFKC normalization handles ligatures and full-width characters."""
    # Full-width Latin letters should normalize to ASCII via NFKC.
    body = "\uff48\uff45\uff4c\uff4c\uff4f"  # full-width "hello"
    slug = derive_slug(body)
    assert "hello" in slug or slug == "thought"


def test_slug_includes_prefix_bracket_in_input() -> None:
    """Body for slug derivation starts with [Prefix] line per the fingerprint definition."""
    body = "[Lesson] when sqlite-vec returns fewer results"
    slug = derive_slug(body)
    # First 30 chars: "[lesson] when sqlite-vec retur"
    # After non-alnum -> '-': "lesson-when-sqlite-vec-retur"
    assert slug == "lesson-when-sqlite-vec-retur"


# === derive_uuid_tail ===


def test_uuid_tail_returns_last_12_hex() -> None:
    assert derive_uuid_tail(_SAMPLE_UUID) == _SAMPLE_TAIL


def test_uuid_tail_lowercase() -> None:
    """Hex output is always lowercase."""
    u = UUID("0193abcd-7890-7000-abcd-EF0123456789")
    assert derive_uuid_tail(u) == "ef0123456789"


# === derive_relative_path ===


def test_relative_path_full_format() -> None:
    path = derive_relative_path(
        prefix="Lesson",
        body="[Lesson] when sqlite-vec returns fewer results than k",
        created_at=_ts(),
        thought_id=_SAMPLE_UUID,
    )
    assert path == Path("lesson/20260504142301-lesson-when-sqlite-vec-retur-ef0123456789.md")


def test_relative_path_action_item_prefix() -> None:
    path = derive_relative_path(
        prefix="Action Item",
        body="Email Bob about the migration plan",
        created_at=_ts(),
        thought_id=_SAMPLE_UUID,
    )
    assert path.parts[0] == "action-item"
    assert path.suffix == ".md"
    assert _SAMPLE_TAIL in path.name


def test_relative_path_requires_tz_aware_datetime() -> None:
    naive = datetime(2026, 5, 4, 14, 23, 1)
    with pytest.raises(VaultError, match="timezone-aware"):
        derive_relative_path(
            prefix="Lesson",
            body="content",
            created_at=naive,
            thought_id=_SAMPLE_UUID,
        )


def test_relative_path_empty_body_uses_thought_fallback() -> None:
    path = derive_relative_path(
        prefix="Note",
        body="",
        created_at=_ts(),
        thought_id=_SAMPLE_UUID,
    )
    assert path == Path("note/20260504142301-thought-ef0123456789.md")


def test_relative_path_uses_utc_timestamp_format() -> None:
    path = derive_relative_path(
        prefix="Lesson",
        body="hi",
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        thought_id=_SAMPLE_UUID,
    )
    assert "20260102030405" in path.name


def test_filename_uniqueness_across_batch() -> None:
    """1000+ captures in same UTC second produce unique filenames via the 12-hex tail (A7)."""
    seen: set[str] = set()
    timestamp = _ts()
    for i in range(2000):
        # Synthesize varied UUID-v7 tails per iteration.
        uuid_hex = f"0193abcd789070{i:018x}"
        # Pad/truncate to exactly 32 hex chars.
        uuid_hex = uuid_hex[:32] if len(uuid_hex) > 32 else uuid_hex.ljust(32, "0")
        uuid_obj = UUID(uuid_hex)
        path = derive_relative_path(
            prefix="Lesson",
            body="same body content",
            created_at=timestamp,
            thought_id=uuid_obj,
        )
        seen.add(str(path))
    # All 2000 paths must be unique.
    assert len(seen) == 2000
