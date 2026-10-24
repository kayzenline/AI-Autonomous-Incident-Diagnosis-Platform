"""Default investigation window from incident timestamps."""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime


def _as_utc(moment: datetime) -> datetime:
    """Treat a naive timestamp as UTC. Aware timestamps convert to UTC."""

    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def default_window(
    occurred_at: datetime | None,
    detected_at: datetime | None,
) -> TimeRange | None:
    """Return the FR-4 window, or None when both timestamps are missing.

    With occurred_at, the window starts 15 minutes earlier and ends 5 minutes
    after detected_at. With only detected_at, it is the 30 minutes before detection.
    Naive timestamps are UTC, matching simulator observations.
    """

    if occurred_at is not None:
        occurred_at = _as_utc(occurred_at)
    if detected_at is not None:
        detected_at = _as_utc(detected_at)

    if occurred_at is not None and detected_at is not None:
        return TimeRange(
            start=occurred_at - timedelta(minutes=15),
            end=detected_at + timedelta(minutes=5),
        )
    if occurred_at is not None:
        return TimeRange(
            start=occurred_at - timedelta(minutes=15),
            end=occurred_at + timedelta(minutes=5),
        )
    if detected_at is not None:
        return TimeRange(
            start=detected_at - timedelta(minutes=30),
            end=detected_at,
        )
    return None


def contains(window: TimeRange, moment: datetime) -> bool:
    return _as_utc(window.start) <= _as_utc(moment) <= _as_utc(window.end)
