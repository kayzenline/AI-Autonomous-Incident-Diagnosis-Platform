"""Exercise the deployed CLI/API and restart path, optionally on a dedicated Postgres DB."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.eval.scenario import frozen_dir, load_scenario
from app.persistence.models import EvidenceRow, RunEventRow, ToolCallRow
from app.persistence.session import dispose_engine, get_sessionmaker, reset_engine
from app.persistence.store import create_incident, mark_running, start_run
from app.schemas.incident import IncidentCreate
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-API-Key": "acceptance-test-key"}


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Overrides the SQLite-only fixture in conftest for this module.
    url = os.getenv("M2_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("API_KEY", HEADERS["X-API-Key"])
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    reset_engine()
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    yield
    reset_engine()


@contextmanager
def api_process(log_path: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", headers=HEADERS, timeout=10
            ) as client:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("API startup timed out")
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def wait_finished(client: httpx.Client, run_id: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        response.raise_for_status()
        if response.json()["status"] not in {"PENDING", "RUNNING"}:
            return response.json()
        time.sleep(0.05)
    raise AssertionError("Diagnosis timed out")


async def seed_active_runs() -> list[tuple[str, str, str]]:
    """Leave both pending and running rows, as an interrupted worker would."""
    try:
        rows = []
        for state in ("PENDING", "RUNNING"):
            incident = await create_incident(
                IncidentCreate(title="Restart acceptance", description="Interrupted work")
            )
            key = f"restart-{state}"
            run, created = await start_run(incident.id, key, None)
            assert created
            if state == "RUNNING":
                assert await mark_running(run.id)
            rows.append((incident.id, run.id, key))
        return rows
    finally:
        await dispose_engine()


async def assert_evidence_links(run_id: str) -> None:
    try:
        async with get_sessionmaker()() as session:
            events = list(
                await session.scalars(select(RunEventRow).where(RunEventRow.run_id == run_id))
            )
            calls = list(
                await session.scalars(select(ToolCallRow).where(ToolCallRow.run_id == run_id))
            )
            evidence = list(
                await session.scalars(select(EvidenceRow).where(EvidenceRow.run_id == run_id))
            )
        assert calls and evidence
        result_ids = {row.id for row in events if row.type == "TOOL_RESULT"}
        assert {row.event_id for row in calls} <= result_ids
        assert {row.tool_call_id for row in evidence} <= {row.id for row in calls}
    finally:
        await dispose_engine()


def test_migrated_database_cli_sse_and_process_restart(tmp_path: Path) -> None:
    scenario = load_scenario(frozen_dir() / "cpu-saturation-checkout.yaml")
    payload = scenario.incident.model_dump(mode="json")
    with api_process(tmp_path / "first.log") as client:
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.cli",
                "diagnose",
                "--scenario",
                scenario.id,
                "--api",
                str(client.base_url),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert cli.returncode == 0, cli.stderr
        assert "RUN_STARTED" in cli.stdout
        assert "cpu_saturation" in cli.stdout

        created = client.post("/v1/incidents", json=payload)
        assert created.status_code == 201
        incident_id = created.json()["id"]
        run_url = f"/v1/incidents/{incident_id}/runs"
        key = {"Idempotency-Key": "persisted-completed-run"}
        started = client.post(run_url, json={"scenario_id": scenario.id}, headers=key)
        assert started.status_code == 202
        run_id = started.json()["id"]
        finished = wait_finished(client, run_id)
        assert finished["status"] == "COMPLETED"
        assert finished["final_report"]["hypotheses"][0]["cause"] == "cpu_saturation"
        events = client.get(f"/v1/runs/{run_id}/events")
        assert events.status_code == 200
        assert "id: 0\n" in events.text
        assert "FINAL_REPORT" in events.text
        asyncio.run(assert_evidence_links(run_id))

        active = asyncio.run(seed_active_runs())
        for active_incident, active_run, active_key in active:
            url = f"/v1/incidents/{active_incident}/runs"
            replay = client.post(url, json={}, headers={"Idempotency-Key": active_key})
            assert replay.status_code == 202
            assert replay.json()["id"] == active_run
            for headers in ({}, {"Idempotency-Key": "other"}):
                assert client.post(url, json={}, headers=headers).status_code == 409

    # A different interpreter initializes lifespan against the SAME migrated database.
    with api_process(tmp_path / "restarted.log") as client:
        assert client.get(f"/v1/incidents/{incident_id}").status_code == 200
        saved = client.get(f"/v1/runs/{run_id}").json()
        assert saved == finished
        replay = client.post(run_url, json={"scenario_id": scenario.id}, headers=key)
        assert replay.status_code == 200
        assert replay.json()["id"] == run_id
        assert client.get(f"/v1/runs/{run_id}/events").text == events.text
        resumed = client.get(f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": "0"})
        assert resumed.status_code == 200
        assert "id: 0\n" not in resumed.text
        assert "FINAL_REPORT" in resumed.text
        for active_incident, active_run, active_key in active:
            saved = client.get(f"/v1/runs/{active_run}").json()
            assert saved["status"] == "FAILED"
            assert saved["failure_reason"] == "restart"
            assert saved["final_report"] is None
            replay = client.post(
                f"/v1/incidents/{active_incident}/runs",
                json={},
                headers={"Idempotency-Key": active_key},
            )
            assert replay.status_code == 200
            assert replay.json()["id"] == active_run
        assert client.delete(f"/v1/incidents/{incident_id}").status_code == 204
        assert client.post(run_url, json={}).status_code == 409
