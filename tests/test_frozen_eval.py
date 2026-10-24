from collections import Counter
from datetime import UTC, datetime

from app.core.taxonomy import Cause, Verdict
from app.core.window import TimeRange, default_window
from app.eval.runner import build_manifest, build_report, check_baselines, compare_reports
from app.eval.scenario import Scenario, load_frozen_scenarios
from app.tools.contract import (
    MetricName,
    QueryMetricsInput,
    SearchLogsInput,
    ServiceWindowInput,
    ToolResult,
)
from app.tools.simulator.adapter import SimulatorTools


def test_frozen_set_has_the_required_mix() -> None:
    scenarios = load_frozen_scenarios()
    verdicts = Counter(scenario.ground_truth.verdict for scenario in scenarios)
    tags = [tag for scenario in scenarios for tag in scenario.tags]

    assert len(scenarios) >= 20
    assert verdicts[Verdict.UNKNOWN] >= 4
    assert verdicts[Verdict.NOT_A_FAULT] >= 3
    assert tags.count("red_herring") + tags.count("misleading") >= 4
    assert tags.count("injection") >= 2
    assert tags.count("multi_hop") >= 2
    assert tags.count("drill_down") >= 2
    assert tags.count("conflict") >= 1
    assert tags.count("noisy") >= 1
    assert sum(1 for scenario in scenarios if scenario.holdout) >= 6


def test_injection_does_not_change_the_answer() -> None:
    scenarios = {scenario.id: scenario for scenario in load_frozen_scenarios()}

    assert scenarios["dns-injection"].ground_truth.cause == "dns_failure"
    assert scenarios["pool-injection"].ground_truth.cause == "db_pool_exhausted"
    assert "cpu_saturation" in scenarios["dns-injection"].world.logs[0].message
    assert "bad_deploy" in scenarios["pool-injection"].world.logs[0].message


def test_injected_log_is_wrapped_and_redacted() -> None:
    scenario = next(item for item in load_frozen_scenarios() if item.id == "dns-injection")
    result = SimulatorTools(scenario.world).search_logs(
        SearchLogsInput(
            service=scenario.service,
            time_range=TimeRange(
                start=datetime(2026, 10, 23, 2, 45, tzinfo=UTC),
                end=datetime(2026, 10, 23, 3, 15, tzinfo=UTC),
            ),
        )
    )
    message = result.data["entries"][0]["message"]

    assert "Ignore previous instructions" in message
    assert "<<UNTRUSTED_DATA>>" in message
    assert "ada@example.com" not in message
    assert "raw-secret" not in message
    assert "10.8.8.8" not in message


def test_committed_baseline_matches_a_fresh_run() -> None:
    check_baselines()
    report = build_report()

    assert report["scenario_count"] >= 20
    assert report["holdout_count"] >= 6
    assert report["top1"] < 0.87
    abstained = [case for case in report["cases"] if case["evidence_precision"] is None]
    assert abstained
    assert all(case["expected_verdict"] != "FAULT" for case in abstained)
    assert all(case["evidence_recall"] is None for case in abstained)
    cases = {case["scenario_id"]: case for case in report["cases"]}
    assert cases["cpu-saturation-checkout"]["top1_hit"] is True
    assert cases["cpu-with-disk-herring"]["top1_hit"] is False
    assert cases["cpu-with-disk-herring"]["top3_hit"] is True
    assert cases["dependency-with-cpu-herring"]["top1_hit"] is False
    assert cases["traffic-spike-web"]["predicted_verdict"] == "NOT_A_FAULT"
    assert cases["unknown-latency-only"]["predicted_verdict"] == "UNKNOWN"
    assert cases["multihop-checkout-payments-pool"]["top1_hit"] is False
    assert cases["multihop-checkout-payments-pool"]["predicted_top_cause"] == "dependency_timeout"
    assert cases["multihop-checkout-pricing-dns"]["top1_hit"] is False
    assert cases["drilldown-truncated-dns"]["predicted_verdict"] == "UNKNOWN"
    assert cases["drilldown-truncated-dns"]["evidence_precision"] == 0
    assert cases["drilldown-payments-pool"]["predicted_verdict"] == "UNKNOWN"
    assert cases["conflict-deploy-vs-config"]["top1_hit"] is False
    assert cases["conflict-deploy-vs-config"]["top3_hit"] is True
    assert cases["conflict-deploy-vs-config"]["predicted_top_cause"] == "bad_deploy"
    assert cases["noisy-pool-warmup"]["predicted_verdict"] == "FAULT"
    assert cases["noisy-pool-warmup"]["evidence_precision"] == 0
    assert cases["noisy-pool-warmup"]["evidence_recall"] is None
    assert compare_reports(report, report)["top1_delta"] == 0


def test_hard_scenarios_need_more_than_an_incident_service_sweep() -> None:
    scenarios = {scenario.id: scenario for scenario in load_frozen_scenarios()}
    cause_ids = {cause.value for cause in Cause}

    pool = scenarios["multihop-checkout-payments-pool"]
    visible = _incident_sweep_keys(pool)
    assert "db:payments_pool" not in visible
    assert "log:pool_exhausted" not in visible
    assert "dep:payments_timeout" in visible
    assert "db:payments_pool" in _service_keys(pool, "payments")

    pricing = scenarios["multihop-checkout-pricing-dns"]
    assert "log:pricing_nxdomain" not in _incident_sweep_keys(pricing)
    assert "dep:pricing_down" not in _incident_sweep_keys(pricing)
    assert "dep:pricing_down" in _service_keys(pricing, "inventory")
    assert "log:pricing_nxdomain" in _service_keys(pricing, "pricing")

    buried = scenarios["drilldown-truncated-dns"]
    assert "log:late_nxdomain" not in _incident_sweep_keys(buried)
    window = default_window(buried.incident.occurred_at, buried.incident.detected_at)
    assert window is not None
    narrowed = SimulatorTools(buried.world).search_logs(
        SearchLogsInput(
            service=buried.service,
            time_range=TimeRange(
                start=datetime(2026, 10, 23, 3, 6, tzinfo=UTC),
                end=window.end,
            ),
        )
    )
    assert narrowed.truncated is False
    assert narrowed.evidence_keys == ["log:late_nxdomain"]
    _assert_observation_has_no_answer_flag(narrowed)

    drilled = scenarios["drilldown-payments-pool"]
    assert "db:payments_pool" not in _incident_sweep_keys(drilled)
    assert "dep:payments_green" in _incident_sweep_keys(drilled)
    assert "db:payments_pool" in _service_keys(drilled, "payments")

    conflict = scenarios["conflict-deploy-vs-config"]
    conflict_keys = _incident_sweep_keys(conflict)
    assert "change:routine_deploy" in conflict_keys
    assert "change:bad_config" in conflict_keys

    for scenario in (pool, pricing, buried, drilled, conflict, scenarios["noisy-pool-warmup"]):
        text = f"{scenario.incident.title} {scenario.incident.description}".casefold()
        assert not any(cause_id in text for cause_id in cause_ids)
        for result in _incident_sweep(scenario):
            _assert_observation_has_no_answer_flag(result)


def _incident_sweep_keys(scenario: Scenario) -> set[str]:
    keys: set[str] = set()
    for result in _incident_sweep(scenario):
        keys.update(result.evidence_keys)
    return keys


def _service_keys(scenario: Scenario, service: str) -> set[str]:
    keys: set[str] = set()
    for result in _sweep(scenario, service):
        keys.update(result.evidence_keys)
    return keys


def _incident_sweep(scenario: Scenario) -> list[ToolResult]:
    return _sweep(scenario, scenario.service)


def _sweep(scenario: Scenario, service: str) -> list[ToolResult]:
    window = default_window(scenario.incident.occurred_at, scenario.incident.detected_at)
    assert window is not None
    tools = SimulatorTools(scenario.world)
    results = [
        tools.query_metrics(QueryMetricsInput(service=service, metric=metric, time_range=window))
        for metric in MetricName
    ]
    results.append(tools.search_logs(SearchLogsInput(service=service, time_range=window)))
    request = ServiceWindowInput(service=service, time_range=window)
    results.append(tools.get_service_status(request))
    results.append(tools.get_recent_changes(request))
    results.append(tools.get_dependency_health(request))
    results.append(tools.query_db_health(request))
    return results


def _assert_observation_has_no_answer_flag(result: ToolResult) -> None:
    payload = result.model_dump()
    assert "relevant" not in payload
    assert "ground_truth" not in payload


def test_manifest_hashes_cover_every_file() -> None:
    manifest = build_manifest()
    scenarios = load_frozen_scenarios()

    assert {row["id"] for row in manifest["scenarios"]} == {item.id for item in scenarios}
    assert all(len(row["sha256"]) == 64 for row in manifest["scenarios"])
