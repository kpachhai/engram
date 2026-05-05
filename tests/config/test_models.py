"""Tests for engram.config.models."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engram.config.models import (
    DEFAULT_EMBEDDING_MODEL,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
    UserConfig,
    VaultConfig,
    VaultMount,
)


def test_sync_config_defaults():
    sc = SyncConfig()
    assert sc.auto_pull_on_startup is True
    assert sc.auto_commit_on_capture is True
    assert sc.auto_push_on_capture is False
    assert sc.git_remote == "origin"
    assert sc.git_branch == "main"
    assert sc.startup_pull_timeout_seconds == pytest.approx(3.0)


def test_sync_config_unknown_field_rejected():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"auto_pull_on_startup": True, "unknown": True})


def test_sync_config_negative_timeout_rejected():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"startup_pull_timeout_seconds": -1.0})


def test_llm_config_default_is_phase1_safe():
    """Phase 1 ignores LLM at runtime; defaults must construct without remote calls."""
    llm = LLMConfig()
    assert llm.provider is None
    assert llm.model is None
    assert llm.api_key_env is None


@pytest.mark.parametrize(
    "provider", ["anthropic", "openai", "ollama", "llama_cpp", "openai_compatible"]
)
def test_llm_config_each_known_provider_accepted(provider: str):
    llm = LLMConfig.model_validate({"provider": provider})
    assert llm.provider == provider


def test_llm_config_unknown_provider_rejected():
    with pytest.raises(ValidationError):
        LLMConfig.model_validate({"provider": "made_up_provider"})


def test_llm_config_temperature_bounds():
    LLMConfig.model_validate({"temperature": 0.0})
    LLMConfig.model_validate({"temperature": 2.0})
    with pytest.raises(ValidationError):
        LLMConfig.model_validate({"temperature": -0.1})
    with pytest.raises(ValidationError):
        LLMConfig.model_validate({"temperature": 2.1})


def test_vault_mount_role_defaults_to_primary():
    vm = VaultMount.model_validate({"name": "personal", "path": "/tmp/x"})
    assert vm.role == "primary"


def test_vault_mount_role_read_only_accepted():
    vm = VaultMount.model_validate({"name": "alice", "path": "/x", "role": "read-only"})
    assert vm.role == "read-only"


def test_vault_mount_invalid_role_rejected():
    with pytest.raises(ValidationError):
        VaultMount.model_validate({"name": "x", "path": "/x", "role": "admin"})


def test_vault_mount_empty_name_rejected():
    with pytest.raises(ValidationError):
        VaultMount.model_validate({"name": "", "path": "/x"})


def test_user_config_defaults():
    uc = UserConfig()
    assert uc.default_user is None
    assert uc.vaults == []
    assert uc.log_level == "INFO"
    assert uc.log_format == "text"
    assert isinstance(uc.llm, LLMConfig)


def test_user_config_invalid_log_format_rejected():
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"log_format": "xml"})


def test_user_config_unknown_field_rejected():
    with pytest.raises(ValidationError):
        UserConfig.model_validate({"unknown_top_level": 1})


def test_vault_config_defaults():
    vc = VaultConfig()
    assert vc.vault_name == "default"
    assert vc.thoughts_dir == Path("thoughts")
    assert vc.index_dir == Path(".indexes")
    assert vc.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert isinstance(vc.sync, SyncConfig)
    assert vc.llm is None  # falls through to user-config llm


def test_vault_config_unknown_field_rejected():
    with pytest.raises(ValidationError):
        VaultConfig.model_validate({"unknown_field": "x"})


def test_effective_config_construction():
    ec = EffectiveConfig(
        default_user="kpachhai",
        vault_path=Path("/home/k/repos/memex"),
        thoughts_dir=Path("/home/k/repos/memex/thoughts"),
        index_dir=Path("/home/k/repos/memex/.indexes"),
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        vault_name="personal",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )
    assert ec.default_user == "kpachhai"
    assert ec.log_level == "INFO"
    assert ec.log_format == "text"


def test_effective_config_empty_default_user_rejected():
    with pytest.raises(ValidationError):
        EffectiveConfig(
            default_user="",
            vault_path=Path("/x"),
            thoughts_dir=Path("/x/thoughts"),
            index_dir=Path("/x/.indexes"),
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            vault_name="x",
            sync=SyncConfig(),
            llm=LLMConfig(),
        )
