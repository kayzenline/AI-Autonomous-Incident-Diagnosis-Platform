"""Tool inputs and the shared ToolResult.

Metric names are a closed enum. A failed call is a ToolResult, not an exception.
Results carry evidence keys and never a relevance flag.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.window import TimeRange


class MetricName(StrEnum):
    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"
    LATENCY_P99 = "latency_p99"
    ERROR_RATE = "error_rate"
    DB_CONNECTIONS = "db_connections"


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str
    evidence_keys: list[str] = Field(default_factory=list)
    truncated: bool = False


class QueryMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    metric: MetricName
    time_range: TimeRange


class SearchLogsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    time_range: TimeRange
    min_level: Literal["ERROR"] = "ERROR"


class ServiceWindowInput(BaseModel):
    """Input for status, changes, dependencies, and database health."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(min_length=1)
    time_range: TimeRange


TOOL_INPUT_MODELS = (
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
)
