"""Frozen rule baseline.

Rules use generic thresholds and the tool port only. They were locked before
the frozen scenarios were scored. Do not reorder RULE_ORDER to chase a score.

Priority, first match wins Top-1:
bad deploy, config change, database pool, disk, memory, CPU, DNS, dependency.
"""

from datetime import datetime

from app.core.report import DiagnosisReport, Hypothesis, Recommendation
from app.core.taxonomy import CAUSE_CATEGORY, CAUSE_SUMMARY, Cause, Verdict
from app.core.window import default_window
from app.schemas.incident import IncidentCreate
from app.tools.contract import (
    MetricName,
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolResult,
    ToolStatus,
)
from app.tools.port import ToolPort

RULE_ORDER: tuple[Cause, ...] = (
    Cause.BAD_DEPLOY,
    Cause.CONFIG_ERROR,
    Cause.DB_POOL_EXHAUSTED,
    Cause.DISK_FULL,
    Cause.MEMORY_LEAK,
    Cause.CPU_SATURATION,
    Cause.DNS_FAILURE,
    Cause.DEPENDENCY_TIMEOUT,
)

CPU_SATURATION_PCT = 90.0
MEMORY_SATURATION_PCT = 90.0
MEMORY_RISE_POINTS = 25.0
MEMORY_RISE_END_PCT = 80.0
DISK_FULL_PCT = 90.0
DB_POOL_PCT = 90.0
ERROR_RATE_PCT = 5.0
LATENCY_MS = 1000.0
TRAFFIC_RATIO = 2.0
HEALTHY_RESOURCE_PCT = 70.0
HEALTHY_ERROR_RATE_PCT = 1.0
HEALTHY_LATENCY_MS = 500.0
SEVERE_RESOURCE_PCT = 95.0
SEVERE_ERROR_RATE_PCT = 20.0
SEVERE_LATENCY_MS = 2000.0

_DNS_TOKENS = ("nxdomain", "name resolution", "unknown host")
_POOL_TOKENS = ("connection pool", "too many connections", "pool exhausted")
_DISK_TOKENS = ("no space left", "disk full")
_CONFIG_TOKENS = ("invalid config", "configuration error", "failed to load config")
_UNHEALTHY = {"unhealthy", "timeout", "down"}


class _Signal:
    def __init__(self, cause: Cause, evidence: list[str], magnitude: float) -> None:
        self.cause = cause
        self.evidence = list(dict.fromkeys(evidence))
        self.magnitude = magnitude


def diagnose(incident: IncidentCreate, tools: ToolPort) -> DiagnosisReport:
    window = default_window(incident.occurred_at, incident.detected_at)
    if incident.service is None or window is None:
        return _unknown("Incident is missing service or time.")

    service = incident.service
    request = ServiceWindowInput(service=service, time_range=window)
    series = {
        metric: tools.query_metrics(
            QueryMetricsInput(service=service, metric=metric, time_range=window)
        )
        for metric in MetricName
    }
    logs = tools.search_logs(SearchLogsInput(service=service, time_range=window))
    status = tools.get_service_status(request)
    changes = tools.get_recent_changes(request)
    dependencies = tools.get_dependency_health(request)
    database = tools.query_db_health(request)

    if any(result.status != ToolStatus.OK for result in series.values()):
        return _unknown("Metrics backend did not return an observation.")

    signals = _signals(series, logs, changes, dependencies, database)
    signals.sort(key=lambda signal: RULE_ORDER.index(signal.cause))
    if signals:
        return _fault(signals, _maximum(series, MetricName.ERROR_RATE))

    ratio = _ratio(status)
    if ratio is not None and ratio >= TRAFFIC_RATIO:
        return _not_a_fault("Request rate is elevated without a resource fault.")
    if _healthy(series, dependencies):
        return _not_a_fault("Observations stay inside the normal band.")
    return _unknown("No threshold fired, and the observations are not a clean healthy band.")


def _signals(
    series: dict[MetricName, ToolResult],
    logs: ToolResult,
    changes: ToolResult,
    dependencies: ToolResult,
    database: ToolResult,
) -> list[_Signal]:
    found: list[_Signal] = []
    error_points = _points(series[MetricName.ERROR_RATE])
    error_max = _maximum(series, MetricName.ERROR_RATE)
    entries = _entries(logs)
    change_rows = _rows(changes, "changes")
    dependency_rows = _rows(dependencies, "dependencies")

    deploy_keys: list[str] = []
    deploy_magnitude: float | None = None
    for change in change_rows:
        if change.get("kind") != "deploy":
            continue
        moment = _time(change.get("at"))
        if moment is None:
            continue
        after = _max_after(error_points, moment)
        if after is not None and after >= ERROR_RATE_PCT:
            deploy_keys.extend(_key_list(change.get("evidence_key")))
            deploy_magnitude = after if deploy_magnitude is None else max(deploy_magnitude, after)
    if deploy_magnitude is not None:
        deploy_keys.extend(series[MetricName.ERROR_RATE].evidence_keys)
        found.append(_Signal(Cause.BAD_DEPLOY, deploy_keys, deploy_magnitude))

    config_rows = [row for row in change_rows if row.get("kind") == "config"]
    config_logs = _matching(entries, _CONFIG_TOKENS)
    if config_rows and ((error_max or 0) >= ERROR_RATE_PCT or config_logs):
        keys = [key for row in config_rows for key in _key_list(row.get("evidence_key"))]
        keys.extend(key for key, _message in config_logs if key)
        if (error_max or 0) >= ERROR_RATE_PCT:
            keys.extend(series[MetricName.ERROR_RATE].evidence_keys)
        found.append(_Signal(Cause.CONFIG_ERROR, keys, error_max or ERROR_RATE_PCT))

    pool_logs = _matching(entries, _POOL_TOKENS)
    db_pct = _maximum(series, MetricName.DB_CONNECTIONS)
    observed_db = _number(database.data.get("connections_pct"))
    if observed_db is not None:
        db_pct = observed_db if db_pct is None else max(db_pct, observed_db)
    if (db_pct or 0) >= DB_POOL_PCT or pool_logs:
        keys = list(series[MetricName.DB_CONNECTIONS].evidence_keys)
        keys.extend(database.evidence_keys)
        keys.extend(key for key, _message in pool_logs if key)
        found.append(_Signal(Cause.DB_POOL_EXHAUSTED, keys, db_pct or DB_POOL_PCT))

    disk_logs = _matching(entries, _DISK_TOKENS)
    disk_max = _maximum(series, MetricName.DISK)
    if (disk_max or 0) >= DISK_FULL_PCT or disk_logs:
        keys = list(series[MetricName.DISK].evidence_keys)
        keys.extend(key for key, _message in disk_logs if key)
        found.append(_Signal(Cause.DISK_FULL, keys, disk_max or DISK_FULL_PCT))

    memory_max = _maximum(series, MetricName.MEMORY)
    memory_points = _points(series[MetricName.MEMORY])
    memory_rise = _memory_rise(memory_points)
    if (memory_max or 0) >= MEMORY_SATURATION_PCT or memory_rise:
        found.append(
            _Signal(
                Cause.MEMORY_LEAK,
                list(series[MetricName.MEMORY].evidence_keys),
                memory_max or MEMORY_RISE_END_PCT,
            )
        )

    cpu_max = _maximum(series, MetricName.CPU)
    if (cpu_max or 0) >= CPU_SATURATION_PCT:
        found.append(
            _Signal(Cause.CPU_SATURATION, list(series[MetricName.CPU].evidence_keys), cpu_max or 0)
        )

    dns_logs = _matching(entries, _DNS_TOKENS)
    if dns_logs:
        found.append(
            _Signal(
                Cause.DNS_FAILURE,
                [key for key, _message in dns_logs if key],
                error_max or 0,
            )
        )

    bad_dependencies = [row for row in dependency_rows if row.get("status") in _UNHEALTHY]
    latency = _maximum(series, MetricName.LATENCY_P99)
    if bad_dependencies and ((latency or 0) >= LATENCY_MS or (error_max or 0) >= ERROR_RATE_PCT):
        keys = [key for row in bad_dependencies for key in _key_list(row.get("evidence_key"))]
        found.append(_Signal(Cause.DEPENDENCY_TIMEOUT, keys, latency or 0))
    return found


def _fault(signals: list[_Signal], error_rate: float | None) -> DiagnosisReport:
    hypotheses = [
        Hypothesis(
            cause=signal.cause,
            cause_summary=CAUSE_SUMMARY[signal.cause],
            category=CAUSE_CATEGORY[signal.cause],
            confidence=0.5,
            supporting_evidence=signal.evidence,
        )
        for signal in signals
    ]
    top = signals[0]
    questions: list[str] = []
    if len(signals) > 1:
        questions.append("More than one signal fired. Order is the frozen rule priority.")
    return DiagnosisReport(
        hypotheses=hypotheses,
        verdict=Verdict.FAULT,
        severity=_severity(top, error_rate),
        recommendations=[
            Recommendation(
                action="Review the cited evidence before any production change.",
                risk="low",
                requires_approval=True,
            )
        ],
        unresolved_questions=questions,
    )


def _severity(signal: _Signal, error_rate: float | None) -> str:
    if signal.cause in {Cause.DNS_FAILURE, Cause.DEPENDENCY_TIMEOUT}:
        if (error_rate or 0) >= SEVERE_ERROR_RATE_PCT or signal.magnitude >= SEVERE_LATENCY_MS:
            return "high"
        return "medium"
    if signal.magnitude >= SEVERE_RESOURCE_PCT or (error_rate or 0) >= SEVERE_ERROR_RATE_PCT:
        return "high"
    return "medium"


def _healthy(series: dict[MetricName, ToolResult], dependencies: ToolResult) -> bool:
    required = (
        MetricName.CPU,
        MetricName.MEMORY,
        MetricName.DISK,
        MetricName.DB_CONNECTIONS,
        MetricName.ERROR_RATE,
        MetricName.LATENCY_P99,
    )
    values = [_maximum(series, metric) for metric in required]
    if any(value is None for value in values):
        return False
    cpu, memory, disk, database, error_rate, latency = values
    assert cpu is not None and memory is not None and disk is not None
    assert database is not None and error_rate is not None and latency is not None
    if max(cpu, memory, disk, database) >= HEALTHY_RESOURCE_PCT:
        return False
    if error_rate >= HEALTHY_ERROR_RATE_PCT or latency >= HEALTHY_LATENCY_MS:
        return False
    return all(row.get("status") not in _UNHEALTHY for row in _rows(dependencies, "dependencies"))


def _not_a_fault(question: str) -> DiagnosisReport:
    return DiagnosisReport(
        verdict=Verdict.NOT_A_FAULT,
        unresolved_questions=[question],
    )


def _unknown(question: str) -> DiagnosisReport:
    return DiagnosisReport(
        verdict=Verdict.UNKNOWN,
        unresolved_questions=[question],
    )


def _maximum(series: dict[MetricName, ToolResult], metric: MetricName) -> float | None:
    return _number(series[metric].data.get("maximum"))


def _points(result: ToolResult) -> list[tuple[datetime, float]]:
    raw = result.data.get("points", [])
    if not isinstance(raw, list):
        return []
    points: list[tuple[datetime, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        moment = _time(item.get("at"))
        value = _number(item.get("value"))
        if moment is not None and value is not None:
            points.append((moment, value))
    points.sort(key=lambda item: item[0])
    return points


def _memory_rise(points: list[tuple[datetime, float]]) -> bool:
    if len(points) < 2:
        return False
    start = points[0][1]
    end = points[-1][1]
    return end - start >= MEMORY_RISE_POINTS and end >= MEMORY_RISE_END_PCT


def _max_after(points: list[tuple[datetime, float]], moment: datetime) -> float | None:
    values = [value for point_at, value in points if point_at >= moment]
    return max(values) if values else None


def _entries(result: ToolResult) -> list[dict[str, object]]:
    return _rows(result, "entries")


def _rows(result: ToolResult, key: str) -> list[dict[str, object]]:
    raw = result.data.get(key, [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _matching(
    entries: list[dict[str, object]],
    tokens: tuple[str, ...],
) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    for entry in entries:
        message = str(entry.get("message", "")).casefold()
        if not any(token in message for token in tokens):
            continue
        key = entry.get("evidence_key")
        found.append((key if isinstance(key, str) and key else None, message))
    return found


def _ratio(result: ToolResult) -> float | None:
    if result.status != ToolStatus.OK:
        return None
    return _number(result.data.get("request_rate_ratio"))


def _key_list(value: object) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    return []


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return None
    return parsed
