"""Tool port with no observations.

Used when a run is not tied to a frozen scenario. Calls succeed and carry
no evidence, so the investigator can still finish.
"""

from app.tools.contract import (
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolResult,
    ToolStatus,
)


class EmptyTools:
    services: tuple[str, ...] = ()

    def query_metrics(self, request: QueryMetricsInput) -> ToolResult:
        return ToolResult(
            status=ToolStatus.OK,
            summary="No metric samples.",
            data={
                "service": request.service,
                "metric": request.metric.value,
                "maximum": None,
                "points": [],
            },
        )

    def search_logs(self, request: SearchLogsInput) -> ToolResult:
        del request
        return ToolResult(status=ToolStatus.OK, summary="No logs.", data={"entries": []})

    def get_service_status(self, request: ServiceWindowInput) -> ToolResult:
        del request
        return ToolResult(status=ToolStatus.OK, summary="No status.", data={})

    def get_recent_changes(self, request: ServiceWindowInput) -> ToolResult:
        del request
        return ToolResult(status=ToolStatus.OK, summary="No changes.", data={"changes": []})

    def get_dependency_health(self, request: ServiceWindowInput) -> ToolResult:
        del request
        return ToolResult(
            status=ToolStatus.OK,
            summary="No dependencies.",
            data={"dependencies": []},
        )

    def query_db_health(self, request: ServiceWindowInput) -> ToolResult:
        del request
        return ToolResult(status=ToolStatus.OK, summary="No database samples.", data={})
