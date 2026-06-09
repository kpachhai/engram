"""Tests for engram.consolidate.staleness - age math for the stale pass."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from engram.consolidate.models import StaleAnchor
from engram.consolidate.staleness import effective_age, is_future_dated

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _days_ago(days: int) -> datetime:
    return _NOW - timedelta(days=days)


class TestEffectiveAge:
    def test_untouched_thought_anchors_on_created(self):
        created = _days_ago(400)
        age, anchor = effective_age(
            created_at=created, updated_at=created, legacy_created_at=None, now=_NOW
        )
        assert age == 400
        assert anchor is StaleAnchor.CREATED

    def test_edited_thought_anchors_on_updated(self):
        """A recently edited old thought is not stale - the edit re-validated it."""
        age, anchor = effective_age(
            created_at=_days_ago(400),
            updated_at=_days_ago(10),
            legacy_created_at=None,
            now=_NOW,
        )
        assert age == 10
        assert anchor is StaleAnchor.UPDATED

    def test_migrated_thought_anchors_on_legacy_date(self):
        """Migration day must not make a years-old thought look fresh."""
        migration_day = _days_ago(30)
        age, anchor = effective_age(
            created_at=migration_day,
            updated_at=migration_day,
            legacy_created_at=_days_ago(900),
            now=_NOW,
        )
        assert age == 900
        assert anchor is StaleAnchor.LEGACY

    def test_migrated_then_edited_anchors_on_edit(self):
        age, anchor = effective_age(
            created_at=_days_ago(30),
            updated_at=_days_ago(5),
            legacy_created_at=_days_ago(900),
            now=_NOW,
        )
        assert age == 5
        assert anchor is StaleAnchor.UPDATED

    def test_age_never_negative(self):
        age, _ = effective_age(
            created_at=_NOW + timedelta(hours=2),
            updated_at=_NOW + timedelta(hours=2),
            legacy_created_at=None,
            now=_NOW,
        )
        assert age == 0

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            effective_age(
                created_at=datetime(2026, 1, 1),
                updated_at=_NOW,
                legacy_created_at=None,
                now=_NOW,
            )


class TestFutureDated:
    def test_within_tolerance_is_not_future(self):
        assert not is_future_dated(_NOW + timedelta(hours=23), now=_NOW)

    def test_beyond_tolerance_is_future(self):
        assert is_future_dated(_NOW + timedelta(hours=25), now=_NOW)

    def test_past_is_not_future(self):
        assert not is_future_dated(_days_ago(1), now=_NOW)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            is_future_dated(datetime(2027, 1, 1), now=_NOW)
