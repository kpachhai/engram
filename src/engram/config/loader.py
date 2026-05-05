"""Five-layer config loader for engram.

The five layers, lowest to highest precedence:

1. Defaults baked into the Pydantic model classes.
2. Per-user config: ``~/.config/engram/config.yaml``.
3. Per-vault config: ``<vault>/engram.config.yaml``.
4. Environment variables: ``ENGRAM_*``.
5. CLI flags / explicit kwargs.

The two-pass design first resolves which vault is being targeted (from the
per-user config + CLI flags) and then loads the per-vault YAML at a path that
depends on that resolution. This breaks the chicken-and-egg between "the
per-vault config is keyed on vault path" and "the vault path is in the
per-user config".

Environment variable mapping (Phase 1 minimal set):

* ``ENGRAM_LOG_LEVEL`` -> ``log_level``
* ``ENGRAM_LOG_FORMAT`` -> ``log_format``
* ``ENGRAM_DEFAULT_USER`` -> ``default_user``
* ``ENGRAM_EMBEDDING_MODEL`` -> ``embedding_model``

Additional env vars can be added in later phases without breaking change;
this loader silently ignores ``ENGRAM_*`` env vars it does not yet recognize.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML

from engram.config.models import (
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
    UserConfig,
    VaultConfig,
    VaultMount,
)
from engram.errors import ConfigError

_USER_CONFIG_DIR = Path.home() / ".config" / "engram"
_USER_CONFIG_FILE = _USER_CONFIG_DIR / "config.yaml"
_DEVKIT_IDENTITY_PATH = Path.home() / ".config" / "devkit" / "identity.json"
_VAULT_CONFIG_FILENAME = "engram.config.yaml"
_USER_CONFIG_DIR_MODE = 0o700


class _EnvOverrides(BaseSettings):
    """Pydantic-settings shim that captures the Phase 1 ``ENGRAM_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="ENGRAM_",
        env_nested_delimiter=None,
        case_sensitive=False,
        extra="ignore",
    )

    log_level: str | None = None
    log_format: str | None = None
    default_user: str | None = None
    embedding_model: str | None = None


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    """Load a YAML file as a plain dict; safe-load equivalent (no Python tags)."""
    yaml = YAML(typ="safe", pure=True)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
    except OSError as exc:
        msg = f"failed to read config file {path}: {exc}"
        raise ConfigError(msg) from exc
    except Exception as exc:
        msg = f"failed to parse YAML at {path}: {exc}"
        raise ConfigError(msg) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        msg = f"config file {path} must be a YAML mapping at the top level"
        raise ConfigError(msg)
    return dict(data)


def load_devkit_identity() -> str | None:
    """Return ``github_username`` from ``~/.config/devkit/identity.json`` if present.

    Returns ``None`` when the file is absent, malformed, or lacks the field.
    Per F5 (soft dependency on devkit) and D5 (malformed file falls through).
    """
    if not _DEVKIT_IDENTITY_PATH.exists():
        return None
    try:
        data = json.loads(_DEVKIT_IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    username = data.get("github_username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


def resolve_default_user(
    user_config: UserConfig | None = None,
    *,
    cli_default_user: str | None = None,
    env_default_user: str | None = None,
) -> str:
    """Resolve the effective ``default_user`` value via the canonical fallback chain.

    Priority: CLI > env > user-config > devkit identity.json > ``$USER`` > ``"engram-user"``.
    """
    if cli_default_user:
        return cli_default_user
    if env_default_user:
        return env_default_user
    if user_config and user_config.default_user:
        return user_config.default_user
    devkit = load_devkit_identity()
    if devkit:
        return devkit
    return os.environ.get("USER") or "engram-user"


def ensure_user_config_dir() -> Path:
    """Create ``~/.config/engram/`` with mode 0700 if missing; return the path.

    Per ``06-SECURITY.md`` Boundary B1, the per-user config directory is mode
    0700 (only the owner can read its contents).
    """
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    current_mode = _USER_CONFIG_DIR.stat().st_mode & 0o777
    if current_mode != _USER_CONFIG_DIR_MODE:
        _USER_CONFIG_DIR.chmod(_USER_CONFIG_DIR_MODE)
    return _USER_CONFIG_DIR


def _load_user_config_if_present(user_config_path: Path | None = None) -> UserConfig | None:
    path = user_config_path or _USER_CONFIG_FILE
    if not path.exists():
        return None
    raw = _load_yaml_dict(path)
    try:
        return UserConfig.model_validate(raw)
    except ValidationError as exc:
        msg = f"per-user config {path} is invalid: {exc}"
        raise ConfigError(msg) from exc


def _select_vault_mount(
    user_config: UserConfig,
    requested_vault_name: str | None,
) -> VaultMount:
    if not user_config.vaults:
        msg = (
            "no vaults configured in per-user config; add a `vaults:` list with at "
            "least one entry, or pass --config <vault-config-path>"
        )
        raise ConfigError(msg)
    if requested_vault_name is not None:
        for mount in user_config.vaults:
            if mount.name == requested_vault_name:
                return mount
        msg = (
            f"requested vault {requested_vault_name!r} is not in the per-user `vaults:` "
            f"list; known: {[m.name for m in user_config.vaults]}"
        )
        raise ConfigError(msg)
    primary = [m for m in user_config.vaults if m.role == "primary"]
    if not primary:
        msg = "no vault marked role=primary in per-user `vaults:` list"
        raise ConfigError(msg)
    return primary[0]


def _deep_merge(*layers: dict[str, Any]) -> dict[str, Any]:
    """Right-most layer wins; nested dicts merge key-by-key, lists are replaced."""
    out: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if key in out and isinstance(out[key], dict) and isinstance(value, dict):
                out[key] = _deep_merge(out[key], value)
            else:
                out[key] = value
    return out


def load_config(
    *,
    explicit_vault_config: Path | None = None,
    vault_name: str | None = None,
    cli_overrides: dict[str, Any] | None = None,
    user_config_path: Path | None = None,
) -> EffectiveConfig:
    """Load and merge the five-layer config.

    Args:
        explicit_vault_config: Path to a vault's ``engram.config.yaml`` file
            (the ``--config`` CLI flag). When supplied, the per-user config's
            ``vaults:`` list is bypassed entirely; only the per-user
            ``default_user`` and ``llm`` settings still apply.
        vault_name: Which vault from the per-user ``vaults:`` list to target
            (the ``--vault`` CLI flag). Ignored when ``explicit_vault_config``
            is supplied.
        cli_overrides: Flat dict of CLI flag overrides (e.g.,
            ``{"log_level": "DEBUG", "embedding_model": "..."}``). Applied
            last; wins over all other layers.
        user_config_path: Override the per-user config path (test seam).

    Raises:
        ConfigError: when no vault can be resolved, when the resolved vault
            path does not exist, when ``--config`` points at a non-existent
            file, or when any YAML file fails validation.
    """
    cli_overrides = dict(cli_overrides or {})

    user_config = _load_user_config_if_present(user_config_path)

    if explicit_vault_config is not None:
        explicit_vault_config = Path(explicit_vault_config).expanduser()
        if not explicit_vault_config.exists():
            msg = f"--config file does not exist: {explicit_vault_config}"
            raise ConfigError(msg)
        vault_path = explicit_vault_config.parent.resolve()
        vault_config_path: Path | None = explicit_vault_config
    elif user_config is not None:
        mount = _select_vault_mount(user_config, vault_name)
        vault_path = mount.path.expanduser().resolve()
        vc_candidate = vault_path / _VAULT_CONFIG_FILENAME
        vault_config_path = vc_candidate if vc_candidate.exists() else None
    else:
        msg = (
            "no vault configured: create ~/.config/engram/config.yaml with a "
            "`vaults:` list, or pass --config <vault-config-path>"
        )
        raise ConfigError(msg)

    if not vault_path.exists():
        msg = f"vault directory does not exist: {vault_path}"
        raise ConfigError(msg)

    vault_yaml: dict[str, Any] = {}
    if vault_config_path is not None:
        vault_yaml = _load_yaml_dict(vault_config_path)

    defaults = VaultConfig().model_dump()
    env_overrides = _EnvOverrides().model_dump(exclude_none=True)

    merged = _deep_merge(defaults, vault_yaml)
    if env_overrides.get("embedding_model"):
        merged["embedding_model"] = env_overrides["embedding_model"]
    if "embedding_model" in cli_overrides:
        merged["embedding_model"] = cli_overrides["embedding_model"]

    try:
        vault_config = VaultConfig.model_validate(merged)
    except ValidationError as exc:
        msg = f"merged vault config failed validation: {exc}"
        raise ConfigError(msg) from exc

    user_llm = user_config.llm if user_config is not None else LLMConfig()
    effective_llm = vault_config.llm if vault_config.llm is not None else user_llm

    log_level = (
        cli_overrides.get("log_level")
        or env_overrides.get("log_level")
        or (user_config.log_level if user_config is not None else "INFO")
    )
    log_format = (
        cli_overrides.get("log_format")
        or env_overrides.get("log_format")
        or (user_config.log_format if user_config is not None else "text")
    )

    default_user = resolve_default_user(
        user_config,
        cli_default_user=cli_overrides.get("default_user"),
        env_default_user=env_overrides.get("default_user"),
    )

    thoughts_dir = vault_config.thoughts_dir
    if not thoughts_dir.is_absolute():
        thoughts_dir = (vault_path / thoughts_dir).resolve()
    index_dir = vault_config.index_dir
    if not index_dir.is_absolute():
        index_dir = (vault_path / index_dir).resolve()

    try:
        return EffectiveConfig(
            default_user=default_user,
            vault_path=vault_path,
            thoughts_dir=thoughts_dir,
            index_dir=index_dir,
            embedding_model=vault_config.embedding_model,
            vault_name=vault_config.vault_name,
            sync=vault_config.sync,
            llm=effective_llm,
            log_level=log_level,
            log_format=log_format,
        )
    except ValidationError as exc:
        msg = f"effective config failed validation: {exc}"
        raise ConfigError(msg) from exc


def _coerce_sync_config(_value: Any) -> SyncConfig:
    """Reserved hook for future custom merging of nested sync settings."""
    return SyncConfig.model_validate(_value)


__all__ = [
    "ensure_user_config_dir",
    "load_config",
    "load_devkit_identity",
    "resolve_default_user",
]
