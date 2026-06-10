"""Tests for engram.consolidate.guards - locking + refusal gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.consolidate.guards import (
    acquire_apply_lock,
    cloud_sync_hint_for,
    ensure_vault_applyable,
)
from engram.errors import (
    ConsolidateError,
    ConsolidateVaultBusy,
    LockError,
    VaultReadOnlyError,
)
from engram.utils.lock import VaultLock


class TestEnsureVaultApplyable:
    def test_primary_local_vault_passes(self, tmp_path: Path):
        ensure_vault_applyable(role="primary", vault_path=tmp_path)

    def test_read_only_refused(self, tmp_path: Path):
        with pytest.raises(VaultReadOnlyError, match="read-only"):
            ensure_vault_applyable(role="read-only", vault_path=tmp_path)

    def test_team_write_refused_with_attribution_reason(self, tmp_path: Path):
        with pytest.raises(ConsolidateError, match="captured_by"):
            ensure_vault_applyable(role="team-write", vault_path=tmp_path)

    def test_cloud_synced_path_refused(self, tmp_path: Path):
        cloudy = tmp_path / "Dropbox" / "vault"
        cloudy.mkdir(parents=True)
        with pytest.raises(ConsolidateError, match="Dropbox"):
            ensure_vault_applyable(role="primary", vault_path=cloudy)


class TestCloudSyncHint:
    def test_detects_icloud_storage_path(self, tmp_path: Path):
        cloudy = tmp_path / "Library" / "CloudStorage" / "vault"
        cloudy.mkdir(parents=True)
        assert cloud_sync_hint_for(cloudy) == "Library/CloudStorage"

    def test_local_path_returns_none(self, tmp_path: Path):
        assert cloud_sync_hint_for(tmp_path) is None


class TestAcquireApplyLock:
    def test_acquires_and_releases(self, tmp_path: Path):
        lock = acquire_apply_lock(tmp_path)
        try:
            assert lock.lock_path.exists()
        finally:
            lock.release()

    def test_busy_when_daemon_style_holder_present(self, tmp_path: Path):
        holder = VaultLock(tmp_path)
        holder.acquire()
        try:
            with pytest.raises(ConsolidateVaultBusy, match="daemon stop"):
                acquire_apply_lock(tmp_path)
        finally:
            holder.release()

    def test_no_force_escape_in_message(self, tmp_path: Path):
        holder = VaultLock(tmp_path)
        holder.acquire()
        try:
            with pytest.raises(ConsolidateVaultBusy) as exc_info:
                acquire_apply_lock(tmp_path)
            assert "--force" not in str(exc_info.value)
        finally:
            holder.release()

    def test_daemon_spawn_shape_fails_cleanly_while_apply_holds(self, tmp_path: Path):
        """While apply holds the lock, a daemon-startup-style acquisition
        (the auto-spawn path) fails with LockError instead of wedging."""
        apply_lock = acquire_apply_lock(tmp_path)
        try:
            daemon_style = VaultLock(tmp_path, install_signal_handlers=False)
            with pytest.raises(LockError):
                daemon_style.acquire()
        finally:
            apply_lock.release()
