"""Pydantic models for engram configuration.

These models describe the YAML config files (``~/.config/engram/config.yaml``
and ``<vault>/engram.config.yaml``) and the merged runtime view returned by
:func:`engram.config.loader.load_config`.

The optional ``llm:`` block configures LLM-mediated features per
``02-TECHNICAL_DESIGN.md``. When LLM features are not in use, the block
is parsed tolerantly and ignored at runtime.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engram.errors import TeamWriteRequiresRemote

#: Default embedding model name.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"


def derive_vault_id(remote_url: str) -> str:
    """Return the canonical 16-hex vault id for ``remote_url``.

    The canonical vault id is ``sha256(remote_url)[:16]``. The user-facing
    ``name:`` is a per-machine alias; two machines may use different
    aliases for the same team vault, but the registry indexes by id
    internally.
    """
    return hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:16]


class SyncConfig(BaseModel):
    """Per-vault git sync settings.

    Drives the multi-machine convergence loop and the sync coordinator
    state machine.
    """

    model_config = ConfigDict(extra="forbid")

    auto_pull_on_startup: bool = True
    auto_commit_on_capture: bool = True
    auto_push_on_capture: bool = False
    git_remote: str = "origin"
    git_branch: str = "main"
    startup_pull_timeout_seconds: float = Field(default=3.0, gt=0.0)

    #: ``primary`` machines push; ``read-only`` machines pull only.
    role: Literal["primary", "read-only"] = "primary"
    #: Master kill-switch; when True the coordinator never enters the run loop.
    disabled: bool = False
    #: Quiet-window before a batched commit fires.
    debounce_window_seconds: float = Field(default=60.0, ge=1.0)
    #: Hard ceiling on coalesced bursts so continuous activity still flushes.
    max_deferral_seconds: float = Field(default=300.0, ge=10.0)
    #: Push-retry attempts for transient network failures.
    push_retry_count: int = Field(default=3, ge=0)
    #: Initial exponential-backoff seconds between push retries.
    push_retry_backoff_seconds: float = Field(default=1.0, ge=0.1)
    #: Hard cap per push invocation.
    push_timeout_seconds: float = Field(default=60.0, ge=1.0)
    #: Permit fallback to unsigned commits when ``commit.gpgsign=true`` is set
    #: globally but no signing key is reachable; off by default.
    allow_unsigned: bool = False
    #: Pass ``--no-verify`` to ``git commit`` so user pre-commit hooks do not
    #: race the coordinator's own queue. Default True.
    use_no_verify: bool = True
    #: Require pulled commits to be GPG-verified against
    #: ``~/.config/engram/trusted-keys.yaml``. Off by default; doctor WARNs
    #: when on but the trusted-keys file is missing.
    signed_pull_required: bool = False
    #: Optional regex the resolved ``origin`` URL must match before any push;
    #: defends against cross-vault contamination.
    expected_remote_pattern: str | None = None


class LLMConfig(BaseModel):
    """Optional LLM provider config.

    Per ``02-TECHNICAL_DESIGN.md`` Optional LLM-Mediated Features.
    ``api_key_env`` names an environment variable holding the key;
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

    #: Wall-clock budget per LLM call; aborts mid-stream on timeout.
    request_timeout_seconds: float = Field(default=60.0, ge=1.0)
    #: Pre-truncation token budget; refuses retrieval that exceeds it.
    max_input_tokens: int = Field(default=8000, ge=100)
    #: Per-day cost cap tracked in ``<vault>/.indexes/llm_usage.json``.
    daily_cost_cap_usd: float = Field(default=5.0, ge=0.0)


class AggregatorConfig(BaseModel):
    """Cross-vault aggregator tunables.

    Composed into :class:`UserConfig` and surfaced on
    :class:`EffectiveConfig` so each vault's resolved view carries the same
    aggregator settings. Defaults: per-vault floor of 3 thoughts.
    """

    model_config = ConfigDict(extra="forbid")

    #: Minimum thoughts each vault contributes to a cross-vault search,
    #: regardless of similarity (small-vault visibility floor).
    min_per_vault_results: int = Field(default=3, ge=0)
    #: Per-vault timeout for the aggregator subquery; slow vaults are
    #: surfaced as ``degraded_vaults`` rather than blocking the merge.
    aggregate_timeout_seconds: float = Field(default=5.0, gt=0.0)
    #: Force the sequential code path even when ``mounted_vault_count <=
    #: 10``. Used by tests; operators rarely need this.
    force_sequential: bool = False


class RoutingRule(BaseModel):
    """Per-prefix routing rule that auto-routes captures to a target vault.

    When ``auto_route: true`` is set in :class:`UserConfig`, a capture
    whose first prefix matches ``prefix`` is routed to the vault aliased
    by ``target_vault`` (unless explicit ``vault:`` arg or
    ``portability: block`` overrides).
    """

    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(min_length=1)
    target_vault: str = Field(min_length=1)
    priority: int | None = None


class VaultMount(BaseModel):
    """One entry in the per-user ``vaults:`` list.

    ``role`` may be ``primary``, ``read-only``, or ``team-write``;
    ``team-write`` mounts carry ``remote_url`` + derived ``vault_id``
    fields for team-vault routing.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    path: Path
    role: Literal["primary", "read-only", "team-write"] = "primary"
    #: Required for ``role: team-write``. Empty for primary / read-only.
    remote_url: str | None = None
    #: Derived from ``remote_url`` at validation time; the canonical
    #: globally-unique vault id.
    vault_id: str | None = None

    @model_validator(mode="after")
    def _check_team_write_requirements(self) -> VaultMount:
        """Refuse ``team-write`` without ``remote_url`` and derive vault_id."""
        if self.role == "team-write":
            if not self.remote_url:
                msg = (
                    f"vault {self.name!r} declares role='team-write' but no "
                    f"remote_url; team-write vaults require a remote where "
                    f"the pre-receive hook lives"
                )
                raise TeamWriteRequiresRemote(msg)
            # Derive vault_id from remote_url for stable cross-machine id.
            object.__setattr__(self, "vault_id", derive_vault_id(self.remote_url))
        elif self.remote_url is not None and self.vault_id is None:
            # Non-team-write vaults may still carry remote_url for sync;
            # derive the vault_id for consistency.
            object.__setattr__(self, "vault_id", derive_vault_id(self.remote_url))
        return self


class UserConfig(BaseModel):
    """Per-user configuration: ``~/.config/engram/config.yaml``.

    ``auto_route`` (off by default) and ``routing_rules`` enable per-prefix
    routing into team-write vaults.
    """

    model_config = ConfigDict(extra="forbid")

    default_user: str | None = None
    vaults: list[VaultMount] = Field(default_factory=list)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    aggregator: AggregatorConfig = Field(default_factory=AggregatorConfig)
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    #: Off by default. When True, per-prefix routing rules fire when no
    #: explicit ``vault:`` arg is supplied to ``capture_thought``.
    auto_route: bool = False
    #: Routing rules for ``auto_route``. Empty when ``auto_route`` is off.
    routing_rules: list[RoutingRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_one_primary_vault(self) -> UserConfig:
        """Validation gate over ``vaults``.

        Empty ``vaults`` is permitted (single-vault deployments synthesize a
        vault list at load time from the legacy single-vault fields). When
        ``vaults`` is non-empty, the multi-vault rules apply:

        * At most one entry may have ``role: primary``.
        * Names are unique (case-sensitive; substring/prefix matches are
          fine - the aggregator's vault filter is exact-match-only).
        * No two ``path`` values resolve to the same on-disk directory
          via :func:`os.path.realpath`. The registry-side check
          (:class:`engram.multivault.registry.VaultRegistry.__init__`) is
          the canonical enforcement point because symlinks can change
          between config load and serve startup, but failing fast at the
          config layer surfaces the easy mistakes early.
        """
        if not self.vaults:
            return self
        primary_count = sum(1 for v in self.vaults if v.role == "primary")
        if primary_count > 1:
            msg = (
                f"At most one vault may declare role='primary'; "
                f"found {primary_count} in vaults: "
                f"{[v.name for v in self.vaults if v.role == 'primary']}"
            )
            raise ValueError(msg)
        # N team-write vaults are permitted alongside one primary, but
        # two team-write vaults must not share the same vault_id (would
        # mean two aliases pointing at the same remote).
        team_write_ids = [
            v.vault_id for v in self.vaults if v.role == "team-write" and v.vault_id is not None
        ]
        if len(team_write_ids) != len(set(team_write_ids)):
            seen_ids: set[str] = set()
            dupes = sorted(
                {vid for vid in team_write_ids if vid in seen_ids or seen_ids.add(vid)}  # type: ignore[func-returns-value]
            )
            msg = f"Two team-write vaults share the same vault_id (same remote_url): {dupes}"
            raise ValueError(msg)
        names = [v.name for v in self.vaults]
        if len(names) != len(set(names)):
            seen: set[str] = set()
            dupes = sorted({n for n in names if (n in seen) or seen.add(n)})  # type: ignore[func-returns-value]
            msg = f"Duplicate vault names in vaults list: {dupes}"
            raise ValueError(msg)
        # Realpath collision pre-check (advisory; registry re-asserts).
        seen_paths: dict[str, str] = {}
        for mount in self.vaults:
            try:
                resolved = os.path.realpath(mount.path)
            except OSError:  # pragma: no cover - filesystem availability
                continue
            if resolved in seen_paths:
                msg = (
                    f"Vault path collision (after realpath): "
                    f"{mount.name!r} and {seen_paths[resolved]!r} "
                    f"both resolve to {resolved}"
                )
                raise ValueError(msg)
            seen_paths[resolved] = mount.name
        return self


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
    #: Cross-vault aggregator tunables; copied from
    #: :class:`UserConfig.aggregator` so each per-vault effective config
    #: carries the same values.
    aggregator: AggregatorConfig = Field(default_factory=AggregatorConfig)
    #: Vaults discovered on this machine. Multi-vault deployments
    #: populate this from :attr:`UserConfig.vaults` so downstream code
    #: can iterate without re-parsing the user config.
    vaults: list[VaultMount] = Field(default_factory=list)
    #: Opt-in per-prefix routing flag (mirrors :attr:`UserConfig.auto_route`).
    auto_route: bool = False
    #: Per-prefix routing rules (mirrors :attr:`UserConfig.routing_rules`).
    routing_rules: list[RoutingRule] = Field(default_factory=list)
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "AggregatorConfig",
    "EffectiveConfig",
    "LLMConfig",
    "RoutingRule",
    "SyncConfig",
    "UserConfig",
    "VaultConfig",
    "VaultMount",
    "derive_vault_id",
]
