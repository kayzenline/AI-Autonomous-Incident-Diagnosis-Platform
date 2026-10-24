"""Score a diagnosis report against ground truth.

Top-1 and Top-3 apply only when the expected verdict is FAULT.
Evidence precision is 0 when the report cites nothing.
"""

from app.core.report import DiagnosisReport
from app.core.taxonomy import Verdict
from app.eval.scenario import GroundTruth


def cited_keys(report: DiagnosisReport) -> set[str]:
    keys: set[str] = set()
    for hypothesis in report.hypotheses:
        keys.update(hypothesis.supporting_evidence)
        keys.update(hypothesis.contradicting_evidence)
    return keys


def top_hit(report: DiagnosisReport, expected: GroundTruth, *, limit: int) -> bool | None:
    if expected.verdict != Verdict.FAULT or expected.cause is None or expected.category is None:
        return None
    accepted = {expected.cause, *expected.acceptable_alternatives}
    for hypothesis in report.hypotheses[:limit]:
        if hypothesis.category == expected.category and hypothesis.cause in accepted:
            return True
    return False


def evidence_recall(cited: set[str], expected_keys: list[str]) -> float | None:
    expected = set(expected_keys)
    if not expected:
        return None
    return len(cited & expected) / len(expected)


def evidence_precision(cited: set[str], expected_keys: list[str]) -> float:
    if not cited:
        return 0.0
    return len(cited & set(expected_keys)) / len(cited)
