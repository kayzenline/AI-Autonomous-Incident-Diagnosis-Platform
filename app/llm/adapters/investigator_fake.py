"""Step-by-step fake LLM adapter for the M4 investigation loop.

`InvestigativeFakeLLM` reads only the conversation messages in `LLMRequest`
(incident metadata, default time window, and prior tool observations) and
chooses one tool call at a time within the 8-call budget before synthesizing
a structured `DiagnosisReport`.
"""

import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
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


class InvestigativeFakeLLM:
    def __init__(
        self,
        responses: Sequence[ResponseItem] | None = None,
        *,
        handler: Callable[[LLMRequest], ResponseItem] | None = None,
        model: str = "fake-m4-investigator-v1",
    ) -> None:
        self.model = model
        self._queue: list[ResponseItem] = list(responses or [])
        self._handler = handler
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._queue:
            return self._coerce(self._queue.pop(0))
        if self._handler is not None:
            return self._coerce(self._handler(request))
        planned = _plan_step(request)
        return self._coerce(planned)

    def _coerce(self, item: ResponseItem) -> LLMResponse:
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, DiagnosisReport):
            content = item.model_dump_json()
        elif isinstance(item, dict):
            content = json.dumps(item, sort_keys=True)
        else:
            content = str(item)
        return LLMResponse(
            content=content,
            model=self.model,
            tokens_in=160,
            tokens_out=96,
        )


def _plan_step(request: LLMRequest) -> dict[str, Any]:
    user_payloads = _parse_user_payloads(request)
    if not user_payloads:
        return {
            "action": "final_report",
            "summary": "No investigation payload provided.",
            "report": DiagnosisReport(
                verdict=Verdict.UNKNOWN,
                unresolved_questions=["Missing investigation context."],
            ).model_dump(mode="json"),
        }

    first = user_payloads[0]
    task = first.get("task")
    if task == "triage_infer_service":
        return {"service": _infer_service(first)}

    last = user_payloads[-1]
    if last.get("task") == "regenerate_final_report":
        return _regenerate_valid_report(request, last)

    state = _build_state(user_payloads)
    if state["must_converge"] or state["remaining_tool_calls"] <= 0:
        return _finalize(state)

    next_call = _select_next_tool(state)
    if next_call is not None:
        return next_call

    return _finalize(state)


def _infer_service(payload: dict[str, Any]) -> str | None:
    text = f"{payload.get('title', '')} {payload.get('description', '')}".casefold()
    known = [str(s) for s in payload.get("known_services", []) if isinstance(s, str)]
    for candidate in known:
        if candidate.casefold() in text:
            return candidate
    return known[0] if known else None


def _regenerate_valid_report(request: LLMRequest, retry_payload: dict[str, Any]) -> dict[str, Any]:
    allowed = set(str(k) for k in retry_payload.get("allowed_evidence_keys", []))
    for msg in reversed(request.messages):
        if msg.role != "assistant":
            continue
        try:
            parsed = json.loads(msg.content)
        except json.JSONDecodeError:
            continue
        rep_raw = parsed.get("report") if isinstance(parsed, dict) else None
        if isinstance(rep_raw, dict):
            cleaned_hyps: list[dict[str, Any]] = []
            for hyp in rep_raw.get("hypotheses", []):
                if not isinstance(hyp, dict):
                    continue
                supp = [k for k in hyp.get("supporting_evidence", []) if k in allowed]
                contra = [k for k in hyp.get("contradicting_evidence", []) if k in allowed]
                if not supp:
                    continue
                cause_val = Cause(str(hyp.get("cause")))
                cleaned_hyps.append(
                    {
                        **hyp,
                        "category": CAUSE_CATEGORY[cause_val].value,
                        "supporting_evidence": supp,
                        "contradicting_evidence": contra,
                    }
                )
            if cleaned_hyps:
                rep_raw["hypotheses"] = cleaned_hyps
                return {
                    "action": "final_report",
                    "summary": "Regenerated report with verified ledger citations.",
                    "report": rep_raw,
                }
    user_payloads = [
        p for p in _parse_user_payloads(request) if p.get("task") != "regenerate_final_report"
    ]
    state = _build_state(user_payloads)
    return _finalize(state)


def _parse_user_payloads(request: LLMRequest) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for msg in request.messages:
        if msg.role != "user":
            continue
        try:
            parsed = json.loads(msg.content)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
    return items


def _build_state(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    init = payloads[0]
    incident = init.get("incident", {})
    root_service = str(incident.get("service") or "")
    window = init.get("default_window", {})
    max_calls = int(init.get("max_tool_calls", 8))

    by_service: dict[str, dict[str, Any]] = {}
    called: set[tuple[str, str, str]] = set()
    ledger_keys: list[str] = []
    remaining = max_calls
    must_converge = False

    def _svc(name: str) -> dict[str, Any]:
        if name not in by_service:
            by_service[name] = {
                "metrics": {},
                "logs": [],
                "logs_truncated": False,
                "logs_last_at": None,
                "logs_narrowed": False,
                "changes": None,
                "dependencies": None,
                "database": None,
            }
        return by_service[name]

    _svc(root_service)

    for obs in payloads[1:]:
        if obs.get("DuplicateToolCall"):
            remaining = int(obs.get("remaining_tool_calls", remaining))
            continue
        tool = str(obs.get("tool") or "")
        args = obs.get("arguments") or {}
        service = str(args.get("service") or root_service)
        result = obs.get("result") or {}
        ledger_keys = [str(k) for k in obs.get("ledger_keys", ledger_keys)]
        remaining = int(obs.get("remaining_tool_calls", remaining))
        must_converge = bool(obs.get("must_emit_final_report_now", False))

        s_obs = _svc(service)
        if tool == "query_metrics":
            metric = str(args.get("metric") or "")
            called.add((tool, service, metric))
            s_obs["metrics"][metric] = result
        elif tool == "search_logs":
            has_custom_range = isinstance(args.get("time_range"), dict)
            called.add((tool, service, "narrow" if has_custom_range else "default"))
            entries = result.get("data", {}).get("entries", [])
            if isinstance(entries, list):
                s_obs["logs"].extend(entries)
                if entries and not has_custom_range:
                    s_obs["logs_last_at"] = entries[-1].get("at")
            if has_custom_range:
                s_obs["logs_narrowed"] = True
            else:
                s_obs["logs_truncated"] = bool(result.get("truncated", False))
        elif tool == "get_recent_changes":
            called.add((tool, service, ""))
            s_obs["changes"] = result
        elif tool == "get_dependency_health":
            called.add((tool, service, ""))
            s_obs["dependencies"] = result
        elif tool == "query_db_health":
            called.add((tool, service, ""))
            s_obs["database"] = result

    return {
        "root_service": root_service,
        "window": window,
        "by_service": by_service,
        "called": called,
        "ledger_keys": set(ledger_keys),
        "remaining_tool_calls": remaining,
        "must_converge": must_converge,
    }


def _select_next_tool(state: dict[str, Any]) -> dict[str, Any] | None:
    s0 = state["root_service"]
    called: set[tuple[str, str, str]] = state["called"]
    root_obs = state["by_service"][s0]

    # Step 1: Search ERROR logs on the incident service
    if ("search_logs", s0, "default") not in called:
        return _call(
            tool="search_logs",
            arguments={"service": s0, "min_level": "ERROR"},
            summary=f"Inspect ERROR logs on {s0} in the default window.",
        )

    # Step 1b: If logs were truncated and show no decisive fault token, narrow time_range
    if (
        root_obs["logs_truncated"]
        and not root_obs["logs_narrowed"]
        and not _has_decisive_log(root_obs["logs"])
        and ("search_logs", s0, "narrow") not in called
    ):
        narrow_range = _narrow_window(root_obs["logs_last_at"], state["window"])
        if narrow_range is not None:
            return _call(
                tool="search_logs",
                arguments={"service": s0, "min_level": "ERROR", "time_range": narrow_range},
                summary=f"First log page on {s0} was truncated; narrow time_range past page 1.",
            )

    # Step 2: Check error_rate on the incident service
    if ("query_metrics", s0, "error_rate") not in called:
        return _call(
            tool="query_metrics",
            arguments={"service": s0, "metric": "error_rate"},
            summary=f"Query error_rate on {s0} to quantify impact and check metrics availability.",
        )

    err_res = root_obs["metrics"].get("error_rate", {})
    if err_res.get("status") != "ok":
        # Metrics backend unavailable -> stop immediately and abstain with UNKNOWN
        return None

    # If DNS error is already confirmed in logs on s0, we have everything we need
    if _matching(root_obs["logs"], _DNS_TOKENS):
        return None

    # If disk full log is confirmed on s0, corroborate with disk metric
    if _matching(root_obs["logs"], _DISK_TOKENS):
        if ("query_metrics", s0, "disk") not in called:
            return _call(
                tool="query_metrics",
                arguments={"service": s0, "metric": "disk"},
                summary=f"Corroborate disk error log on {s0} with disk utilization metric.",
                hypotheses=[
                    {
                        "cause": Cause.DISK_FULL.value,
                        "status": "PROPOSED",
                        "confidence": 0.75,
                    }
                ],
            )
        if (_metric_max(root_obs["metrics"], "disk") or 0.0) >= 85.0:
            return None

    # If DB pool log is confirmed on s0, corroborate with db_connections metric & query_db_health
    if _matching(root_obs["logs"], _POOL_TOKENS):
        if ("query_metrics", s0, "db_connections") not in called:
            return _call(
                tool="query_metrics",
                arguments={"service": s0, "metric": "db_connections"},
                summary=f"Corroborate pool exhaustion log on {s0} with db_connections metric.",
                hypotheses=[
                    {
                        "cause": Cause.DB_POOL_EXHAUSTED.value,
                        "status": "PROPOSED",
                        "confidence": 0.8,
                    }
                ],
            )
        db_conn_max = _metric_max(root_obs["metrics"], "db_connections") or 0.0
        if db_conn_max >= 70.0:
            if ("query_db_health", s0, "") not in called:
                return _call(
                    tool="query_db_health",
                    arguments={"service": s0},
                    summary=f"Check database connection pool waiters on {s0}.",
                )
            return None

    # Step 3: Check downstream dependencies on s0
    if ("get_dependency_health", s0, "") not in called:
        return _call(
            tool="get_dependency_health",
            arguments={"service": s0},
            summary=f"Check downstream dependency health from {s0}.",
        )

    err_max = _metric_max(root_obs["metrics"], "error_rate") or 0.0
    config_logs = _matching(root_obs["logs"], _CONFIG_TOKENS)

    # Step 4: If error_rate is elevated or config logs exist, check recent changes on s0
    if (err_max >= 5.0 or config_logs) and ("get_recent_changes", s0, "") not in called:
        return _call(
            tool="get_recent_changes",
            arguments={"service": s0},
            summary=f"Check recent deploys or config changes on {s0}.",
        )

    changes = (root_obs["changes"] or {}).get("data", {}).get("changes", [])
    if changes:
        # Recent deploy/config change with elevated error_rate explains the incident
        return None

    # Step 5: If s0 has downstream dependencies and elevated error_rate, drill down
    dep_rows = (root_obs["dependencies"] or {}).get("data", {}).get("dependencies", [])
    if dep_rows and err_max >= 5.0:
        d1 = str(dep_rows[0].get("name") or "")
        if d1:
            if ("get_dependency_health", d1, "") not in called:
                return _call(
                    tool="get_dependency_health",
                    arguments={"service": d1},
                    summary=f"Inspect downstream dependency {d1} for multi-hop failures.",
                )
            d1_obs = state["by_service"].get(d1, {})
            d1_deps = (d1_obs.get("dependencies") or {}).get("data", {}).get("dependencies", [])
            bad_d2 = [row for row in d1_deps if row.get("status") in _UNHEALTHY]
            if bad_d2:
                d2 = str(bad_d2[0].get("name") or "")
                if d2 and ("search_logs", d2, "default") not in called:
                    return _call(
                        tool="search_logs",
                        arguments={"service": d2, "min_level": "ERROR"},
                        summary=f"Drill down into second-hop service {d2} error logs.",
                    )
                if d2 and ("query_metrics", d2, "error_rate") not in called:
                    return _call(
                        tool="query_metrics",
                        arguments={"service": d2, "metric": "error_rate"},
                        summary=f"Query error_rate on second-hop service {d2}.",
                    )
                return None

            if ("query_db_health", d1, "") not in called:
                return _call(
                    tool="query_db_health",
                    arguments={"service": d1},
                    summary=f"Check database pool health on downstream service {d1}.",
                )
            d1_db = (d1_obs.get("database") or {}).get("data", {})
            if (_number(d1_db.get("connections_pct")) or 0.0) >= 90.0 or int(
                _number(d1_db.get("waiters")) or 0
            ) > 0:
                if ("query_metrics", d1, "db_connections") not in called:
                    return _call(
                        tool="query_metrics",
                        arguments={"service": d1, "metric": "db_connections"},
                        summary=f"Query db_connections metric on downstream service {d1}.",
                    )
                if ("search_logs", d1, "default") not in called:
                    return _call(
                        tool="search_logs",
                        arguments={"service": d1, "min_level": "ERROR"},
                        summary=f"Search ERROR logs on downstream service {d1}.",
                    )
                return None

            if ("query_metrics", s0, "latency_p99") not in called:
                return _call(
                    tool="query_metrics",
                    arguments={"service": s0, "metric": "latency_p99"},
                    summary=f"Query latency_p99 on {s0} to confirm dependency timeout impact.",
                )
            if ("query_metrics", s0, "cpu") not in called:
                return _call(
                    tool="query_metrics",
                    arguments={"service": s0, "metric": "cpu"},
                    summary=f"Check CPU utilization on {s0} for concurrent resource pressure.",
                )
            return None

    # Step 6: Local resource & saturation sweep on s0 (within remaining budget)
    for metric_name in ("latency_p99", "db_connections", "cpu", "memory", "disk"):
        if ("query_metrics", s0, metric_name) not in called:
            return _call(
                tool="query_metrics",
                arguments={"service": s0, "metric": metric_name},
                summary=f"Query {metric_name} on {s0}.",
            )

    return None


def _finalize(state: dict[str, Any]) -> dict[str, Any]:
    s0 = state["root_service"]
    by_service: dict[str, dict[str, Any]] = state["by_service"]
    allowed_keys: set[str] = state["ledger_keys"]
    root_obs = by_service[s0]
    root_metrics = root_obs["metrics"]

    if any(m.get("status") != "ok" for m in root_metrics.values()):
        report = DiagnosisReport(
            verdict=Verdict.UNKNOWN,
            unresolved_questions=["One or more required metric series could not be retrieved."],
        )
        return {
            "action": "final_report",
            "summary": "Abstaining with UNKNOWN due to missing or unavailable metric series.",
            "report": report.model_dump(mode="json"),
        }

    # Gather candidate causes across inspected services
    candidates: list[tuple[int, float, Cause, list[str]]] = []
    refuted_updates: list[dict[str, Any]] = []

    for svc_name, obs in by_service.items():
        is_downstream = svc_name != s0
        svc_cands, svc_refuted = _evaluate_service(
            obs, is_downstream=is_downstream, allowed_keys=allowed_keys
        )
        candidates.extend(svc_cands)
        refuted_updates.extend(svc_refuted)

    if candidates:
        candidates.sort(key=lambda item: (item[0], -item[1]))
        top_priority, top_mag, top_cause, top_keys = candidates[0]
        if top_cause == Cause.DNS_FAILURE and top_priority == 19:
            for obs in by_service.values():
                dep_rows = ((obs["dependencies"] or {}).get("data", {}) or {}).get(
                    "dependencies", []
                )
                for row in dep_rows:
                    if row.get("status") == "down":
                        for k in _key_list(row.get("evidence_key")):
                            if k in allowed_keys and k not in top_keys:
                                top_keys.append(k)

        # Keep primary root-cause hypothesis; mark weaker conflicting herrings as REJECTED
        hyp_updates: list[dict[str, Any]] = [
            {
                "cause": top_cause.value,
                "cause_summary": CAUSE_SUMMARY[top_cause],
                "status": "LEADING",
                "confidence": 0.9,
                "supporting_evidence": top_keys,
            }
        ]
        for _prio, _mag, other_cause, other_keys in candidates[1:]:
            if other_cause == top_cause:
                continue
            hyp_updates.append(
                {
                    "cause": other_cause.value,
                    "cause_summary": CAUSE_SUMMARY[other_cause],
                    "status": "REJECTED",
                    "confidence": 0.25,
                    "supporting_evidence": other_keys,
                }
            )
        hyp_updates.extend(refuted_updates)

        error_max = _max_error_rate(by_service)
        report = DiagnosisReport(
            hypotheses=[
                Hypothesis(
                    cause=top_cause,
                    cause_summary=CAUSE_SUMMARY[top_cause],
                    category=CAUSE_CATEGORY[top_cause],
                    confidence=0.9,
                    supporting_evidence=top_keys,
                )
            ],
            verdict=Verdict.FAULT,
            severity=_severity(top_cause, top_mag, error_max),
            recommendations=[
                Recommendation(
                    action=f"Mitigate {top_cause.value} on affected service and verify recovery.",
                    risk="low",
                    requires_approval=True,
                )
            ],
            unresolved_questions=[],
        )
        return {
            "action": "final_report",
            "summary": f"Confirmed {top_cause.value} with {len(top_keys)} evidence key(s).",
            "hypotheses": hyp_updates,
            "report": report.model_dump(mode="json"),
        }

    if _is_confirmed_healthy(root_obs):
        report = DiagnosisReport(
            verdict=Verdict.NOT_A_FAULT,
            unresolved_questions=[
                "All inspected metrics and dependencies are within normal bounds."
            ],
        )
        return {
            "action": "final_report",
            "summary": "Confirmed NOT_A_FAULT across metrics, logs, and dependencies.",
            "report": report.model_dump(mode="json"),
        }

    report = DiagnosisReport(
        verdict=Verdict.UNKNOWN,
        unresolved_questions=["Observed signals do not establish a single root cause."],
    )
    return {
        "action": "final_report",
        "summary": "Abstaining with UNKNOWN due to ambiguous or sub-threshold signals.",
        "report": report.model_dump(mode="json"),
    }


def _evaluate_service(
    obs: dict[str, Any],
    *,
    is_downstream: bool,
    allowed_keys: set[str],
) -> tuple[list[tuple[int, float, Cause, list[str]]], list[dict[str, Any]]]:
    found: list[tuple[int, float, Cause, list[str]]] = []
    refuted: list[dict[str, Any]] = []

    metrics: dict[str, Any] = obs["metrics"]
    entries: list[Any] = obs["logs"]
    change_rows = ((obs["changes"] or {}).get("data", {}) or {}).get("changes", [])
    dep_rows = ((obs["dependencies"] or {}).get("data", {}) or {}).get("dependencies", [])
    db_data = (obs["database"] or {}).get("data", {}) or {}
    db_ev_keys = (obs["database"] or {}).get("evidence_keys", [])

    error_series = metrics.get("error_rate", {})
    error_points = _points(error_series)
    error_max = _metric_max(metrics, "error_rate")

    def _filter(keys: list[str]) -> list[str]:
        return [k for k in dict.fromkeys(keys) if k in allowed_keys]

    # 1. Config error
    config_rows = [row for row in change_rows if row.get("kind") == "config"]
    config_logs = _matching(entries, _CONFIG_TOKENS)
    if config_rows and ((error_max or 0.0) >= 5.0 or config_logs):
        keys = [k for row in config_rows for k in _key_list(row.get("evidence_key"))]
        keys.extend(k for k, _m in config_logs if k)
        if (error_max or 0.0) >= 5.0:
            keys.extend(error_series.get("evidence_keys", []))
        filtered = _filter(keys)
        if filtered:
            found.append(
                (10 if config_logs else 25, error_max or 5.0, Cause.CONFIG_ERROR, filtered)
            )

    # 2. Bad deploy
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
        filtered = _filter(deploy_keys)
        if filtered:
            found.append((15 if not config_logs else 30, deploy_mag, Cause.BAD_DEPLOY, filtered))

    # 3. DB connection pool exhaustion
    pool_logs = _matching(entries, _POOL_TOKENS)
    db_metric_max = _metric_max(metrics, "db_connections")
    db_obs_pct = _number(db_data.get("connections_pct"))
    waiters = int(_number(db_data.get("waiters")) or 0)
    db_pct = db_metric_max
    if db_obs_pct is not None:
        db_pct = db_obs_pct if db_pct is None else max(db_pct, db_obs_pct)
    if (db_pct or 0.0) >= 90.0 or (pool_logs and ((db_pct or 0.0) >= 70.0 or waiters > 0)):
        keys = list(metrics.get("db_connections", {}).get("evidence_keys", []))
        keys.extend(db_ev_keys)
        keys.extend(k for k, _m in pool_logs if k)
        filtered = _filter(keys)
        if filtered:
            found.append(
                (18 if is_downstream else 20, db_pct or 90.0, Cause.DB_POOL_EXHAUSTED, filtered)
            )

    # 4. DNS failure (downstream DNS failure with down dependency or local NXDOMAIN)
    dns_logs = _matching(entries, _DNS_TOKENS)
    if dns_logs:
        keys = [k for k, _m in dns_logs if k]
        filtered = _filter(keys)
        if filtered:
            found.append(
                (19 if is_downstream else 32, error_max or 25.0, Cause.DNS_FAILURE, filtered)
            )

    # 5. Disk full (corroborated by disk error logs vs uncorroborated disk metric herring)
    disk_logs = _matching(entries, _DISK_TOKENS)
    disk_max = _metric_max(metrics, "disk")
    if (disk_max or 0.0) >= 90.0 or (disk_logs and (disk_max or 0.0) >= 85.0):
        keys = list(metrics.get("disk", {}).get("evidence_keys", []))
        keys.extend(k for k, _m in disk_logs if k)
        filtered = _filter(keys)
        if filtered:
            # Corroborated disk failure (with disk error logs) ranks at 34;
            # uncorroborated high disk watermark without disk logs ranks at 48.
            priority = 34 if disk_logs else 48
            found.append((priority, disk_max or 90.0, Cause.DISK_FULL, filtered))

    # 6. Dependency timeout
    bad_deps = [row for row in dep_rows if row.get("status") in _UNHEALTHY]
    latency_max = _metric_max(metrics, "latency_p99")
    if bad_deps and ((latency_max or 0.0) >= 1000.0 or (error_max or 0.0) >= 5.0):
        keys = [k for row in bad_deps for k in _key_list(row.get("evidence_key"))]
        filtered = _filter(keys)
        if filtered:
            # Active dependency timeout with elevated latency/errors ranks at 38.
            found.append((38, latency_max or 1900.0, Cause.DEPENDENCY_TIMEOUT, filtered))

    # 7. Memory leak
    memory_max = _metric_max(metrics, "memory")
    memory_points = _points(metrics.get("memory", {}))
    if (memory_max or 0.0) >= 90.0 or _memory_rise(memory_points):
        keys = list(metrics.get("memory", {}).get("evidence_keys", []))
        filtered = _filter(keys)
        if filtered:
            found.append((40, memory_max or 85.0, Cause.MEMORY_LEAK, filtered))

    # 8. CPU saturation
    cpu_max = _metric_max(metrics, "cpu")
    if (cpu_max or 0.0) >= 90.0:
        keys = list(metrics.get("cpu", {}).get("evidence_keys", []))
        filtered = _filter(keys)
        if filtered:
            found.append((45, cpu_max or 90.0, Cause.CPU_SATURATION, filtered))

    # Attach upstream dependency evidence to downstream DNS failure when present
    if is_downstream and dns_logs and bad_deps:
        pass

    return found, refuted


def _call(
    *,
    tool: str,
    arguments: dict[str, Any],
    summary: str,
    hypotheses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "call_tool",
        "tool": tool,
        "arguments": arguments,
        "summary": summary,
    }
    if hypotheses:
        payload["hypotheses"] = hypotheses
    return payload


def _has_decisive_log(entries: list[Any]) -> bool:
    for tokens in (_DNS_TOKENS, _POOL_TOKENS, _DISK_TOKENS, _CONFIG_TOKENS):
        if _matching(entries, tokens):
            return True
    return False


def _narrow_window(last_at_raw: object, default_win: dict[str, Any]) -> dict[str, str] | None:
    last_dt = _time(last_at_raw)
    end_dt = _time(default_win.get("end"))
    if last_dt is None or end_dt is None:
        return None
    new_start = last_dt + timedelta(seconds=1)
    if new_start >= end_dt:
        return None
    return {
        "start": new_start.isoformat().replace("+00:00", "Z"),
        "end": end_dt.isoformat().replace("+00:00", "Z"),
    }


def _is_confirmed_healthy(obs: dict[str, Any]) -> bool:
    metrics: dict[str, Any] = obs["metrics"]
    required = ("cpu", "memory", "disk", "db_connections", "error_rate", "latency_p99")
    values = [_metric_max(metrics, name) for name in required]
    if any(v is None for v in values):
        return False
    cpu, memory, disk, db_conn, err, lat = [float(v) for v in values if v is not None]
    if max(cpu, memory, disk, db_conn) >= 70.0:
        return False
    if err >= 1.0 or lat >= 500.0:
        return False
    dep_rows = ((obs["dependencies"] or {}).get("data", {}) or {}).get("dependencies", [])
    return not any(row.get("status") in _UNHEALTHY for row in dep_rows)


def _max_error_rate(by_service: dict[str, dict[str, Any]]) -> float | None:
    values = [_metric_max(obs.get("metrics", {}), "error_rate") for obs in by_service.values()]
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _severity(cause: Cause, magnitude: float, error_rate: float | None) -> str:
    if cause in {Cause.DNS_FAILURE, Cause.DEPENDENCY_TIMEOUT}:
        if (error_rate or 0.0) >= 20.0 or magnitude >= 1800.0:
            return "high"
        return "medium"
    if magnitude >= 95.0 or (error_rate or 0.0) >= 20.0:
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
