"""Pydantic models for engram configuration.

These models describe the YAML config files (``~/.config/engram/config.yaml``
and ``<vault>/engram.config.yaml``) and the merged runtime view returned by
:func:`engram.config.loader.load_config`.

The optional ``llm:`` block is reserved for Phase 3+ LLM-mediated features
per ``02-TECHNICAL_DESIGN.md``. Phase 1 parses it tolerantly and ignores it
at runtime; the architectural reservation just keeps the config schema
forward-compatible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Default embedding model name pinned for Phase 1.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


class SyncConfig(BaseModel):
    """Per-vault git sync settings (Phase 2+; Phase 1 ignores)."""

    model_config = ConfigDict(extra="forbid")

    auto_pull_on_startup: bool = True
    auto_commit_on_capture: bool = True
    auto_push_on_capture: bool = False
    git_remote: str = "origin"
    git_branch: str = "main"
    startup_pull_timeout_seconds: float = Field(default=3.0, gt=0.0)


class LLMConfig(BaseModel):
    """Optional LLM provider config; Phase 1 parses but ignores at runtime.

    Reserved structure per ``02-TECHNICAL_DESIGN.md`` Optional LLM-Mediated
    Features. ``api_key_env`` names an environment variable holding the key;
    engram never stores keys on disk.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic", "openai", "ollama", "llama_cpp", "openai_compatible"] | None = (
        None
    )
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)


class VaultMount(BaseModel):
    """One entry in the per-user ``vaults:`` list."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    path: Path
    role: Literal["primary", "read-only"] = "primary"


class UserConfig(BaseModel):
    """Per-user configuration: ``~/.config/engram/config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    default_user: str | None = None
    vaults: list[VaultMount] = Field(default_factory=list)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"


class VaultConfig(BaseModel):
    """Per-vault configuration: ``<vault>/engram.config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    vault_name: str = "default"
    thoughts_dir: Path = Path("thoughts")
    index_dir: Path = Path(".indexes")
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    sync: SyncConfig = Field(default_factory=SyncConfig)
    # Per-vault LLM override; if None, falls through to UserConfig.llm.
    llm: LLMConfig | None = None


class EffectiveConfig(BaseModel):
    """Final merged config used by the running engram process.

    Built by :func:`engram.config.loader.load_config` after applying all five
    precedence layers. Paths are absolute and resolved.
    """

    model_config = ConfigDict(extra="forbid")

    default_user: str = Field(min_length=1)
    vault_path: Path
    thoughts_dir: Path
    index_dir: Path
    embedding_model: str
    vault_name: str
    sync: SyncConfig
    llm: LLMConfig
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "EffectiveConfig",
    "LLMConfig",
    "SyncConfig",
    "UserConfig",
    "VaultConfig",
    "VaultMount",
]
