"""Multi-vault config-model tests.

Covers: new ``LLMConfig`` fields, the ``AggregatorConfig`` model, the
``UserConfig._check_one_primary_vault`` validator, and ``EffectiveConfig``
exposure of the new aggregator + vaults fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engram.config.models import (
    AggregatorConfig,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
    UserConfig,
    VaultConfig,
    VaultMount,
)

# --- LLMConfig new-field tests ------------------------------------------


def test_llm_config_phase_3_defaults() -> None:
    cfg = LLMConfig()
    assert cfg.request_timeout_seconds == 60.0
    assert cfg.max_input_tokens == 8000
    assert cfg.daily_cost_cap_usd == 5.0


def test_llm_config_round_trip_new_fields() -> None:
    cfg = LLMConfig(
        request_timeout_seconds=30.0,
        max_input_tokens=16000,
        daily_cost_cap_usd=12.5,
    )
    dumped = cfg.model_dump()
    rebuilt = LLMConfig.model_validate(dumped)
    assert rebuilt.request_timeout_seconds == 30.0
    assert rebuilt.max_input_tokens == 16000
    assert rebuilt.daily_cost_cap_usd == 12.5


def test_llm_config_request_timeout_floor() -> None:
    with pytest.raises(ValidationError):
        LLMConfig(request_timeout_seconds=0.5)


def test_llm_config_max_input_tokens_floor() -> None:
    with pytest.raises(ValidationError):
        LLMConfig(max_input_tokens=10)


def test_llm_config_daily_cost_cap_non_negative() -> None:
    LLMConfig(daily_cost_cap_usd=0.0)
    with pytest.raises(ValidationError):
        LLMConfig(daily_cost_cap_usd=-0.01)


# --- AggregatorConfig tests ---------------------------------------------


def test_aggregator_config_defaults() -> None:
    cfg = AggregatorConfig()
    assert cfg.min_per_vault_results == 3
    assert cfg.aggregate_timeout_seconds == 5.0
    assert cfg.force_sequential is False


def test_aggregator_config_round_trip() -> None:
    cfg = AggregatorConfig(
        min_per_vault_results=10, aggregate_timeout_seconds=2.0, force_sequential=True
    )
    rebuilt = AggregatorConfig.model_validate(cfg.model_dump())
    assert rebuilt.min_per_vault_results == 10
    assert rebuilt.aggregate_timeout_seconds == 2.0
    assert rebuilt.force_sequential is True


def test_aggregator_config_min_per_vault_non_negative() -> None:
    AggregatorConfig(min_per_vault_results=0)
    with pytest.raises(ValidationError):
        AggregatorConfig(min_per_vault_results=-1)


def test_aggregator_config_timeout_strictly_positive() -> None:
    with pytest.raises(ValidationError):
        AggregatorConfig(aggregate_timeout_seconds=0.0)


def test_aggregator_config_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        AggregatorConfig.model_validate({"min_per_vault_results": 3, "unknown": True})


# --- UserConfig._check_one_primary_vault validator ----------------------


def test_user_config_empty_vaults_allowed() -> None:
    """Empty vaults: still permitted (single-vault parity)."""
    cfg = UserConfig()
    assert cfg.vaults == []


def test_user_config_one_primary_ok(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    cfg = UserConfig(
        vaults=[
            VaultMount(name="primary", path=a, role="primary"),
            VaultMount(name="alice", path=b, role="read-only"),
        ]
    )
    assert len(cfg.vaults) == 2


def test_user_config_zero_primary_allowed_at_config_layer(tmp_path: Path) -> None:
    """Zero primaries permitted at config layer; serve startup checks per-call."""
    a = tmp_path / "a"
    a.mkdir()
    cfg = UserConfig(vaults=[VaultMount(name="alice", path=a, role="read-only")])
    assert len([v for v in cfg.vaults if v.role == "primary"]) == 0


def test_user_config_multiple_primary_refused(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    with pytest.raises(ValidationError) as exc_info:
        UserConfig(
            vaults=[
                VaultMount(name="one", path=a, role="primary"),
                VaultMount(name="two", path=b, role="primary"),
            ]
        )
    assert "primary" in str(exc_info.value)


def test_user_config_duplicate_names_refused(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    with pytest.raises(ValidationError) as exc_info:
        UserConfig(
            vaults=[
                VaultMount(name="dup", path=a, role="primary"),
                VaultMount(name="dup", path=b, role="read-only"),
            ]
        )
    assert "Duplicate vault names" in str(exc_info.value)


def test_user_config_realpath_collision_refused(tmp_path: Path) -> None:
    """Two vault paths that resolve to the same realpath are rejected."""
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValidationError) as exc_info:
        UserConfig(
            vaults=[
                VaultMount(name="real", path=target, role="primary"),
                VaultMount(name="symlink", path=link, role="read-only"),
            ]
        )
    assert "collision" in str(exc_info.value)


# --- EffectiveConfig exposure of new fields -----------------------------


def test_effective_config_has_aggregator_and_vaults_defaults(tmp_path: Path) -> None:
    cfg = EffectiveConfig(
        default_user="me",
        vault_path=tmp_path,
        thoughts_dir=tmp_path / "t",
        index_dir=tmp_path / "i",
        embedding_model="m",
        vault_name="v",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )
    assert cfg.aggregator == AggregatorConfig()
    assert cfg.vaults == []


def test_effective_config_round_trips_aggregator_and_vaults(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    cfg = EffectiveConfig(
        default_user="me",
        vault_path=a,
        thoughts_dir=a / "t",
        index_dir=a / "i",
        embedding_model="m",
        vault_name="v",
        sync=SyncConfig(),
        llm=LLMConfig(),
        aggregator=AggregatorConfig(min_per_vault_results=7),
        vaults=[VaultMount(name="v", path=a, role="primary")],
    )
    assert cfg.aggregator.min_per_vault_results == 7
    assert cfg.vaults[0].name == "v"


# --- VaultConfig.llm extra-fields gate (defense-in-depth) ---------------


def test_vault_config_llm_block_rejects_unknown_field() -> None:
    """Per-vault LLM block is the same Pydantic model; extra=forbid gate holds."""
    with pytest.raises(ValidationError):
        VaultConfig.model_validate(
            {"vault_name": "v", "llm": {"provider": "ollama", "unknown_key": "x"}}
        )
