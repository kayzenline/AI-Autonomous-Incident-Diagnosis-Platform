"""Fake LLM adapters for offline unit tests and deterministic M3 evaluation.

`FakeLLMAdapter` supports:
1. Scripted responses (`responses=[...]`) or a custom `handler` for testing
   schema validation, illegal-citation detection, and single-retry rules.
2. Default prompt-observation synthesis when no script is supplied: it reads
   only the JSON prompt produced by the M3 sweep (never the scenario answer key)
   and emits a `DiagnosisReport` citing keys from `allowed_evidence_keys`.
"""

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from app.core.report import DiagnosisReport, Hypothesis, Recommendation
from app.core.taxonomy import CAUSE_CATEGORY, CAUSE_SUMMARY, Cause, Verdict
from app.llm.port import LLMRequest, LLMResponse

ResponseItem = str | dict[str, Any] | DiagnosisReport | LLMResponse

_DNS_TOKENS = ("nxdomain", "name resolution", "unknown host")
_POOL_TOKENS = ("connection pool", "too many connections", "pool exhausted")
_DISK_TOKENS = ("no space left", "disk full")
_CONFIG_TOKENS = ("invalid config", "configuration error", "failed to load config")
_UNHEALTHY = {"unhealthy", "timeout", "down"}


class FakeLLMAdapter:
    def __init__(
        self,
        responses: Sequence[ResponseItem] | None = None,
        *,
        handler: Callable[[LLMRequest], ResponseItem] | None = None,
        model: str = "fake-m3-v1",
    ) -> None:
        self.model = model
        self._queue: list[ResponseItem] = list(responses or [])
        self._handler = handler
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._queue:
            item = self._queue.pop(0)
            return self._coerce(item)
        if self._handler is not None:
            return self._coerce(self._handler(request))
        report = _synthesize_from_prompt(request)
        return self._coerce(report)

    def _coerce(self, item: ResponseItem) -> LLMResponse:
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, DiagnosisReport):
            content = item.model_dump_json()
        elif isinstance(item, dict):
            content = json.dumps(item)
        else:
            content = str(item)
        return LLMResponse(
            content=content,
            model=self.model,
            tokens_in=128,
            tokens_out=64,
        )


def _synthesize_from_prompt(request: LLMRequest) -> DiagnosisReport:
    payload = _extract_prompt_payload(request)
    if payload is None:
        return DiagnosisReport(
            verdict=Verdict.UNKNOWN,
            unresolved_questions=["Prompt payload could not be parsed."],
        )

    incident_service = str(payload.get("incident", {}).get("service") or "")
    observations: dict[str, Any] = payload.get("service_observations", {})
    if not incident_service or incident_service not in observations:
        return DiagnosisReport(
            verdict=Verdict.UNKNOWN,
            unresolved_questions=["No observations for the incident service."],
        )

    inc_obs = observations[incident_service]
    inc_metrics: dict[str, Any] = inc_obs.get("metrics", {})
    if any(metric.get("status") != "ok" for metric in inc_metrics.values()):
        return DiagnosisReport(
            verdict=Verdict.UNKNOWN,
            unresolved_questions=["Metrics backend did not return an observation."],
        )

    candidates: list[tuple[int, float, Cause, list[str]]] = []
    for service_name, obs in observations.items():
        is_downstream = service_name != incident_service
        candidates.extend(_service_candidates(obs, is_downstream=is_downstream))

    if candidates:
        candidates.sort(key=lambda item: (item[0], -item[1]))
        merged: list[tuple[Cause, list[str], float]] = []
        seen: dict[Cause, int] = {}
        for _priority, magnitude, cause, keys in candidates:
            if cause not in seen:
                seen[cause] = len(merged)
                merged.append((cause, list(dict.fromkeys(keys)), magnitude))
            else:
                idx = seen[cause]
                prev_cause, prev_keys, prev_mag = merged[idx]
                combined = list(dict.fromkeys([*prev_keys, *keys]))
                merged[idx] = (prev_cause, combined, max(prev_mag, magnitude))

        hypotheses = [
            Hypothesis(
                cause=cause,
                cause_summary=CAUSE_SUMMARY[cause],
                category=CAUSE_CATEGORY[cause],
                confidence=round(max(0.3, 0.85 - 0.1 * idx), 2),
                supporting_evidence=keys,
            )
            for idx, (cause, keys, _mag) in enumerate(merged)
        ]
        top_cause, _top_keys, top_mag = merged[0]
        error_max = _max_error_rate(observations)
        return DiagnosisReport(
            hypotheses=hypotheses,
            verdict=Verdict.FAULT,
            severity=_severity(top_cause, top_mag, error_max),
            recommendations=[
                Recommendation(
                    action="Inspect the cited service and evidence before applying remediation.",
                    risk="low",
                    requires_approval=True,
                )
            ],
            unresolved_questions=(
                ["Multiple candidate causes detected across the swept topology."]
                if len(merged) > 1
                else []
            ),
        )

    if _all_healthy(observations):
        return DiagnosisReport(
            verdict=Verdict.NOT_A_FAULT,
            unresolved_questions=["All swept services remain within normal operating bands."],
        )
    return DiagnosisReport(
        verdict=Verdict.UNKNOWN,
        unresolved_questions=["Swept observations are inconclusive in the default window."],
    )


def _service_candidates(
    obs: dict[str, Any], *, is_downstream: bool
) -> list[tuple[int, float, Cause, list[str]]]:
    found: list[tuple[int, float, Cause, list[str]]] = []
    metrics: dict[str, Any] = obs.get("metrics", {})
    logs_res: dict[str, Any] = obs.get("logs", {})
    changes_res: dict[str, Any] = obs.get("changes", {})
    deps_res: dict[str, Any] = obs.get("dependencies", {})
    db_res: dict[str, Any] = obs.get("database", {})

    error_series = metrics.get("error_rate", {})
    error_points = _points(error_series)
    error_max = _metric_max(metrics, "error_rate")
    entries = logs_res.get("data", {}).get("entries", [])
    change_rows = changes_res.get("data", {}).get("changes", [])
    dep_rows = deps_res.get("data", {}).get("dependencies", [])

    config_rows = [row for row in change_rows if row.get("kind") == "config"]
    config_logs = _matching(entries, _CONFIG_TOKENS)
    if config_rows and ((error_max or 0) >= 5.0 or config_logs):
        keys = [k for row in config_rows for k in _key_list(row.get("evidence_key"))]
        keys.extend(k for k, _msg in config_logs if k)
        if (error_max or 0) >= 5.0:
            keys.extend(error_series.get("evidence_keys", []))
        priority = 10 if config_logs else 25
        found.append((priority, error_max or 5.0, Cause.CONFIG_ERROR, keys))

    deploy_keys: list[str] = []
    deploy_mag: float | None = None
    for change in change_rows:
        if change.get("kind") != "deploy":
            continue
        moment = _time(change.get("at"))
        if moment is None:
            continue
        after = _max_after(error_points, moment)
        if after is not None and after >= 5.0:
            deploy_keys.extend(_key_list(change.get("evidence_key")))
            deploy_mag = after if deploy_mag is None else max(deploy_mag, after)
    if deploy_mag is not None:
        deploy_keys.extend(error_series.get("evidence_keys", []))
        priority = 15 if not config_logs else 30
        found.append((priority, deploy_mag, Cause.BAD_DEPLOY, deploy_keys))

    pool_logs = _matching(entries, _POOL_TOKENS)
    db_metric_max = _metric_max(metrics, "db_connections")
    db_obs_pct = _number(db_res.get("data", {}).get("connections_pct"))
    waiters = int(_number(db_res.get("data", {}).get("waiters")) or 0)
    db_pct = db_metric_max
    if db_obs_pct is not None:
        db_pct = db_obs_pct if db_pct is None else max(db_pct, db_obs_pct)
    if (db_pct or 0) >= 90.0 or (pool_logs and ((db_pct or 0) >= 70.0 or waiters > 0)):
        keys = list(metrics.get("db_connections", {}).get("evidence_keys", []))
        keys.extend(db_res.get("evidence_keys", []))
        keys.extend(k for k, _msg in pool_logs if k)
        priority = 18 if is_downstream else 20
        found.append((priority, db_pct or 90.0, Cause.DB_POOL_EXHAUSTED, keys))

    disk_logs = _matching(entries, _DISK_TOKENS)
    disk_max = _metric_max(metrics, "disk")
    if (disk_max or 0) >= 90.0 or (disk_logs and (disk_max or 0) >= 85.0):
        keys = list(metrics.get("disk", {}).get("evidence_keys", []))
        keys.extend(k for k, _msg in disk_logs if k)
        found.append((35, disk_max or 90.0, Cause.DISK_FULL, keys))

    memory_max = _metric_max(metrics, "memory")
    memory_points = _points(metrics.get("memory", {}))
    if (memory_max or 0) >= 90.0 or _memory_rise(memory_points):
        keys = list(metrics.get("memory", {}).get("evidence_keys", []))
        found.append((40, memory_max or 80.0, Cause.MEMORY_LEAK, keys))

    cpu_max = _metric_max(metrics, "cpu")
    if (cpu_max or 0) >= 90.0:
        keys = list(metrics.get("cpu", {}).get("evidence_keys", []))
        found.append((45, cpu_max or 90.0, Cause.CPU_SATURATION, keys))

    dns_logs = _matching(entries, _DNS_TOKENS)
    if dns_logs:
        keys = [k for k, _msg in dns_logs if k]
        priority = 19 if is_downstream else 50
        found.append((priority, error_max or 25.0, Cause.DNS_FAILURE, keys))

    bad_deps = [row for row in dep_rows if row.get("status") in _UNHEALTHY]
    latency_max = _metric_max(metrics, "latency_p99")
    if bad_deps and ((latency_max or 0) >= 1000.0 or (error_max or 0) >= 5.0):
        keys = [k for row in bad_deps for k in _key_list(row.get("evidence_key"))]
        found.append((60, latency_max or 1000.0, Cause.DEPENDENCY_TIMEOUT, keys))

    return found


def _extract_prompt_payload(request: LLMRequest) -> dict[str, Any] | None:
    for msg in request.messages:
        if msg.role != "user":
            continue
        try:
            parsed = json.loads(msg.content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "service_observations" in parsed:
            return parsed
    return None


def _all_healthy(observations: dict[str, Any]) -> bool:
    for obs in observations.values():
        metrics: dict[str, Any] = obs.get("metrics", {})
        required = ("cpu", "memory", "disk", "db_connections", "error_rate", "latency_p99")
        values = [_metric_max(metrics, name) for name in required]
        if any(v is None for v in values):
            return False
        cpu, memory, disk, db_conn, err, lat = [float(v) for v in values if v is not None]
        if max(cpu, memory, disk, db_conn) >= 70.0:
            return False
        if err >= 1.0 or lat >= 500.0:
            return False
        dep_rows = obs.get("dependencies", {}).get("data", {}).get("dependencies", [])
        if any(row.get("status") in _UNHEALTHY for row in dep_rows):
            return False
    return True


def _max_error_rate(observations: dict[str, Any]) -> float | None:
    values = [_metric_max(obs.get("metrics", {}), "error_rate") for obs in observations.values()]
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _severity(cause: Cause, magnitude: float, error_rate: float | None) -> str:
    if cause in {Cause.DNS_FAILURE, Cause.DEPENDENCY_TIMEOUT}:
        if (error_rate or 0) >= 20.0 or magnitude >= 2000.0:
            return "high"
        return "medium"
    if magnitude >= 95.0 or (error_rate or 0) >= 20.0:
        return "high"
    return "medium"


def _metric_max(metrics: dict[str, Any], name: str) -> float | None:
    series = metrics.get(name, {})
    return _number(series.get("data", {}).get("maximum"))


def _points(series: dict[str, Any]) -> list[tuple[datetime, float]]:
    raw = series.get("data", {}).get("points", [])
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
    return (points[-1][1] - points[0][1]) >= 25.0 and points[-1][1] >= 80.0


def _max_after(points: list[tuple[datetime, float]], moment: datetime) -> float | None:
    values = [val for pt_at, val in points if pt_at >= moment]
    return max(values) if values else None


def _matching(entries: list[Any], tokens: tuple[str, ...]) -> list[tuple[str | None, str]]:
    found: list[tuple[str | None, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = str(entry.get("message", "")).casefold()
        if not any(tok in message for tok in tokens):
            continue
        key = entry.get("evidence_key")
        found.append((key if isinstance(key, str) and key else None, message))
    return found


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
    return None if parsed.tzinfo is None else parsed
