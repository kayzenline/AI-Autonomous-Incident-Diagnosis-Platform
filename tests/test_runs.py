import asyncio
import time

import pytest
from app.api import runs as runs_api
from app.core.report import DiagnosisReport
from app.core.taxonomy import Verdict
from app.executor import service as executor
from app.main import app
from app.persistence.models import EvidenceRow, RunEventRow, ToolCallRow
from app.persistence.outcomes import validate_outcome
from app.persistence.session import get_sessionmaker
from app.persistence.store import (
    cancel_run,
    create_incident,
    fail_runs_after_restart,
    finish_run,
    get_run,
    mark_running,
    start_run,
)
from app.schemas.incident import IncidentCreate
from fastapi.testclient import TestClient
from sqlalchemy import select

HEADERS = {"X-API-Key": "dev-key"}
INCIDENT = {
    "title": "Checkout latency high",
    "description": "Shoppers time out while paying.",
    "service": "checkout",
    "occurred_at": "2026-10-23T03:00:00Z",
    "detected_at": "2026-10-23T03:10:00Z",
}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _create(client: TestClient) -> str:
    response = client.post("/v1/incidents", json=INCIDENT, headers=HEADERS)
    assert response.status_code == 201
    return str(response.json()["id"])


def _wait(client: TestClient, run_id: str) -> dict[str, object]:
    last: dict[str, object] = {}
    for _ in range(200):
        last = client.get(f"/v1/runs/{run_id}", headers=HEADERS).json()
        if last["status"] not in {"PENDING", "RUNNING"}:
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def test_idempotency_key_replays_and_a_second_run_conflicts(client: TestClient) -> None:
    incident_id = _create(client)
    executor.pause_runs = True
    key = {"Idempotency-Key": "same-key"}
    body = {"scenario_id": "cpu-saturation-checkout"}
    first = client.post(f"/v1/incidents/{incident_id}/runs", json=body, headers=HEADERS | key)
    replay = client.post(f"/v1/incidents/{incident_id}/runs", json=body, headers=HEADERS | key)
    other = client.post(f"/v1/incidents/{incident_id}/runs", json=body, headers=HEADERS)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    assert other.status_code == 409
    assert other.json()["error_code"] == "conflict"

    executor.pause_runs = False
    finished = _wait(client, first.json()["id"])
    assert finished["status"] == "COMPLETED"
    assert finished["final_report"]["verdict"] == "FAULT"
    assert finished["final_report"]["hypotheses"][0]["cause"] == "cpu_saturation"

    again = client.post(f"/v1/incidents/{incident_id}/runs", json=body, headers=HEADERS | key)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    incident = client.get(f"/v1/incidents/{incident_id}", headers=HEADERS).json()
    assert incident["status"] == "DIAGNOSED"


def test_archived_incident_rejects_a_new_run(client: TestClient) -> None:
    incident_id = _create(client)
    assert client.delete(f"/v1/incidents/{incident_id}", headers=HEADERS).status_code == 204

    response = client.post(f"/v1/incidents/{incident_id}/runs", json={}, headers=HEADERS)

    assert response.status_code == 409


def test_cancel_stores_no_report(client: TestClient) -> None:
    incident_id = _create(client)
    executor.pause_runs = True
    started = client.post(
        f"/v1/incidents/{incident_id}/runs",
        json={"scenario_id": "cpu-saturation-checkout"},
        headers=HEADERS,
    )
    cancelled = client.post(f"/v1/runs/{started.json()['id']}/cancel", headers=HEADERS)
    executor.pause_runs = False
    time.sleep(0.2)
    current = client.get(f"/v1/runs/{started.json()['id']}", headers=HEADERS).json()

    assert cancelled.status_code == 200
    assert cancelled.json()["final_report"] is None
    assert current["status"] == "CANCELLED"
    assert current["final_report"] is None


def test_resolved_incident_stays_resolved_after_a_run(client: TestClient) -> None:
    incident_id = _create(client)
    patched = client.patch(
        f"/v1/incidents/{incident_id}",
        json={"status": "RESOLVED"},
        headers=HEADERS,
    )
    assert patched.status_code == 200
    started = client.post(
        f"/v1/incidents/{incident_id}/runs",
        json={"scenario_id": "cpu-saturation-checkout"},
        headers=HEADERS,
    )
    finished = _wait(client, started.json()["id"])
    incident = client.get(f"/v1/incidents/{incident_id}", headers=HEADERS).json()

    assert finished["status"] == "COMPLETED"
    assert incident["status"] == "RESOLVED"


def test_tool_call_and_evidence_share_the_result_event(client: TestClient) -> None:
    incident_id = _create(client)
    started = client.post(
        f"/v1/incidents/{incident_id}/runs",
        json={"scenario_id": "cpu-saturation-checkout"},
        headers=HEADERS,
    )
    finished = _wait(client, started.json()["id"])
    assert finished["status"] == "COMPLETED"

    async def load() -> tuple[list[ToolCallRow], list[RunEventRow], list[EvidenceRow]]:
        factory = get_sessionmaker()
        async with factory() as session:
            calls = list(
                await session.scalars(
                    select(ToolCallRow).where(ToolCallRow.run_id == started.json()["id"])
                )
            )
            events = list(
                await session.scalars(
                    select(RunEventRow).where(RunEventRow.run_id == started.json()["id"])
                )
            )
            evidence = list(
                await session.scalars(
                    select(EvidenceRow).where(EvidenceRow.run_id == started.json()["id"])
                )
            )
            return calls, events, evidence

    calls, events, evidence = asyncio.run(load())
    result_ids = {event.id for event in events if event.type == "TOOL_RESULT"}
    assert calls
    assert {call.event_id for call in calls} <= result_ids
    assert {item.tool_call_id for item in evidence} <= {call.id for call in calls}
    assert "metric:cpu_high" in {item.evidence_key for item in evidence}


def test_event_stream_replays_from_last_event_id(client: TestClient) -> None:
    incident_id = _create(client)
    started = client.post(
        f"/v1/incidents/{incident_id}/runs",
        json={"scenario_id": "cpu-saturation-checkout"},
        headers=HEADERS,
    )
    _wait(client, started.json()["id"])
    full = client.get(f"/v1/runs/{started.json()['id']}/events", headers=HEADERS)
    assert full.status_code == 200
    assert "id: 0" in full.text
    resumed = client.get(
        f"/v1/runs/{started.json()['id']}/events",
        headers=HEADERS | {"Last-Event-ID": "0"},
    )
    assert "id: 0\n" not in resumed.text
    assert "event:" in resumed.text


def test_closing_the_stream_does_not_cancel_the_run(client: TestClient) -> None:
    incident_id = _create(client)
    executor.pause_runs = True
    started = client.post(
        f"/v1/incidents/{incident_id}/runs",
        json={"scenario_id": "cpu-saturation-checkout"},
        headers=HEADERS,
    )
    run_id = started.json()["id"]

    async def disconnect() -> None:
        response_started = asyncio.Event()
        disconnected = False

        async def receive() -> dict:
            nonlocal disconnected
            await response_started.wait()
            disconnected = True
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                assert message["status"] == 200
                response_started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/v1/runs/{run_id}/events",
            "query_string": b"",
            "headers": [(b"x-api-key", b"dev-key")],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
        # Unlike TestClient's buffered transport, disconnect independently of EOF.
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        assert disconnected
        assert (await get_run(run_id)).status in {"PENDING", "RUNNING"}

    try:
        client.portal.call(disconnect)
    finally:
        executor.pause_runs = False
    finished = _wait(client, run_id)
    assert finished["status"] == "COMPLETED"


def test_stream_drains_event_committed_after_empty_batch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident_id = _create(client)
    # Keep the run active without scheduling a worker.
    monkeypatch.setattr(runs_api, "schedule", lambda run_id: None)
    started = client.post(f"/v1/incidents/{incident_id}/runs", json={}, headers=HEADERS)
    run_id = started.json()["id"]
    original = runs_api.events_after
    raced = False

    async def events_with_race(run_id: str, cursor: int) -> list[RunEventRow]:
        nonlocal raced
        rows = await original(run_id, cursor)
        if not raced:
            assert rows == []
            raced = True
            await cancel_run(run_id)
        return rows

    monkeypatch.setattr(runs_api, "events_after", events_with_race)
    response = client.get(f"/v1/runs/{run_id}/events", headers=HEADERS)
    assert response.status_code == 200
    assert raced
    assert response.text.count("id: 0\n") == 1
    assert "Run cancelled." in response.text


def test_restart_fails_leftover_active_runs() -> None:
    async def body() -> None:
        running_incident = await create_incident(
            IncidentCreate(title="Checkout latency high", description="Shoppers time out.")
        )
        pending_incident = await create_incident(
            IncidentCreate(title="Checkout latency high", description="Shoppers time out.")
        )
        running, _created = await start_run(running_incident.id, None, None)
        pending, _created = await start_run(pending_incident.id, None, None)
        assert await mark_running(running.id)
        changed = await fail_runs_after_restart()
        assert changed == 2
        saved_running = await get_run(running.id)
        saved_pending = await get_run(pending.id)
        assert saved_running.status == "FAILED"
        assert saved_running.failure_reason == "restart"
        assert saved_running.final_report is None
        assert saved_pending.status == "FAILED"
        assert saved_pending.failure_reason == "restart"

    asyncio.run(body())


def test_partial_reports_and_cancelled_reports_follow_the_rule() -> None:
    with pytest.raises(ValueError):
        validate_outcome("TIMED_OUT", None)
    with pytest.raises(ValueError):
        validate_outcome("CANCELLED", {"verdict": "UNKNOWN"})

    async def body() -> None:
        incident = await create_incident(
            IncidentCreate(title="Checkout latency high", description="Shoppers time out.")
        )
        timed_out, _created = await start_run(incident.id, None, None)
        await mark_running(timed_out.id)
        report = DiagnosisReport(
            verdict=Verdict.UNKNOWN,
            unresolved_questions=["Wall-clock timeout reached before completion."],
        ).model_dump(mode="json")
        await finish_run(timed_out.id, "TIMED_OUT", report, "wall_clock_timeout")
        saved = await get_run(timed_out.id)
        assert saved.status == "TIMED_OUT"
        assert saved.final_report == report

        budget, _created = await start_run(incident.id, None, None)
        await mark_running(budget.id)
        await finish_run(budget.id, "BUDGET_EXCEEDED", report, "budget_exceeded")
        saved_budget = await get_run(budget.id)
        assert saved_budget.final_report == report

    asyncio.run(body())
