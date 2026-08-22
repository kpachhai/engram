"""Multi-vault doctor extension tests."""

from __future__ import annotations

from pathlib import Path

from engram.config.models import LLMConfig, UserConfig, VaultMount
from engram.diagnostics.check_codes import (
    AGGREGATOR_MODE,
    EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
    FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
    LLM_DAILY_COST_CAP_APPROACHED,
    LLM_PROVIDER_REACHABLE,
    MULTIPLE_PRIMARY_VAULTS,
    READ_ONLY_VAULT_DECLARES_LLM,
    USER_CONFIG_VAULT_NAME_MISMATCH,
    VAULT_PATH_COLLISION,
)
from engram.diagnostics.doctor import CheckStatus, DoctorReport
from engram.diagnostics.phase3_checks import (
    check_aggregator_mode,
    check_embedding_model_mismatch_across_vaults,
    check_friend_vault_block_thought_present,
    check_llm_daily_cost_cap_approached,
    check_llm_provider_reachable,
    check_multiple_primary_vaults,
    check_read_only_vault_declares_llm,
    check_user_config_vault_name_mismatch,
    check_vault_path_collision,
    run_llm_checks,
    run_phase3_checks,
)
from engram.llm.budget import LLMBudget
from engram.llm.providers import MockProvider
from engram.multivault.registry import VaultRegistry
from engram.storage.facade import VaultStorage
from engram.storage.sqlite import set_setting


def _vault_storage(tmp_path: Path, name: str, *, model: str = "m") -> VaultStorage:
    thoughts_dir = tmp_path / name / "thoughts"
    indexes_dir = tmp_path / name / ".indexes"
    thoughts_dir.mkdir(parents=True, exist_ok=True)
    indexes_dir.mkdir(parents=True, exist_ok=True)
    storage = VaultStorage(
        thoughts_dir=thoughts_dir,
        index_db_path=indexes_dir / "engram.db",
        embedding_dim=16,
        embedding_model_name=model,
        vault_name=name,
    )
    set_setting(storage.conn, "embedding_model_name", model)
    set_setting(storage.conn, "embedding_dim", "16")
    return storage


def _find_check(report: DoctorReport, code: str) -> str:
    """Return the status of the row for ``code``; raises if missing."""
    for c in report.checks:
        if c.name == code:
            return c.status.value
    msg = f"check {code!r} missing from report"
    raise KeyError(msg)


# === multiple_primary_vaults ===


def test_multiple_primary_pass_when_one_or_zero(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    user_config = UserConfig(vaults=[VaultMount(name="primary", path=a, role="primary")])
    report = DoctorReport()
    check_multiple_primary_vaults(report, user_config)
    assert _find_check(report, MULTIPLE_PRIMARY_VAULTS) == CheckStatus.OK.value


# Multiple primaries are now refused at the UserConfig level (Layer A
# validator). The doctor check still exists as defense-in-depth in case
# the validator changes; it's exercised via the Layer A validator instead.


# === user_config_vault_name_mismatch ===


def test_vault_name_mismatch_ok_when_names_match(tmp_path: Path) -> None:
    vault_path = tmp_path / "my-vault"
    vault_path.mkdir()
    (vault_path / "engram.config.yaml").write_text("vault_name: my-vault\n")
    user_config = UserConfig(vaults=[VaultMount(name="my-vault", path=vault_path, role="primary")])
    report = DoctorReport()
    check_user_config_vault_name_mismatch(report, user_config)
    assert _find_check(report, USER_CONFIG_VAULT_NAME_MISMATCH) == CheckStatus.OK.value


def test_vault_name_mismatch_warn_when_names_differ(tmp_path: Path) -> None:
    vault_path = tmp_path / "my-vault"
    vault_path.mkdir()
    (vault_path / "engram.config.yaml").write_text("vault_name: my-vault\n")
    user_config = UserConfig(
        vaults=[VaultMount(name="wrong-name", path=vault_path, role="primary")]
    )
    report = DoctorReport()
    check_user_config_vault_name_mismatch(report, user_config)
    result = _find_check(report, USER_CONFIG_VAULT_NAME_MISMATCH)
    assert result == CheckStatus.WARN.value


def test_vault_name_mismatch_ok_when_no_vault_config(tmp_path: Path) -> None:
    vault_path = tmp_path / "my-vault"
    vault_path.mkdir()
    # No engram.config.yaml present - check skips this vault
    user_config = UserConfig(vaults=[VaultMount(name="any-name", path=vault_path, role="primary")])
    report = DoctorReport()
    check_user_config_vault_name_mismatch(report, user_config)
    assert _find_check(report, USER_CONFIG_VAULT_NAME_MISMATCH) == CheckStatus.OK.value


def test_vault_name_mismatch_ok_when_vault_path_missing(tmp_path: Path) -> None:
    vault_path = tmp_path / "nonexistent"
    user_config = UserConfig(vaults=[VaultMount(name="any-name", path=vault_path, role="primary")])
    report = DoctorReport()
    check_user_config_vault_name_mismatch(report, user_config)
    assert _find_check(report, USER_CONFIG_VAULT_NAME_MISMATCH) == CheckStatus.OK.value


# === vault_path_collision ===


def test_vault_path_collision_clean(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    report = DoctorReport()
    check_vault_path_collision(report, registry)
    assert _find_check(report, VAULT_PATH_COLLISION) == CheckStatus.OK.value


# === embedding model mismatch ===


def test_embedding_compat_pass(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    report = DoctorReport()
    check_embedding_model_mismatch_across_vaults(report, registry)
    assert _find_check(report, EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS) == CheckStatus.OK.value


def test_embedding_compat_fail(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary", model="m1")
    alice = _vault_storage(tmp_path, "alice", model="m2")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    report = DoctorReport()
    check_embedding_model_mismatch_across_vaults(report, registry)
    assert _find_check(report, EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS) == CheckStatus.FAIL.value


# === aggregator_mode ===


def test_aggregator_mode_attach_under_threshold(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    report = DoctorReport()
    check_aggregator_mode(report, registry)
    assert _find_check(report, AGGREGATOR_MODE) == CheckStatus.OK.value
    msg = next(c.message for c in report.checks if c.name == AGGREGATOR_MODE)
    assert "ATTACH" in msg


def test_aggregator_mode_sequential_at_force(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    report = DoctorReport()
    check_aggregator_mode(report, registry, force_sequential=True)
    msg = next(c.message for c in report.checks if c.name == AGGREGATOR_MODE)
    assert "SEQUENTIAL" in msg


# === llm_provider_reachable ===


def test_llm_provider_reachable_ok() -> None:
    report = DoctorReport()
    check_llm_provider_reachable(report, MockProvider())
    assert _find_check(report, LLM_PROVIDER_REACHABLE) == CheckStatus.OK.value


def test_llm_provider_reachable_no_provider() -> None:
    report = DoctorReport()
    check_llm_provider_reachable(report, None)
    assert _find_check(report, LLM_PROVIDER_REACHABLE) == CheckStatus.OK.value


def test_llm_provider_reachable_configured_but_unresolved_warns() -> None:
    """An LLM in the config with no provider built was never measured."""
    report = DoctorReport()
    check_llm_provider_reachable(
        report,
        None,
        configured=True,
        unmeasured_reason="LLMProviderError: base_url not trusted",
    )
    assert _find_check(report, LLM_PROVIDER_REACHABLE) == CheckStatus.WARN.value


# === llm_daily_cost_cap_approached ===


def test_cap_approached_warns_at_eighty_percent(tmp_path: Path) -> None:
    budget = LLMBudget(state_path=tmp_path / "u.json", daily_cost_cap_usd=10.0)
    budget.record_usage(cost_usd=8.5)
    report = DoctorReport()
    check_llm_daily_cost_cap_approached(report, budget=budget, cap=10.0)
    assert _find_check(report, LLM_DAILY_COST_CAP_APPROACHED) == CheckStatus.WARN.value


def test_cap_approached_ok_under_threshold(tmp_path: Path) -> None:
    budget = LLMBudget(state_path=tmp_path / "u.json", daily_cost_cap_usd=10.0)
    budget.record_usage(cost_usd=2.0)
    report = DoctorReport()
    check_llm_daily_cost_cap_approached(report, budget=budget, cap=10.0)
    assert _find_check(report, LLM_DAILY_COST_CAP_APPROACHED) == CheckStatus.OK.value


def test_cap_configured_without_budget_warns() -> None:
    """A configured cap and no tracker means no measurement, not headroom."""
    report = DoctorReport()
    check_llm_daily_cost_cap_approached(report, budget=None, cap=5.0)
    assert _find_check(report, LLM_DAILY_COST_CAP_APPROACHED) == CheckStatus.WARN.value


def test_cap_disabled_is_ok() -> None:
    report = DoctorReport()
    check_llm_daily_cost_cap_approached(report, budget=None, cap=0.0)
    assert _find_check(report, LLM_DAILY_COST_CAP_APPROACHED) == CheckStatus.OK.value


def test_run_llm_checks_emits_both_rows(tmp_path: Path) -> None:
    budget = LLMBudget(state_path=tmp_path / "u.json", daily_cost_cap_usd=10.0)
    report = DoctorReport()
    run_llm_checks(
        report,
        provider=MockProvider(),
        budget=budget,
        daily_cost_cap_usd=10.0,
        configured=True,
    )
    codes = {c.name for c in report.checks}
    assert {LLM_PROVIDER_REACHABLE, LLM_DAILY_COST_CAP_APPROACHED}.issubset(codes)


# === read_only_vault_declares_llm ===


def test_read_only_vault_declares_llm_warns(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    user_config = UserConfig(
        vaults=[
            VaultMount(name="primary", path=a, role="primary"),
            VaultMount(name="alice", path=b, role="read-only"),
        ]
    )
    per_vault_llm: dict[str, LLMConfig | None] = {"alice": LLMConfig(provider="anthropic")}
    report = DoctorReport()
    check_read_only_vault_declares_llm(report, user_config=user_config, per_vault_llm=per_vault_llm)
    assert _find_check(report, READ_ONLY_VAULT_DECLARES_LLM) == CheckStatus.WARN.value


def test_read_only_vault_no_llm_block_ok(tmp_path: Path) -> None:
    a = tmp_path / "a"
    a.mkdir()
    user_config = UserConfig(vaults=[VaultMount(name="primary", path=a, role="primary")])
    report = DoctorReport()
    check_read_only_vault_declares_llm(report, user_config=user_config, per_vault_llm={})
    assert _find_check(report, READ_ONLY_VAULT_DECLARES_LLM) == CheckStatus.OK.value


# === friend_vault_block_thought_present ===


def test_friend_vault_block_clean(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    registry.mount(name="alice", storage=alice, role="read-only")
    report = DoctorReport()
    check_friend_vault_block_thought_present(report, registry)
    assert _find_check(report, FRIEND_VAULT_BLOCK_THOUGHT_PRESENT) == CheckStatus.OK.value


def test_friend_vault_block_present_fail(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    alice = _vault_storage(tmp_path, "alice")
    # Manually inject a block thought into alice's SQLite (bypassing the
    # importer's filter) to simulate a malicious or out-of-band write.
    v = [0.0] * 16
    v[0] = 1.0
    alice.capture(
        content="[Decision] block-tagged",
        portability="block",
        embedding=v,
    )
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    # Mount alice as read-only AFTER the capture so the role guard
    # doesn't intercept the test setup write.
    registry.mount(name="alice", storage=alice, role="read-only")
    report = DoctorReport()
    check_friend_vault_block_thought_present(report, registry)
    assert _find_check(report, FRIEND_VAULT_BLOCK_THOUGHT_PRESENT) == CheckStatus.FAIL.value


# === run_phase3_checks orchestration ===


def test_run_phase3_checks_emits_the_cross_vault_rows(tmp_path: Path) -> None:
    primary = _vault_storage(tmp_path, "primary")
    registry = VaultRegistry()
    registry.mount(name="primary", storage=primary, role="primary")
    a = tmp_path / "a_uc"
    a.mkdir()
    user_config = UserConfig(vaults=[VaultMount(name="primary", path=a, role="primary")])
    report = DoctorReport()
    run_phase3_checks(
        report,
        user_config=user_config,
        registry=registry,
    )
    codes = {c.name for c in report.checks}
    expected = {
        MULTIPLE_PRIMARY_VAULTS,
        VAULT_PATH_COLLISION,
        EMBEDDING_MODEL_MISMATCH_ACROSS_VAULTS,
        AGGREGATOR_MODE,
        READ_ONLY_VAULT_DECLARES_LLM,
        FRIEND_VAULT_BLOCK_THOUGHT_PRESENT,
    }
    assert expected.issubset(codes)
    # The LLM rows are not cross-vault properties; run_llm_checks owns them
    # so a single-vault install gets them too.
    assert LLM_PROVIDER_REACHABLE not in codes
    assert LLM_DAILY_COST_CAP_APPROACHED not in codes
