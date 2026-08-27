"""Configuration loading for engram.

Five-layer precedence (lowest to highest):

1. Defaults baked into the Pydantic model classes.
2. Per-user config: ``~/.config/engram/config.yaml``.
3. Per-vault config: ``<vault>/engram.config.yaml``.
4. Environment variables: ``ENGRAM_*``.
5. CLI flags: explicit kwargs to :func:`load_config`.

A two-pass load resolves the vault path from the per-user config + CLI before
loading the per-vault layer (which lives at a path that depends on which vault
the user is targeting).
"""

from __future__ import annotations

from engram.config.loader import (
    ensure_user_config_dir,
    load_config,
    load_devkit_identity,
    resolve_default_user,
)
from engram.config.models import (
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
    UserConfig,
    VaultConfig,
    VaultMount,
)

__all__ = [
    "EffectiveConfig",
    "LLMConfig",
    "SyncConfig",
    "UserConfig",
    "VaultConfig",
    "VaultMount",
    "ensure_user_config_dir",
    "load_config",
    "load_devkit_identity",
    "resolve_default_user",
]
