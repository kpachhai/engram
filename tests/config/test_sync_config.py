"""SyncConfig field tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from engram.config.models import SyncConfig


def test_phase_2_defaults():
    sc = SyncConfig()
    assert sc.role == "primary"
    assert sc.disabled is False
    assert sc.debounce_window_seconds == pytest.approx(60.0)
    assert sc.max_deferral_seconds == pytest.approx(300.0)
    assert sc.push_retry_count == 3
    assert sc.push_retry_backoff_seconds == pytest.approx(1.0)
    assert sc.push_timeout_seconds == pytest.approx(60.0)
    assert sc.allow_unsigned is False
    assert sc.use_no_verify is True
    assert sc.signed_pull_required is False
    assert sc.expected_remote_pattern is None


def test_role_invalid_value_rejected():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"role": "primary-or-readonly"})


def test_role_read_only_accepted():
    sc = SyncConfig.model_validate({"role": "read-only"})
    assert sc.role == "read-only"


def test_debounce_window_floor_enforced():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"debounce_window_seconds": 0.5})


def test_max_deferral_floor_enforced():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"max_deferral_seconds": 5.0})


def test_push_retry_count_negative_rejected():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"push_retry_count": -1})


def test_push_retry_count_zero_accepted():
    sc = SyncConfig.model_validate({"push_retry_count": 0})
    assert sc.push_retry_count == 0


def test_push_retry_backoff_floor_enforced():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"push_retry_backoff_seconds": 0.0})


def test_push_timeout_floor_enforced():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"push_timeout_seconds": 0.5})


def test_expected_remote_pattern_accepts_regex_string():
    pattern = r"^git@github\.com:owner/.*-personal\.git$"
    sc = SyncConfig.model_validate({"expected_remote_pattern": pattern})
    assert sc.expected_remote_pattern == pattern


def test_full_round_trip_dump_then_validate():
    original = SyncConfig.model_validate(
        {
            "auto_pull_on_startup": False,
            "auto_commit_on_capture": False,
            "auto_push_on_capture": True,
            "git_remote": "personal",
            "git_branch": "trunk",
            "startup_pull_timeout_seconds": 5.0,
            "role": "read-only",
            "disabled": True,
            "debounce_window_seconds": 120.0,
            "max_deferral_seconds": 600.0,
            "push_retry_count": 5,
            "push_retry_backoff_seconds": 2.0,
            "push_timeout_seconds": 90.0,
            "allow_unsigned": True,
            "use_no_verify": False,
            "signed_pull_required": True,
            "expected_remote_pattern": r"^git@example\.com:org/repo\.git$",
        }
    )
    dumped = original.model_dump()
    restored = SyncConfig.model_validate(dumped)
    assert restored == original


def test_unknown_phase_2_field_rejected():
    with pytest.raises(ValidationError):
        SyncConfig.model_validate({"role": "primary", "phase_3_thing": True})
