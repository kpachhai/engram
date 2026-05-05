"""Tests for engram.models.mcp - MCP tool I/O models."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.models.mcp import (
    CaptureInput,
    CaptureInputMetadata,
    CaptureOutput,
    FetchInput,
    FetchOutput,
    Filter,
    ListInput,
    ListOutput,
    PortabilityCounts,
    SearchInput,
    SearchOutput,
    StatsOutput,
)
from engram.models.thought import Thought, ThoughtWithSimilarity

_NOW = datetime.now(UTC)
_FP = "a" * 64
_THOUGHT_DICT = {
    "id": uuid4(),
    "prefix": "Lesson",
    "portability": "portable",
    "source": "kpachhai",
    "created_at": _NOW,
    "updated_at": _NOW,
    "fingerprint": _FP,
    "content": "body",
    "file_path": "lesson/x.md",
}


# === CaptureInput / Metadata / Output ===


def test_capture_input_minimal():
    inp = CaptureInput.model_validate({"content": "hello"})
    assert inp.content == "hello"
    assert inp.metadata is None


def test_capture_input_with_full_metadata():
    inp = CaptureInput.model_validate(
        {
            "content": "[Lesson] body",
            "metadata": {
                "prefix": "Lesson",
                "portability": "portable",
                "source": "user",
                "tags": ["a"],
            },
        }
    )
    assert inp.metadata is not None
    assert inp.metadata.prefix == "Lesson"
    assert inp.metadata.tags == ["a"]


def test_capture_input_unknown_field_rejected():
    """extra='forbid' on inputs surfaces typos and bad client requests."""
    with pytest.raises(ValidationError):
        CaptureInput.model_validate({"content": "x", "unknown_field": "y"})


def test_capture_input_metadata_unknown_field_rejected():
    with pytest.raises(ValidationError):
        CaptureInputMetadata.model_validate({"prefix": "Lesson", "unknown": "z"})


def test_capture_output_shape():
    out = CaptureOutput.model_validate(
        {
            "id": "0193abcd-7890-7000-abcd-ef0123456789",
            "file_path": "lesson/x.md",
            "fingerprint": _FP,
        }
    )
    assert str(out.id) == "0193abcd-7890-7000-abcd-ef0123456789"
    assert out.file_path == "lesson/x.md"


# === SearchInput / Output ===


def test_search_input_defaults_k_to_10():
    inp = SearchInput.model_validate({"query": "hello"})
    assert inp.k == 10
    assert inp.filter is None


def test_search_input_k_lower_bound_rejected():
    with pytest.raises(ValidationError):
        SearchInput.model_validate({"query": "x", "k": 0})


def test_search_input_k_upper_bound_rejected():
    with pytest.raises(ValidationError):
        SearchInput.model_validate({"query": "x", "k": 101})


def test_search_input_k_at_max_accepted():
    inp = SearchInput.model_validate({"query": "x", "k": 100})
    assert inp.k == 100


def test_search_input_empty_query_rejected():
    with pytest.raises(ValidationError):
        SearchInput.model_validate({"query": ""})


def test_search_input_filter_full_shape():
    inp = SearchInput.model_validate(
        {
            "query": "x",
            "filter": {
                "prefix": ["Lesson", "Pattern"],
                "portability": "portable",
                "source": "kpachhai",
                "tags": ["debugging"],
                "vault": "personal",
                "created_after": "2026-01-01T00:00:00+00:00",
                "created_before": "2026-12-31T23:59:59+00:00",
            },
        }
    )
    assert inp.filter is not None
    assert inp.filter.prefix == ["Lesson", "Pattern"]


def test_search_output_with_results():
    tws = ThoughtWithSimilarity.model_validate({**_THOUGHT_DICT, "similarity": 0.9})
    out = SearchOutput.model_validate({"results": [tws.model_dump()], "total_found": 1})
    assert len(out.results) == 1
    assert out.results[0].similarity == pytest.approx(0.9)


# === ListInput / Output ===


def test_list_input_defaults():
    inp = ListInput.model_validate({})
    assert inp.limit == 50
    assert inp.offset == 0
    assert inp.sort == "created_at_desc"


def test_list_input_invalid_sort_rejected():
    with pytest.raises(ValidationError):
        ListInput.model_validate({"sort": "random"})


def test_list_input_offset_negative_rejected():
    with pytest.raises(ValidationError):
        ListInput.model_validate({"offset": -1})


def test_list_input_limit_over_500_rejected():
    with pytest.raises(ValidationError):
        ListInput.model_validate({"limit": 501})


def test_list_input_limit_zero_allowed():
    """B4 / Q2: limit=0 is allowed (returns empty results, correct total_count)."""
    inp = ListInput.model_validate({"limit": 0})
    assert inp.limit == 0


def test_list_output_with_results():
    t = Thought.model_validate(_THOUGHT_DICT)
    out = ListOutput.model_validate({"results": [t.model_dump()], "total_count": 1})
    assert out.total_count == 1


# === StatsOutput / PortabilityCounts ===


def test_stats_output_empty_vault():
    """B7 / Q3: oldest/newest are nullable on an empty vault."""
    out = StatsOutput.model_validate(
        {
            "total_count": 0,
            "by_prefix": {},
            "by_portability": {"portable": 0, "sensitive": 0, "block": 0},
            "by_source": {},
            "by_vault": {},
            "oldest": None,
            "newest": None,
            "index_size_bytes": 0,
            "vault_paths": [],
        }
    )
    assert out.total_count == 0
    assert out.oldest is None
    assert out.newest is None


def test_stats_output_populated():
    out = StatsOutput.model_validate(
        {
            "total_count": 5,
            "by_prefix": {"Lesson": 3, "Pattern": 2},
            "by_portability": {"portable": 4, "sensitive": 1, "block": 0},
            "by_source": {"kpachhai": 5},
            "by_vault": {"personal": 5},
            "oldest": _NOW,
            "newest": _NOW,
            "index_size_bytes": 12345,
            "vault_paths": ["/home/k/repos/memex"],
        }
    )
    assert out.total_count == 5
    assert out.by_prefix["Lesson"] == 3
    assert out.by_portability.portable == 4


def test_portability_counts_unknown_key_rejected():
    """PortabilityCounts has a strict shape (only the three known keys)."""
    with pytest.raises(ValidationError):
        PortabilityCounts.model_validate({"portable": 1, "weird": 2})


# === FetchInput / Output ===


def test_fetch_input_validates_uuid():
    inp = FetchInput.model_validate({"id": "0193abcd-7890-7000-abcd-ef0123456789"})
    assert str(inp.id) == "0193abcd-7890-7000-abcd-ef0123456789"


def test_fetch_input_invalid_uuid_rejected():
    with pytest.raises(ValidationError):
        FetchInput.model_validate({"id": "not-a-uuid"})


def test_fetch_output_thought_present():
    t = Thought.model_validate(_THOUGHT_DICT)
    out = FetchOutput.model_validate({"thought": t.model_dump()})
    assert out.thought is not None


def test_fetch_output_thought_null():
    """B6: fetch returns null thought (not error) when id is unknown."""
    out = FetchOutput.model_validate({"thought": None})
    assert out.thought is None


def test_fetch_output_default_thought_none():
    out = FetchOutput.model_validate({})
    assert out.thought is None


# === Filter ===


def test_filter_all_fields_optional():
    f = Filter.model_validate({})
    assert f.prefix is None
    assert f.tags is None
    assert f.vault is None


def test_filter_unknown_key_rejected():
    with pytest.raises(ValidationError):
        Filter.model_validate({"unknown_dimension": "x"})


def test_filter_invalid_portability_rejected():
    with pytest.raises(ValidationError):
        Filter.model_validate({"portability": "confidential"})
