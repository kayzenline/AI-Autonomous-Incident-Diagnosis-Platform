"""Default investigation window from incident timestamps."""

from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime


def default_window(
    occurred_at: datetime | None,
    detected_at: datetime | None,
) -> TimeRange | None:
    """Return the FR-4 window, or None when both timestamps are missing.

    With occurred_at, the window starts 15 minutes earlier and ends 5 minutes
    after detected_at. With only detected_at, it is the 30 minutes before detection.
    """

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
    return window.start <= moment <= window.end
