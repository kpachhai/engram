"""Drift-cleanliness tests for the consolidation frontmatter fields.

Archived originals carry ``archived_at`` + ``superseded_by``; merged thoughts
carry ``consolidated_from`` + ``consolidated_range``. All four must be known
to the frontmatter boundary so consolidated vaults stay drift-clean under
``engram doctor``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.models.frontmatter import Frontmatter
from engram.storage.markdown import _KNOWN_FRONTMATTER_FIELDS, DriftReason, read_thought

_FP = "b" * 64
_EARLY = datetime(2024, 1, 1, tzinfo=UTC)
_LATE = datetime(2026, 6, 9, tzinfo=UTC)

NEW_FIELDS = ("archived_at", "superseded_by", "consolidated_from", "consolidated_range")


def _base_frontmatter() -> dict[str, Any]:
    return {
        "id": uuid4(),
        "prefix": "Lesson",
        "portability": "portable",
        "source": "test",
        "created_at": _EARLY,
        "updated_at": _LATE,
        "fingerprint": _FP,
    }


def _md_with(extra_yaml: str) -> str:
    return (
        "---\n"
        f"id: {uuid4()}\n"
        "prefix: Lesson\n"
        "portability: portable\n"
        "source: test\n"
        "created_at: 2024-01-01T00:00:00+00:00\n"
        "updated_at: 2026-06-09T00:00:00+00:00\n"
        f"fingerprint: {_FP}\n"
        f"{extra_yaml}"
        "---\n"
        "body text\n"
    )


def test_new_fields_are_known_to_drift_scan():
    for field in NEW_FIELDS:
        assert field in _KNOWN_FRONTMATTER_FIELDS


def test_archived_file_produces_no_unknown_field_drift(tmp_path: Path):
    file_path = tmp_path / "archived.md"
    file_path.write_text(
        _md_with(f"archived_at: 2026-06-09T01:00:00+00:00\nsuperseded_by: {uuid4()}\n")
    )
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is not None
    assert not any(d.reason == DriftReason.UNKNOWN_EXTRA_FIELD for d in drifts)


def test_merged_file_produces_no_unknown_field_drift(tmp_path: Path):
    file_path = tmp_path / "merged.md"
    file_path.write_text(
        _md_with(
            f"consolidated_from:\n- {uuid4()}\n- {uuid4()}\n"
            "consolidated_range:\n- 2024-01-01T00:00:00+00:00\n- 2026-06-09T00:00:00+00:00\n"
        )
    )
    result = read_thought(file_path)
    assert result is not None
    thought, drifts = result
    assert thought is not None
    assert not any(d.reason == DriftReason.UNKNOWN_EXTRA_FIELD for d in drifts)


def test_fields_default_to_none_when_absent():
    frontmatter = Frontmatter(**_base_frontmatter())
    assert frontmatter.archived_at is None
    assert frontmatter.superseded_by is None
    assert frontmatter.consolidated_from is None
    assert frontmatter.consolidated_range is None


def test_archived_at_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="timezone-aware"):
        Frontmatter(**_base_frontmatter(), archived_at=datetime(2026, 6, 9))


def test_consolidated_range_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="timezone-aware"):
        Frontmatter(
            **_base_frontmatter(),
            consolidated_range=(datetime(2024, 1, 1), _LATE),
        )


def test_consolidated_range_must_be_ordered():
    with pytest.raises(ValidationError, match="ordered"):
        Frontmatter(**_base_frontmatter(), consolidated_range=(_LATE, _EARLY))


def test_full_provenance_roundtrip():
    source_ids = [uuid4(), uuid4()]
    frontmatter = Frontmatter(
        **_base_frontmatter(),
        consolidated_from=source_ids,
        consolidated_range=(_EARLY, _LATE),
    )
    again = Frontmatter.model_validate(frontmatter.model_dump())
    assert again.consolidated_from == source_ids
    assert again.consolidated_range == (_EARLY, _LATE)
