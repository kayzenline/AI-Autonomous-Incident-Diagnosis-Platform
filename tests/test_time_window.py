from datetime import UTC, datetime, timedelta

from app.core.clock import ScenarioClock, WallClock
from app.core.window import default_window


def test_window_uses_occurred_and_detected() -> None:
    occurred = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
    detected = datetime(2026, 10, 23, 3, 10, tzinfo=UTC)

    window = default_window(occurred, detected)

    assert window is not None
    assert window.start == occurred - timedelta(minutes=15)
    assert window.end == detected + timedelta(minutes=5)


def test_window_uses_detected_only() -> None:
    detected = datetime(2026, 10, 23, 3, 10, tzinfo=UTC)

    window = default_window(None, detected)

    assert window is not None
    assert window.start == detected - timedelta(minutes=30)
    assert window.end == detected


def test_window_is_absent_without_timestamps() -> None:
    assert default_window(None, None) is None


def test_scenario_clock_stays_put() -> None:
    moment = datetime(2026, 10, 23, 3, 10, tzinfo=UTC)
    clock = ScenarioClock(moment)

    assert clock.now() == moment
    assert clock.now() == moment


def test_wall_clock_is_aware() -> None:
    assert WallClock().now().tzinfo is not None
