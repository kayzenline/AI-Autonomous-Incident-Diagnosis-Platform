"""Evidence Ledger, Hypothesis Board, and Trace Events for the M4 investigation loop.

The Evidence Ledger is the single source of truth for observations gathered
during a run. Duplicate tool calls (same tool + same normalized arguments) are
intercepted by the ledger so the investigator never wastes budget on identical
queries.
"""

import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.taxonomy import CAUSE_CATEGORY, Category, Cause
from app.core.window import TimeRange
from app.tools.contract import ToolResult


class TraceEventType(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    LLM_CALL = "LLM_CALL"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    HYPOTHESIS_UPDATED = "HYPOTHESIS_UPDATED"
    GUARDRAIL_TRIGGERED = "GUARDRAIL_TRIGGERED"
    FINAL_REPORT = "FINAL_REPORT"
    ERROR = "ERROR"


class HypothesisStatus(StrEnum):
    PROPOSED = "PROPOSED"
    LEADING = "LEADING"
    REJECTED = "REJECTED"


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    type: TraceEventType
    created_at: datetime
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_key: str
    tool: str
    service: str
    summary: str
    time_range: TimeRange


class ToolCallEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0)
    tool: str
    arguments: dict[str, Any]
    result: ToolResult


class HypothesisRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: Cause
    category: Category
    cause_summary: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)


class EvidenceLedger:
    """Append-only per-run ledger of tool calls and evidence keys."""

    def __init__(self) -> None:
        self._evidence: dict[str, EvidenceItem] = {}
        self._tool_calls: list[ToolCallEntry] = []
        self._call_cache: dict[str, ToolResult] = {}

    @property
    def tool_calls(self) -> tuple[ToolCallEntry, ...]:
        return tuple(self._tool_calls)

    @property
    def items(self) -> tuple[EvidenceItem, ...]:
        return tuple(self._evidence.values())

    def keys(self) -> tuple[str, ...]:
        return tuple(self._evidence.keys())

    def key_set(self) -> set[str]:
        return set(self._evidence.keys())

    def has_key(self, key: str) -> bool:
        return key in self._evidence

    def find_cached_call(self, tool: str, arguments: dict[str, Any]) -> ToolResult | None:
        signature = _call_signature(tool, arguments)
        return self._call_cache.get(signature)

    def record_tool_call(
        self,
        *,
        tool: str,
        service: str,
        time_range: TimeRange,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> list[EvidenceItem]:
        signature = _call_signature(tool, arguments)
        self._call_cache[signature] = result
        entry = ToolCallEntry(
            seq=len(self._tool_calls),
            tool=tool,
            arguments=arguments,
            result=result,
        )
        self._tool_calls.append(entry)

        added: list[EvidenceItem] = []
        for key in result.evidence_keys:
            if key not in self._evidence:
                item = EvidenceItem(
                    evidence_key=key,
                    tool=tool,
                    service=service,
                    summary=result.summary,
                    time_range=time_range,
                )
                self._evidence[key] = item
                added.append(item)
        return added


class HypothesisBoard:
    """Tracks candidate root-cause hypotheses and their lifecycle during a run."""

    def __init__(self, initial_rejected: set[Cause] | None = None) -> None:
        self._records: dict[Cause, HypothesisRecord] = {}
        for cause in initial_rejected or set():
            self._records[cause] = HypothesisRecord(
                cause=cause,
                category=CAUSE_CATEGORY[cause],
                cause_summary="Previously rejected hypothesis.",
                confidence=0.0,
                status=HypothesisStatus.REJECTED,
            )

    def upsert(
        self,
        *,
        cause: Cause,
        cause_summary: str,
        confidence: float,
        status: HypothesisStatus = HypothesisStatus.PROPOSED,
        supporting_evidence: list[str] | None = None,
        contradicting_evidence: list[str] | None = None,
    ) -> HypothesisRecord:
        existing = self._records.get(cause)
        version = 1 if existing is None else existing.version + 1
        supp = list(
            dict.fromkeys(
                [*(existing.supporting_evidence if existing else []), *(supporting_evidence or [])]
            )
        )
        contra = list(
            dict.fromkeys(
                [
                    *(existing.contradicting_evidence if existing else []),
                    *(contradicting_evidence or []),
                ]
            )
        )
        record = HypothesisRecord(
            cause=cause,
            category=CAUSE_CATEGORY[cause],
            cause_summary=cause_summary,
            confidence=confidence,
            status=status,
            supporting_evidence=supp,
            contradicting_evidence=contra,
            version=version,
        )
        self._records[cause] = record
        return record

    def records(self) -> tuple[HypothesisRecord, ...]:
        return tuple(self._records.values())

    def active_records(self) -> list[HypothesisRecord]:
        active = [rec for rec in self._records.values() if rec.status != HypothesisStatus.REJECTED]
        active.sort(
            key=lambda rec: (
                0 if rec.status == HypothesisStatus.LEADING else 1,
                -rec.confidence,
            )
        )
        return active


def _call_signature(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}"
