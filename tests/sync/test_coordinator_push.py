"""Tests for SyncCoordinator.push_cycle retry classification (Step 9)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from engram.sync.coordinator import (
    CoordinatorConfig,
    SyncCoordinator,
    SyncState,
)
from engram.sync.gitops import GitErrorClass, PushResult


def _coord(tmp_path: Path, **overrides: object) -> SyncCoordinator:
    base_kwargs: dict[str, object] = {
        "auto_push_on_capture": True,
        "push_retry_count": 2,
        "push_retry_backoff_seconds": 0.0,
        "push_timeout_seconds": 5.0,
    }
    base_kwargs.update(overrides)
    return SyncCoordinator(
        repo_dir=tmp_path,
        config=CoordinatorConfig(**base_kwargs),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_push_ok_transitions_to_idle(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    with patch(
        "engram.sync.coordinator.gitops.push",
        new=AsyncMock(return_value=PushResult(error_class=GitErrorClass.OK)),
    ):
        await coord._push_cycle()
    assert coord.state is SyncState.IDLE


@pytest.mark.asyncio
async def test_push_auth_transitions_to_auth_required(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    with patch(
        "engram.sync.coordinator.gitops.push",
        new=AsyncMock(
            return_value=PushResult(error_class=GitErrorClass.AUTH, stderr="permission denied")
        ),
    ):
        await coord._push_cycle()
    assert coord.state is SyncState.AUTH_REQUIRED


@pytest.mark.asyncio
async def test_push_network_permanent_transitions_to_committed_not_pushed(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    with patch(
        "engram.sync.coordinator.gitops.push",
        new=AsyncMock(
            return_value=PushResult(
                error_class=GitErrorClass.NETWORK_PERMANENT, stderr="404 not found"
            )
        ),
    ):
        await coord._push_cycle()
    assert coord.state is SyncState.COMMITTED_NOT_PUSHED


@pytest.mark.asyncio
async def test_push_network_transient_retries_until_giving_up(tmp_path: Path) -> None:
    coord = _coord(tmp_path, push_retry_count=2)
    transient = PushResult(error_class=GitErrorClass.NETWORK_TRANSIENT, stderr="timeout")
    push_mock = AsyncMock(return_value=transient)
    with patch("engram.sync.coordinator.gitops.push", new=push_mock):
        await coord._push_cycle()
    # The first attempt + push_retry_count retries = 3 calls total.
    assert push_mock.await_count == 3
    assert coord.state is SyncState.COMMITTED_NOT_PUSHED


@pytest.mark.asyncio
async def test_push_unknown_transitions_to_manual_resolution(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    with patch(
        "engram.sync.coordinator.gitops.push",
        new=AsyncMock(return_value=PushResult(error_class=GitErrorClass.UNKNOWN, stderr="weird")),
    ):
        await coord._push_cycle()
    assert coord.state is SyncState.MANUAL_RESOLUTION_REQUIRED


@pytest.mark.asyncio
async def test_push_skipped_for_read_only_role(tmp_path: Path) -> None:
    coord = _coord(tmp_path, role="read-only")
    with patch(
        "engram.sync.coordinator.gitops.push",
        new=AsyncMock(return_value=PushResult(error_class=GitErrorClass.OK)),
    ) as push_mock:
        await coord._push_cycle()
    assert push_mock.await_count == 0
    assert coord.state is SyncState.IDLE
