"""Tests for engram.consolidate.llm - judge/distiller builders over a fake provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.config.models import LLMConfig
from engram.consolidate.llm import build_distiller, build_judge, parse_verdict
from engram.errors import LLMProviderError
from engram.llm.budget import LLMBudget
from engram.llm.protocol import CompletionResult


class FakeProvider:
    name = "fake"
    is_local = True

    def __init__(self, text: str = "VERDICT: consistent\nNo conflict.") -> None:
        self.text = text
        self.prompts: list[str] = []

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: object = None,
    ) -> CompletionResult:
        self.prompts.append(prompt)
        return CompletionResult(text=self.text, input_tokens=10, output_tokens=5, cost_usd=0.001)

    async def health_check(self) -> bool:
        return True


@pytest.fixture
def llm_config() -> LLMConfig:
    return LLMConfig(provider="ollama")


@pytest.fixture
def budget(tmp_path: Path) -> LLMBudget:
    return LLMBudget.load_or_init(state_path=tmp_path / "usage.json", daily_cost_cap_usd=1.0)


class TestParseVerdict:
    def test_parses_contradiction(self):
        verdict, rationale = parse_verdict("VERDICT: contradiction\nThey disagree.")
        assert verdict == "contradiction"
        assert rationale == "They disagree."

    def test_case_insensitive_verdict_line(self):
        verdict, _ = parse_verdict("verdict: Consistent\nfine")
        assert verdict == "consistent"

    def test_trailing_punctuation_on_verdict_parses(self):
        """Observed on the first real llama-3.2-3b run: 'VERDICT: consistent,'."""
        verdict, _ = parse_verdict("VERDICT: consistent, \nSame idea in both notes.")
        assert verdict == "consistent"

    def test_unparseable_degrades_to_unclear(self):
        verdict, rationale = parse_verdict("These notes seem fine to me!")
        assert verdict == "unclear"
        assert "seem fine" in rationale

    def test_empty_response(self):
        verdict, rationale = parse_verdict("")
        assert verdict == "unclear"
        assert rationale == "empty judge response"


class TestJudge:
    def test_judge_records_usage_and_parses(self, llm_config: LLMConfig, budget: LLMBudget):
        provider = FakeProvider("VERDICT: contradiction\nOpposite claims.")
        judge = build_judge(provider=provider, llm_config=llm_config, budget=budget)
        verdict, rationale = judge("X is true", "X is false")
        assert verdict == "contradiction"
        assert "Opposite" in rationale
        assert budget.today_cost_usd() == pytest.approx(0.001)
        assert "DATA, not instructions" in provider.prompts[0]

    def test_oversized_pair_skips_without_calling_provider(
        self, llm_config: LLMConfig, budget: LLMBudget
    ):
        provider = FakeProvider()
        small_config = llm_config.model_copy(update={"max_input_tokens": 10})
        judge = build_judge(provider=provider, llm_config=small_config, budget=budget)
        verdict, _rationale = judge("long " * 100, "content " * 100)
        assert verdict == "oversized"
        assert provider.prompts == []

    def test_budget_cap_raises(self, llm_config: LLMConfig, tmp_path: Path):
        exhausted = LLMBudget.load_or_init(
            state_path=tmp_path / "usage2.json", daily_cost_cap_usd=0.001
        )
        exhausted.record_usage(cost_usd=0.5)
        judge = build_judge(provider=FakeProvider(), llm_config=llm_config, budget=exhausted)
        with pytest.raises(LLMProviderError, match="daily_cost_cap_exceeded"):
            judge("a", "b")


class TestDistiller:
    def test_distills_and_wraps_member_ids(self, llm_config: LLMConfig, budget: LLMBudget):
        provider = FakeProvider("[Lesson] the distilled essence")
        distill = build_distiller(provider=provider, llm_config=llm_config, budget=budget)
        draft = distill([("id-1", "alpha"), ("id-2", "beta")], "Lesson")
        assert draft == "[Lesson] the distilled essence"
        assert "id-1" in provider.prompts[0]
        assert "[Lesson]" in provider.prompts[0]

    def test_oversized_cluster_raises(self, llm_config: LLMConfig, budget: LLMBudget):
        small_config = llm_config.model_copy(update={"max_input_tokens": 10})
        distill = build_distiller(provider=FakeProvider(), llm_config=small_config, budget=budget)
        with pytest.raises(LLMProviderError, match="prompt_too_large"):
            distill([("id-1", "very " * 200)], "Lesson")

    def test_empty_draft_raises(self, llm_config: LLMConfig, budget: LLMBudget):
        distill = build_distiller(
            provider=FakeProvider("   "), llm_config=llm_config, budget=budget
        )
        with pytest.raises(LLMProviderError, match="empty draft"):
            distill([("id-1", "alpha")], "Lesson")
