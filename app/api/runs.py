"""Create a run, read it, stream its events, cancel it, or submit feedback.

A new run is 202. The same idempotency key is 202 while the run is active
and 200 once it is terminal. A second active run is 409.
"""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from app.api.auth import require_api_key
from app.core.taxonomy import Cause
from app.executor.service import request_cancel, resolve_scenario_path, schedule
from app.memory.models import FeedbackVerdict
from app.persistence.models import RunRow
from app.persistence.outcomes import ACTIVE
from app.persistence.store import (
    StoreError,
    cancel_run,
    events_after,
    get_run,
    record_run_feedback,
    start_run,
)

router = APIRouter(prefix="/v1", tags=["runs"])


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str | None = None
    inherit_prior_runs: bool = True
    is_eval: bool = False


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: FeedbackVerdict
    true_cause: Cause | None = None
    notes: str | None = None


class RunPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    incident_id: str
    status: str
    agent_version: str
    model: str | None
    prompt_version: str | None
    config: dict[str, Any]
    final_report: dict[str, Any] | None
    failure_reason: str | None


def _public(row: RunRow) -> dict[str, object]:
    public_config = {k: v for k, v in (row.config or {}).items() if not str(k).startswith("_")}
    return RunPublic(
        id=row.id,
        incident_id=row.incident_id,
        status=row.status,
        agent_version=row.agent_version,
        model=row.model,
        prompt_version=row.prompt_version,
        config=public_config,
        final_report=row.final_report,
        failure_reason=row.failure_reason,
    ).model_dump(mode="json")


def _http(exc: StoreError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post("/incidents/{incident_id}/runs")
async def post_run(
    incident_id: str,
    body: RunCreate | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_api_key),
) -> JSONResponse:
    scenario_id = None if body is None else body.scenario_id
    inherit_prior = True if body is None else body.inherit_prior_runs
    is_eval = False if body is None else body.is_eval
    if scenario_id is not None and resolve_scenario_path(scenario_id) is None:
        raise HTTPException(status_code=422, detail="Unknown scenario_id.")
    key = idempotency_key.strip() if idempotency_key else None
    if key == "":
        key = None
    try:
        run, created = await start_run(
            incident_id,
            key,
            scenario_id,
            inherit_prior_runs=inherit_prior,
            is_eval=is_eval,
        )
    except StoreError as exc:
        raise _http(exc) from exc
    if created:
        schedule(run.id)
        await asyncio.sleep(0)
        run = await get_run(run.id)
    status_code = 202 if created or run.status in ACTIVE else 200
    return JSONResponse(status_code=status_code, content=_public(run))


@router.get("/runs/{run_id}")
async def get_one_run(run_id: str, _: None = Depends(require_api_key)) -> JSONResponse:
    try:
        run = await get_run(run_id)
    except StoreError as exc:
        raise _http(exc) from exc
    return JSONResponse(status_code=200, content=_public(run))


@router.get("/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _: None = Depends(require_api_key),
) -> StreamingResponse:
    try:
        await get_run(run_id)
    except StoreError as exc:
        raise _http(exc) from exc
    try:
        after = -1 if last_event_id is None else int(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Last-Event-ID must be an integer.") from exc

    async def stream() -> object:
        cursor = after
        while True:
            if await request.is_disconnected():
                return
            # Observe completion before fetching its committed final events.
            current = await get_run(run_id)
            rows = await events_after(run_id, cursor)
            for row in rows:
                cursor = row.seq
                payload = row.payload if isinstance(row.payload, dict) else {}
                data = json.dumps(
                    {
                        "seq": row.seq,
                        "type": row.type,
                        "summary": payload.get("summary", ""),
                        "payload": payload,
                        "created_at": row.created_at.isoformat(),
                    },
                    default=str,
                )
                yield f"id: {row.seq}\nevent: {row.type}\ndata: {data}\n\n"
            if current.status not in ACTIVE and not rows:
                return
            await asyncio.sleep(0.05)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def post_cancel(run_id: str, _: None = Depends(require_api_key)) -> JSONResponse:
    request_cancel(run_id)
    try:
        run = await cancel_run(run_id)
    except StoreError as exc:
        raise _http(exc) from exc
    return JSONResponse(status_code=200, content=_public(run))


@router.post("/runs/{run_id}/feedback")
async def post_feedback(
    run_id: str,
    body: FeedbackCreate,
    _: None = Depends(require_api_key),
) -> JSONResponse:
    try:
        result = await record_run_feedback(
            run_id,
            body.verdict,
            body.true_cause,
            body.notes,
        )
    except StoreError as exc:
        raise _http(exc) from exc
    return JSONResponse(status_code=200, content=result)
