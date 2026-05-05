"""Provider resolver tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from engram.config.models import (
    AggregatorConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.errors import BlockThoughtLLMDisallowed, LLMProviderError
from engram.llm.resolver import resolve_provider, validate_base_url
from engram.models import ThoughtWithSimilarity

_ProviderName = Literal["anthropic", "openai", "ollama", "llama_cpp", "openai_compatible"]


def _thought(*, portability: str, vault: str = "primary") -> ThoughtWithSimilarity:
    now = datetime.now(UTC)
    return ThoughtWithSimilarity.model_validate(
        {
            "id": uuid4(),
            "schema_version": 1,
            "prefix": "Pattern",
            "portability": portability,
            "source": "test",
            "created_at": now,
            "updated_at": now,
            "fingerprint": "a" * 64,
            "tags": [],
            "vault": vault,
            "legacy_id": None,
            "content": "x",
            "file_path": Path("/tmp/x.md"),
            "similarity": 0.9,
        }
    )


def _effective_config(
    *,
    provider: _ProviderName | None = "ollama",
    base_url: str | None = None,
    api_key_env: str | None = None,
) -> EffectiveConfig:
    return EffectiveConfig(
        default_user="me",
        vault_path=Path("/tmp/vault"),
        thoughts_dir=Path("/tmp/vault/thoughts"),
        index_dir=Path("/tmp/vault/.indexes"),
        embedding_model="m",
        vault_name="primary",
        sync=SyncConfig(),
        llm=LLMConfig(provider=provider, base_url=base_url, api_key_env=api_key_env),
        aggregator=AggregatorConfig(),
    )


# --- block thought always refuses --------------------------------------


def test_block_always_refuses_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _effective_config(provider="ollama")
    thoughts = [_thought(portability="block")]
    with pytest.raises(BlockThoughtLLMDisallowed) as exc_info:
        resolve_provider(thoughts, cfg)
    assert exc_info.value.error_code == "block_thought_llm_disallowed"


# --- sensitive needs local provider ------------------------------------


def test_sensitive_needs_local_refuses_remote_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_KEY", "test")
    cfg = _effective_config(provider="anthropic", api_key_env="ANTHROPIC_KEY")
    thoughts = [_thought(portability="sensitive")]
    with pytest.raises(LLMProviderError) as exc_info:
        resolve_provider(thoughts, cfg)
    assert "sensitive_thought_remote_provider_disallowed" in str(exc_info.value)


def test_sensitive_with_local_provider_passes() -> None:
    cfg = _effective_config(provider="ollama")
    thoughts = [_thought(portability="sensitive")]
    provider = resolve_provider(thoughts, cfg)
    assert provider.is_local is True
    assert provider.name == "ollama"


def test_portable_with_remote_provider_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_KEY", "test")
    cfg = _effective_config(provider="openai", api_key_env="OPENAI_KEY")
    thoughts = [_thought(portability="portable")]
    provider = resolve_provider(thoughts, cfg)
    assert provider.is_local is False
    assert provider.name == "openai"


# --- base_url trust gate -----------------------------------------------


def test_base_url_localhost_trusted_default() -> None:
    validate_base_url("http://localhost:11434/v1")  # should not raise


def test_base_url_anthropic_trusted_default() -> None:
    validate_base_url("https://api.anthropic.com/v1/messages")


def test_base_url_unknown_refused() -> None:
    with pytest.raises(LLMProviderError) as exc_info:
        validate_base_url("https://attacker.example.com/v1")
    assert "base_url" in str(exc_info.value).lower()


def test_resolver_refuses_untrusted_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEY", "x")
    cfg = _effective_config(
        provider="openai_compatible",
        base_url="https://attacker.example.com",
        api_key_env="KEY",
    )
    thoughts = [_thought(portability="portable")]
    with pytest.raises(LLMProviderError):
        resolve_provider(thoughts, cfg)


# --- read-only vault LLM config dropped (SF-13) ------------------------


def test_read_only_vault_llm_config_ignored() -> None:
    """Friend's vault (read-only) declaring anthropic does NOT influence resolver.

    primary vault -> ollama (local) -> sensitive thoughts permitted.
    """
    cfg = _effective_config(provider="ollama")
    thoughts = [_thought(portability="sensitive", vault="alice")]
    # alice declares anthropic but is read-only, so resolver drops it.
    per_vault_llm: dict[str, LLMConfig | None] = {
        "alice": LLMConfig(provider="anthropic"),
        "primary": None,
    }
    provider = resolve_provider(
        thoughts,
        cfg,
        read_only_vault_names={"alice"},
        per_vault_llm=per_vault_llm,
    )
    assert provider.is_local is True
    assert provider.name == "ollama"


# --- cross-provider refused -------------------------------------------


def test_cross_provider_refused() -> None:
    """Two vaults with different providers across thought set -> refuse."""
    cfg = _effective_config(provider=None)  # no user-level provider
    thoughts = [
        _thought(portability="portable", vault="primary"),
        _thought(portability="portable", vault="other"),
    ]
    per_vault_llm: dict[str, LLMConfig | None] = {
        "primary": LLMConfig(provider="ollama"),
        "other": LLMConfig(provider="openai"),
    }
    with pytest.raises(LLMProviderError) as exc_info:
        resolve_provider(thoughts, cfg, per_vault_llm=per_vault_llm)
    assert "cross_provider_synthesis_disallowed" in str(exc_info.value)


# --- no provider configured -------------------------------------------


def test_no_provider_configured_refuses() -> None:
    cfg = _effective_config(provider=None)
    thoughts = [_thought(portability="portable")]
    with pytest.raises(LLMProviderError) as exc_info:
        resolve_provider(thoughts, cfg)
    assert "no LLM provider configured" in str(exc_info.value)
