"""Per-day cost cap + token-budget pre-truncation.

The budget tracker persists to ``<primary-vault>/.indexes/llm_usage.json``
so cap state survives serve restarts:

* :meth:`LLMBudget.check_budget` raises
  :class:`engram.errors.LLMProviderError` with reason
  ``daily_cost_cap_exceeded`` when today's tally + the estimate would
  cross :attr:`engram.config.models.LLMConfig.daily_cost_cap_usd`.
* :meth:`LLMBudget.record_usage` writes back atomically via
  :func:`engram.utils.atomic_write.atomic_write_text` so a crash mid
  write leaves either the previous-good or the new-good file.
* :func:`truncate_to_budget` drops lowest-similarity thoughts until the
  prompt fits ``max_input_tokens``, BUT preserves the per-vault floor
  (``min_per_vault_results``). If the floor itself exceeds the budget,
  raise ``prompt_too_large_even_at_floor``.

Token estimates use a simple ``len(text) // 4`` heuristic for the
naked thought body; providers update the actual token count post-hoc
via :meth:`record_usage`.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from engram.errors import LLMProviderError
from engram.utils.atomic_write import atomic_write_text

if TYPE_CHECKING:
    from engram.models import ThoughtWithSimilarity

_log = logging.getLogger("engram.llm.budget")

#: Heuristic token-per-byte ratio for prompt assembly. Conservative.
_TOKENS_PER_CHAR = 0.25  # ~4 characters per token, common rule-of-thumb.


def estimate_tokens(text: str) -> int:
    """Return an int estimate of token count for ``text``.

    Public so tests + the synthesizer can use the same heuristic.
    """
    return max(1, int(len(text) * _TOKENS_PER_CHAR))


@dataclass(slots=True)
class LLMUsageRecord:
    """One day's usage row inside the persisted JSON."""

    date_iso: str
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0


@dataclass(slots=True)
class LLMBudget:
    """Per-day cost / token tracker persisted under the primary vault."""

    state_path: Path
    daily_cost_cap_usd: float
    history: dict[str, LLMUsageRecord] = field(default_factory=dict)

    @classmethod
    def load_or_init(cls, *, state_path: Path, daily_cost_cap_usd: float) -> LLMBudget:
        """Read ``state_path`` if it exists; else return an empty tracker."""
        budget = cls(state_path=state_path, daily_cost_cap_usd=daily_cost_cap_usd)
        if state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _log.warning("failed to read %s: %s; resetting budget", state_path, exc)
                return budget
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(value, dict):
                        budget.history[key] = LLMUsageRecord(
                            date_iso=key,
                            cost_usd=float(value.get("cost_usd", 0.0)),
                            input_tokens=int(value.get("input_tokens", 0)),
                            output_tokens=int(value.get("output_tokens", 0)),
                            call_count=int(value.get("call_count", 0)),
                        )
        return budget

    def _today_key(self) -> str:
        return datetime.now(UTC).date().isoformat()

    def today_cost_usd(self) -> float:
        """Return cumulative spend for today (UTC)."""
        rec = self.history.get(self._today_key())
        return rec.cost_usd if rec else 0.0

    def check_budget(self, *, estimate_cost_usd: float) -> None:
        """Refuse if today's total + estimate would exceed the cap.

        Raises:
            LLMProviderError: with reason ``daily_cost_cap_exceeded``.
        """
        if self.daily_cost_cap_usd <= 0:
            return
        running = self.today_cost_usd()
        if running + estimate_cost_usd > self.daily_cost_cap_usd:
            msg = (
                f"daily_cost_cap_exceeded: today={running:.4f} USD + "
                f"estimate={estimate_cost_usd:.4f} USD > "
                f"cap={self.daily_cost_cap_usd:.2f} USD. Wait until 00:00 UTC "
                "or raise llm.daily_cost_cap_usd in your per-user config."
            )
            raise LLMProviderError(msg)

    def record_usage(
        self,
        *,
        cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        """Append today's totals and persist atomically."""
        key = self._today_key()
        rec = self.history.get(key) or LLMUsageRecord(date_iso=key)
        rec.cost_usd += cost_usd
        rec.input_tokens += input_tokens
        rec.output_tokens += output_tokens
        rec.call_count += 1
        self.history[key] = rec
        self._persist()

    def _persist(self) -> None:
        payload = {
            key: {
                "cost_usd": rec.cost_usd,
                "input_tokens": rec.input_tokens,
                "output_tokens": rec.output_tokens,
                "call_count": rec.call_count,
            }
            for key, rec in self.history.items()
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.state_path, json.dumps(payload, indent=2, sort_keys=True))


def truncate_to_budget(
    thoughts: Iterable[ThoughtWithSimilarity],
    *,
    max_input_tokens: int,
    min_per_vault_results: int = 3,
) -> list[ThoughtWithSimilarity]:
    """Drop lowest-similarity thoughts until the prompt fits the budget.

    The per-vault floor (Step 5) wins over the budget cap: every vault
    contributes at least ``min_per_vault_results`` thoughts even if the
    floor itself exceeds the budget. In that case raise
    :class:`engram.errors.LLMProviderError` with reason
    ``prompt_too_large_even_at_floor`` (SF-6).

    Args:
        thoughts: input list (may be unsorted; we sort by similarity).
        max_input_tokens: the budget cap.
        min_per_vault_results: floor per vault; ``0`` disables the
            floor entirely.

    Returns:
        Filtered list of thoughts, ordered by similarity desc within
        each vault; the floor is preserved before any other dropping.
    """
    rows = list(thoughts)
    if not rows:
        return []

    # Group by vault, sort each vault's rows by similarity desc.
    by_vault: dict[str, list[ThoughtWithSimilarity]] = defaultdict(list)
    for r in rows:
        by_vault[r.vault].append(r)
    for _vault, vault_rows in by_vault.items():
        vault_rows.sort(key=lambda t: t.similarity, reverse=True)

    floor: list[ThoughtWithSimilarity] = []
    leftover: list[ThoughtWithSimilarity] = []
    for _vault, vault_rows in by_vault.items():
        floor.extend(vault_rows[:min_per_vault_results])
        leftover.extend(vault_rows[min_per_vault_results:])

    floor_tokens = sum(estimate_tokens(t.content) for t in floor)
    if floor_tokens > max_input_tokens:
        msg = (
            f"prompt_too_large_even_at_floor: per-vault floor of "
            f"{min_per_vault_results} per vault implies "
            f"{floor_tokens} tokens > budget {max_input_tokens}. Reduce "
            "min_per_vault_results or raise llm.max_input_tokens."
        )
        raise LLMProviderError(msg)

    leftover.sort(key=lambda t: t.similarity, reverse=True)
    out = list(floor)
    running = floor_tokens
    for t in leftover:
        cost = estimate_tokens(t.content)
        if running + cost > max_input_tokens:
            break
        out.append(t)
        running += cost
    out.sort(key=lambda t: t.similarity, reverse=True)
    return out


def usage_state_path_for(*, primary_vault_index_dir: Path) -> Path:
    """Return the canonical ``llm_usage.json`` path under the primary vault."""
    return primary_vault_index_dir / "llm_usage.json"


def is_today_iso(date_iso: str) -> bool:
    """Return True iff ``date_iso`` matches today's UTC date."""
    return date_iso == datetime.now(UTC).date().isoformat()


def parse_date_iso(date_iso: str) -> date:
    """Wrapper for tests that need to inspect a stored row's date."""
    return date.fromisoformat(date_iso)
