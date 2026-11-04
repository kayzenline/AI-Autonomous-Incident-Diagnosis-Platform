"""M7 LangGraph orchestrator evaluation and checkpoint recovery controlled experiment (§4.4, §6 M7).

Verifies two properties:
1. Zero regression after migrating the M4 hand-written loop to the M7 LangGraph
   `StateGraph` across all 29 `frozen-v1` scenarios (`Top-1 = 1.0`, `top1_delta = 0.0`,
   `verifier_tool_calls = 0`).
2. Controlled comparison between M2's default restart policy (`RUNNING -> FAILED(restart)`,
   `checkpoint_policy='fail'`) and M7's checkpoint recovery policy (`checkpoint_policy='resume'`,
   explicitly replacing M2's restart-fail policy for opted-in runs).
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.agent.langgraph_investigator import (
    InterruptedAfterCheckpointError,
    investigate_with_langgraph,
)
from app.eval.fairness import assert_baseline_is_isolated
from app.eval.scenario import Scenario, load_frozen_scenarios
from app.eval.score import cited_keys, evidence_precision, evidence_recall, top_hit
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.llm.port import LLMPort
from app.tools.simulator.adapter import SimulatorTools

M7_REPORT_NAME = "m7-langgraph.json"
M7_COMPARE_NAME = "m7-vs-m4.json"

CHECKPOINT_EXPERIMENT_SCENARIOS: tuple[str, ...] = (
    "cpu-saturation-checkout",
    "db-pool-with-cpu-herring",
    "drilldown-payments-pool",
    "drilldown-truncated-dns",
    "multihop-checkout-payments-pool",
)


async def build_m7_report_async(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
    include_reports: bool = False,
) -> dict[str, Any]:
    from app.eval.runner import _aggregate_report, _m4_case, _mean

    assert_baseline_is_isolated()
    adapter = llm or InvestigativeFakeLLM()
    ordered = sorted(scenarios or load_frozen_scenarios(), key=lambda item: item.id)
    cases: list[dict[str, Any]] = []

    for scenario in ordered:
        checkpoints: dict[int, dict[str, Any]] = {}

        async def _record_cp(step_index: int, node_name: str, state_json: dict[str, Any]) -> None:
            checkpoints[step_index] = {"node_name": node_name, "state": state_json}

        result = await investigate_with_langgraph(
            scenario.incident,
            SimulatorTools(scenario.world),
            adapter,
            known_services=scenario.world.services,
            is_eval=True,
            checkpoint_saver=_record_cp,
        )
        row = _m4_case(scenario, result)
        row["checkpoints_saved"] = len(checkpoints)
        if include_reports:
            row["failure_reason"] = result.failure_reason
            row["final_report"] = (
                None if result.report is None else result.report.model_dump(mode="json")
            )
        cases.append(row)

    report = _aggregate_report(
        agent="langgraph-investigator",
        agent_version="m7",
        cases=cases,
        model=getattr(adapter, "model", "unknown"),
    )
    report["avg_tool_calls"] = _mean([float(case["tool_calls"]) for case in cases])
    report["max_tool_calls_used"] = max((int(case["tool_calls"]) for case in cases), default=0)
    report["first_output_illegal_citation_rate"] = _mean(
        [float(case["first_output_illegal_citation_rate"]) for case in cases]
    )
    report["accepted_illegal_citation_rate"] = _mean(
        [
            float(case["accepted_illegal_citation_rate"])
            for case in cases
            if case["status"] == "COMPLETED"
        ]
    )
    return report


def build_m7_report(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> dict[str, Any]:
    return asyncio.run(build_m7_report_async(scenarios, llm=llm))


class CountingSimulatorTools(SimulatorTools):
    """Wraps SimulatorTools to count actual underlying tool executions across runs."""

    def __init__(self, world: Any) -> None:
        super().__init__(world)
        self.executed_calls: list[tuple[str, str]] = []

    def query_metrics(self, request: Any) -> Any:
        self.executed_calls.append(("query_metrics", request.service))
        return super().query_metrics(request)

    def search_logs(self, request: Any) -> Any:
        self.executed_calls.append(("search_logs", request.service))
        return super().search_logs(request)

    def get_service_status(self, request: Any) -> Any:
        self.executed_calls.append(("get_service_status", request.service))
        return super().get_service_status(request)

    def get_recent_changes(self, request: Any) -> Any:
        self.executed_calls.append(("get_recent_changes", request.service))
        return super().get_recent_changes(request)

    def get_dependency_health(self, request: Any) -> Any:
        self.executed_calls.append(("get_dependency_health", request.service))
        return super().get_dependency_health(request)

    def query_db_health(self, request: Any) -> Any:
        self.executed_calls.append(("query_db_health", request.service))
        return super().query_db_health(request)


async def run_m7_checkpoint_experiment_async(
    scenarios: list[Scenario] | None = None,
) -> dict[str, Any]:
    """Controlled comparison: M2 `RUNNING -> FAILED(restart)` vs M7 `checkpoint_policy='resume'`."""
    assert_baseline_is_isolated()
    by_id = {s.id: s for s in (scenarios or load_frozen_scenarios())}
    selected = [by_id[sid] for sid in CHECKPOINT_EXPERIMENT_SCENARIOS if sid in by_id]

    control_cases: list[dict[str, Any]] = []
    treatment_cases: list[dict[str, Any]] = []

    for scenario in selected:
        interrupt_after = 1

        # 1. Control (M2 restart-fail policy: checkpoint_policy='fail')
        # Interrupted run transitions RUNNING -> FAILED(restart); completing requires full rerun.
        control_tools = CountingSimulatorTools(scenario.world)
        try:
            await investigate_with_langgraph(
                scenario.incident,
                control_tools,
                InvestigativeFakeLLM(),
                known_services=scenario.world.services,
                is_eval=True,
                stop_after_tool_calls=interrupt_after,
            )
        except InterruptedAfterCheckpointError:
            pass
        else:
            raise AssertionError("Expected InterruptedAfterCheckpointError for control case")
        calls_before_restart = len(control_tools.executed_calls)
        # Under M2 policy, the interrupted run is marked FAILED(restart) with final_report=None.
        # A manual retry starts from step 0 and re-executes the first `interrupt_after` tool calls.
        rerun_res = await investigate_with_langgraph(
            scenario.incident,
            control_tools,
            InvestigativeFakeLLM(),
            known_services=scenario.world.services,
            is_eval=True,
        )
        total_control_calls = len(control_tools.executed_calls)
        control_cases.append(
            {
                "scenario_id": scenario.id,
                "policy": "m2_restart_fail",
                "interrupted_run_status": "FAILED",
                "interrupted_failure_reason": "restart",
                "interrupted_run_completed": False,
                "tool_calls_before_interruption": calls_before_restart,
                "manual_rerun_tool_calls": rerun_res.tool_calls,
                "total_tool_executions_to_complete": total_control_calls,
                "reexecuted_tool_calls": calls_before_restart,
            }
        )

        # 2. Treatment (M7 checkpoint recovery policy: checkpoint_policy='resume')
        # Explicitly replaces M2's restart-fail policy for opted-in runs.
        treatment_tools = CountingSimulatorTools(scenario.world)
        saved_checkpoints: dict[int, dict[str, Any]] = {}

        async def _saver(step_index: int, node_name: str, state_json: dict[str, Any]) -> None:
            saved_checkpoints[step_index] = {
                "step_index": step_index,
                "node_name": node_name,
                "state_json": state_json,
            }

        try:
            await investigate_with_langgraph(
                scenario.incident,
                treatment_tools,
                InvestigativeFakeLLM(),
                known_services=scenario.world.services,
                is_eval=True,
                checkpoint_saver=_saver,
                stop_after_tool_calls=interrupt_after,
            )
        except InterruptedAfterCheckpointError:
            pass
        else:
            raise AssertionError("Expected InterruptedAfterCheckpointError for treatment case")
        calls_before_resume = len(treatment_tools.executed_calls)
        latest_step = max(saved_checkpoints)
        latest_cp = saved_checkpoints[latest_step]["state_json"]

        resumed_res = await investigate_with_langgraph(
            scenario.incident,
            treatment_tools,
            InvestigativeFakeLLM(),
            known_services=scenario.world.services,
            is_eval=True,
            checkpoint_saver=_saver,
            resume_checkpoint=latest_cp,
        )
        total_treatment_calls = len(treatment_tools.executed_calls)
        expected = scenario.ground_truth
        rep = resumed_res.report
        assert rep is not None
        cited = cited_keys(rep)
        treatment_cases.append(
            {
                "scenario_id": scenario.id,
                "policy": "m7_checkpoint_resume",
                "resumed_from_step": latest_step,
                "status": resumed_res.status,
                "verdict_hit": rep.verdict == expected.verdict,
                "top1_hit": top_hit(rep, expected, limit=1),
                "evidence_recall": round(
                    evidence_recall(cited, expected.key_evidence_keys) or 0.0, 4
                ),
                "evidence_precision": round(
                    evidence_precision(cited, expected.key_evidence_keys) or 0.0, 4
                ),
                "tool_calls_before_interruption": calls_before_resume,
                "tool_calls_after_resume": total_treatment_calls - calls_before_resume,
                "total_tool_executions_to_complete": total_treatment_calls,
                "reexecuted_tool_calls": total_treatment_calls - resumed_res.tool_calls,
            }
        )

    return {
        "policy_statement": (
            "M2's default restart policy (`RUNNING -> FAILED(restart)`, "
            "`checkpoint_policy='fail'`) remains active by default. M7's "
            "`checkpoint_policy='resume'` explicitly replaces M2's restart-fail rule "
            "for opted-in runs so an interrupted run resumes from its latest "
            "`run_checkpoints` row without re-executing completed tool calls (§4.4, §6 M7)."
        ),
        "scenario_count": len(selected),
        "interrupt_after_tool_calls": 1,
        "control_m2_restart_fail": {
            "interrupted_run_completion_rate": 0.0,
            "avg_reexecuted_tool_calls_on_manual_rerun": round(
                sum(c["reexecuted_tool_calls"] for c in control_cases) / len(control_cases),
                4,
            ),
            "avg_total_tool_executions_to_complete": round(
                sum(c["total_tool_executions_to_complete"] for c in control_cases)
                / len(control_cases),
                4,
            ),
            "cases": control_cases,
        },
        "treatment_m7_checkpoint_resume": {
            "resumed_run_completion_rate": round(
                sum(1 for c in treatment_cases if c["status"] == "COMPLETED")
                / len(treatment_cases),
                4,
            ),
            "resumed_top1": round(
                sum(1 for c in treatment_cases if c["top1_hit"]) / len(treatment_cases),
                4,
            ),
            "avg_reexecuted_tool_calls": round(
                sum(c["reexecuted_tool_calls"] for c in treatment_cases) / len(treatment_cases),
                4,
            ),
            "avg_total_tool_executions_to_complete": round(
                sum(c["total_tool_executions_to_complete"] for c in treatment_cases)
                / len(treatment_cases),
                4,
            ),
            "cases": treatment_cases,
        },
    }


def build_m7_vs_m4_comparison(
    m4_report: dict[str, Any],
    m7_report: dict[str, Any],
) -> dict[str, Any]:
    from app.eval.runner import compare_reports

    comparison = compare_reports(m4_report, m7_report)
    comparison["zero_regression"] = (
        comparison["top1_delta"] == 0.0
        and comparison["top3_delta"] == 0.0
        and comparison["evidence_recall_delta"] == 0.0
        and comparison["evidence_precision_delta"] == 0.0
        and comparison["changed_cases"] == []
    )
    comparison["checkpoint_recovery_experiment"] = asyncio.run(run_m7_checkpoint_experiment_async())
    return comparison


def write_m7_baselines(directory: Path, m4_report: dict[str, Any]) -> None:
    m7_report = build_m7_report()
    comparison = build_m7_vs_m4_comparison(m4_report, m7_report)
    (directory / M7_REPORT_NAME).write_text(
        json.dumps(m7_report, indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / M7_COMPARE_NAME).write_text(
        json.dumps(comparison, indent=2) + "\n",
        encoding="utf-8",
    )


def check_m7_baselines(directory: Path, m4_report: dict[str, Any]) -> None:
    from app.eval.runner import _expect

    m7_report = build_m7_report()
    comparison = build_m7_vs_m4_comparison(m4_report, m7_report)
    _expect(directory / M7_REPORT_NAME, m7_report, ignore_severity=True)
    _expect(directory / M7_COMPARE_NAME, comparison)
