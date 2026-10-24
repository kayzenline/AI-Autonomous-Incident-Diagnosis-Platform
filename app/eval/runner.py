"""Run the frozen rule baseline and compare score files.

`check` recomputes the report and fails when the committed file drifted.
`write` refreshes the committed file after an intentional baseline change.
"""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from app.agent.baseline import diagnose
from app.eval.fairness import assert_baseline_is_isolated
from app.eval.scenario import Scenario, frozen_dir, load_frozen_scenarios
from app.eval.score import cited_keys, evidence_precision, evidence_recall, top_hit
from app.tools.simulator.adapter import SimulatorTools

REPORT_NAME = "m1-rule.json"
MANIFEST_NAME = "frozen-manifest.json"


def baselines_dir() -> Path:
    return frozen_dir().parents[1] / "evals" / "baselines"


def build_report(scenarios: list[Scenario] | None = None) -> dict[str, Any]:
    assert_baseline_is_isolated()
    cases = [
        _case(scenario)
        for scenario in sorted(scenarios or load_frozen_scenarios(), key=lambda item: item.id)
    ]
    faults = [case for case in cases if case["expected_verdict"] == "FAULT"]
    recalls = [case["evidence_recall"] for case in cases if case["evidence_recall"] is not None]
    precisions = [
        case["evidence_precision"] for case in cases if case["evidence_precision"] is not None
    ]
    severities = [case["severity_hit"] for case in faults if case["severity_hit"] is not None]
    verdicts = ("FAULT", "NOT_A_FAULT", "UNKNOWN")
    return {
        "agent": "rule-baseline",
        "agent_version": "m1",
        "scenario_set": "frozen-v1",
        "scenario_count": len(cases),
        "holdout_count": sum(1 for case in cases if case["holdout"]),
        "top1": _rate([case["top1_hit"] for case in faults]),
        "top3": _rate([case["top3_hit"] for case in faults]),
        "verdict_accuracy": {
            verdict: _rate(
                [case["verdict_hit"] for case in cases if case["expected_verdict"] == verdict]
            )
            for verdict in verdicts
        },
        "evidence_recall": _mean([float(value) for value in recalls]),
        "evidence_precision": _mean([float(value) for value in precisions]),
        "severity_accuracy": _rate(severities),
        "cases": cases,
    }


def build_manifest(directory: Path | None = None) -> dict[str, Any]:
    root = directory or frozen_dir()
    scenarios = {scenario.id: scenario for scenario in load_frozen_scenarios(root)}
    rows = []
    for path in sorted(root.glob("*.yaml")):
        scenario = scenarios[path.stem]
        rows.append(
            {
                "id": scenario.id,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "holdout": scenario.holdout,
            }
        )
    return {"set": "frozen-v1", "scenarios": rows}


def compare_reports(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "left": left.get("agent_version"),
        "right": right.get("agent_version"),
        "top1_delta": _delta(left.get("top1"), right.get("top1")),
        "top3_delta": _delta(left.get("top3"), right.get("top3")),
        "evidence_recall_delta": _delta(left.get("evidence_recall"), right.get("evidence_recall")),
        "evidence_precision_delta": _delta(
            left.get("evidence_precision"), right.get("evidence_precision")
        ),
    }


def write_baselines() -> None:
    directory = baselines_dir()
    directory.mkdir(parents=True, exist_ok=True)
    _dump(directory / REPORT_NAME, build_report())
    _dump(directory / MANIFEST_NAME, build_manifest())


def check_baselines() -> None:
    directory = baselines_dir()
    _expect(directory / REPORT_NAME, build_report())
    _expect(directory / MANIFEST_NAME, build_manifest())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.eval")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("write")
    compare = subparsers.add_parser("compare")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    args = parser.parse_args(argv)
    if args.command == "write":
        write_baselines()
        return 0
    if args.command == "check":
        check_baselines()
        return 0
    left = json.loads(args.left.read_text(encoding="utf-8"))
    right = json.loads(args.right.read_text(encoding="utf-8"))
    print(json.dumps(compare_reports(left, right), indent=2))
    return 0


def _case(scenario: Scenario) -> dict[str, Any]:
    report = diagnose(scenario.incident, SimulatorTools(scenario.world))
    expected = scenario.ground_truth
    cited = cited_keys(report)
    top_cause = report.hypotheses[0].cause.value if report.hypotheses else None
    severity_hit = None
    if expected.verdict == "FAULT":
        severity_hit = report.severity == expected.severity
    recall = evidence_recall(cited, expected.key_evidence_keys)
    precision = evidence_precision(cited, expected.key_evidence_keys)
    return {
        "scenario_id": scenario.id,
        "holdout": scenario.holdout,
        "expected_verdict": expected.verdict.value,
        "expected_cause": None if expected.cause is None else expected.cause.value,
        "predicted_verdict": report.verdict.value,
        "predicted_top_cause": top_cause,
        "predicted_severity": report.severity,
        "verdict_hit": report.verdict == expected.verdict,
        "top1_hit": top_hit(report, expected, limit=1),
        "top3_hit": top_hit(report, expected, limit=3),
        "severity_hit": severity_hit,
        "evidence_recall": None if recall is None else round(recall, 4),
        "evidence_precision": None if precision is None else round(precision, 4),
    }


def _rate(flags: list[Any]) -> float | None:
    if not flags:
        return None
    return round(sum(1 for flag in flags if flag) / len(flags), 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _delta(left: Any, right: Any) -> float | None:
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return None
    return round(float(right) - float(left), 4)


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _expect(path: Path, payload: dict[str, Any]) -> None:
    if not path.exists():
        raise SystemExit(f"missing {path}")
    current = json.loads(path.read_text(encoding="utf-8"))
    if current != payload:
        raise SystemExit(f"{path.name} does not match a fresh baseline run")
