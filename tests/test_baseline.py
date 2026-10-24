from datetime import UTC, datetime

import pytest
from app.agent.baseline import RULE_ORDER, diagnose
from app.core.report import DiagnosisReport, Hypothesis
from app.core.taxonomy import Category, Cause, Verdict
from app.eval.fairness import assert_baseline_is_isolated
from app.eval.scenario import GroundTruth
from app.schemas.incident import IncidentCreate
from app.tools.contract import MetricName, ToolResult, ToolStatus
from app.tools.simulator.adapter import SimulatorTools
from app.tools.simulator.models import MetricPoint, MetricSeries, StatusRecord, World
from pydantic import ValidationError

_WHEN = datetime(2026, 10, 23, 3, 0, tzinfo=UTC)
_DETECTED = datetime(2026, 10, 23, 3, 10, tzinfo=UTC)


class _Spy:
    def __init__(self) -> None:
        self.calls = 0

    def query_metrics(self, request: object) -> ToolResult:
        del request
        self.calls += 1
        return ToolResult(status=ToolStatus.OK, summary="unused")

    def search_logs(self, request: object) -> ToolResult:
        return self.query_metrics(request)

    def get_service_status(self, request: object) -> ToolResult:
        return self.query_metrics(request)

    def get_recent_changes(self, request: object) -> ToolResult:
        return self.query_metrics(request)

    def get_dependency_health(self, request: object) -> ToolResult:
        return self.query_metrics(request)

    def query_db_health(self, request: object) -> ToolResult:
        return self.query_metrics(request)


def _incident(**overrides: object) -> IncidentCreate:
    payload: dict[str, object] = {
        "title": "Checkout latency high",
        "description": "Shoppers time out while paying.",
        "service": "checkout",
        "occurred_at": _WHEN,
        "detected_at": _DETECTED,
        "user_severity": "high",
    }
    payload.update(overrides)
    return IncidentCreate.model_validate(payload)


def _cpu_world() -> World:
    return World(
        services=["checkout"],
        metrics=[
            MetricSeries(
                service="checkout",
                metric=metric,
                evidence_key="metric:cpu_high" if metric == MetricName.CPU else None,
                points=[
                    MetricPoint(
                        at=_DETECTED,
                        value=97 if metric == MetricName.CPU else 10,
                    )
                ],
            )
            for metric in MetricName
        ],
        statuses=[StatusRecord(service="checkout", request_rate_ratio=1.0)],
    )


def test_rule_order_is_frozen() -> None:
    assert RULE_ORDER == (
        Cause.BAD_DEPLOY,
        Cause.CONFIG_ERROR,
        Cause.DB_POOL_EXHAUSTED,
        Cause.DISK_FULL,
        Cause.MEMORY_LEAK,
        Cause.CPU_SATURATION,
        Cause.DNS_FAILURE,
        Cause.DEPENDENCY_TIMEOUT,
    )


def test_baseline_is_isolated_from_the_answer_key() -> None:
    assert_baseline_is_isolated()


def test_missing_service_does_not_call_tools() -> None:
    spy = _Spy()

    report = diagnose(_incident(service=None), spy)

    assert report.verdict == Verdict.UNKNOWN
    assert spy.calls == 0


def test_baseline_ignores_user_severity() -> None:
    world = _cpu_world()

    high = diagnose(_incident(user_severity="high"), SimulatorTools(world))
    low = diagnose(_incident(user_severity="low"), SimulatorTools(world))

    assert high == low
    assert high.verdict == Verdict.FAULT
    assert high.hypotheses[0].cause == Cause.CPU_SATURATION
    assert high.severity == "high"


def test_hypothesis_rejects_a_mismatched_category() -> None:
    with pytest.raises(ValidationError):
        Hypothesis(
            cause=Cause.CPU_SATURATION,
            cause_summary="CPU",
            category=Category.DISK,
            confidence=0.5,
        )


def test_non_fault_report_rejects_a_cause() -> None:
    with pytest.raises(ValidationError):
        DiagnosisReport(
            verdict=Verdict.NOT_A_FAULT,
            severity="high",
            hypotheses=[
                Hypothesis(
                    cause=Cause.CPU_SATURATION,
                    cause_summary="CPU",
                    category=Category.CPU,
                    confidence=0.5,
                )
            ],
        )


def test_ground_truth_rejects_a_cross_category_alternative() -> None:
    with pytest.raises(ValidationError):
        GroundTruth(
            verdict=Verdict.FAULT,
            category=Category.CPU,
            cause=Cause.CPU_SATURATION,
            acceptable_alternatives=[Cause.DNS_FAILURE],
            key_evidence_keys=["metric:cpu_high"],
            severity="high",
        )
