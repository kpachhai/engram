"""Provider adapter HTTP-shape tests.

These tests use ``httpx.MockTransport`` (built into httpx; no extra
dependency) to record the requests each adapter would make and to
return canned responses. The aim is to verify the wire shaping (URL +
method + payload) and the response parsing, without making any real
network calls.
"""

from __future__ import annotations

import json

import httpx
import pytest

from engram.errors import LLMProviderError
from engram.llm.providers import (
    AnthropicProvider,
    LlamaCppProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
)


@pytest.mark.asyncio
async def test_anthropic_complete_shapes_request_and_parses_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_url: list[str] = []
    captured_headers: list[dict[str, str]] = []
    captured_body: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_url.append(str(request.url))
        captured_headers.append(dict(request.headers))
        captured_body.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "hi from anthropic"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    provider = AnthropicProvider(api_key="test-key", model="claude-3-5-sonnet-latest")
    result = await provider.complete("say hi", max_tokens=64, timeout=5.0)
    assert result.text == "hi from anthropic"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert captured_url[0] == "https://api.anthropic.com/v1/messages"
    assert captured_headers[0]["x-api-key"] == "test-key"
    assert captured_body[0]["model"] == "claude-3-5-sonnet-latest"


@pytest.mark.asyncio
async def test_anthropic_missing_api_key_refuses() -> None:
    provider = AnthropicProvider(api_key=None)
    with pytest.raises(LLMProviderError):
        await provider.complete("hi", max_tokens=10, timeout=1.0)


@pytest.mark.asyncio
async def test_openai_complete_parses_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi from openai"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4},
            },
        )

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    provider = OpenAIProvider(api_key="k")
    result = await provider.complete("hi", max_tokens=10, timeout=1.0)
    assert result.text == "hi from openai"
    assert result.input_tokens == 7
    assert result.output_tokens == 4


@pytest.mark.asyncio
async def test_ollama_5xx_raises_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream busy")

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    provider = OllamaProvider()
    with pytest.raises(LLMProviderError) as exc_info:
        await provider.complete("hi", max_tokens=10, timeout=1.0)
    assert "503" in str(exc_info.value)


@pytest.mark.asyncio
async def test_llama_cpp_health_check_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "boom"
        raise httpx.ConnectError(msg)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    provider = LlamaCppProvider()
    assert (await provider.health_check()) is False


@pytest.mark.asyncio
async def test_openai_compatible_requires_base_url() -> None:
    provider = OpenAICompatibleProvider(base_url="")
    with pytest.raises(LLMProviderError):
        await provider.complete("x", max_tokens=10, timeout=1.0)
