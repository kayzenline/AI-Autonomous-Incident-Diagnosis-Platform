from datetime import UTC, datetime, timedelta

import pytest
from app.core.window import TimeRange
from app.tools.contract import (
    MetricName,
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolStatus,
)
from app.tools.simulator.adapter import SimulatorTools
from app.tools.simulator.models import (
    LogRecord,
    MetricPoint,
    MetricSeries,
    World,
)
from pydantic import ValidationError


def _window() -> TimeRange:
    start = datetime(2026, 10, 23, 2, 45, tzinfo=UTC)
    return TimeRange(start=start, end=start + timedelta(minutes=30))


def _world() -> World:
    return World(
        services=["checkout"],
        metrics=[
            MetricSeries(
                service="checkout",
                metric=MetricName.CPU,
                evidence_key="metric:cpu_high",
                points=[
                    MetricPoint(at=datetime(2026, 10, 23, 2, 0, tzinfo=UTC), value=99),
                    MetricPoint(at=datetime(2026, 10, 23, 3, 0, tzinfo=UTC), value=97),
                ],
            )
        ],
        logs=[
            LogRecord(
                service="checkout",
                at=datetime(2026, 10, 23, 3, 0, tzinfo=UTC),
                level="INFO",
                message="info line",
                evidence_key="log:info",
            ),
            LogRecord(
                service="checkout",
                at=datetime(2026, 10, 23, 3, 0, tzinfo=UTC),
                level="ERROR",
                message="NXDOMAIN user ada@example.com from 10.8.8.8 token=raw-secret",
                evidence_key="log:dns_nxdomain",
            ),
            *[
                LogRecord(
                    service="checkout",
                    at=datetime(2026, 10, 23, 3, 0, tzinfo=UTC),
                    level="ERROR",
                    message=f"error {index}",
                    evidence_key=f"log:extra_{index}",
                )
                for index in range(24)
            ],
        ],
    )


def test_metric_window_drops_the_outside_point() -> None:
    result = SimulatorTools(_world()).query_metrics(
        QueryMetricsInput(service="checkout", metric=MetricName.CPU, time_range=_window())
    )

    assert result.status == ToolStatus.OK
    assert result.data["maximum"] == 97
    assert result.evidence_keys == ["metric:cpu_high"]
    assert "relevant" not in result.model_dump()
    assert "ground_truth" not in result.model_dump()


def test_logs_are_filtered_redacted_and_truncated() -> None:
    result = SimulatorTools(_world()).search_logs(
        SearchLogsInput(service="checkout", time_range=_window())
    )

    messages = [entry["message"] for entry in result.data["entries"]]
    assert result.truncated is True
    assert len(messages) == 20
    assert all("info line" not in message for message in messages)
    joined = "\n".join(messages)
    assert "ada@example.com" not in joined
    assert "10.8.8.8" not in joined
    assert "raw-secret" not in joined
    assert "NXDOMAIN" in joined
    assert "<<UNTRUSTED_DATA>>" in joined


def test_unknown_service_is_an_observation() -> None:
    result = SimulatorTools(_world()).get_service_status(
        ServiceWindowInput(service="missing", time_range=_window())
    )

    assert result.status == ToolStatus.ERROR
    assert result.evidence_keys == []


def test_metric_enum_is_closed() -> None:
    schema = QueryMetricsInput.model_json_schema()
    encoded = str(schema)

    for name in ("cpu", "memory", "disk", "latency_p99", "error_rate", "db_connections"):
        assert name in encoded


def test_world_rejects_ground_truth() -> None:
    with pytest.raises(ValidationError):
        World.model_validate({"services": ["checkout"], "ground_truth": {"verdict": "FAULT"}})
