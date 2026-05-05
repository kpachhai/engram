"""Provider resolution + per-thought portability gate (Phase 3 Step 12).

The resolver is the single decision point that answers: "given a list
of thoughts the caller wants to send to an LLM, and given the
per-user / per-vault config, which provider should we use - or do we
refuse?". It encodes the load-bearing security rules:

1. **block thought ALWAYS refuses** (R-H10). No flag, config, or
   provider locality overrides this. Returns
   :class:`engram.errors.BlockThoughtLLMDisallowed`.
2. **sensitive thought requires a local provider** (R-H9). If the
   resolved provider is remote (``is_local=False``), refuse with
   ``sensitive_thought_remote_provider_disallowed``.
3. **Per-vault LLM config from a read-only-role vault is dropped**
   (SF-13 / R-M2). Friend's vault declaring ``provider: anthropic``
   does not influence the importer's runtime choice.
4. **Cross-provider synthesis is refused by default** (Q4). If the
   thoughts span multiple vaults whose per-vault LLM config disagrees
   on provider, refuse with ``cross_provider_synthesis_disallowed``.
   Per-thought lookup uses the registry's role/coordinator/config
   surface.
5. **base_url validation against trust file** (SF-9 / R-M5). The
   trust file at ``~/.config/engram/trusted-llm-urls.yaml`` lists
   regex patterns; only ``base_url``s matching a pattern are
   permitted. Three default patterns are baked in
   (``localhost`` + ``api.anthropic.com`` + ``api.openai.com``).

The resolver returns the singleton :class:`engram.llm.protocol.LLMProvider`
the caller should use; ``None`` when no LLM is configured.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from engram.errors import (
    BlockThoughtLLMDisallowed,
    LLMProviderError,
)
from engram.llm.providers import build_provider

if TYPE_CHECKING:
    from engram.config.models import EffectiveConfig, LLMConfig
    from engram.llm.protocol import LLMProvider
    from engram.models import ThoughtWithSimilarity

_log = logging.getLogger("engram.llm.resolver")

#: Default trust patterns shipped with engram. Operators can add to
#: ``~/.config/engram/trusted-llm-urls.yaml`` after a confirmation step
#: documented in ``docs/LLM_FEATURES.md``.
_DEFAULT_TRUSTED_BASE_URL_PATTERNS: tuple[str, ...] = (
    r"^http://localhost(:\d+)?(/.*)?$",
    r"^https://api\.anthropic\.com(/.*)?$",
    r"^https://api\.openai\.com(/.*)?$",
)

_TRUST_FILE_RELATIVE = Path("trusted-llm-urls.yaml")


def _trust_file_path() -> Path:
    """Return the per-user trust-file path."""
    return Path.home() / ".config" / "engram" / _TRUST_FILE_RELATIVE


def _load_trusted_patterns() -> list[str]:
    r"""Combine baked-in defaults with any user-added patterns.

    The user file is expected to be a YAML list of regex strings, e.g.::

        - "^https://my-internal\\.example\\.com(/.*)?$"
        - "^http://gpu-box\\.lan:8080(/.*)?$"

    Missing file is fine; it just means defaults only.
    """
    patterns = list(_DEFAULT_TRUSTED_BASE_URL_PATTERNS)
    path = _trust_file_path()
    if not path.exists():
        return patterns
    try:
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe", pure=True)
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.load(fh)
    except Exception as exc:
        _log.warning("failed to parse %s: %s; using default trust list only", path, exc)
        return patterns
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and item:
                patterns.append(item)
    return patterns


def validate_base_url(base_url: str | None) -> None:
    """Raise :class:`LLMProviderError` if ``base_url`` matches no trust pattern.

    ``None`` and empty string pass through (the adapter constructor
    handles the missing-base-url case for providers that need one).
    """
    if not base_url:
        return
    for pat in _load_trusted_patterns():
        try:
            if re.match(pat, base_url):
                return
        except re.error:
            _log.warning("ignoring invalid regex in trust file: %r", pat)
            continue
    msg = (
        f"LLM base_url {base_url!r} does not match any trusted pattern. "
        "Add a regex to ~/.config/engram/trusted-llm-urls.yaml after "
        "reviewing the URL (and the destination's privacy posture). "
        "See docs/LLM_FEATURES.md for guidance."
    )
    raise LLMProviderError(msg)


def _per_vault_provider_set(
    thoughts: Iterable[ThoughtWithSimilarity],
    *,
    primary_vault_name: str,
    read_only_vault_names: set[str],
    user_llm: LLMConfig,
    per_vault_llm: dict[str, LLMConfig | None] | None = None,
) -> tuple[str | None, set[str]]:
    """Return ``(provider_name, vaults_seen)``.

    Cross-provider check: if the per-thought provider lookup yields
    more than one provider name across the thought set, the resolver
    refuses (Q4).

    Args:
        thoughts: thoughts in the LLM context window.
        primary_vault_name: which vault holds primary writes (per-vault
            LLM config from the primary is honored).
        read_only_vault_names: per SF-13, the resolver IGNORES per-vault
            LLM config from read-only-role vaults.
        user_llm: per-user fallback LLM config.
        per_vault_llm: lookup table keyed on vault name. ``None`` value
            means "no per-vault override; fall back to user_llm".
    """
    per_vault_llm = per_vault_llm or {}
    providers_seen: set[str] = set()
    vaults_seen: set[str] = set()
    for t in thoughts:
        vaults_seen.add(t.vault)
        if t.vault in read_only_vault_names:
            # SF-13: drop the read-only vault's LLM config; use user-level.
            cfg = user_llm
        else:
            cfg = per_vault_llm.get(t.vault) or user_llm
        if cfg.provider is not None:
            providers_seen.add(cfg.provider)
    # If only the primary's per-vault config has a provider, prefer it.
    if not providers_seen and user_llm.provider is not None:
        providers_seen.add(user_llm.provider)
    if len(providers_seen) > 1:
        msg = (
            "cross_provider_synthesis_disallowed: thoughts span vaults "
            f"with different providers: {sorted(providers_seen)}. "
            "Run synthesize per-vault and combine results manually."
        )
        raise LLMProviderError(msg)
    provider_name = next(iter(providers_seen), None)
    return provider_name, vaults_seen


def resolve_provider(
    thoughts: list[ThoughtWithSimilarity],
    config: EffectiveConfig,
    *,
    read_only_vault_names: set[str] | None = None,
    per_vault_llm: dict[str, LLMConfig | None] | None = None,
) -> LLMProvider:
    """Return the provider to use for ``thoughts``, applying all gates.

    Args:
        thoughts: thoughts that will appear in the LLM prompt context.
        config: per-user effective config (carries primary vault name +
            per-user LLM block).
        read_only_vault_names: vaults whose per-vault LLM config the
            resolver should drop (SF-13).
        per_vault_llm: per-vault LLM overrides keyed by vault name.

    Raises:
        BlockThoughtLLMDisallowed: any thought has ``portability='block'``.
        LLMProviderError: sensitive thought + remote provider; cross-
            provider thoughts; provider unconfigured; base_url not
            trusted.
    """
    # 1. block always refuses, before any provider construction.
    block_ids = [str(t.id) for t in thoughts if t.portability == "block"]
    if block_ids:
        msg = (
            "block_thought_llm_disallowed: thought(s) "
            f"{block_ids} carry portability=block; LLM resolver refuses."
        )
        raise BlockThoughtLLMDisallowed(msg)

    # 2. cross-provider check + per-vault config dropping (SF-13).
    primary_vault_name = config.vault_name
    ro = read_only_vault_names or set()
    provider_name, _vaults_seen = _per_vault_provider_set(
        thoughts,
        primary_vault_name=primary_vault_name,
        read_only_vault_names=ro,
        user_llm=config.llm,
        per_vault_llm=per_vault_llm,
    )
    if provider_name is None:
        msg = "no LLM provider configured; set llm.provider in per-user config"
        raise LLMProviderError(msg)

    # 3. compose the effective LLMConfig: primary vault's LLM if any,
    #    else user_llm; never a read-only vault's. We use the primary
    #    vault entry when available, falling back to user_llm.
    effective_llm = (per_vault_llm or {}).get(primary_vault_name) or config.llm

    # 4. base_url trust gate.
    validate_base_url(effective_llm.base_url)

    # 5. construct provider singleton (lazy, per R-L5).
    provider = build_provider(effective_llm)
    if provider is None:
        msg = "build_provider returned None despite resolved provider name"
        raise LLMProviderError(msg)

    # 6. sensitive-thought-needs-local-provider check.
    sensitive_ids = [str(t.id) for t in thoughts if t.portability == "sensitive"]
    if sensitive_ids and not provider.is_local:
        msg = (
            f"sensitive_thought_remote_provider_disallowed: thought(s) "
            f"{sensitive_ids} have portability=sensitive but the "
            f"resolved provider {provider.name!r} is remote. "
            "Configure a local provider (ollama / llama_cpp) for "
            "sensitive content, or downgrade to portable."
        )
        raise LLMProviderError(msg)

    return provider
