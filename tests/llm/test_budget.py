"""LLMBudget tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from engram.errors import LLMProviderError
from engram.llm.budget import (
    LLMBudget,
    estimate_tokens,
    truncate_to_budget,
)
from engram.models import ThoughtWithSimilarity


def _thought(content: str, similarity: float, vault: str = "primary") -> ThoughtWithSimilarity:
    now = datetime.now(UTC)
    return ThoughtWithSimilarity.model_validate(
        {
            "id": uuid4(),
            "schema_version": 1,
            "prefix": "Pattern",
            "portability": "portable",
            "source": "test",
            "created_at": now,
            "updated_at": now,
            "fingerprint": "a" * 64,
            "tags": [],
            "vault": vault,
            "legacy_id": None,
            "content": content,
            "file_path": Path("/tmp/x.md"),
            "similarity": similarity,
        }
    )


# --- daily-cap behavior ------------------------------------------------


def test_daily_cap_refuses_when_estimate_exceeds(tmp_path: Path) -> None:
    budget = LLMBudget(state_path=tmp_path / "u.json", daily_cost_cap_usd=5.0)
    budget.record_usage(cost_usd=4.5)
    with pytest.raises(LLMProviderError) as exc_info:
        budget.check_budget(estimate_cost_usd=1.0)
    assert "daily_cost_cap_exceeded" in str(exc_info.value)


def test_daily_cap_passes_when_under(tmp_path: Path) -> None:
    budget = LLMBudget(state_path=tmp_path / "u.json", daily_cost_cap_usd=5.0)
    budget.record_usage(cost_usd=2.0)
    budget.check_budget(estimate_cost_usd=1.0)  # 2 + 1 < 5; no raise.


def test_daily_cap_zero_disables_check(tmp_path: Path) -> None:
    budget = LLMBudget(state_path=tmp_path / "u.json", daily_cost_cap_usd=0.0)
    budget.record_usage(cost_usd=999.0)
    budget.check_budget(estimate_cost_usd=999.0)


def test_persisted_state_survives_reload(tmp_path: Path) -> None:
    state = tmp_path / "u.json"
    budget = LLMBudget(state_path=state, daily_cost_cap_usd=10.0)
    budget.record_usage(cost_usd=2.5, input_tokens=100, output_tokens=50)
    rebuilt = LLMBudget.load_or_init(state_path=state, daily_cost_cap_usd=10.0)
    assert rebuilt.today_cost_usd() == pytest.approx(2.5)


def test_corrupt_state_falls_back_to_empty(tmp_path: Path) -> None:
    state = tmp_path / "u.json"
    state.write_text("not json", encoding="utf-8")
    budget = LLMBudget.load_or_init(state_path=state, daily_cost_cap_usd=5.0)
    assert budget.today_cost_usd() == 0.0


# --- truncate_to_budget ------------------------------------------------


def test_truncate_respects_floor_per_vault() -> None:
    rows = [
        _thought("primary thought 1 " * 10, 0.9, vault="primary"),
        _thought("primary thought 2 " * 10, 0.85, vault="primary"),
        _thought("primary thought 3 " * 10, 0.8, vault="primary"),
        _thought("alice thought 1 " * 10, 0.7, vault="alice"),
        _thought("alice thought 2 " * 10, 0.65, vault="alice"),
    ]
    out = truncate_to_budget(rows, max_input_tokens=200, min_per_vault_results=2)
    primary_out = [r for r in out if r.vault == "primary"]
    alice_out = [r for r in out if r.vault == "alice"]
    assert len(primary_out) >= 2
    assert len(alice_out) >= 2


def test_truncate_drops_lowest_first_above_floor() -> None:
    rows = [
        _thought("a", 0.9, vault="primary"),
        _thought("a", 0.5, vault="primary"),
        _thought("a", 0.99, vault="alice"),
    ]
    out = truncate_to_budget(rows, max_input_tokens=10000, min_per_vault_results=0)
    # Top similarity first
    assert out[0].similarity == pytest.approx(0.99)


def test_truncate_raises_when_floor_exceeds_budget() -> None:
    big_content = "x" * 10000
    rows = [
        _thought(big_content, 0.9, vault="primary"),
        _thought(big_content, 0.85, vault="primary"),
        _thought(big_content, 0.8, vault="primary"),
    ]
    with pytest.raises(LLMProviderError) as exc_info:
        truncate_to_budget(rows, max_input_tokens=100, min_per_vault_results=3)
    assert "prompt_too_large_even_at_floor" in str(exc_info.value)


def test_truncate_empty_input_returns_empty() -> None:
    assert truncate_to_budget([], max_input_tokens=1000) == []


# --- estimate_tokens ---------------------------------------------------


def test_estimate_tokens_minimum_one() -> None:
    assert estimate_tokens("") >= 1
    assert estimate_tokens("hi") >= 1


def test_estimate_tokens_grows_with_content() -> None:
    short = estimate_tokens("hello")
    long = estimate_tokens("hello " * 100)
    assert long > short
