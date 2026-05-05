"""Phase 3 doctor extensions (Step 18).

These are the eight per-vault / cross-vault checks the Phase 3 plan
adds on top of the Phase 1+2 doctor surface:

* :func:`check_multiple_primary_vaults` - FAIL when ``UserConfig.vaults``
  lists more than one ``role: primary`` entry.
* :func:`check_vault_path_collision` - FAIL when two configured vaults
  resolve to the same realpath.
* :func:`check_embedding_model_mismatch_across_vaults` - FAIL when
  mounted vaults declare different ``embedding_model`` / dim.
* :func:`check_aggregator_mode` - INFO row reporting ATTACH vs
  SEQUENTIAL based on mounted vault count.
* :func:`check_llm_provider_reachable` - WARN if LLM is configured but
  ``provider.health_check()`` returns False.
* :func:`check_llm_daily_cost_cap_approached` - WARN at >= 80% of cap.
* :func:`check_read_only_vault_declares_llm` - WARN when a read-only
  vault's per-vault config declares an ``llm`` block.
* :func:`check_friend_vault_block_thought_present` - FAIL when a
  friend-imported (read-only) vault somehow contains a ``portability=block``
  thought; the importer should prevent this but defense-in-depth.

Each check appends a :class:`engram.diagnostics.doctor.CheckResult` to
the supplied :class:`DoctorReport`. Callers chain these from the
extended ``run_diagnostics`` after the Phase 1+2 sync checks land.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from engram.diagnostics.check_codes import (
    AGGREGATOR_MODE,
    EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
    FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
    LLM_DAILY_COST_CAP_APPROACHED,
    LLM_PROVIDER_REACHABLE,
    MULTIPLE_PRIMARY_VAULTS,
    READ_ONLY_VAULT_DECLARES_LLM,
    VAULT_PATH_COLLISION,
)
from engram.diagnostics.doctor import CheckStatus, DoctorReport
from engram.errors import EmbeddingModelMismatch
from engram.multivault.aggregator import (
    ATTACH_VAULT_COUNT_CEILING,
    AggregatorMode,
    assert_compatible_embeddings,
)

if TYPE_CHECKING:
    from engram.config.models import LLMConfig, UserConfig
    from engram.llm.budget import LLMBudget
    from engram.llm.protocol import LLMProvider
    from engram.multivault.registry import VaultRegistry

_log = logging.getLogger("engram.diagnostics.phase3_checks")


def check_multiple_primary_vaults(
    report: DoctorReport,
    user_config: UserConfig,
) -> None:
    """FAIL when more than one vault declares ``role: primary``."""
    primaries = [v for v in user_config.vaults if v.role == "primary"]
    if len(primaries) > 1:
        report.add(
            MULTIPLE_PRIMARY_VAULTS,
            CheckStatus.FAIL,
            (
                f"{len(primaries)} vaults declare role=primary: "
                f"{[p.name for p in primaries]}; expected exactly one."
            ),
        )
        return
    report.add(
        MULTIPLE_PRIMARY_VAULTS,
        CheckStatus.OK,
        f"exactly {len(primaries)} primary vault(s) configured",
    )


def check_vault_path_collision(
    report: DoctorReport,
    registry: VaultRegistry,
) -> None:
    """FAIL when two mounted vaults resolve to the same realpath.

    The registry already refuses construction in this case so a live
    registry passed here cannot collide. The check exercises the
    invariant for defense-in-depth (a future change might bypass the
    registry's __init__ check).
    """
    seen: dict[str, str] = {}
    for name, storage, _role in registry.iter_storages():
        import os

        try:
            resolved = os.path.realpath(storage.thoughts_dir)
        except OSError as exc:
            report.add(
                VAULT_PATH_COLLISION,
                CheckStatus.WARN,
                f"could not resolve thoughts_dir for {name!r}: {exc}",
            )
            continue
        if resolved in seen:
            report.add(
                VAULT_PATH_COLLISION,
                CheckStatus.FAIL,
                (
                    f"vault {name!r} and {seen[resolved]!r} both resolve to "
                    f"{resolved}; one mount must be removed."
                ),
            )
            return
        seen[resolved] = name
    report.add(
        VAULT_PATH_COLLISION,
        CheckStatus.OK,
        f"all {len(registry)} mounted vaults have unique realpaths",
    )


def check_embedding_model_mismatch_across_vaults(
    report: DoctorReport,
    registry: VaultRegistry,
) -> None:
    """FAIL when mounted vaults disagree on embedding model + dim."""
    try:
        assert_compatible_embeddings(registry)
    except EmbeddingModelMismatch as exc:
        report.add(
            EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
            CheckStatus.FAIL,
            str(exc),
        )
        return
    report.add(
        EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
        CheckStatus.OK,
        f"all {len(registry)} mounted vaults agree on embedding model",
    )


def check_aggregator_mode(
    report: DoctorReport,
    registry: VaultRegistry,
    *,
    force_sequential: bool = False,
) -> None:
    """INFO row showing the active aggregator mode for the current vault count."""
    count = len(registry)
    if force_sequential or count > ATTACH_VAULT_COUNT_CEILING:
        mode = AggregatorMode.SEQUENTIAL
    else:
        mode = AggregatorMode.ATTACH
    report.add(
        AGGREGATOR_MODE,
        CheckStatus.OK,
        f"aggregator mode {mode.value} (mounted={count}; ceiling={ATTACH_VAULT_COUNT_CEILING})",
    )


def check_llm_provider_reachable(
    report: DoctorReport,
    provider: LLMProvider | None,
) -> None:
    """WARN if a provider is configured but ``health_check`` returns False."""
    if provider is None:
        report.add(
            LLM_PROVIDER_REACHABLE,
            CheckStatus.OK,
            "no LLM provider configured (LLM features disabled)",
        )
        return
    try:
        reachable = asyncio.run(provider.health_check())
    except Exception as exc:
        report.add(
            LLM_PROVIDER_REACHABLE,
            CheckStatus.WARN,
            f"provider {provider.name} health_check raised: {exc}",
        )
        return
    if not reachable:
        report.add(
            LLM_PROVIDER_REACHABLE,
            CheckStatus.WARN,
            (
                f"provider {provider.name} did not respond to health_check; "
                "LLM tools may fail until the provider is reachable."
            ),
        )
        return
    report.add(
        LLM_PROVIDER_REACHABLE,
        CheckStatus.OK,
        f"provider {provider.name} responded to health_check",
    )


def check_llm_daily_cost_cap_approached(
    report: DoctorReport,
    *,
    budget: LLMBudget | None,
    cap: float,
) -> None:
    """WARN at >= 80% of the daily cap; INFO under that threshold."""
    if budget is None or cap <= 0:
        report.add(
            LLM_DAILY_COST_CAP_APPROACHED,
            CheckStatus.OK,
            "daily cost cap disabled or budget tracker not configured",
        )
        return
    used = budget.today_cost_usd()
    fraction = used / cap if cap > 0 else 0
    if fraction >= 0.8:
        report.add(
            LLM_DAILY_COST_CAP_APPROACHED,
            CheckStatus.WARN,
            (
                f"today's usage {used:.4f} USD is {fraction * 100:.1f}% "
                f"of the daily cap {cap:.2f} USD."
            ),
        )
        return
    report.add(
        LLM_DAILY_COST_CAP_APPROACHED,
        CheckStatus.OK,
        f"today's usage {used:.4f} USD is {fraction * 100:.1f}% of cap",
    )


def check_read_only_vault_declares_llm(
    report: DoctorReport,
    *,
    user_config: UserConfig,
    per_vault_llm: dict[str, LLMConfig | None],
) -> None:
    """WARN when a read-only vault declares its own LLM block.

    The resolver drops this config (SF-13 / R-M2); the warning makes
    operators aware that the friend's per-vault LLM choice is dead
    code.
    """
    offenders: list[str] = []
    for mount in user_config.vaults:
        if mount.role != "read-only":
            continue
        cfg = per_vault_llm.get(mount.name)
        if cfg is not None and cfg.provider is not None:
            offenders.append(mount.name)
    if offenders:
        report.add(
            READ_ONLY_VAULT_DECLARES_LLM,
            CheckStatus.WARN,
            (
                f"read-only vault(s) {offenders} declare a per-vault "
                "LLM provider; engram drops this config and uses the "
                "primary vault's LLM choice instead. Remove the "
                "per-vault llm block to silence this warning."
            ),
        )
        return
    report.add(
        READ_ONLY_VAULT_DECLARES_LLM,
        CheckStatus.OK,
        "no read-only vault declares an LLM block",
    )


def check_friend_vault_block_thought_present(
    report: DoctorReport,
    registry: VaultRegistry,
) -> None:
    """FAIL when a read-only vault carries a ``portability=block`` thought.

    The bundle importer filters block at import time (Step 10) and the
    aggregator never returns block across vaults, but this check
    re-asserts the invariant against the live SQLite state in case a
    friend-imported vault somehow contains block content (e.g.
    out-of-band manual modification).
    """
    offenders: list[str] = []
    for name, storage, role in registry.iter_storages():
        if role != "read-only":
            continue
        rows = storage.conn.execute(
            "SELECT id FROM thoughts WHERE portability = 'block' LIMIT 1"
        ).fetchall()
        if rows:
            offenders.append(name)
    if offenders:
        report.add(
            FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
            CheckStatus.FAIL,
            (
                f"read-only vault(s) {offenders} contain "
                "portability=block thought(s); refuse to mount such "
                "vaults until the offending thoughts are removed or "
                "re-tagged."
            ),
        )
        return
    report.add(
        FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
        CheckStatus.OK,
        "no read-only vault carries portability=block thoughts",
    )


def run_phase3_checks(
    report: DoctorReport,
    *,
    user_config: UserConfig,
    registry: VaultRegistry,
    provider: LLMProvider | None = None,
    budget: LLMBudget | None = None,
    daily_cost_cap_usd: float = 0.0,
    per_vault_llm: dict[str, LLMConfig | None] | None = None,
    force_sequential: bool = False,
) -> None:
    """Run all eight Phase 3 checks against ``registry``.

    Convenience for the doctor CLI command; tests typically call
    individual check functions to assert specific scenarios.
    """
    check_multiple_primary_vaults(report, user_config)
    check_vault_path_collision(report, registry)
    check_embedding_model_mismatch_across_vaults(report, registry)
    check_aggregator_mode(report, registry, force_sequential=force_sequential)
    check_llm_provider_reachable(report, provider)
    check_llm_daily_cost_cap_approached(report, budget=budget, cap=daily_cost_cap_usd)
    check_read_only_vault_declares_llm(
        report,
        user_config=user_config,
        per_vault_llm=per_vault_llm or {},
    )
    check_friend_vault_block_thought_present(report, registry)


__all__ = [
    "check_aggregator_mode",
    "check_embedding_model_mismatch_across_vaults",
    "check_friend_vault_block_thought_present",
    "check_llm_daily_cost_cap_approached",
    "check_llm_provider_reachable",
    "check_multiple_primary_vaults",
    "check_read_only_vault_declares_llm",
    "check_vault_path_collision",
    "run_phase3_checks",
]
