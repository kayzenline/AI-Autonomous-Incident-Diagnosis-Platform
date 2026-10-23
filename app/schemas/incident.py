"""Incident models for FR-1.

Callers submit IncidentCreate. The server assigns id, status, and archived_at.
Metric values are tool observations, not fields on the incident.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class IncidentStatus(StrEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    DIAGNOSED = "DIAGNOSED"
    RESOLVED = "RESOLVED"


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    service: str | None = None
    occurred_at: datetime | None = None
    detected_at: datetime | None = None
    severity: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Incident(IncidentCreate):
    id: str
    status: IncidentStatus = IncidentStatus.OPEN
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
