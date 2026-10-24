"""Score a diagnosis report against ground truth.

Top-1 and Top-3 apply only when the expected verdict is FAULT.
Evidence precision is undefined when nothing was expected and nothing was cited.
"""

from app.core.report import DiagnosisReport, cited_keys
from app.core.taxonomy import Verdict
from app.eval.scenario import GroundTruth

__all__ = ["cited_keys", "evidence_precision", "evidence_recall", "top_hit"]


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


def evidence_precision(cited: set[str], expected_keys: list[str]) -> float | None:
    """Return precision, or None when both sides are empty.

    Recall is already undefined when the expected set is empty. Precision is
    the same for a report that correctly cites nothing (`NOT_A_FAULT` /
    `UNKNOWN` with no key evidence): averaging a zero would punish that
    abstention. Citing nothing when keys were expected is a real miss and
    stays 0. Citing keys when none were expected is also 0.
    """

    expected = set(expected_keys)
    if not cited and not expected:
        return None
    if not cited:
        return 0.0
    return len(cited & expected) / len(cited)
