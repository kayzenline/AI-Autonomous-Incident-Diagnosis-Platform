"""Incident CRUD.

The caller can set RESOLVED. The executor moves OPEN to INVESTIGATING and
then to DIAGNOSED. DELETE sets archived_at and does not add a status.
"""

from datetime import datetime
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_api_key
from app.persistence.models import IncidentRow
from app.persistence.store import (
    StoreError,
    archive_incident,
    create_incident,
    get_incident,
    patch_incident,
)
from app.persistence.store import list_incidents as list_incident_rows
from app.schemas.incident import Incident, IncidentCreate, IncidentStatus

router = APIRouter(prefix="/v1", tags=["incidents"])


class IncidentPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Incident]
    limit: int
    offset: int


class IncidentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    service: str | None = None
    occurred_at: datetime | None = None
    detected_at: datetime | None = None
    user_severity: str | None = None
    metadata: dict[str, Any] | None = None
    status: Literal["RESOLVED"] | None = None


def _public(row: IncidentRow) -> Incident:
    return Incident(
        id=row.id,
        title=row.title,
        description=row.description,
        service=row.service,
        occurred_at=row.occurred_at,
        detected_at=row.detected_at,
        user_severity=row.user_severity,
        metadata=dict(row.details or {}),
        status=IncidentStatus(row.status),
        archived_at=row.archived_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _raise(exc: StoreError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/incidents", response_model=Incident, status_code=201)
async def post_incident(
    body: IncidentCreate,
    _: None = Depends(require_api_key),
) -> Incident:
    return _public(await create_incident(body))


@router.get("/incidents", response_model=IncidentPage)
async def get_incidents(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_api_key),
) -> IncidentPage:
    rows = await list_incident_rows(limit, offset)
    return IncidentPage(items=[_public(row) for row in rows], limit=limit, offset=offset)


@router.get("/incidents/{incident_id}", response_model=Incident)
async def get_one_incident(
    incident_id: str,
    _: None = Depends(require_api_key),
) -> Incident:
    try:
        return _public(await get_incident(incident_id))
    except StoreError as exc:
        _raise(exc)


@router.patch("/incidents/{incident_id}", response_model=Incident)
async def patch_one_incident(
    incident_id: str,
    body: IncidentPatch,
    _: None = Depends(require_api_key),
) -> Incident:
    changes = body.model_dump(exclude_unset=True)
    try:
        return _public(await patch_incident(incident_id, changes))
    except StoreError as exc:
        _raise(exc)


@router.delete("/incidents/{incident_id}", status_code=204)
async def delete_incident(
    incident_id: str,
    _: None = Depends(require_api_key),
) -> Response:
    try:
        await archive_incident(incident_id)
    except StoreError as exc:
        _raise(exc)
    return Response(status_code=204)
