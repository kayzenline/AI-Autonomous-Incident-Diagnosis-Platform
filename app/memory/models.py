"""Data models for M6 episodic memory and semantic service knowledge (FR-7, FR-9).

No procedural memory is stored; working state for a single run remains in
EvidenceLedger and HypothesisBoard.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.core.taxonomy import Category, Cause


class MemoryLayer(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class TrustLevel(StrEnum):
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class FeedbackVerdict(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARTIAL = "partial"


class MemoryConfig(BaseModel):
    """Configurable thresholds and weights for deduplication and hybrid retrieval (§9)."""

    model_config = ConfigDict(extra="forbid")

    similarity_dedup_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    half_life_days: float = Field(default=30.0, gt=0.0)
    vector_weight: float = Field(default=0.55, ge=0.0, le=1.0)
    bm25_weight: float = Field(default=0.45, ge=0.0, le=1.0)
    confirmed_trust_weight: float = Field(default=1.0, ge=0.0)
    candidate_trust_weight: float = Field(default=0.65, ge=0.0)
    rejected_trust_weight: float = Field(default=0.25, ge=0.0)
    stale_version_multiplier: float = Field(default=0.35, ge=0.0, le=1.0)
    matching_version_multiplier: float = Field(default=1.15, ge=1.0)
    enabled_layers: tuple[MemoryLayer, ...] = (MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC)

    def trust_weight(self, level: TrustLevel) -> float:
        if level == TrustLevel.CONFIRMED:
            return self.confirmed_trust_weight
        if level == TrustLevel.CANDIDATE:
            return self.candidate_trust_weight
        return self.rejected_trust_weight


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: MemoryLayer
    content: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    service: str = Field(min_length=1)
    category: Category | None = None
    cause: Cause | None = None
    trust_level: TrustLevel = TrustLevel.CANDIDATE
    source_run_id: str | None = None
    source_feedback_id: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    service_version: str | None = None
    created_at: datetime | None = None


class MemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    layer: MemoryLayer
    content: str
    summary: str
    service: str
    category: Category | None = None
    cause: Cause | None = None
    trust_level: TrustLevel
    source_run_id: str | None = None
    source_feedback_id: str | None = None
    embedding: list[float] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    service_version: str | None = None
    usage_count: int = Field(default=1, ge=1)
    helpful_count: int = Field(default=0, ge=0)
    last_used_at: datetime | None = None
    superseded_by: str | None = None
    created_at: datetime


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="")
    service: str | None = None
    layers: tuple[MemoryLayer, ...] = (MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC)
    include_candidate: bool = False
    include_rejected: bool = False
    service_version: str | None = None
    reference_time: datetime | None = None
    limit: int = Field(default=5, ge=1, le=50)


class ScoredMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: MemoryItem
    score: float
    similarity_score: float
    time_decay_weight: float
    trust_weight: float
    version_weight: float
    stale: bool = False


class MemoryRecallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    layer: MemoryLayer
    service: str
    category: Category | None = None
    cause: Cause | None = None
    trust_level: TrustLevel
    service_version: str | None = None
    stale: bool = False
    score: float
    injected: bool = True
    cited: bool = False
    summary: str = ""
    content: str = ""


class PriorRunContext(BaseModel):
    """Inherited context across runs for the same incident (FR-7)."""

    model_config = ConfigDict(extra="forbid")

    prior_run_ids: tuple[str, ...] = ()
    rejected_causes: tuple[Cause, ...] = ()
    evidence_summaries: tuple[dict[str, str], ...] = ()
