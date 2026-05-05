"""Five LLM adapters + a MockProvider for tests (Phase 3 Step 12).

Each adapter speaks its native HTTP API via :mod:`httpx`. The adapters
are deliberately thin - they translate one Pydantic-ish request shape
into the provider's wire protocol, parse the response, and surface
errors as :class:`engram.errors.LLMProviderError`. Pricing / token
counts come straight from the provider response when available; for
local providers (Ollama, llama.cpp) cost is always ``0.0`` and token
counts may be approximate.

Tests substitute :class:`MockProvider` which records every prompt + can
be primed with canned completions. ``respx`` (a transitive
test-only dependency) covers the live-HTTP shaping in
``tests/llm/test_providers_mocked.py``.

Per the Phase 3 plan SF-9 fix, ``OpenAICompatibleProvider`` validates
its ``base_url`` against the trust file at
``~/.config/engram/trusted-llm-urls.yaml``; the validation is run by
:func:`engram.llm.resolver.validate_base_url` BEFORE provider
construction so a misconfigured URL never even opens an HTTP client.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx

from engram.config.models import LLMConfig
from engram.errors import LLMProviderError
from engram.llm.protocol import CompletionResult, LLMProvider
from engram.models import ThoughtWithSimilarity

_log = logging.getLogger("engram.llm.providers")


def _read_api_key(api_key_env: str | None) -> str | None:
    """Return the env var's value if set, else ``None``.

    Phase 3 plan: keys NEVER stored on disk. ``api_key_env`` names the
    env var; the operator sets it in their shell. Default ``None`` is
    valid for local providers.
    """
    if not api_key_env:
        return None
    return os.environ.get(api_key_env)


# ----- AnthropicProvider --------------------------------------------------


@dataclass(slots=True)
class AnthropicProvider:
    """Anthropic Messages API adapter (api.anthropic.com)."""

    name: str = "anthropic"
    is_local: bool = False
    model: str = "claude-3-5-sonnet-latest"
    api_key: str | None = None
    base_url: str = "https://api.anthropic.com"

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """POST to /v1/messages and parse the assistant turn.

        Args:
            retrieved_thoughts: Phase 3 optional context (unused by this
                adapter; the post-validator handles citation attribution).
        """
        del retrieved_thoughts
        if not self.api_key:
            msg = "anthropic provider missing api_key (env var unset?)"
            raise LLMProviderError(msg)
        headers = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": self.api_key,
        }
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/messages", json=payload, headers=headers
                )
        except httpx.HTTPError as exc:
            msg = f"anthropic provider transport error: {exc}"
            raise LLMProviderError(msg) from exc
        if resp.status_code >= 400:
            msg = f"anthropic provider status {resp.status_code}: {resp.text[:200]}"
            raise LLMProviderError(msg)
        data = resp.json()
        text_chunks = [
            c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"
        ]
        usage = data.get("usage", {})
        return CompletionResult(
            text="".join(text_chunks),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            raw=data,
        )

    async def health_check(self) -> bool:
        """Return True iff the API key is present and the endpoint is reachable."""
        if not self.api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.head(f"{self.base_url}/v1/messages")
        except httpx.HTTPError:
            return False
        return resp.status_code < 500


# ----- OpenAIProvider -----------------------------------------------------


@dataclass(slots=True)
class OpenAIProvider:
    """OpenAI Chat Completions adapter."""

    name: str = "openai"
    is_local: bool = False
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    base_url: str = "https://api.openai.com"

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """POST to /v1/chat/completions and parse the choice."""
        del retrieved_thoughts
        if not self.api_key:
            msg = "openai provider missing api_key (env var unset?)"
            raise LLMProviderError(msg)
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            msg = f"openai provider transport error: {exc}"
            raise LLMProviderError(msg) from exc
        if resp.status_code >= 400:
            msg = f"openai provider status {resp.status_code}: {resp.text[:200]}"
            raise LLMProviderError(msg)
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return CompletionResult(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            raw=data,
        )

    async def health_check(self) -> bool:
        """Return True iff the API key is present and the endpoint reachable."""
        if not self.api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.head(f"{self.base_url}/v1/models")
        except httpx.HTTPError:
            return False
        return resp.status_code < 500


# ----- OllamaProvider -----------------------------------------------------


@dataclass(slots=True)
class OllamaProvider:
    """Ollama (localhost:11434) adapter; OpenAI-compatible /v1/chat/completions."""

    name: str = "ollama"
    is_local: bool = True
    model: str = "llama3.2"
    base_url: str = "http://localhost:11434/v1"

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """POST to /chat/completions on Ollama's OpenAI-compat endpoint."""
        del retrieved_thoughts
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            msg = (
                f"ollama provider transport error: {exc}. Hint: is the "
                "Ollama daemon running on localhost:11434? Run "
                "`ollama serve` to start it."
            )
            raise LLMProviderError(msg) from exc
        if resp.status_code >= 400:
            msg = f"ollama provider status {resp.status_code}: {resp.text[:200]}"
            raise LLMProviderError(msg)
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return CompletionResult(
            text=text,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cost_usd=0.0,
            raw=data,
        )

    async def health_check(self) -> bool:
        """Return True iff Ollama answers /api/tags within 2s."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                # /api/tags is the canonical Ollama liveness endpoint.
                resp = await client.get(f"{self.base_url.rstrip('/v1')}/api/tags")
        except httpx.HTTPError:
            return False
        return resp.status_code < 500


# ----- LlamaCppProvider ---------------------------------------------------


@dataclass(slots=True)
class LlamaCppProvider:
    """llama.cpp server adapter; OpenAI-compatible interface."""

    name: str = "llama_cpp"
    is_local: bool = True
    model: str = "default"
    base_url: str = "http://localhost:8080/v1"

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """POST to /chat/completions on the llama.cpp OpenAI-compat endpoint."""
        del retrieved_thoughts
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            msg = f"llama_cpp provider transport error: {exc}"
            raise LLMProviderError(msg) from exc
        if resp.status_code >= 400:
            msg = f"llama_cpp provider status {resp.status_code}: {resp.text[:200]}"
            raise LLMProviderError(msg)
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        text = choice.get("message", {}).get("content", "")
        return CompletionResult(text=text, raw=data)

    async def health_check(self) -> bool:
        """Return True iff the llama.cpp server answers within 2s."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/models")
        except httpx.HTTPError:
            return False
        return resp.status_code < 500


# ----- OpenAICompatibleProvider ------------------------------------------


@dataclass(slots=True)
class OpenAICompatibleProvider:
    """Generic OpenAI-compatible adapter for custom ``base_url``s.

    The trust-file gate (R-M5 / SF-9) runs at provider construction in
    :func:`engram.llm.resolver.validate_base_url`; this adapter assumes
    the URL has already been validated.
    """

    name: str = "openai_compatible"
    is_local: bool = False
    model: str = "default"
    api_key: str | None = None
    base_url: str = ""

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """POST to /chat/completions on the configured base_url."""
        del retrieved_thoughts
        if not self.base_url:
            msg = "openai_compatible provider requires base_url"
            raise LLMProviderError(msg)
        headers: dict[str, str] = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            msg = f"openai_compatible provider transport error: {exc}"
            raise LLMProviderError(msg) from exc
        if resp.status_code >= 400:
            msg = f"openai_compatible status {resp.status_code}: {resp.text[:200]}"
            raise LLMProviderError(msg)
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        return CompletionResult(text=choice.get("message", {}).get("content", ""), raw=data)

    async def health_check(self) -> bool:
        """Return True iff the endpoint answers within 2s."""
        if not self.base_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{self.base_url}/models")
        except httpx.HTTPError:
            return False
        return resp.status_code < 500


# ----- MockProvider -------------------------------------------------------


@dataclass(slots=True)
class MockProvider:
    """In-memory provider for tests; records every prompt + returns canned text."""

    name: str = "mock"
    is_local: bool = True
    canned_text: str = "(mock completion)"
    canned_input_tokens: int = 0
    canned_output_tokens: int = 0
    canned_cost_usd: float = 0.0
    fail_with: Exception | None = None
    recorded_prompts: list[str] = field(default_factory=list)
    recorded_retrievals: list[list[ThoughtWithSimilarity]] = field(default_factory=list)

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        timeout: float,
        retrieved_thoughts: list[ThoughtWithSimilarity] | None = None,
    ) -> CompletionResult:
        """Record the call and return the canned result."""
        del max_tokens, timeout
        self.recorded_prompts.append(prompt)
        if retrieved_thoughts is not None:
            self.recorded_retrievals.append(list(retrieved_thoughts))
        if self.fail_with is not None:
            raise self.fail_with
        return CompletionResult(
            text=self.canned_text,
            input_tokens=self.canned_input_tokens,
            output_tokens=self.canned_output_tokens,
            cost_usd=self.canned_cost_usd,
        )

    async def health_check(self) -> bool:
        """Always True for the test seam."""
        return True


# ----- factory ------------------------------------------------------------


def build_provider(config: LLMConfig) -> LLMProvider | None:
    """Construct the right adapter for ``config.provider``; ``None`` when unset.

    The provider singleton is built lazily on first LLM tool call (R-L5)
    so engram serve startup is unaffected when no LLM is configured.
    Trust-file validation runs in :func:`engram.llm.resolver.validate_base_url`
    BEFORE this function for ``openai_compatible``.
    """
    if config.provider is None:
        return None
    api_key = _read_api_key(config.api_key_env)
    if config.provider == "anthropic":
        return AnthropicProvider(
            model=config.model or "claude-3-5-sonnet-latest",
            api_key=api_key,
            base_url=config.base_url or "https://api.anthropic.com",
        )
    if config.provider == "openai":
        return OpenAIProvider(
            model=config.model or "gpt-4o-mini",
            api_key=api_key,
            base_url=config.base_url or "https://api.openai.com",
        )
    if config.provider == "ollama":
        return OllamaProvider(
            model=config.model or "llama3.2",
            base_url=config.base_url or "http://localhost:11434/v1",
        )
    if config.provider == "llama_cpp":
        return LlamaCppProvider(
            model=config.model or "default",
            base_url=config.base_url or "http://localhost:8080/v1",
        )
    # The remaining literal value is "openai_compatible"; the Pydantic
    # Literal exhaustiveness guarantees we reach here. Validate base_url
    # and construct.
    if not config.base_url:
        msg = "openai_compatible provider requires base_url"
        raise LLMProviderError(msg)
    return OpenAICompatibleProvider(
        model=config.model or "default",
        api_key=api_key,
        base_url=config.base_url,
    )
