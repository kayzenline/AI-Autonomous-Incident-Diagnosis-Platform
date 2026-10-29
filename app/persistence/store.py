"""Incident and run writes.

A second active run, and a run on an archived incident, are conflicts.
The same idempotency key returns the row that already exists.
"""

from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.ledger import TraceEvent
from app.core.clock import WallClock
from app.core.window import _as_utc
from app.persistence.models import EvidenceRow, IncidentRow, RunEventRow, RunRow, ToolCallRow
from app.persistence.outcomes import validate_outcome
from app.persistence.session import ensure_ready, get_sessionmaker
from app.schemas.incident import IncidentCreate

AGENT_VERSION = "m4"
PROMPT_VERSION = "m4-investigator-v1"
MODEL_NAME = "fake-m4-investigator-v1"


class StoreError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _stamp(moment: object) -> object:
    if moment is None:
        return None
    return _as_utc(moment)  # type: ignore[arg-type]


async def create_incident(body: IncidentCreate) -> IncidentRow:
    await ensure_ready()
    now = WallClock().now()
    row = IncidentRow(
        id=str(uuid4()),
        title=body.title,
        description=body.description,
        service=body.service,
        occurred_at=_stamp(body.occurred_at),
        detected_at=_stamp(body.detected_at),
        status="OPEN",
        user_severity=body.user_severity,
        details=dict(body.metadata),
        archived_at=None,
        created_at=now,
        updated_at=now,
    )
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            session.add(row)
        return row


async def list_incidents(limit: int, offset: int) -> list[IncidentRow]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        rows = await session.scalars(
            select(IncidentRow)
            .where(IncidentRow.archived_at.is_(None))
            .order_by(IncidentRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows)


async def get_incident(incident_id: str) -> IncidentRow:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        row = await session.get(IncidentRow, incident_id)
        if row is None:
            raise StoreError(404, "Incident not found.")
        return row


async def patch_incident(incident_id: str, changes: dict[str, Any]) -> IncidentRow:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            row = await session.get(IncidentRow, incident_id)
            if row is None:
                raise StoreError(404, "Incident not found.")
            if row.archived_at is not None:
                raise StoreError(409, "Incident is archived.")
            for key, value in changes.items():
                if key == "metadata":
                    row.details = dict(value)
                elif key in {"occurred_at", "detected_at"}:
                    setattr(row, key, _stamp(value))
                else:
                    setattr(row, key, value)
            row.updated_at = now
            return row


async def archive_incident(incident_id: str) -> None:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            row = await session.get(IncidentRow, incident_id)
            if row is None:
                raise StoreError(404, "Incident not found.")
            if row.archived_at is None:
                row.archived_at = now
                row.updated_at = now


async def start_run(
    incident_id: str,
    idempotency_key: str | None,
    scenario_id: str | None,
) -> tuple[RunRow, bool]:
    await ensure_ready()
    factory = get_sessionmaker()
    try:
        async with factory() as session:
            async with session.begin():
                return await _insert_run(session, incident_id, idempotency_key, scenario_id)
    except IntegrityError:
        return await _resolve_race(incident_id, idempotency_key)


async def _insert_run(
    session: object,
    incident_id: str,
    idempotency_key: str | None,
    scenario_id: str | None,
) -> tuple[RunRow, bool]:
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(session, AsyncSession)
    incident = await session.get(IncidentRow, incident_id)
    if incident is None:
        raise StoreError(404, "Incident not found.")
    if idempotency_key:
        existing = await session.scalar(
            select(RunRow).where(
                RunRow.incident_id == incident_id,
                RunRow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing, False
    if incident.archived_at is not None:
        raise StoreError(409, "Incident is archived.")
    active = await session.scalar(
        select(RunRow).where(
            RunRow.incident_id == incident_id,
            RunRow.status.in_(("PENDING", "RUNNING")),
        )
    )
    if active is not None:
        raise StoreError(409, "Another run is still active.")
    now = WallClock().now()
    run = RunRow(
        id=str(uuid4()),
        incident_id=incident_id,
        idempotency_key=idempotency_key,
        status="PENDING",
        agent_version=AGENT_VERSION,
        model=MODEL_NAME,
        prompt_version=PROMPT_VERSION,
        config={
            "executor": "m4",
            "scenario_id": scenario_id,
            "llm": MODEL_NAME,
            "timeout_seconds": 60,
        },
        started_at=None,
        ended_at=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        final_report=None,
        failure_reason=None,
        created_at=now,
    )
    session.add(run)
    await session.flush()
    return run, True


async def _resolve_race(incident_id: str, idempotency_key: str | None) -> tuple[RunRow, bool]:
    factory = get_sessionmaker()
    async with factory() as session:
        if idempotency_key:
            existing = await session.scalar(
                select(RunRow).where(
                    RunRow.incident_id == incident_id,
                    RunRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing, False
        active = await session.scalar(
            select(RunRow).where(
                RunRow.incident_id == incident_id,
                RunRow.status.in_(("PENDING", "RUNNING")),
            )
        )
        if active is not None:
            raise StoreError(409, "Another run is still active.")
    raise StoreError(409, "Run was not created.")


async def get_run(run_id: str) -> RunRow:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        row = await session.get(RunRow, run_id)
        if row is None:
            raise StoreError(404, "Run not found.")
        return row


async def mark_running(run_id: str) -> bool:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None or run.status != "PENDING":
                return False
            run.status = "RUNNING"
            run.started_at = now
            incident = await session.get(IncidentRow, run.incident_id)
            if incident is not None and incident.status == "OPEN":
                incident.status = "INVESTIGATING"
                incident.updated_at = now
            return True


async def append_event(run_id: str, event: TraceEvent) -> None:
    """Write one timeline row. A tool result also writes its call and evidence."""

    await ensure_ready()
    payload = {"summary": event.summary, **event.payload}
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            row = RunEventRow(
                id=str(uuid4()),
                run_id=run_id,
                seq=event.seq,
                type=event.type.value,
                payload=payload,
                created_at=event.created_at,
            )
            session.add(row)
            if event.type.value != "TOOL_RESULT":
                return
            await session.flush()
            arguments = payload.get("arguments")
            output = payload.get("output")
            call = ToolCallRow(
                id=str(uuid4()),
                run_id=run_id,
                event_id=row.id,
                tool=str(payload.get("tool") or ""),
                input=arguments if isinstance(arguments, dict) else {},
                output=output if isinstance(output, dict) else {},
                status=str(payload.get("status") or ""),
                latency_ms=_latency(payload.get("latency_ms")),
            )
            session.add(call)
            await session.flush()
            keys = payload.get("evidence_keys")
            if not isinstance(keys, list):
                return
            window = payload.get("time_range")
            summary = str(event.summary)[:500]
            for key in keys:
                if not isinstance(key, str) or not key:
                    continue
                session.add(
                    EvidenceRow(
                        id=str(uuid4()),
                        run_id=run_id,
                        tool_call_id=call.id,
                        evidence_key=key,
                        summary=summary,
                        excerpt=summary,
                        time_range=window if isinstance(window, dict) else None,
                    )
                )


def _latency(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def events_after(run_id: str, seq: int) -> list[RunEventRow]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        rows = await session.scalars(
            select(RunEventRow)
            .where(RunEventRow.run_id == run_id, RunEventRow.seq > seq)
            .order_by(RunEventRow.seq)
        )
        return list(rows)


async def finish_run(
    run_id: str,
    status: str,
    report: dict[str, Any] | None,
    failure_reason: str | None,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    validate_outcome(status, report)
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None or run.status not in {"PENDING", "RUNNING"}:
                return
            run.status = status
            run.final_report = report
            run.failure_reason = failure_reason
            run.ended_at = now
            run.tokens_in = tokens_in
            run.tokens_out = tokens_out
            run.cost_usd = cost_usd
            incident = await session.get(IncidentRow, run.incident_id)
            if incident is None or incident.status == "RESOLVED":
                return
            if report is not None and status in {"COMPLETED", "TIMED_OUT", "BUDGET_EXCEEDED"}:
                incident.status = "DIAGNOSED"
                incident.updated_at = now


async def cancel_run(run_id: str) -> RunRow:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None:
                raise StoreError(404, "Run not found.")
            if run.status not in {"PENDING", "RUNNING"}:
                return run
            run.status = "CANCELLED"
            run.final_report = None
            run.failure_reason = "cancelled"
            run.ended_at = now
            last = await session.scalar(
                select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)
            )
            session.add(
                RunEventRow(
                    id=str(uuid4()),
                    run_id=run_id,
                    seq=0 if last is None else int(last) + 1,
                    type="ERROR",
                    payload={"summary": "Run cancelled.", "reason": "cancelled"},
                    created_at=now,
                )
            )
            return run


async def fail_runs_after_restart() -> int:
    """Mark leftover active runs failed. A dead process will not resume them."""

    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            rows = list(
                await session.scalars(
                    select(RunRow).where(RunRow.status.in_(("PENDING", "RUNNING")))
                )
            )
            for run in rows:
                run.status = "FAILED"
                run.failure_reason = "restart"
                run.ended_at = now
                run.final_report = None
                last = await session.scalar(
                    select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run.id)
                )
                session.add(
                    RunEventRow(
                        id=str(uuid4()),
                        run_id=run.id,
                        seq=0 if last is None else int(last) + 1,
                        type="ERROR",
                        payload={"summary": "Process restarted.", "reason": "restart"},
                        created_at=now,
                    )
                )
            return len(rows)
