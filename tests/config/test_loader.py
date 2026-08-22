"""Tests for engram.config.loader - the 5-layer precedence engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from engram.config import loader as loader_module
from engram.config.loader import (
    ensure_user_config_dir,
    load_config,
    load_devkit_identity,
    resolve_default_user,
)
from engram.config.models import UserConfig
from engram.errors import ConfigError


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-root ~/.config to a temp dir so tests don't touch the real home."""
    monkeypatch.setattr(loader_module, "_USER_CONFIG_DIR", tmp_path / ".config" / "engram")
    monkeypatch.setattr(
        loader_module, "_USER_CONFIG_FILE", tmp_path / ".config" / "engram" / "config.yaml"
    )
    monkeypatch.setattr(
        loader_module,
        "_DEVKIT_IDENTITY_PATH",
        tmp_path / ".config" / "devkit" / "identity.json",
    )
    return tmp_path


@pytest.fixture
def sample_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memex"
    vault.mkdir()
    return vault


# === D1: no config files at all ===


def test_no_config_at_all_raises(fake_home: Path) -> None:
    """D1: no per-user config + no --config -> fatal."""
    with pytest.raises(ConfigError, match="no vault configured"):
        load_config()


# === D2/D6: vault path resolution ===


def test_user_config_no_vaults_raises(fake_home: Path) -> None:
    """D2: per-user config exists but has no `vaults:` -> fatal."""
    _write_yaml(loader_module._USER_CONFIG_FILE, "default_user: testuser\n")
    with pytest.raises(ConfigError, match="no vaults configured"):
        load_config()


def test_user_config_vault_path_doesnt_exist_raises(fake_home: Path) -> None:
    """D6: vault path doesn't exist -> fatal."""
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {fake_home / 'nonexistent'}\n"
        "    role: primary\n",
    )
    with pytest.raises(ConfigError, match="vault directory does not exist"):
        load_config()


def test_explicit_config_missing_raises(fake_home: Path, tmp_path: Path) -> None:
    """D7: --config <nonexistent> -> fatal."""
    with pytest.raises(ConfigError, match="--config file does not exist"):
        load_config(explicit_vault_config=tmp_path / "no" / "such.yaml")


def test_user_config_no_primary_vault_raises(fake_home: Path, sample_vault: Path) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: a\n    path: {sample_vault}\n    role: read-only\n",
    )
    with pytest.raises(ConfigError, match="no vault marked role=primary"):
        load_config()


def test_user_config_explicit_vault_name_not_found_raises(
    fake_home: Path, sample_vault: Path
) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: personal\n    path: {sample_vault}\n    role: primary\n",
    )
    with pytest.raises(ConfigError, match="not in the per-user"):
        load_config(vault_name="nonexistent-vault")


# === happy paths ===


def test_load_config_from_user_yaml_only(fake_home: Path, sample_vault: Path) -> None:
    """Per-user YAML alone is enough; vault YAML is optional."""
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    cfg = load_config()
    assert cfg.default_user == "testuser"
    assert cfg.vault_path == sample_vault.resolve()
    assert cfg.vault_name == "default"  # from VaultConfig defaults
    assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"


def test_load_config_with_vault_yaml(fake_home: Path, sample_vault: Path) -> None:
    """Vault YAML overrides defaults."""
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    _write_yaml(
        sample_vault / "engram.config.yaml",
        "vault_name: personal\n"
        "embedding_model: custom/model-v2\n"
        "sync:\n"
        "  auto_push_on_capture: true\n",
    )
    cfg = load_config()
    assert cfg.vault_name == "personal"
    assert cfg.embedding_model == "custom/model-v2"
    assert cfg.sync.auto_push_on_capture is True
    assert cfg.sync.auto_pull_on_startup is True  # default preserved


def test_explicit_vault_config_bypasses_user_config(fake_home: Path, sample_vault: Path) -> None:
    """--config points engram at one vault config directly; per-user vaults: list ignored."""
    vault_config = sample_vault / "engram.config.yaml"
    _write_yaml(
        vault_config,
        "vault_name: standalone\nembedding_model: standalone/model\n",
    )
    cfg = load_config(explicit_vault_config=vault_config)
    assert cfg.vault_name == "standalone"
    assert cfg.embedding_model == "standalone/model"


def test_load_config_explicit_vault_name_resolves(fake_home: Path, tmp_path: Path) -> None:
    vault_a = tmp_path / "vault_a"
    vault_b = tmp_path / "vault_b"
    vault_a.mkdir()
    vault_b.mkdir()
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "vaults:\n"
        "  - name: a\n"
        f"    path: {vault_a}\n"
        "    role: primary\n"
        "  - name: b\n"
        f"    path: {vault_b}\n"
        "    role: read-only\n",
    )
    cfg = load_config(vault_name="b")
    assert cfg.vault_path == vault_b.resolve()


# === D4: 5-layer precedence ===


def test_env_overrides_yaml(
    fake_home: Path, sample_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "log_level: INFO\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    _write_yaml(
        sample_vault / "engram.config.yaml",
        "embedding_model: yaml/model\n",
    )
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ENGRAM_EMBEDDING_MODEL", "env/model")
    cfg = load_config()
    assert cfg.log_level == "DEBUG"
    assert cfg.embedding_model == "env/model"


def test_cli_overrides_env_and_yaml(
    fake_home: Path, sample_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: yaml-user\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    _write_yaml(sample_vault / "engram.config.yaml", "embedding_model: yaml/model\n")
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("ENGRAM_EMBEDDING_MODEL", "env/model")

    cfg = load_config(
        cli_overrides={
            "log_level": "WARNING",
            "embedding_model": "cli/model",
            "default_user": "cli-user",
        }
    )
    assert cfg.log_level == "WARNING"
    assert cfg.embedding_model == "cli/model"
    assert cfg.default_user == "cli-user"


def test_defaults_apply_when_no_other_layer_sets(fake_home: Path, sample_vault: Path) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    cfg = load_config()
    assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"
    assert cfg.log_level == "INFO"
    assert cfg.log_format == "text"
    assert cfg.sync.auto_pull_on_startup is True


# === D5: identity.json fallback ===


def test_devkit_identity_provides_default_user(
    fake_home: Path, sample_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENGRAM_DEFAULT_USER", raising=False)
    identity_path = loader_module._DEVKIT_IDENTITY_PATH
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(json.dumps({"github_username": "from-devkit"}))
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: personal\n    path: {sample_vault}\n    role: primary\n",
    )
    cfg = load_config()
    assert cfg.default_user == "from-devkit"


def test_devkit_identity_malformed_falls_back_to_user_env(
    fake_home: Path, sample_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENGRAM_DEFAULT_USER", raising=False)
    monkeypatch.setenv("USER", "from-user-env")
    identity_path = loader_module._DEVKIT_IDENTITY_PATH
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text("{not valid json")
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: personal\n    path: {sample_vault}\n    role: primary\n",
    )
    cfg = load_config()
    assert cfg.default_user == "from-user-env"


def test_devkit_identity_missing_field_falls_back(
    fake_home: Path, sample_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENGRAM_DEFAULT_USER", raising=False)
    monkeypatch.setenv("USER", "system-user")
    identity_path = loader_module._DEVKIT_IDENTITY_PATH
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(json.dumps({"other_field": "x"}))
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        f"vaults:\n  - name: personal\n    path: {sample_vault}\n    role: primary\n",
    )
    cfg = load_config()
    assert cfg.default_user == "system-user"


def test_load_devkit_identity_returns_none_when_absent(fake_home: Path) -> None:
    assert load_devkit_identity() is None


def test_load_devkit_identity_returns_none_when_empty_username(fake_home: Path) -> None:
    identity_path = loader_module._DEVKIT_IDENTITY_PATH
    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(json.dumps({"github_username": ""}))
    assert load_devkit_identity() is None


def test_resolve_default_user_priority_chain(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USER", "system-user")
    del fake_home
    # CLI wins over everything.
    assert (
        resolve_default_user(
            UserConfig(default_user="yaml-user"),
            cli_default_user="cli-user",
            env_default_user="env-user",
        )
        == "cli-user"
    )
    # env wins over yaml/devkit/USER.
    assert (
        resolve_default_user(UserConfig(default_user="yaml-user"), env_default_user="env-user")
        == "env-user"
    )
    # yaml wins over USER when no cli/env.
    assert resolve_default_user(UserConfig(default_user="yaml-user")) == "yaml-user"
    # USER fallback when nothing else.
    assert resolve_default_user(UserConfig()) == "system-user"


# === LLM block parses but is ignored at runtime ===


def test_llm_block_in_user_yaml_parses(fake_home: Path, sample_vault: Path) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "llm:\n"
        "  provider: anthropic\n"
        "  model: claude-sonnet-4-6\n"
        "  api_key_env: ANTHROPIC_API_KEY\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    cfg = load_config()
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-sonnet-4-6"
    assert cfg.llm.api_key_env == "ANTHROPIC_API_KEY"


def test_vault_llm_overrides_user_llm(fake_home: Path, sample_vault: Path) -> None:
    _write_yaml(
        loader_module._USER_CONFIG_FILE,
        "default_user: testuser\n"
        "llm:\n"
        "  provider: anthropic\n"
        "vaults:\n"
        "  - name: personal\n"
        f"    path: {sample_vault}\n"
        "    role: primary\n",
    )
    _write_yaml(
        sample_vault / "engram.config.yaml",
        "llm:\n  provider: ollama\n  base_url: http://localhost:11434/v1\n",
    )
    cfg = load_config()
    assert cfg.llm.provider == "ollama"
    assert cfg.llm.base_url == "http://localhost:11434/v1"


# === ensure_user_config_dir mode 0700 ===


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_ensure_user_config_dir_sets_0700(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "engram-config"
    monkeypatch.setattr(loader_module, "_USER_CONFIG_DIR", target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Pre-create with loose mode to verify chmod is applied.
    target.mkdir(mode=0o755)
    ensure_user_config_dir()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_ensure_user_config_dir_creates_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "subdir" / "engram-config"
    monkeypatch.setattr(loader_module, "_USER_CONFIG_DIR", target)
    assert not target.exists()
    ensure_user_config_dir()
    assert target.is_dir()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o700


# === malformed YAML ===


def test_malformed_user_yaml_raises(fake_home: Path) -> None:
    _write_yaml(loader_module._USER_CONFIG_FILE, ":\n  invalid: : : yaml")
    with pytest.raises(ConfigError):
        load_config()


def test_yaml_top_level_must_be_mapping(fake_home: Path) -> None:
    _write_yaml(loader_module._USER_CONFIG_FILE, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="must be a YAML mapping"):
        load_config()
