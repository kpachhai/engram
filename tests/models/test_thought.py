"""Tests for engram.models.thought."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.models.thought import Thought, ThoughtWithSimilarity

_NOW = datetime.now(UTC)
_GOOD_FINGERPRINT = "a" * 64


def _base_thought_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": uuid4(),
        "schema_version": 1,
        "prefix": "Lesson",
        "portability": "portable",
        "source": "kpachhai",
        "created_at": _NOW,
        "updated_at": _NOW,
        "fingerprint": _GOOD_FINGERPRINT,
        "content": "[Lesson] body content",
        "file_path": Path("lesson/20260504142301-body-content-deadbeef0123.md"),
    }
    base.update(overrides)
    return base


def test_basic_thought_construction():
    t = Thought.model_validate(_base_thought_dict())
    assert t.prefix == "Lesson"
    assert t.content == "[Lesson] body content"
    assert isinstance(t.file_path, Path)


def test_vault_defaults_to_default():
    t = Thought.model_validate(_base_thought_dict())
    assert t.vault == "default"


def test_explicit_vault_preserved():
    t = Thought.model_validate(_base_thought_dict(vault="personal"))
    assert t.vault == "personal"


def test_legacy_id_optional_and_default_none():
    t = Thought.model_validate(_base_thought_dict())
    assert t.legacy_id is None


def test_legacy_id_string_accepted():
    t = Thought.model_validate(_base_thought_dict(legacy_id="ob-uuid-v4-here"))
    assert t.legacy_id == "ob-uuid-v4-here"


def test_thought_with_similarity_score_in_range():
    raw = _base_thought_dict(similarity=0.873)
    tws = ThoughtWithSimilarity.model_validate(raw)
    assert tws.similarity == pytest.approx(0.873)


def test_thought_with_similarity_score_out_of_range_rejected():
    with pytest.raises(ValidationError):
        ThoughtWithSimilarity.model_validate(_base_thought_dict(similarity=1.5))
    with pytest.raises(ValidationError):
        ThoughtWithSimilarity.model_validate(_base_thought_dict(similarity=-0.1))


def test_file_path_string_coerced_to_path():
    raw = _base_thought_dict()
    raw["file_path"] = "lesson/x.md"
    t = Thought.model_validate(raw)
    assert isinstance(t.file_path, Path)


def test_thought_dump_round_trip():
    t = Thought.model_validate(_base_thought_dict(tags=["a", "b"]))
    dumped = t.model_dump()
    t2 = Thought.model_validate(dumped)
    assert t.id == t2.id
    assert t.tags == t2.tags
