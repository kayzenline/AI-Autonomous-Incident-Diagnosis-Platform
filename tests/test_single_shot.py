"""Tests for M3 single-shot LLM diagnosis (#13, #27)."""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.agent.single_shot import diagnose_single_shot
from app.core.report import DiagnosisReport, Hypothesis
from app.core.taxonomy import Category, Cause, Verdict
from app.eval.runner import build_m3_report, build_report, compare_reports
from app.eval.scenario import load_frozen_scenarios
from app.llm.adapters.fake import FakeLLMAdapter
from app.llm.adapters.openai import OpenAIAdapter
from app.llm.port import LLMMessage, LLMRequest
from app.schemas.incident import IncidentCreate
from app.tools.simulator.adapter import SimulatorTools


def _scenario(scenario_id: str):
    return next(item for item in load_frozen_scenarios() if item.id == scenario_id)


def test_code_generated_sweep_covers_all_topology_services_and_metrics() -> None:
    scenario = _scenario("multihop-checkout-pricing-dns")
    fake = FakeLLMAdapter()

    result = asyncio.run(
        diagnose_single_shot(
            scenario.incident,
            SimulatorTools(scenario.world),
            fake,
            services=scenario.world.services,
        )
    )

    assert result.status == "COMPLETED"
    assert result.swept_services == ("checkout", "inventory", "pricing")
    # 3 services * (6 metrics + 1 log + 4 status/change/dep/db) = 33 tool calls
    assert result.tool_calls == 33
    assert result.llm_attempts == 1
    assert "log:pricing_nxdomain" in result.ledger_keys
    assert result.report is not None
    assert result.report.verdict == Verdict.FAULT
    assert result.report.hypotheses[0].cause == Cause.DNS_FAILURE


def test_missing_occurred_at_uses_30_minutes_before_detected_at() -> None:
    scenario = _scenario("cpu-saturation-checkout")
    incident = IncidentCreate(
        title=scenario.incident.title,
        description=scenario.incident.description,
        service=scenario.incident.service,
        occurred_at=None,
        detected_at=datetime(2026, 10, 23, 3, 10, tzinfo=UTC),
    )
    fake = FakeLLMAdapter()

    result = asyncio.run(diagnose_single_shot(incident, SimulatorTools(scenario.world), fake))

    assert result.status == "COMPLETED"
    assert len(fake.requests) == 1
    user_msg = fake.requests[0].messages[1].content
    assert "2026-10-23T02:40:00Z" in user_msg
    assert "2026-10-23T03:10:00Z" in user_msg


def test_illegal_first_citation_triggers_one_retry_without_new_tool_calls() -> None:
    scenario = _scenario("cpu-saturation-checkout")
    bad_first = {
        "hypotheses": [
            {
                "cause": "cpu_saturation",
                "cause_summary": "CPU is saturated.",
                "category": "CPU",
                "confidence": 0.9,
                "supporting_evidence": ["metric:cpu_high", "log:invented_hallucination"],
                "contradicting_evidence": [],
            }
        ],
        "verdict": "FAULT",
        "severity": "high",
        "recommendations": [],
        "unresolved_questions": [],
    }
    good_second = DiagnosisReport(
        hypotheses=[
            Hypothesis(
                cause=Cause.CPU_SATURATION,
                cause_summary="CPU is saturated.",
                category=Category.CPU,
                confidence=0.9,
                supporting_evidence=["metric:cpu_high"],
            )
        ],
        verdict=Verdict.FAULT,
        severity="high",
    )
    fake = FakeLLMAdapter(responses=[bad_first, good_second])

    result = asyncio.run(
        diagnose_single_shot(scenario.incident, SimulatorTools(scenario.world), fake)
    )

    assert result.status == "COMPLETED"
    assert result.llm_attempts == 2
    assert result.tool_calls == 11  # no extra tool calls on retry
    assert result.first_output_illegal_citation is True
    assert result.first_output_illegal_citation_rate == 0.5
    assert result.accepted_illegal_citation_rate == 0.0
    assert result.report is not None
    assert result.report.hypotheses[0].supporting_evidence == ["metric:cpu_high"]


def test_illegal_citation_on_both_attempts_fails_run_with_empty_report() -> None:
    scenario = _scenario("cpu-saturation-checkout")
    hallucinated = {
        "hypotheses": [
            {
                "cause": "cpu_saturation",
                "cause_summary": "CPU is saturated.",
                "category": "CPU",
                "confidence": 0.9,
                "supporting_evidence": ["log:not_in_ledger"],
                "contradicting_evidence": [],
            }
        ],
        "verdict": "FAULT",
        "severity": "high",
        "recommendations": [],
        "unresolved_questions": [],
    }
    fake = FakeLLMAdapter(responses=[hallucinated, hallucinated])

    result = asyncio.run(
        diagnose_single_shot(scenario.incident, SimulatorTools(scenario.world), fake)
    )

    assert result.status == "FAILED"
    assert result.report is None
    assert result.llm_attempts == 2
    assert result.first_output_illegal_citation is True
    assert result.first_output_illegal_citation_rate == 1.0


def test_openai_adapter_reads_env_key_and_runs_sync_sdk_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    missing_adapter = OpenAIAdapter()
    request = LLMRequest(messages=[LLMMessage(role="user", content="hello")])
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(missing_adapter.generate(request))

    class _SyncSDKClient:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            time.sleep(0.06)
            return SimpleNamespace(
                model=kwargs["model"],
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4),
            )

    async def _run_concurrent() -> tuple[int, str]:
        adapter = OpenAIAdapter(model="gpt-4o-mini", client=_SyncSDKClient())
        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            for _ in range(4):
                await asyncio.sleep(0.01)
                ticks += 1

        t_task = asyncio.create_task(_ticker())
        resp = await adapter.generate(request)
        await t_task
        return ticks, resp.content

    ticks, content = asyncio.run(_run_concurrent())
    assert ticks == 4
    assert content == '{"ok": true}'


def test_m3_comparison_solves_multihop_but_leaves_truncated_log_for_m4() -> None:
    m1 = build_report()
    m3 = build_m3_report()
    comparison = compare_reports(m1, m3)
    m3_cases = {case["scenario_id"]: case for case in m3["cases"]}

    assert comparison["top1_delta"] > 0
    assert m3["accepted_illegal_citation_rate"] == 0.0
    assert m3_cases["multihop-checkout-payments-pool"]["top1_hit"] is True
    assert m3_cases["multihop-checkout-pricing-dns"]["top1_hit"] is True
    assert m3_cases["drilldown-payments-pool"]["top1_hit"] is True
    # Truncated log case requires narrowing the log window, which M3 cannot hand-write
    assert m3_cases["drilldown-truncated-dns"]["top1_hit"] is False
    assert m3_cases["drilldown-truncated-dns"]["predicted_verdict"] == "UNKNOWN"
