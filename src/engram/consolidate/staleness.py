"""Age math for the stale-candidate pass.

engram records no retrieval/access data, so staleness is age-only by design
(an honest signal beats an invented one). The anchor rules:

* A thought edited after capture anchors on ``updated_at`` - the edit
  re-validated it.
* A migrated thought anchors on ``legacy_created_at`` - its migration-day
  ``created_at`` must not make years-old content look fresh.
* Everything else anchors on ``created_at``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from engram.consolidate.models import StaleAnchor

#: Clocks across synced machines may skew; only beyond this is a thought
#: considered future-dated (a data-quality finding, excluded from age math).
FUTURE_TOLERANCE = timedelta(hours=24)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        msg = f"{name} must be timezone-aware (UTC)"
        raise ValueError(msg)


def effective_age(
    *,
    created_at: datetime,
    updated_at: datetime,
    legacy_created_at: datetime | None,
    now: datetime,
) -> tuple[int, StaleAnchor]:
    """Return (age_days, anchor) for staleness; age is floored at 0."""
    _require_aware(created_at, "created_at")
    _require_aware(updated_at, "updated_at")
    _require_aware(now, "now")
    if legacy_created_at is not None:
        _require_aware(legacy_created_at, "legacy_created_at")

    if updated_at > created_at:
        anchor_time, anchor = updated_at, StaleAnchor.UPDATED
    elif legacy_created_at is not None:
        anchor_time, anchor = legacy_created_at, StaleAnchor.LEGACY
    else:
        anchor_time, anchor = created_at, StaleAnchor.CREATED

    age_days = max(0, (now - anchor_time).days)
    return age_days, anchor


def is_future_dated(
    created_at: datetime,
    *,
    now: datetime,
    tolerance: timedelta = FUTURE_TOLERANCE,
) -> bool:
    """True when ``created_at`` is beyond clock-skew tolerance into the future."""
    _require_aware(created_at, "created_at")
    _require_aware(now, "now")
    return created_at > now + tolerance


__all__ = ["FUTURE_TOLERANCE", "effective_age", "is_future_dated"]
