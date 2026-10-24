"""Read-only simulator behind ToolPort.

Observations are redacted and wrapped. ground_truth is not a field on World,
and tool payloads do not grow a relevance flag.
"""

from datetime import UTC, datetime

from app.core.window import contains
from app.tools.contract import (
    MetricName,
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolResult,
    ToolStatus,
)
from app.tools.redact import redact_text, wrap_untrusted
from app.tools.simulator.models import LogRecord, World

_LOG_RANK = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
_LOG_LIMIT = 20


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class SimulatorTools:
    def __init__(self, world: World) -> None:
        self._world = world
        self._ip_aliases: dict[str, str] = {}

    def query_metrics(self, request: QueryMetricsInput) -> ToolResult:
        unknown = self._unknown(request.service)
        if unknown is not None:
            return unknown
        if self._world.metrics_unavailable:
            key = self._world.metrics_error_key
            return ToolResult(
                status=ToolStatus.ERROR,
                summary="Metrics backend timed out.",
                evidence_keys=[key] if key else [],
            )
        series = next(
            (
                item
                for item in self._world.metrics
                if item.service == request.service and item.metric == request.metric
            ),
            None,
        )
        if series is None:
            return self._empty_metric(request.service, request.metric)
        points = [point for point in series.points if contains(request.time_range, point.at)]
        maximum = max((point.value for point in points), default=None)
        key = series.evidence_key
        return ToolResult(
            status=ToolStatus.OK,
            summary=_metric_summary(request.metric.value, maximum, len(points)),
            evidence_keys=[key] if key and points else [],
            data={
                "service": request.service,
                "metric": request.metric.value,
                "maximum": maximum,
                "points": [{"at": _stamp(point.at), "value": point.value} for point in points],
            },
        )

    def search_logs(self, request: SearchLogsInput) -> ToolResult:
        unknown = self._unknown(request.service)
        if unknown is not None:
            return unknown
        minimum = _LOG_RANK[request.min_level]
        matched = [
            record
            for record in self._world.logs
            if record.service == request.service
            and _LOG_RANK[record.level] >= minimum
            and contains(request.time_range, record.at)
        ]
        matched.sort(key=lambda record: record.at)
        shown = matched[:_LOG_LIMIT]
        keys = [record.evidence_key for record in shown if record.evidence_key]
        return ToolResult(
            status=ToolStatus.OK,
            summary=_log_summary(len(shown), len(matched)),
            evidence_keys=keys,
            truncated=len(matched) > len(shown),
            data={"entries": [self._log_entry(record) for record in shown]},
        )

    def get_service_status(self, request: ServiceWindowInput) -> ToolResult:
        unknown = self._unknown(request.service)
        if unknown is not None:
            return unknown
        record = next(
            (item for item in self._world.statuses if item.service == request.service),
            None,
        )
        ratio = 1.0 if record is None else record.request_rate_ratio
        key = None if record is None else record.evidence_key
        return ToolResult(
            status=ToolStatus.OK,
            summary=f"Request rate ratio for {request.service} is {ratio:.2f}.",
            evidence_keys=[key] if key else [],
            data={"service": request.service, "request_rate_ratio": ratio},
        )

    def get_recent_changes(self, request: ServiceWindowInput) -> ToolResult:
        unknown = self._unknown(request.service)
        if unknown is not None:
            return unknown
        matched = [
            record
            for record in self._world.changes
            if record.service == request.service and contains(request.time_range, record.at)
        ]
        matched.sort(key=lambda record: record.at)
        keys = [record.evidence_key for record in matched if record.evidence_key]
        return ToolResult(
            status=ToolStatus.OK,
            summary=f"{len(matched)} change(s) for {request.service} in the window.",
            evidence_keys=keys,
            data={
                "changes": [
                    {
                        "at": _stamp(record.at),
                        "kind": record.kind,
                        "summary": self._untrusted(record.summary),
                        "evidence_key": record.evidence_key,
                    }
                    for record in matched
                ]
            },
        )

    def get_dependency_health(self, request: ServiceWindowInput) -> ToolResult:
        unknown = self._unknown(request.service)
        if unknown is not None:
            return unknown
        matched = [
            record for record in self._world.dependencies if record.service == request.service
        ]
        keys = [record.evidence_key for record in matched if record.evidence_key]
        return ToolResult(
            status=ToolStatus.OK,
            summary=f"{len(matched)} dependency observation(s) for {request.service}.",
            evidence_keys=keys,
            data={
                "dependencies": [
                    {
                        "name": record.name,
                        "status": record.status,
                        "latency_ms": record.latency_ms,
                        "evidence_key": record.evidence_key,
                    }
                    for record in matched
                ]
            },
        )

    def query_db_health(self, request: ServiceWindowInput) -> ToolResult:
        unknown = self._unknown(request.service)
        if unknown is not None:
            return unknown
        record = next(
            (item for item in self._world.databases if item.service == request.service),
            None,
        )
        if record is None:
            return ToolResult(
                status=ToolStatus.OK,
                summary=f"No database observation for {request.service}.",
                data={"connections_pct": None, "waiters": None},
            )
        key = record.evidence_key
        return ToolResult(
            status=ToolStatus.OK,
            summary=(
                f"Database connections at {record.connections_pct:.0f}% "
                f"with {record.waiters} waiter(s)."
            ),
            evidence_keys=[key] if key else [],
            data={"connections_pct": record.connections_pct, "waiters": record.waiters},
        )

    def _unknown(self, service: str) -> ToolResult | None:
        if service in self._world.services:
            return None
        return ToolResult(
            status=ToolStatus.ERROR,
            summary=f"Unknown service: {service}.",
        )

    def _empty_metric(self, service: str, metric: MetricName) -> ToolResult:
        return ToolResult(
            status=ToolStatus.OK,
            summary=f"No {metric.value} series for {service}.",
            data={
                "service": service,
                "metric": metric.value,
                "maximum": None,
                "points": [],
            },
        )

    def _log_entry(self, record: LogRecord) -> dict[str, object]:
        return {
            "at": _stamp(record.at),
            "level": record.level,
            "message": self._untrusted(record.message),
            "evidence_key": record.evidence_key,
        }

    def _untrusted(self, text: str) -> str:
        return wrap_untrusted(redact_text(text, self._ip_aliases))


def _metric_summary(metric: str, maximum: float | None, count: int) -> str:
    if maximum is None:
        return f"No {metric} points in the window."
    return f"{metric} maximum {maximum:.1f} across {count} point(s)."


def _log_summary(shown: int, total: int) -> str:
    if total > shown:
        return f"Showing {shown} of {total} error logs."
    return f"{total} error log(s) in the window."
