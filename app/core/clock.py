"""Wall clock and scenario clock.

Tool windows use the scenario clock. Run timestamps use the wall clock.
Tests can replace either one.
"""

from datetime import UTC, datetime
from typing import Protocol


class ClockPort(Protocol):
    def now(self) -> datetime:
        """Return the current time for this clock."""


class WallClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ScenarioClock:
    def __init__(self, current: datetime) -> None:
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        self._current = current.astimezone(UTC)

    def now(self) -> datetime:
        return self._current
