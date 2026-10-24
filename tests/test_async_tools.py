"""Tests for the AsyncToolPort / ThreadedToolPort boundary (#29)."""

import asyncio
import time
from datetime import UTC, datetime

from app.core.window import TimeRange
from app.tools.contract import (
    MetricName,
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolResult,
    ToolStatus,
)
from app.tools.port import ThreadedToolPort


class _BlockingTools:
    services = ("checkout",)

    def __init__(self, delay_seconds: float = 0.08) -> None:
        self._delay = delay_seconds

    def query_metrics(self, request: QueryMetricsInput) -> ToolResult:
        time.sleep(self._delay)
        return ToolResult(status=ToolStatus.OK, summary=f"ok:{request.metric.value}")

    def search_logs(self, request: SearchLogsInput) -> ToolResult:
        time.sleep(self._delay)
        return ToolResult(status=ToolStatus.OK, summary="logs")

    def get_service_status(self, request: ServiceWindowInput) -> ToolResult:
        time.sleep(self._delay)
        return ToolResult(status=ToolStatus.OK, summary="status")

    def get_recent_changes(self, request: ServiceWindowInput) -> ToolResult:
        time.sleep(self._delay)
        return ToolResult(status=ToolStatus.OK, summary="changes")

    def get_dependency_health(self, request: ServiceWindowInput) -> ToolResult:
        time.sleep(self._delay)
        return ToolResult(status=ToolStatus.OK, summary="deps")

    def query_db_health(self, request: ServiceWindowInput) -> ToolResult:
        time.sleep(self._delay)
        return ToolResult(status=ToolStatus.OK, summary="db")


def test_blocking_tool_call_does_not_stall_event_loop() -> None:
    async def _run() -> tuple[int, ToolResult]:
        async_tools = ThreadedToolPort(_BlockingTools(delay_seconds=0.08))
        window = TimeRange(
            start=datetime(2026, 10, 23, 2, 45, tzinfo=UTC),
            end=datetime(2026, 10, 23, 3, 15, tzinfo=UTC),
        )
        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks += 1

        ticker_task = asyncio.create_task(_ticker())
        result = await async_tools.query_metrics(
            QueryMetricsInput(service="checkout", metric=MetricName.CPU, time_range=window)
        )
        await ticker_task
        return ticks, result

    ticks, result = asyncio.run(_run())
    assert ticks == 5
    assert result.status == ToolStatus.OK
    assert result.summary == "ok:cpu"
