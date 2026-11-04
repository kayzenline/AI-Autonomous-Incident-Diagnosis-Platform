"""Unit and integration tests for M7 LangGraph orchestrator, checkpoints, and HITL approvals."""

import asyncio
import json
import time

import pytest
from app.agent.langgraph_investigator import (
    InterruptedAfterCheckpointError,
    investigate_with_langgraph,
)
from app.agent.ledger import TraceEventType
from app.eval.m7_langgraph import CountingSimulatorTools
from app.eval.runner import baselines_dir
from app.eval.scenario import frozen_dir, load_scenario
from app.executor import service as executor
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.main import app
from app.persistence.store import (
    fail_runs_after_restart,
    list_run_checkpoints,
    save_run_checkpoint,
)
from fastapi.testclient import TestClient

HEADERS = {"X-API-Key": "dev-key"}
INCIDENT = {
    "title": "Checkout CPU saturation",
    "description": "High CPU on checkout pods.",
    "service": "checkout",
    "occurred_at": "2026-10-23T03:00:00Z",
    "detected_at": "2026-10-23T03:10:00Z",
}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _create_incident(client: TestClient) -> str:
    resp = client.post("/v1/incidents", json=INCIDENT, headers=HEADERS)
    assert resp.status_code == 201
    return str(resp.json()["id"])


def _wait_run(client: TestClient, run_id: str) -> dict:
    last: dict = {}
    for _ in range(200):
        last = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        if last["status"] not in {"PENDING", "RUNNING"}:
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def test_langgraph_baseline_has_zero_regression_against_m4() -> None:
    m7_report = json.loads((baselines_dir() / "m7-langgraph.json").read_text(encoding="utf-8"))
    m7_compare = json.loads((baselines_dir() / "m7-vs-m4.json").read_text(encoding="utf-8"))
    assert m7_report["agent"] == "langgraph-investigator"
    assert m7_report["agent_version"] == "m7"
    assert m7_report["top1"] == 1.0
    assert m7_report["accepted_illegal_citation_rate"] == 0.0
    assert all(case["verifier_tool_calls"] == 0 for case in m7_report["cases"])
    assert all(case["checkpoints_saved"] >= 2 for case in m7_report["cases"])
    assert m7_compare["zero_regression"] is True
    assert m7_compare["top1_delta"] == 0.0
    assert m7_compare["changed_cases"] == []
    exp = m7_compare["checkpoint_recovery_experiment"]
    assert exp["scenario_count"] == 5
    assert exp["control_m2_restart_fail"]["interrupted_run_completion_rate"] == 0.0
    assert exp["treatment_m7_checkpoint_resume"]["resumed_run_completion_rate"] == 1.0
    assert exp["treatment_m7_checkpoint_resume"]["resumed_top1"] == 1.0
    assert exp["treatment_m7_checkpoint_resume"]["avg_reexecuted_tool_calls"] == 0.0


def test_langgraph_checkpoint_resume_skips_completed_tool_calls() -> None:
    scenario = load_scenario(frozen_dir() / "multihop-checkout-payments-pool.yaml")
    tools = CountingSimulatorTools(scenario.world)
    saved: dict[int, dict] = {}

    async def saver(step_index: int, node_name: str, state_json: dict) -> None:
        saved[step_index] = {"node_name": node_name, "state_json": state_json}

    with pytest.raises(InterruptedAfterCheckpointError) as exc_info:
        asyncio.run(
            investigate_with_langgraph(
                scenario.incident,
                tools,
                InvestigativeFakeLLM(),
                known_services=scenario.world.services,
                checkpoint_saver=saver,
                stop_after_tool_calls=2,
            )
        )
    assert exc_info.value.step_index == 2
    assert len(tools.executed_calls) == 2
    first_two_calls = list(tools.executed_calls)

    resumed = asyncio.run(
        investigate_with_langgraph(
            scenario.incident,
            tools,
            InvestigativeFakeLLM(),
            known_services=scenario.world.services,
            checkpoint_saver=saver,
            resume_checkpoint=saved[2]["state_json"],
        )
    )
    assert resumed.status == "COMPLETED"
    assert resumed.report is not None
    assert resumed.report.hypotheses[0].cause.value == "db_pool_exhausted"
    # Total underlying tool executions equals the final ledger length (no re-execution)
    assert len(tools.executed_calls) == resumed.tool_calls
    assert tools.executed_calls[:2] == first_two_calls
    assert any(ev.type == TraceEventType.CHECKPOINT_RESUMED for ev in resumed.events)


def test_m2_restart_fail_remains_default_and_blocks_checkpoint_resume(client: TestClient) -> None:
    incident_id = _create_incident(client)
    executor.pause_runs = True
    try:
        started = client.post(
            f"/v1/incidents/{incident_id}/runs",
            json={"scenario_id": "cpu-saturation-checkout"},
            headers=HEADERS,
        )
        assert started.status_code == 202
        run_id = started.json()["id"]
        assert started.json()["config"]["checkpoint_policy"] == "fail"
        changed = client.portal.call(fail_runs_after_restart)
        assert changed == 1
    finally:
        executor.pause_runs = False

    failed = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert failed["status"] == "FAILED"
    assert failed["failure_reason"] == "restart"

    resume_resp = client.post(f"/v1/runs/{run_id}/resume", headers=HEADERS)
    assert resume_resp.status_code == 409
    assert "checkpoint_policy='fail'" in resume_resp.json()["message"]


def test_opted_in_run_resumes_from_saved_checkpoint_after_restart(client: TestClient) -> None:
    scenario = load_scenario(frozen_dir() / "cpu-saturation-checkout.yaml")
    tools = CountingSimulatorTools(scenario.world)
    saved_cp: dict[int, dict] = {}

    async def saver(step_index: int, node_name: str, state_json: dict) -> None:
        saved_cp[step_index] = state_json

    with pytest.raises(InterruptedAfterCheckpointError):
        asyncio.run(
            investigate_with_langgraph(
                scenario.incident,
                tools,
                InvestigativeFakeLLM(),
                known_services=scenario.world.services,
                checkpoint_saver=saver,
                stop_after_tool_calls=1,
            )
        )

    incident_id = _create_incident(client)
    executor.pause_runs = True
    try:
        started = client.post(
            f"/v1/incidents/{incident_id}/runs",
            json={
                "scenario_id": "cpu-saturation-checkout",
                "checkpoint_policy": "resume",
            },
            headers=HEADERS,
        )
        assert started.status_code == 202
        run_id = started.json()["id"]
        client.portal.call(save_run_checkpoint, run_id, 1, "tool_dispatch", saved_cp[1])
        client.portal.call(fail_runs_after_restart)
    finally:
        executor.pause_runs = False

    after_restart = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
    assert after_restart["status"] == "FAILED"
    assert after_restart["failure_reason"] == "restart"

    resumed = client.post(f"/v1/runs/{run_id}/resume", headers=HEADERS)
    assert resumed.status_code in {200, 202}
    finished = _wait_run(client, run_id)
    assert finished["status"] == "COMPLETED"
    assert finished["config"]["resumed_from_checkpoint_step"] == 1
    assert finished["final_report"]["hypotheses"][0]["cause"] == "cpu_saturation"

    checkpoints = client.portal.call(list_run_checkpoints, run_id)
    assert len(checkpoints) >= 2
    events_text = client.get(f"/v1/runs/{run_id}/events", headers=HEADERS).text
    assert "CHECKPOINT_RESUMED" in events_text


def test_hitl_recommendation_approvals_and_events(client: TestClient) -> None:
    incident_id = _create_incident(client)
    started = client.post(
        f"/v1/incidents/{incident_id}/runs",
        json={"scenario_id": "cpu-saturation-checkout"},
        headers=HEADERS,
    )
    assert started.status_code == 202
    run_id = started.json()["id"]
    finished = _wait_run(client, run_id)
    assert finished["status"] == "COMPLETED"

    approvals_resp = client.get(f"/v1/runs/{run_id}/approvals", headers=HEADERS)
    assert approvals_resp.status_code == 200
    approvals = approvals_resp.json()["approvals"]
    assert len(approvals) >= 1
    assert approvals[0]["status"] == "PENDING"
    assert approvals[0]["requires_approval"] is True

    approve_resp = client.post(
        f"/v1/runs/{run_id}/approve",
        json={
            "action_index": 0,
            "decision": "approved",
            "actor": "sre-lead",
            "notes": "Verified CPU saturation; safe to execute.",
        },
        headers=HEADERS,
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "APPROVED"
    assert approve_resp.json()["actor"] == "sre-lead"

    reject_resp = client.post(
        f"/v1/runs/{run_id}/approve",
        json={
            "action_index": 0,
            "decision": "rejected",
            "actor": "sre-lead",
            "notes": "Hold action during freeze window.",
        },
        headers=HEADERS,
    )
    assert reject_resp.status_code == 200
    assert reject_resp.json()["status"] == "REJECTED"

    events_text = client.get(f"/v1/runs/{run_id}/events", headers=HEADERS).text
    assert "RECOMMENDATION_APPROVED" in events_text
    assert "RECOMMENDATION_REJECTED" in events_text
