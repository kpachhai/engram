"""Bundle format tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.bundle.format import (
    BUNDLE_MANIFEST_FILENAME,
    BUNDLE_SCHEMA_VERSION,
    BUNDLE_THOUGHTS_DIR,
    MAX_PER_FILE_BYTES,
    MAX_TOTAL_BYTES,
    BundleManifest,
)


def _make_manifest(**overrides: object) -> BundleManifest:
    base = {
        "schema_version": 1,
        "source_user": "alice",
        "source_vault": "personal",
        "exported_at": datetime.now(UTC),
        "thought_count": 5,
        "portability_filter": ["portable"],
        "embedding_model": "BAAI/bge-small-en-v1.5",
        "bundle_id": uuid4(),
    }
    base.update(overrides)
    return BundleManifest.model_validate(base)


def test_manifest_round_trip_via_json() -> None:
    original = _make_manifest()
    raw = original.to_json()
    rebuilt = BundleManifest.from_json(raw)
    assert rebuilt.bundle_id == original.bundle_id
    assert rebuilt.source_user == "alice"
    assert rebuilt.thought_count == 5
    assert rebuilt.portability_filter == ["portable"]


def test_manifest_extra_field_refused() -> None:
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(
            {
                "schema_version": 1,
                "source_user": "alice",
                "source_vault": "personal",
                "exported_at": datetime.now(UTC).isoformat(),
                "thought_count": 0,
                "portability_filter": ["portable"],
                "embedding_model": "m",
                "bundle_id": str(uuid4()),
                "unknown_field": "x",
            }
        )


def test_manifest_schema_version_must_be_one() -> None:
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(
            {
                "schema_version": 2,
                "source_user": "alice",
                "source_vault": "personal",
                "exported_at": datetime.now(UTC).isoformat(),
                "thought_count": 0,
                "portability_filter": ["portable"],
                "embedding_model": "m",
                "bundle_id": str(uuid4()),
            }
        )


def test_manifest_thought_count_non_negative() -> None:
    with pytest.raises(ValidationError):
        _make_manifest(thought_count=-1)


def test_bundle_source_tag_format() -> None:
    m = _make_manifest()
    assert m.bundle_source_tag == f"bundle:{m.bundle_id}"


def test_constants_match_security_md_caps() -> None:
    """Per 06-SECURITY.md: 1 MB / file, 4 GB / bundle."""
    assert MAX_PER_FILE_BYTES == 1 * 1024 * 1024
    assert MAX_TOTAL_BYTES == 4 * 1024 * 1024 * 1024
    assert BUNDLE_THOUGHTS_DIR == "thoughts"
    assert BUNDLE_MANIFEST_FILENAME == "manifest.json"
    assert BUNDLE_SCHEMA_VERSION == 1
