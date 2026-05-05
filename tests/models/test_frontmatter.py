"""Tests for engram.models.frontmatter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from engram.models.frontmatter import (
    CANONICAL_PREFIXES,
    DEFAULT_PORTABILITY_BY_PREFIX,
    Frontmatter,
    is_canonical_prefix,
)

_GOOD_FINGERPRINT = "0" * 64
_NOW = datetime.now(UTC)


def _base_frontmatter_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "schema_version": 1,
        "id": uuid4(),
        "prefix": "Lesson",
        "portability": "portable",
        "source": "kpachhai",
        "created_at": _NOW,
        "updated_at": _NOW,
        "fingerprint": _GOOD_FINGERPRINT,
    }
    base.update(overrides)
    return base


# === canonical vocabulary ===


def test_canonical_prefixes_count():
    assert len(CANONICAL_PREFIXES) == 15


def test_each_canonical_prefix_validates():
    for prefix in CANONICAL_PREFIXES:
        fm = Frontmatter.model_validate(_base_frontmatter_dict(prefix=prefix))
        assert fm.prefix == prefix


def test_is_canonical_prefix():
    assert is_canonical_prefix("Lesson")
    assert is_canonical_prefix("Action Item")
    assert not is_canonical_prefix("lesson")  # case-sensitive
    assert not is_canonical_prefix("Brainstorm")
    assert not is_canonical_prefix("")


def test_default_portability_by_prefix_only_lists_sensitive_prefixes():
    """Per spec: only Domain and Artifact default to sensitive; others are portable."""
    assert DEFAULT_PORTABILITY_BY_PREFIX["Domain"] == "sensitive"
    assert DEFAULT_PORTABILITY_BY_PREFIX["Artifact"] == "sensitive"
    assert "Lesson" not in DEFAULT_PORTABILITY_BY_PREFIX
    assert "Friction" not in DEFAULT_PORTABILITY_BY_PREFIX


# === schema_version ===


def test_schema_version_defaults_to_1_when_missing():
    """Per NFR5: a markdown file omitting schema_version is read as v1."""
    raw = _base_frontmatter_dict()
    del raw["schema_version"]
    fm = Frontmatter.model_validate(raw)
    assert fm.schema_version == 1


def test_schema_version_explicit_value_preserved():
    fm = Frontmatter.model_validate(_base_frontmatter_dict(schema_version=2))
    assert fm.schema_version == 2


def test_schema_version_zero_or_negative_rejected():
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(_base_frontmatter_dict(schema_version=0))


# === required fields ===


def test_missing_id_rejected():
    raw = _base_frontmatter_dict()
    del raw["id"]
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(raw)


def test_missing_prefix_rejected():
    raw = _base_frontmatter_dict()
    del raw["prefix"]
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(raw)


def test_missing_fingerprint_rejected():
    raw = _base_frontmatter_dict()
    del raw["fingerprint"]
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(raw)


# === prefix safety ===


def test_unknown_prefix_value_accepted():
    """Per Frontmatter Schema Drift Handling: unknown prefix is INDEXED, not rejected."""
    fm = Frontmatter.model_validate(_base_frontmatter_dict(prefix="Brainstorm"))
    assert fm.prefix == "Brainstorm"


def test_prefix_path_traversal_rejected():
    with pytest.raises(ValidationError, match="path-traversal"):
        Frontmatter.model_validate(_base_frontmatter_dict(prefix="../escape"))


def test_prefix_null_byte_rejected():
    with pytest.raises(ValidationError, match="path-traversal"):
        Frontmatter.model_validate(_base_frontmatter_dict(prefix="foo\x00bar"))


def test_prefix_rtl_override_rejected():
    with pytest.raises(ValidationError, match="right-to-left"):
        Frontmatter.model_validate(_base_frontmatter_dict(prefix="bad\u202ebad"))


def test_prefix_empty_rejected():
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(_base_frontmatter_dict(prefix=""))


# === portability ===


@pytest.mark.parametrize("portability", ["portable", "sensitive", "block"])
def test_each_valid_portability_accepted(portability: str):
    fm = Frontmatter.model_validate(_base_frontmatter_dict(portability=portability))
    assert fm.portability == portability


def test_invalid_portability_rejected():
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(_base_frontmatter_dict(portability="confidential"))


def test_portability_defaults_to_portable():
    raw = _base_frontmatter_dict()
    del raw["portability"]
    fm = Frontmatter.model_validate(raw)
    assert fm.portability == "portable"


# === datetime tz-awareness ===


def test_naive_created_at_rejected():
    naive = datetime(2026, 5, 4, 14, 0, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Frontmatter.model_validate(_base_frontmatter_dict(created_at=naive))


def test_naive_legacy_created_at_rejected():
    naive = datetime(2026, 5, 4, 14, 0, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Frontmatter.model_validate(_base_frontmatter_dict(legacy_created_at=naive))


def test_legacy_created_at_can_be_none():
    fm = Frontmatter.model_validate(_base_frontmatter_dict())
    assert fm.legacy_created_at is None


# === fingerprint ===


def test_fingerprint_must_be_64_hex_chars():
    with pytest.raises(ValidationError, match="64 lowercase hex"):
        Frontmatter.model_validate(_base_frontmatter_dict(fingerprint="too-short"))


def test_fingerprint_uppercase_rejected():
    upper = "F" * 64
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(_base_frontmatter_dict(fingerprint=upper))


def test_fingerprint_non_hex_rejected():
    with pytest.raises(ValidationError):
        Frontmatter.model_validate(_base_frontmatter_dict(fingerprint="g" * 64))


# === unknown extra fields ===


def test_unknown_extra_field_preserved():
    raw = _base_frontmatter_dict()
    raw["custom_field"] = "preserved"
    fm = Frontmatter.model_validate(raw)
    dumped = fm.model_dump()
    assert dumped["custom_field"] == "preserved"


def test_unknown_extra_nested_field_preserved():
    raw = _base_frontmatter_dict()
    raw["future_metadata"] = {"depth": 1, "items": ["a", "b"]}
    fm = Frontmatter.model_validate(raw)
    dumped = fm.model_dump()
    assert dumped["future_metadata"] == {"depth": 1, "items": ["a", "b"]}


# === id ===


def test_id_accepts_uuid_string():
    raw = _base_frontmatter_dict()
    raw["id"] = "0193abcd-7890-7000-abcd-ef0123456789"
    fm = Frontmatter.model_validate(raw)
    assert isinstance(fm.id, UUID)
    assert fm.id.hex == "0193abcd789070" + "00abcd" + "ef0123456789"


# === tags ===


def test_tags_default_to_empty_list():
    raw = _base_frontmatter_dict()
    raw.pop("tags", None)
    fm = Frontmatter.model_validate(raw)
    assert fm.tags == []


def test_tags_explicit_list_preserved():
    fm = Frontmatter.model_validate(_base_frontmatter_dict(tags=["a", "b"]))
    assert fm.tags == ["a", "b"]


# === round-trip ===


def test_round_trip_dump_then_validate():
    fm = Frontmatter.model_validate(_base_frontmatter_dict(tags=["x"]))
    dumped = fm.model_dump()
    fm2 = Frontmatter.model_validate(dumped)
    assert fm.id == fm2.id
    assert fm.prefix == fm2.prefix
    assert fm.tags == fm2.tags
    assert fm.fingerprint == fm2.fingerprint
