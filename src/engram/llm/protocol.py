"""LLMProvider Protocol + lightweight result types (Phase 3 Step 12).

Adapters in :mod:`engram.llm.providers` implement this Protocol; tests
substitute :class:`engram.llm.providers.MockProvider`. The protocol is
intentionally narrow:

* ``name``: short string identifier (``"anthropic"``, ``"openai"``,
  ``"ollama"``, ``"llama_cpp"``, ``"openai_compatible"``,
  ``"mock"``).
* ``is_local``: distinguishes local (Ollama / llama.cpp) from remote
  providers. The resolver uses this to enforce the
  sensitive-thought-needs-local-provider rule (R-H9).
* ``complete``: async; takes a fully-assembled prompt + token budget +
  timeout and returns a :class:`CompletionResult`. Raising
  :class:`engram.errors.LLMProviderError` is the expected failure
  signal; callers do not catch generic exceptions.
* ``health_check``: lazy probe; returns False rather than raising on
  unreachable provider so doctor's row stays informative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from engram.models import ThoughtWithSimilarity

#: A "thought id" can be a raw UUID string or a Thought-shaped object;
#: helpers normalize via ``str(...)``.
ThoughtIdLike = str


@dataclass(slots=True)
class CompletionResult:
    """Output of :meth:`LLMProvider.complete`."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    citations: list[str] = field(default_factory=list)
    raw: object | None = None

    @property
    def total_tokens(self) -> int:
        """Sum of input + output tokens."""
        return self.input_tokens + self.output_tokens


@runtime_checkable
class LLMProvider(Protocol):
    """Async interface every Phase 3 LLM adapter must implement."""

    name: str
    is_local: bool

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """Generate a completion for ``prompt`` and return the result.

        ``retrieved_thoughts`` is optional context about which thoughts
        were assembled into the prompt; providers that do per-thought
        attribution (citations) use it. Most adapters ignore it and
        post-validate the response via :func:`engram.llm.citations.validate_citations`.

        Raises:
            engram.errors.LLMProviderError: on transient failures
                (network, 5xx, timeout) AND on the configured-but-not-
                reachable case. Reason carried in the message.
        """
        ...

    async def health_check(self) -> bool:
        """Return True if the provider is reachable and responding.

        Lazy: only called when the operator opts in via doctor or when
        the resolver builds the singleton on first LLM call. Must not
        raise; return False on any error.
        """
        ...
