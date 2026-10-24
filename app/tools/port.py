"""ToolPort. Callers see observations only.

search_memory is not part of this port. The scenario's ground_truth never crosses it.
"""

from typing import Protocol

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
