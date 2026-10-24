"""Structured diagnosis report.

Confidence is display-only. Non-fault verdicts leave cause, category, and severity empty.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.taxonomy import CAUSE_CATEGORY, Category, Cause, Verdict

Severity = Literal["low", "medium", "high"]
Risk = Literal["low", "med", "high"]


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: Cause
    cause_summary: str
    category: Category
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def cause_matches_category(self) -> Self:
        expected = CAUSE_CATEGORY[self.cause]
        if self.category != expected:
            raise ValueError(f"{self.cause} pairs with {expected}, not {self.category}")
        return self


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1)
    risk: Risk
    requires_approval: bool = True


class DiagnosisReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypotheses: list[Hypothesis] = Field(default_factory=list)
    verdict: Verdict
    severity: Severity | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def non_fault_is_empty(self) -> Self:
        if self.verdict != Verdict.FAULT and (self.hypotheses or self.severity is not None):
            raise ValueError("NOT_A_FAULT and UNKNOWN reports omit hypotheses and severity")
        return self
