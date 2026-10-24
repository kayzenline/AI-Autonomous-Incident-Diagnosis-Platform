from collections import Counter
from datetime import UTC, datetime

from app.core.taxonomy import Verdict
from app.core.window import TimeRange
from app.eval.runner import build_manifest, build_report, check_baselines, compare_reports
from app.eval.scenario import load_frozen_scenarios
from app.tools.contract import SearchLogsInput
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
    assert report["top1"] < 0.9
    cases = {case["scenario_id"]: case for case in report["cases"]}
    assert cases["cpu-saturation-checkout"]["top1_hit"] is True
    assert cases["cpu-with-disk-herring"]["top1_hit"] is False
    assert cases["cpu-with-disk-herring"]["top3_hit"] is True
    assert cases["dependency-with-cpu-herring"]["top1_hit"] is False
    assert cases["traffic-spike-web"]["predicted_verdict"] == "NOT_A_FAULT"
    assert cases["unknown-latency-only"]["predicted_verdict"] == "UNKNOWN"
    assert compare_reports(report, report)["top1_delta"] == 0


def test_manifest_hashes_cover_every_file() -> None:
    manifest = build_manifest()
    scenarios = load_frozen_scenarios()

    assert {row["id"] for row in manifest["scenarios"]} == {item.id for item in scenarios}
    assert all(len(row["sha256"]) == 64 for row in manifest["scenarios"])
