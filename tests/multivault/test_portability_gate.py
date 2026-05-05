"""Phase 3 portability gate tests (Step 6 verifier).

Per ``docs/PHASE_3_PLAN.md`` Step 6:

* ``strip_block_thoughts`` removes block rows from a list (read paths).
* ``assert_no_block_in_results`` raises BlockThoughtLLMDisallowed when a
  block row reaches an LLM-context-assembly path.
* push-down + gate compose - if push-down is bypassed, the gate still
  catches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from engram.errors import BlockThoughtLLMDisallowed
from engram.models import Thought, ThoughtWithSimilarity
from engram.multivault.aggregator import aggregate_search
from engram.multivault.portability import (
    assert_no_block_in_results,
    split_portabilities,
    strip_block_thoughts,
)
from engram.multivault.registry import VaultRegistry
from tests.multivault.conftest import (
    fixed_query_vec,
    make_vault_storage,
    populate_vault,
)


def _thought_dict(portability: str, *, content: str = "x") -> Thought:
    now = datetime.now(UTC)
    return Thought.model_validate(
        {
            "id": uuid4(),
            "schema_version": 1,
            "prefix": "Pattern",
            "portability": portability,
            "source": "test",
            "created_at": now,
            "updated_at": now,
            "fingerprint": "deadbeef",
            "tags": [],
            "vault": "default",
            "legacy_id": None,
            "content": content,
            "file_path": Path("/tmp/x.md"),
        }
    )


def test_strip_block_drops_block_rows() -> None:
    rows = [
        _thought_dict("portable"),
        _thought_dict("block"),
        _thought_dict("sensitive"),
    ]
    out = strip_block_thoughts(rows)
    assert len(out) == 2
    assert all(t.portability != "block" for t in out)


def test_strip_block_returns_input_when_no_block_present() -> None:
    rows = [_thought_dict("portable"), _thought_dict("sensitive")]
    out = strip_block_thoughts(rows)
    assert out == rows


def test_assert_no_block_passes_when_clean() -> None:
    rows = [_thought_dict("portable"), _thought_dict("sensitive")]
    # Should not raise.
    assert_no_block_in_results(rows)


def test_assert_no_block_raises_with_block_present() -> None:
    rows = [_thought_dict("portable"), _thought_dict("block")]
    with pytest.raises(BlockThoughtLLMDisallowed) as exc_info:
        assert_no_block_in_results(rows)
    assert exc_info.value.error_code == "block_thought_llm_disallowed"


def test_assert_no_block_works_on_thought_with_similarity() -> None:
    base = _thought_dict("block")
    rows = [ThoughtWithSimilarity(**base.model_dump(), similarity=0.9)]
    with pytest.raises(BlockThoughtLLMDisallowed):
        assert_no_block_in_results(rows)


def test_split_portabilities_partitions_correctly() -> None:
    rows = [
        _thought_dict("portable"),
        _thought_dict("sensitive"),
        _thought_dict("block"),
        _thought_dict("portable"),
    ]
    portable, sensitive, block = split_portabilities(rows)
    assert len(portable) == 2
    assert len(sensitive) == 1
    assert len(block) == 1


def test_pushdown_and_gate_compose(tmp_path: Path) -> None:
    """SQL push-down already excludes block; the gate is defense-in-depth.

    We exercise the gate against the aggregator's output. The aggregator
    pushed-down ``portability != 'block'`` at the SQL layer; even if a
    bug allowed a block row to survive, the gate would catch it. Here
    we assert that the aggregator's normal output passes the gate
    without raising (i.e., zero block rows in cross-vault results).
    """
    primary = make_vault_storage(base=tmp_path, name="primary")
    populate_vault(
        primary,
        thoughts=[
            ("[Decision] block", "block", 0),
            ("[Pattern] portable", "portable", 0),
        ],
    )
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")

    result = aggregate_search(
        registry=registry,
        query_embedding=fixed_query_vec(0),
        k=10,
    )
    # The aggregator output should have NO block rows; the gate accepts.
    underlying = [r.thought for r in result.rows]
    assert_no_block_in_results(underlying)

    primary.close()
