from app.core.report import DiagnosisReport, Hypothesis, Recommendation
from app.core.taxonomy import Category, Cause, Verdict
from app.eval.scenario import GroundTruth
from app.eval.score import evidence_precision, evidence_recall, top_hit


def _report(*causes: tuple[Cause, list[str]]) -> DiagnosisReport:
    return DiagnosisReport(
        verdict=Verdict.FAULT,
        severity="high",
        hypotheses=[
            Hypothesis(
                cause=cause,
                cause_summary="summary",
                category=Category.CPU if cause == Cause.CPU_SATURATION else Category.DISK,
                confidence=0.5,
                supporting_evidence=keys,
            )
            for cause, keys in causes
        ],
        recommendations=[
            Recommendation(action="Review the cited evidence.", risk="low", requires_approval=True)
        ],
    )


def _expected() -> GroundTruth:
    return GroundTruth(
        verdict=Verdict.FAULT,
        category=Category.CPU,
        cause=Cause.CPU_SATURATION,
        key_evidence_keys=["metric:cpu_high"],
        severity="high",
    )


def test_top1_and_top3_follow_hypothesis_order() -> None:
    expected = _expected()
    wrong_first = _report(
        (Cause.DISK_FULL, ["metric:disk_high"]),
        (Cause.CPU_SATURATION, ["metric:cpu_high"]),
    )

    assert top_hit(wrong_first, expected, limit=1) is False
    assert top_hit(wrong_first, expected, limit=3) is True


def test_non_fault_skips_top_scores() -> None:
    expected = GroundTruth(verdict=Verdict.NOT_A_FAULT, key_evidence_keys=[])
    report = DiagnosisReport(verdict=Verdict.NOT_A_FAULT)

    assert top_hit(report, expected, limit=1) is None
    assert evidence_precision(set(), []) == 0
    assert evidence_recall(set(), []) is None


def test_red_herring_key_lowers_precision() -> None:
    cited = {"metric:cpu_high", "metric:disk_high"}

    assert evidence_recall(cited, ["metric:cpu_high"]) == 1
    assert evidence_precision(cited, ["metric:cpu_high"]) == 0.5
