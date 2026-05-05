"""State-transition validation for SyncCoordinator."""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engram.errors import SyncError
from engram.sync.coordinator import (
    ALLOWED_TRANSITIONS,
    CoordinatorConfig,
    SyncCoordinator,
    SyncState,
)


def _make_coord(tmp_path: Path) -> SyncCoordinator:
    return SyncCoordinator(repo_dir=tmp_path, config=CoordinatorConfig())


def test_initial_state_is_idle(tmp_path: Path) -> None:
    coord = _make_coord(tmp_path)
    assert coord.state is SyncState.IDLE


def test_allowed_transition_succeeds(tmp_path: Path) -> None:
    coord = _make_coord(tmp_path)
    coord._transition(SyncState.DEBOUNCING, note="test")
    assert coord.state is SyncState.DEBOUNCING


def test_disallowed_transition_raises(tmp_path: Path) -> None:
    coord = _make_coord(tmp_path)
    # IDLE -> COMMITTED_NOT_PUSHED is NOT in the table.
    with pytest.raises(SyncError):
        coord._transition(SyncState.COMMITTED_NOT_PUSHED, note="bad")


def test_transition_to_same_state_is_noop(tmp_path: Path) -> None:
    coord = _make_coord(tmp_path)
    coord._transition(SyncState.IDLE, note="self")
    assert coord.state is SyncState.IDLE
    assert coord.events == ()


def test_allow_from_any_overrides_table(tmp_path: Path) -> None:
    coord = _make_coord(tmp_path)
    coord._transition(SyncState.MANUAL_RESOLUTION_REQUIRED, note="emergency", allow_from_any=True)
    assert coord.state is SyncState.MANUAL_RESOLUTION_REQUIRED


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (SyncState.IDLE, SyncState.DEBOUNCING),
        (SyncState.DEBOUNCING, SyncState.COMMITTING),
        (SyncState.COMMITTING, SyncState.PUSHING),
        (SyncState.PUSHING, SyncState.IDLE),
        (SyncState.PUSHING, SyncState.COMMITTED_NOT_PUSHED),
        (SyncState.PUSHING, SyncState.FETCHING),
        (SyncState.FETCHING, SyncState.PUSHING),
        (SyncState.FETCHING, SyncState.MANUAL_RESOLUTION_REQUIRED),
        (SyncState.COMMITTED_NOT_PUSHED, SyncState.PUSHING),
        (SyncState.PAUSED_FOR_MIGRATION, SyncState.IDLE),
    ],
)
def test_documented_transitions_are_in_table(from_state: SyncState, to_state: SyncState) -> None:
    """Each documented transition appears in :data:`ALLOWED_TRANSITIONS`."""
    assert to_state in ALLOWED_TRANSITIONS[from_state]


def test_event_buffer_records_each_transition(tmp_path: Path) -> None:
    coord = _make_coord(tmp_path)
    coord._transition(SyncState.DEBOUNCING, note="enq1")
    coord._transition(SyncState.COMMITTING, note="commit")
    events = coord.events
    assert len(events) == 2
    assert events[0].from_state is SyncState.IDLE
    assert events[0].to_state is SyncState.DEBOUNCING
    assert events[1].from_state is SyncState.DEBOUNCING
    assert events[1].to_state is SyncState.COMMITTING


def test_event_buffer_caps_at_size(tmp_path: Path) -> None:
    """The ring buffer keeps only the most recent EVENT_BUFFER_SIZE events."""
    from engram.sync.coordinator import EVENT_BUFFER_SIZE

    coord = _make_coord(tmp_path)
    for i in range(EVENT_BUFFER_SIZE + 50):
        # Toggle between two states to generate transitions.
        target = SyncState.DEBOUNCING if i % 2 == 0 else SyncState.IDLE
        with contextlib.suppress(SyncError):
            coord._transition(target, note=f"i={i}")
    assert len(coord.events) <= EVENT_BUFFER_SIZE


# === Hypothesis property test (sf-10) ===


_TRANSITION_EVENTS = st.sampled_from(
    [
        SyncState.IDLE,
        SyncState.DEBOUNCING,
        SyncState.COMMITTING,
        SyncState.PUSHING,
        SyncState.FETCHING,
        SyncState.IDLE,  # mostly-IDLE-ish weighting
    ]
)


@given(events=st.lists(_TRANSITION_EVENTS, min_size=0, max_size=30))
@settings(max_examples=50, deadline=None)
def test_property_bounded_event_log_and_no_crash(
    tmp_path_factory: pytest.TempPathFactory,
    events: list[SyncState],
) -> None:
    """For any modeled event sequence the coordinator never enters an
    undocumented state and the event log stays bounded."""
    base = tmp_path_factory.mktemp("hypo")
    coord = SyncCoordinator(repo_dir=base, config=CoordinatorConfig())
    valid_states = set(SyncState)
    for target in events:
        with contextlib.suppress(SyncError):
            coord._transition(target, note="hypothesis")
        assert coord.state in valid_states
        assert len(coord.events) <= 256
