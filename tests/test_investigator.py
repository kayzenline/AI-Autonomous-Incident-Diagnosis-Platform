"""Tests for the M4 hand-written investigation loop (#14)."""

import asyncio
from datetime import UTC, datetime, timedelta

from app.agent.investigator import (
    InvestigationBudget,
    investigate,
)
from app.agent.ledger import EvidenceLedger, HypothesisBoard, HypothesisStatus, TraceEventType
from app.agent.verifier import ReadOnlyVerifier
from app.core.clock import ScenarioClock
from app.core.report import DiagnosisReport, Hypothesis, Recommendation
from app.core.taxonomy import Category, Cause, Verdict
from app.core.window import TimeRange
from app.eval.runner import build_m3_report, build_m4_report, compare_reports
from app.eval.scenario import load_frozen_scenarios
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.schemas.incident import IncidentCreate
from app.tools.contract import ToolResult, ToolStatus
from app.tools.simulator.adapter import SimulatorTools


class AdvancingWallClock:
    """Fake wall clock that advances by `step_seconds` on each `.now()` call."""

    def __init__(self, start: datetime, *, step_seconds: float = 0.0) -> None:
        self._current = start
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> datetime:
        val = self._current
        self._current = self._current + self._step
        return val


def _scenario_by_id(scenario_id: str):
    for scenario in load_frozen_scenarios():
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def test_triage_uses_code_when_service_present_and_model_when_missing() -> None:
    scenario = _scenario_by_id("dns-nxdomain-edge")

    # 1. Service present -> code triage, no model inference call
    direct_res = asyncio.run(
        investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            InvestigativeFakeLLM(),
            known_services=scenario.world.services,
        )
    )
    assert direct_res.status == "COMPLETED"
    assert direct_res.resolved_service == "edge"
    assert direct_res.service_inferred_by_model is False

    # 2. Service missing -> model infers service and trace marks inferred_by_model=True
    missing_svc_incident = IncidentCreate(
        title="Edge gateway returning 502 errors",
        description="Requests through the edge proxy are failing.",
        service=None,
        occurred_at=scenario.incident.occurred_at,
        detected_at=scenario.incident.detected_at,
    )
    inferred_res = asyncio.run(
        investigate(
            missing_svc_incident,
            SimulatorTools(scenario.world),
            InvestigativeFakeLLM(),
            known_services=scenario.world.services,
        )
    )
    assert inferred_res.status == "COMPLETED"
    assert inferred_res.resolved_service == "edge"
    assert inferred_res.service_inferred_by_model is True
    assert any(
        ev.type == TraceEventType.RUN_STARTED
        and ev.payload.get("service_inferred_by_model") is True
        for ev in inferred_res.events
    )


def test_duplicate_tool_call_is_suppressed_by_ledger() -> None:
    scenario = _scenario_by_id("dns-nxdomain-edge")
    scripted = InvestigativeFakeLLM(
        responses=[
            {
                "action": "call_tool",
                "summary": "First log query on edge.",
                "tool": "search_logs",
                "arguments": {"service": "edge", "min_level": "ERROR"},
            },
            {
                "action": "call_tool",
                "summary": "Duplicate log query on edge with identical arguments.",
                "tool": "search_logs",
                "arguments": {"service": "edge", "min_level": "ERROR"},
            },
            {
                "action": "final_report",
                "summary": "Emit report using cached log evidence.",
                "report": DiagnosisReport(
                    hypotheses=[
                        Hypothesis(
                            cause=Cause.DNS_FAILURE,
                            cause_summary="DNS lookup failed for an upstream host.",
                            category=Category.NETWORK,
                            confidence=0.9,
                            supporting_evidence=["log:dns_nxdomain"],
                        )
                    ],
                    verdict=Verdict.FAULT,
                    severity="high",
                    recommendations=[
                        Recommendation(
                            action="Check internal DNS records.",
                            risk="low",
                            requires_approval=True,
                        )
                    ],
                ).model_dump(mode="json"),
            },
        ]
    )
    res = asyncio.run(
        investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            scripted,
            known_services=scenario.world.services,
        )
    )
    assert res.status == "COMPLETED"
    assert res.tool_calls == 1
    assert any(
        ev.type == TraceEventType.GUARDRAIL_TRIGGERED
        and ev.payload.get("reason") == "duplicate_tool_call"
        for ev in res.events
    )


def test_wall_clock_and_scenario_clock_are_separated_on_timeout() -> None:
    scenario = _scenario_by_id("cpu-saturation-checkout")
    # ScenarioClock stays fixed at 03:10:00Z while WallClock jumps 35s per call (> 60s timeout)
    wall_clock = AdvancingWallClock(
        datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC),
        step_seconds=35.0,
    )
    scenario_clock = ScenarioClock(datetime(2026, 10, 23, 3, 10, 0, tzinfo=UTC))

    incident_no_detected = IncidentCreate(
        title=scenario.incident.title,
        description=scenario.incident.description,
        service=scenario.incident.service,
        occurred_at=scenario.incident.occurred_at,
        detected_at=None,
    )
    res = asyncio.run(
        investigate(
            incident_no_detected,
            SimulatorTools(scenario.world),
            InvestigativeFakeLLM(),
            budget=InvestigationBudget(wall_timeout_seconds=60.0),
            wall_clock=wall_clock,
            scenario_clock=scenario_clock,
            known_services=scenario.world.services,
        )
    )
    assert res.status == "TIMED_OUT"
    assert res.report is not None
    assert any("timeout" in q.casefold() for q in res.report.unresolved_questions)
    run_started = next(ev for ev in res.events if ev.type == TraceEventType.RUN_STARTED)
    assert run_started.payload["window"]["end"].startswith("2026-10-23T03:15:00")


def test_budget_exceeded_emits_partial_report_from_hypothesis_board() -> None:
    scenario = _scenario_by_id("disk-full-logger")
    over_budget_llm = InvestigativeFakeLLM(
        responses=[
            {
                "action": "call_tool",
                "summary": "Check error logs on logger.",
                "tool": "search_logs",
                "arguments": {"service": "logger", "min_level": "ERROR"},
                "hypotheses": [
                    {
                        "cause": Cause.DISK_FULL.value,
                        "status": "LEADING",
                        "confidence": 0.75,
                        "supporting_evidence": ["log:disk_full"],
                    }
                ],
            },
            {
                "action": "call_tool",
                "summary": "Attempt second tool call past max_tool_calls=1.",
                "tool": "query_metrics",
                "arguments": {"service": "logger", "metric": "disk"},
            },
        ]
    )
    res = asyncio.run(
        investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            over_budget_llm,
            budget=InvestigationBudget(max_tool_calls=1),
            known_services=scenario.world.services,
        )
    )
    assert res.status == "BUDGET_EXCEEDED"
    assert res.tool_calls == 1
    assert res.report is not None
    assert res.report.verdict == Verdict.FAULT
    assert res.report.hypotheses[0].cause == Cause.DISK_FULL


def test_readonly_verifier_catches_illegal_citation_and_preserves_first_rate() -> None:
    scenario = _scenario_by_id("dns-nxdomain-edge")
    bad_report = DiagnosisReport(
        hypotheses=[
            Hypothesis(
                cause=Cause.DNS_FAILURE,
                cause_summary="DNS lookup failed for an upstream host.",
                category=Category.NETWORK,
                confidence=0.9,
                supporting_evidence=["log:dns_nxdomain", "log:invented_hallucination"],
            )
        ],
        verdict=Verdict.FAULT,
        severity="high",
        recommendations=[
            Recommendation(
                action="Inspect DNS resolver.",
                risk="low",
                requires_approval=True,
            )
        ],
    )
    scripted = InvestigativeFakeLLM(
        responses=[
            {
                "action": "call_tool",
                "summary": "Search error logs on edge.",
                "tool": "search_logs",
                "arguments": {"service": "edge", "min_level": "ERROR"},
            },
            {
                "action": "final_report",
                "summary": "Emit report with 1 valid and 1 invented citation.",
                "report": bad_report.model_dump(mode="json"),
            },
        ]
    )
    res = asyncio.run(
        investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            scripted,
            known_services=scenario.world.services,
        )
    )
    assert res.status == "COMPLETED"
    assert res.verifier_runs == 1
    assert res.verifier_tool_calls == 0
    assert res.first_output_illegal_citation is True
    assert res.first_output_illegal_citation_rate == 0.5
    assert res.accepted_illegal_citation_rate == 0.0
    assert res.report is not None
    assert res.report.hypotheses[0].supporting_evidence == ["log:dns_nxdomain"]


def test_readonly_verifier_rejects_contradicting_rejected_hypothesis() -> None:
    ledger = EvidenceLedger()
    ledger.record_tool_call(
        tool="query_metrics",
        service="checkout",
        time_range=TimeRange(
            start=datetime(2026, 10, 23, 2, 40, tzinfo=UTC),
            end=datetime(2026, 10, 23, 3, 10, tzinfo=UTC),
        ),
        arguments={"service": "checkout", "metric": "disk"},
        result=ToolResult(
            status=ToolStatus.OK,
            summary="disk 96%",
            evidence_keys=["metric:disk_high"],
        ),
    )
    board = HypothesisBoard()
    board.upsert(
        cause=Cause.DISK_FULL,
        cause_summary="Disk space exhausted.",
        confidence=0.1,
        status=HypothesisStatus.REJECTED,
    )
    verifier = ReadOnlyVerifier(ledger, board)
    rep = DiagnosisReport(
        hypotheses=[
            Hypothesis(
                cause=Cause.DISK_FULL,
                cause_summary="Disk space exhausted.",
                category=Category.DISK,
                confidence=0.8,
                supporting_evidence=["metric:disk_high"],
            )
        ],
        verdict=Verdict.FAULT,
        severity="high",
    )
    check = verifier.verify_raw(rep.model_dump_json())
    assert check.valid is False
    assert check.error is not None
    assert "REJECTED" in check.error


def test_m4_beats_m3_on_frozen_evaluation_set() -> None:
    m3 = build_m3_report()
    m4 = build_m4_report()
    diff = compare_reports(m3, m4)
    assert m4["max_tool_calls_used"] <= 8
    assert m4["top1"] > m3["top1"]
    assert diff["top1_delta"] == 0.15
    assert m4["top1"] == 1.0
    assert m4["top3"] == 1.0
    assert m4["accepted_illegal_citation_rate"] == 0.0


def test_real_model_receives_tool_catalog_and_report_contract() -> None:
    import json

    from app.core.taxonomy import CAUSE_CATEGORY
    from app.llm.port import LLMResponse

    scenario = _scenario_by_id("cpu-saturation-checkout")

    class ContractDrivenModel:
        model = "contract-test"
        rounds = 0

        async def generate(self, request):
            self.rounds += 1
            task = json.loads(request.messages[1].content)
            assert request.response_schema is not None
            assert "checkout" in task["known_services"]
            assert "ground_truth" not in task
            catalog = {tool["name"]: tool for tool in task["tool_catalog"]}
            assert len(catalog) == 6
            assert "metric" in catalog["query_metrics"]["arguments_schema"]["required"]
            assert task["cause_category_pairs"] == {
                cause.value: category.value for cause, category in CAUSE_CATEGORY.items()
            }
            schema = task["report_schema"]
            assert schema["additionalProperties"] is False
            assert "verdict" in schema["required"]
            if self.rounds == 1:
                return LLMResponse(
                    model=self.model,
                    content=json.dumps(
                        {
                            "action": "call_tool",
                            "tool": catalog["query_metrics"]["name"],
                            "arguments": {
                                "service": task["incident"]["service"],
                                "metric": "cpu",
                                "time_range": task["default_window"],
                            },
                        }
                    ),
                )
            observation = json.loads(request.messages[-1].content)
            keys = observation["ledger_keys"]
            assert keys
            return LLMResponse(
                model=self.model,
                content=json.dumps(
                    {
                        "action": "final_report",
                        "report": {
                            "verdict": "FAULT",
                            "severity": "high",
                            "hypotheses": [
                                {
                                    "cause": "cpu_saturation",
                                    "category": "CPU",
                                    "cause_summary": "CPU observations warrant investigation.",
                                    "confidence": 0.8,
                                    "supporting_evidence": keys,
                                }
                            ],
                        },
                    }
                ),
            )

    result = asyncio.run(
        investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            ContractDrivenModel(),
            known_services=scenario.world.services,
        )
    )
    assert result.status == "COMPLETED"
    assert result.tool_calls == 1
    assert result.accepted_illegal_citation_rate == 0


def test_report_retry_carries_schema_and_forbids_more_tools() -> None:
    import json

    from app.llm.port import LLMResponse

    scenario = _scenario_by_id("cpu-saturation-checkout")

    class SchemaRetryModel:
        rounds = 0

        async def generate(self, request):
            self.rounds += 1
            if self.rounds == 1:
                return LLMResponse(model="test", content='{"action":"final_report","report":{}}')
            task = json.loads(request.messages[-1].content)
            assert task["task"] == "regenerate_final_report"
            assert task["report_schema"] == request.response_schema
            assert "No tool calls" in task["instruction"]
            return LLMResponse(model="test", content='{"verdict":"UNKNOWN"}')

    result = asyncio.run(
        investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            SchemaRetryModel(),
        )
    )
    assert result.status == "COMPLETED"
    assert result.tool_calls == 0
