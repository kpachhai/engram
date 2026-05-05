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
    """Per-vault git sync settings.

    Phase 2 introduces the full multi-machine convergence loop. Existing
    Phase-1 fields (``auto_pull_on_startup``, ``auto_commit_on_capture``,
    ``auto_push_on_capture``, ``git_remote``, ``git_branch``,
    ``startup_pull_timeout_seconds``) keep their semantics; the new fields
    below tune the sync coordinator state machine and surface the safety
    knobs documented in ``docs/PHASE_2_PLAN.md`` Layer A.
    """

    model_config = ConfigDict(extra="forbid")

    # Phase 1 fields (kept stable).
    auto_pull_on_startup: bool = True
    auto_commit_on_capture: bool = True
    auto_push_on_capture: bool = False
    git_remote: str = "origin"
    git_branch: str = "main"
    startup_pull_timeout_seconds: float = Field(default=3.0, gt=0.0)

    # Phase 2 additions.
    #: ``primary`` machines push; ``read-only`` machines pull only (R-H3 work-machine guard).
    role: Literal["primary", "read-only"] = "primary"
    #: Master kill-switch; when True the coordinator never enters the run loop.
    disabled: bool = False
    #: Quiet-window before a batched commit fires (R-M1 / edge 13/14).
    debounce_window_seconds: float = Field(default=60.0, ge=1.0)
    #: Hard ceiling on coalesced bursts so continuous activity still flushes.
    max_deferral_seconds: float = Field(default=300.0, ge=10.0)
    #: Push-retry attempts for transient network failures (R-M5/M6 / edge 26/27).
    push_retry_count: int = Field(default=3, ge=0)
    #: Initial exponential-backoff seconds between push retries.
    push_retry_backoff_seconds: float = Field(default=1.0, ge=0.1)
    #: Hard cap per push invocation (edge 41).
    push_timeout_seconds: float = Field(default=60.0, ge=1.0)
    #: Permit fallback to unsigned commits when ``commit.gpgsign=true`` is set
    #: globally but no signing key is reachable (R-H8); off by default.
    allow_unsigned: bool = False
    #: Pass ``--no-verify`` to ``git commit`` so user pre-commit hooks do not
    #: race the coordinator's own queue (edge 53). Default True per Q3.
    use_no_verify: bool = True
    #: Require pulled commits to be GPG-verified against
    #: ``~/.config/engram/trusted-keys.yaml`` (R-H2 hardening). Off by default
    #: per Q2; doctor WARNs when on but the trusted-keys file is missing.
    signed_pull_required: bool = False
    #: Optional regex the resolved ``origin`` URL must match before any push;
    #: defends against cross-vault contamination (R-H3).
    expected_remote_pattern: str | None = None


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
