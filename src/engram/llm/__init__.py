"""Optional LLM-mediated features.

Surface area:

* :class:`engram.llm.protocol.LLMProvider` - the Protocol every adapter
  implements (async ``complete`` + sync ``health_check`` + ``is_local``
  + ``name``).
* :mod:`engram.llm.providers` - 5 concrete adapters: Anthropic, OpenAI,
  Ollama, llama.cpp, OpenAI-compatible (custom ``base_url``).
* :func:`engram.llm.resolver.resolve_provider` - per-thought portability
  gate + cross-provider refusal + read-only-vault-LLM-config-drop +
  trust-file ``base_url`` validation.
* :class:`engram.llm.budget.LLMBudget` - per-day cost tracking
  persisted to ``<primary>/.indexes/llm_usage.json`` + token-budget
  pre-truncation respecting the per-vault floor.
* :func:`engram.llm.citations.validate_citations` - strips hallucinated
  thought-id citations from LLM responses.

The whole package degrades gracefully when no LLM is configured: the
resolver returns ``None`` and the LLM tools refuse with a clear message.
"""

from __future__ import annotations

from engram.llm.budget import LLMBudget, LLMUsageRecord
from engram.llm.citations import validate_citations
from engram.llm.protocol import CompletionResult, LLMProvider, ThoughtIdLike
from engram.llm.providers import (
    AnthropicProvider,
    LlamaCppProvider,
    MockProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    build_provider,
)
from engram.llm.resolver import resolve_provider, validate_base_url

__all__ = [
    "AnthropicProvider",
    "CompletionResult",
    "LLMBudget",
    "LLMProvider",
    "LLMUsageRecord",
    "LlamaCppProvider",
    "MockProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ThoughtIdLike",
    "build_provider",
    "resolve_provider",
    "validate_base_url",
    "validate_citations",
]
