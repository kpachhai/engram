"""Test DaemonConfig Pydantic model with Field bounds + defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from engram.config.loader import load_config
from engram.config.models import DaemonConfig, EffectiveConfig


def test_defaults_match_spec() -> None:
    cfg = DaemonConfig()
    assert cfg.auto_spawn is True
    assert cfg.idle_shutdown_seconds == 3600
    assert cfg.spawn_timeout_seconds == 30
    assert cfg.spawn_lock_timeout_seconds == 10
    assert cfg.wal_recovery_grace_seconds == 60
    assert cfg.shutdown_drain_seconds == 5
    assert cfg.coordinator_flush_seconds == 30
    assert cfg.connection_idle_timeout_seconds == 86400
    assert cfg.max_frame_bytes == 16 * 1024 * 1024
    assert cfg.log_max_size_mb == 100
    assert cfg.log_retention_days == 7
    assert cfg.log_level == "INFO"
    assert cfg.log_redact_thought_content is True


def test_extra_forbid() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DaemonConfig.model_validate({"unknown_field": True})
    assert "Extra inputs are not permitted" in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("idle_shutdown_seconds", -1),
        ("spawn_timeout_seconds", 0),
        ("spawn_lock_timeout_seconds", 0),
        ("wal_recovery_grace_seconds", -1),
        ("shutdown_drain_seconds", 0),
        ("coordinator_flush_seconds", 0),
        ("connection_idle_timeout_seconds", -1),
        ("max_frame_bytes", 32_000),
        ("log_max_size_mb", 0),
        ("log_retention_days", 0),
    ],
)
def test_field_lower_bounds_enforced(field: str, bad_value: int) -> None:
    with pytest.raises(ValidationError) as exc_info:
        DaemonConfig.model_validate({field: bad_value})
    assert field in str(exc_info.value)


def test_idle_shutdown_zero_means_never() -> None:
    cfg = DaemonConfig(idle_shutdown_seconds=0)
    # 0 is allowed; semantically means "never auto-shutdown"
    assert cfg.idle_shutdown_seconds == 0


def test_huge_idle_shutdown_accepted() -> None:
    cfg = DaemonConfig(idle_shutdown_seconds=999_999_999)
    assert cfg.idle_shutdown_seconds == 999_999_999


def test_effective_config_has_daemon_default() -> None:
    """EffectiveConfig should expose a daemon: DaemonConfig with the model defaults."""
    fields = EffectiveConfig.model_fields
    assert "daemon" in fields, "EffectiveConfig is missing the daemon field"


# Loader-level tests (Task A3 — 5-layer precedence) -------------------------


def _write_per_user_with_vault(tmp_path: Path, vault_dir: Path, daemon_block: str) -> Path:
    """Helper: build a per-user config pointing at a vault directory.

    Returns the per-user config path. The vault directory is initialized as a
    primary vault; the per-vault YAML at `<vault>/engram.config.yaml` receives
    the supplied ``daemon_block`` so we exercise the per-vault layer of the
    five-layer precedence chain.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "thoughts").mkdir(exist_ok=True)
    (vault_dir / ".indexes").mkdir(exist_ok=True)

    per_vault = vault_dir / "engram.config.yaml"
    per_vault.write_text(f"vault_name: test\n{daemon_block}")

    user_dir = tmp_path / "user_config"
    user_dir.mkdir(parents=True, exist_ok=True)
    user_config = user_dir / "config.yaml"
    user_config.write_text(
        "default_user: testuser\n"
        "vaults:\n"
        f"  - name: test\n"
        f"    path: {vault_dir}\n"
        f"    role: primary\n",
    )
    return user_config


def test_daemon_config_loaded_from_per_vault_yaml(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    daemon_block = "daemon:\n  idle_shutdown_seconds: 7200\n  log_max_size_mb: 50\n"
    user_config = _write_per_user_with_vault(tmp_path, vault_dir, daemon_block)
    cfg = load_config(user_config_path=user_config, vault_name="test")
    assert cfg.daemon.idle_shutdown_seconds == 7200
    assert cfg.daemon.log_max_size_mb == 50
    # defaults preserved for unspecified fields
    assert cfg.daemon.spawn_timeout_seconds == 30
    assert cfg.daemon.shutdown_drain_seconds == 5


def test_daemon_config_empty_block_uses_defaults(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    user_config = _write_per_user_with_vault(tmp_path, vault_dir, "daemon: {}\n")
    cfg = load_config(user_config_path=user_config, vault_name="test")
    assert cfg.daemon.idle_shutdown_seconds == 3600
    assert cfg.daemon.spawn_timeout_seconds == 30


def test_daemon_config_missing_block_uses_defaults(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    # No daemon: block at all.
    user_config = _write_per_user_with_vault(tmp_path, vault_dir, "")
    cfg = load_config(user_config_path=user_config, vault_name="test")
    assert cfg.daemon.idle_shutdown_seconds == 3600
    assert cfg.daemon.log_max_size_mb == 100
