"""Read-only Verifier for FR-4 and FR-5.

The Verifier is NOT a second investigator:
- It has no reference to `ToolPort` or `AsyncToolPort` and cannot call any tools.
- It reads only this run's `EvidenceLedger` and `HypothesisBoard`.
- It runs once per report validation pass to verify that:
  (a) Every cited `evidence_key` in the report exists in `EvidenceLedger`.
  (b) The report conforms to the FR-5 `DiagnosisReport` schema (including
      `cause`/`category` pairing and empty fields on `NOT_A_FAULT`/`UNKNOWN`).
  (c) Any contradicting evidence recorded on the `HypothesisBoard` against the
      Top-1 hypothesis is surfaced on the report.
"""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.ledger import EvidenceLedger, HypothesisBoard, HypothesisStatus
from app.core.report import DiagnosisReport, cited_keys


class VerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    report: DiagnosisReport | None = None
    illegal_keys: tuple[str, ...] = ()
    illegal_citation_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    contradictions_noted: tuple[str, ...] = ()
    error: str | None = None


class ReadOnlyVerifier:
    """Single-pass read-only verifier bound strictly to the run's ledger."""

    def __init__(self, ledger: EvidenceLedger, board: HypothesisBoard | None = None) -> None:
        self._ledger = ledger
        self._board = board

    def verify_raw(self, raw_content: str) -> VerificationResult:
        ledger_set = self._ledger.key_set()
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            return VerificationResult(
                valid=False,
                error=f"Invalid JSON: {exc.msg}",
            )

        raw_cited = _extract_raw_citations(parsed)
        raw_illegal = tuple(sorted(raw_cited - ledger_set))
        raw_rate = round(len(raw_illegal) / len(raw_cited), 4) if raw_cited else 0.0

        try:
            report = DiagnosisReport.model_validate(parsed)
        except ValidationError as exc:
            return VerificationResult(
                valid=False,
                illegal_keys=raw_illegal,
                illegal_citation_rate=raw_rate,
                error=f"Schema validation error: {exc}",
            )

        return self.verify_report(report)

    def verify_report(self, report: DiagnosisReport) -> VerificationResult:
        ledger_set = self._ledger.key_set()
        cited = cited_keys(report)
        illegal = tuple(sorted(cited - ledger_set))
        if illegal:
            rate = round(len(illegal) / len(cited), 4) if cited else 0.0
            return VerificationResult(
                valid=False,
                illegal_keys=illegal,
                illegal_citation_rate=rate,
                error=f"Cited evidence keys not in ledger: {list(illegal)}",
            )

        contradictions: list[str] = []
        if report.hypotheses and self._board is not None:
            top = report.hypotheses[0]
            for rec in self._board.records():
                if rec.cause == top.cause:
                    if rec.status == HypothesisStatus.REJECTED:
                        return VerificationResult(
                            valid=False,
                            error=(
                                f"Top hypothesis {top.cause.value} is marked "
                                "REJECTED on HypothesisBoard."
                            ),
                        )
                    for key in rec.contradicting_evidence:
                        if key in ledger_set and key not in top.contradicting_evidence:
                            top.contradicting_evidence.append(key)
                            contradictions.append(key)
                elif rec.status != HypothesisStatus.REJECTED and rec.supporting_evidence:
                    for key in rec.supporting_evidence:
                        if key in ledger_set and key not in top.supporting_evidence:
                            contradictions.append(key)
            if contradictions and not report.unresolved_questions:
                report.unresolved_questions.append(
                    "Ledger also contains competing signals: "
                    + ", ".join(dict.fromkeys(contradictions))
                )

        return VerificationResult(
            valid=True,
            report=report,
            illegal_keys=(),
            illegal_citation_rate=0.0,
            contradictions_noted=tuple(dict.fromkeys(contradictions)),
        )


def _extract_raw_citations(parsed: object) -> set[str]:
    if not isinstance(parsed, dict):
        return set()
    hypotheses = parsed.get("hypotheses")
    if not isinstance(hypotheses, list):
        return set()
    keys: set[str] = set()
    for item in hypotheses:
        if not isinstance(item, dict):
            continue
        for field_name in ("supporting_evidence", "contradicting_evidence"):
            values = item.get(field_name)
            if isinstance(values, list):
                for val in values:
                    if isinstance(val, str):
                        keys.add(val)
    return keys
