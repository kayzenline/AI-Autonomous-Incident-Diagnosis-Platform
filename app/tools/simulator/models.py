"""World model consumed by the simulator.

This model has no ground_truth field. Extra keys are rejected so an answer
key cannot be smuggled in beside the observations.
"""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.tools.contract import MetricName

_EVIDENCE_KEY = re.compile(r"^[a-z]+:[a-z0-9_]+$")


def _utc(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("expected a timestamp")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, BeforeValidator(_utc)]


def _evidence_key(value: str | None) -> str | None:
    if value is not None and _EVIDENCE_KEY.fullmatch(value) is None:
        raise ValueError(f"invalid evidence key: {value}")
    return value


EvidenceKey = Annotated[str | None, BeforeValidator(_evidence_key)]


class MetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: UtcDateTime
    value: float


class MetricSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    metric: MetricName
    evidence_key: EvidenceKey = None
    points: list[MetricPoint] = Field(default_factory=list)


class LogRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    at: UtcDateTime
    level: Literal["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"]
    message: str
    evidence_key: EvidenceKey = None


class ChangeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    at: UtcDateTime
    kind: Literal["deploy", "config", "scale"]
    summary: str
    evidence_key: EvidenceKey = None


class DependencyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    name: str
    status: Literal["healthy", "unhealthy", "timeout", "down"]
    latency_ms: float | None = None
    evidence_key: EvidenceKey = None


class DatabaseRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    connections_pct: float = Field(ge=0, le=100)
    waiters: int = Field(default=0, ge=0)
    evidence_key: EvidenceKey = None


class StatusRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    request_rate_ratio: float = Field(ge=0)
    evidence_key: EvidenceKey = None


class World(BaseModel):
    model_config = ConfigDict(extra="forbid")

    services: list[str] = Field(min_length=1)
    metrics_unavailable: bool = False
    metrics_error_key: EvidenceKey = None
    metrics: list[MetricSeries] = Field(default_factory=list)
    logs: list[LogRecord] = Field(default_factory=list)
    changes: list[ChangeRecord] = Field(default_factory=list)
    dependencies: list[DependencyRecord] = Field(default_factory=list)
    databases: list[DatabaseRecord] = Field(default_factory=list)
    statuses: list[StatusRecord] = Field(default_factory=list)
