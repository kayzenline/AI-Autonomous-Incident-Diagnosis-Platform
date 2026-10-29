"""ToolPort and its async thread-offload boundary. Callers see observations only.

search_memory is not part of this port. The scenario's ground_truth never crosses it.
Synchronous callers (such as the M1 rule baseline) use ToolPort directly; async
harnesses and executors use AsyncToolPort / ThreadedToolPort so synchronous tool
IO never blocks the single-process event loop.
"""

import asyncio
import inspect
from typing import Protocol

from app.core.telemetry import traced
from app.tools.contract import (
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolResult,
)


class ToolPort(Protocol):
    def query_metrics(self, request: QueryMetricsInput) -> ToolResult:
        """Return metric points inside the requested window."""

    def search_logs(self, request: SearchLogsInput) -> ToolResult:
        """Return logs at or above the fixed minimum level."""

    def get_service_status(self, request: ServiceWindowInput) -> ToolResult:
        """Return the service's request-rate observation."""

    def get_recent_changes(self, request: ServiceWindowInput) -> ToolResult:
        """Return deploys, config edits, and scale events in the window."""

    def get_dependency_health(self, request: ServiceWindowInput) -> ToolResult:
        """Return dependency health for the service."""

    def query_db_health(self, request: ServiceWindowInput) -> ToolResult:
        """Return database connection observations."""


class AsyncToolPort(Protocol):
    async def query_metrics(self, request: QueryMetricsInput) -> ToolResult:
        """Return metric points inside the requested window without blocking the loop."""

    async def search_logs(self, request: SearchLogsInput) -> ToolResult:
        """Return logs at or above the fixed minimum level without blocking the loop."""

    async def get_service_status(self, request: ServiceWindowInput) -> ToolResult:
        """Return the service's request-rate observation without blocking the loop."""

    async def get_recent_changes(self, request: ServiceWindowInput) -> ToolResult:
        """Return deploys, config edits, and scale events without blocking the loop."""

    async def get_dependency_health(self, request: ServiceWindowInput) -> ToolResult:
        """Return dependency health for the service without blocking the loop."""

    async def query_db_health(self, request: ServiceWindowInput) -> ToolResult:
        """Return database connection observations without blocking the loop."""


class ThreadedToolPort:
    """Offload a synchronous ToolPort onto worker threads via asyncio.to_thread."""

    def __init__(self, tools: ToolPort) -> None:
        self._tools = tools

    @property
    def services(self) -> tuple[str, ...]:
        raw = getattr(self._tools, "services", ())
        return tuple(raw)

    @traced("agentops.tool.query_metrics")
    async def query_metrics(self, request: QueryMetricsInput) -> ToolResult:
        return await asyncio.to_thread(self._tools.query_metrics, request)

    @traced("agentops.tool.search_logs")
    async def search_logs(self, request: SearchLogsInput) -> ToolResult:
        return await asyncio.to_thread(self._tools.search_logs, request)

    @traced("agentops.tool.get_service_status")
    async def get_service_status(self, request: ServiceWindowInput) -> ToolResult:
        return await asyncio.to_thread(self._tools.get_service_status, request)

    @traced("agentops.tool.get_recent_changes")
    async def get_recent_changes(self, request: ServiceWindowInput) -> ToolResult:
        return await asyncio.to_thread(self._tools.get_recent_changes, request)

    @traced("agentops.tool.get_dependency_health")
    async def get_dependency_health(self, request: ServiceWindowInput) -> ToolResult:
        return await asyncio.to_thread(self._tools.get_dependency_health, request)

    @traced("agentops.tool.query_db_health")
    async def query_db_health(self, request: ServiceWindowInput) -> ToolResult:
        return await asyncio.to_thread(self._tools.query_db_health, request)


def ensure_async_tools(tools: ToolPort | AsyncToolPort) -> AsyncToolPort:
    if isinstance(tools, ThreadedToolPort):
        return tools
    if inspect.iscoroutinefunction(getattr(tools, "query_metrics", None)):
        return tools  # type: ignore[return-value]
    return ThreadedToolPort(tools)  # type: ignore[arg-type]
