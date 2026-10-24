"""Run the frozen rule baseline and M3 single-shot LLM, and compare score files.

`check` recomputes the reports and fails when the committed files drifted.
`write` refreshes the committed files after an intentional change.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from app.agent.baseline import diagnose
from app.agent.single_shot import SingleShotResult, diagnose_single_shot
from app.eval.fairness import assert_baseline_is_isolated
from app.eval.scenario import Scenario, frozen_dir, load_frozen_scenarios
from app.eval.score import cited_keys, evidence_precision, evidence_recall, top_hit
from app.llm.adapters.fake import FakeLLMAdapter
from app.llm.adapters.openai import OpenAIAdapter
from app.llm.port import LLMPort
from app.tools.simulator.adapter import SimulatorTools

REPORT_NAME = "m1-rule.json"
M3_REPORT_NAME = "m3-single-shot.json"
M3_COMPARE_NAME = "m3-vs-m1.json"
MANIFEST_NAME = "frozen-manifest.json"


def baselines_dir() -> Path:
    return frozen_dir().parents[1] / "evals" / "baselines"


def build_report(scenarios: list[Scenario] | None = None) -> dict[str, Any]:
    assert_baseline_is_isolated()
    cases = [
        _case(scenario)
        for scenario in sorted(scenarios or load_frozen_scenarios(), key=lambda item: item.id)
    ]
    return _aggregate_report(
        agent="rule-baseline",
        agent_version="m1",
        cases=cases,
    )


async def build_m3_report_async(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> dict[str, Any]:
    assert_baseline_is_isolated()
    adapter = llm or FakeLLMAdapter()
    ordered = sorted(scenarios or load_frozen_scenarios(), key=lambda item: item.id)
    cases: list[dict[str, Any]] = []
    for scenario in ordered:
        result = await diagnose_single_shot(
            scenario.incident,
            SimulatorTools(scenario.world),
            adapter,
            services=scenario.world.services,
        )
        cases.append(_m3_case(scenario, result))

    report = _aggregate_report(
        agent="single-shot-llm",
        agent_version="m3",
        cases=cases,
        model=getattr(adapter, "model", "unknown"),
    )
    report["first_output_illegal_citation_rate"] = _mean(
        [float(case["first_output_illegal_citation_rate"]) for case in cases]
    )
    report["accepted_illegal_citation_rate"] = _mean(
        [
            float(case["accepted_illegal_citation_rate"])
            for case in cases
            if case["status"] == "COMPLETED"
        ]
    )
    return report


def build_m3_report(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> dict[str, Any]:
    return asyncio.run(build_m3_report_async(scenarios, llm=llm))


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
    left_cases = {c["scenario_id"]: c for c in left.get("cases", []) if "scenario_id" in c}
    right_cases = {c["scenario_id"]: c for c in right.get("cases", []) if "scenario_id" in c}
    changed: list[dict[str, Any]] = []
    for scenario_id in sorted(set(left_cases) & set(right_cases)):
        lc = left_cases[scenario_id]
        rc = right_cases[scenario_id]
        if lc.get("top1_hit") != rc.get("top1_hit") or lc.get("verdict_hit") != rc.get(
            "verdict_hit"
        ):
            changed.append(
                {
                    "scenario_id": scenario_id,
                    "expected_verdict": rc.get("expected_verdict"),
                    "expected_cause": rc.get("expected_cause"),
                    "left_verdict": lc.get("predicted_verdict"),
                    "left_top_cause": lc.get("predicted_top_cause"),
                    "left_top1_hit": lc.get("top1_hit"),
                    "right_verdict": rc.get("predicted_verdict"),
                    "right_top_cause": rc.get("predicted_top_cause"),
                    "right_top1_hit": rc.get("top1_hit"),
                }
            )
    return {
        "left": left.get("agent_version"),
        "right": right.get("agent_version"),
        "left_top1": left.get("top1"),
        "right_top1": right.get("top1"),
        "top1_delta": _delta(left.get("top1"), right.get("top1")),
        "left_top3": left.get("top3"),
        "right_top3": right.get("top3"),
        "top3_delta": _delta(left.get("top3"), right.get("top3")),
        "evidence_recall_delta": _delta(left.get("evidence_recall"), right.get("evidence_recall")),
        "evidence_precision_delta": _delta(
            left.get("evidence_precision"), right.get("evidence_precision")
        ),
        "changed_cases": changed,
    }


def write_baselines() -> None:
    directory = baselines_dir()
    directory.mkdir(parents=True, exist_ok=True)
    m1_report = build_report()
    m3_report = build_m3_report()
    _dump(directory / REPORT_NAME, m1_report)
    _dump(directory / M3_REPORT_NAME, m3_report)
    _dump(directory / M3_COMPARE_NAME, compare_reports(m1_report, m3_report))
    _dump(directory / MANIFEST_NAME, build_manifest())


def check_baselines() -> None:
    directory = baselines_dir()
    m1_report = build_report()
    m3_report = build_m3_report()
    _expect(directory / REPORT_NAME, m1_report)
    _expect(directory / M3_REPORT_NAME, m3_report)
    _expect(directory / M3_COMPARE_NAME, compare_reports(m1_report, m3_report))
    _expect(directory / MANIFEST_NAME, build_manifest())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.eval")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("write")
    m3_parser = subparsers.add_parser("run-m3")
    m3_parser.add_argument(
        "--provider",
        choices=("fake", "openai"),
        default="fake",
    )
    m3_parser.add_argument("--output", type=Path, default=None)
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
    if args.command == "run-m3":
        llm: LLMPort = OpenAIAdapter() if args.provider == "openai" else FakeLLMAdapter()
        report = build_m3_report(llm=llm)
        if args.output is not None:
            _dump(args.output, report)
        else:
            print(json.dumps(report, indent=2))
        return 0
    left = json.loads(args.left.read_text(encoding="utf-8"))
    right = json.loads(args.right.read_text(encoding="utf-8"))
    print(json.dumps(compare_reports(left, right), indent=2))
    return 0


def _aggregate_report(
    *,
    agent: str,
    agent_version: str,
    cases: list[dict[str, Any]],
    model: str | None = None,
) -> dict[str, Any]:
    faults = [case for case in cases if case["expected_verdict"] == "FAULT"]
    recalls = [case["evidence_recall"] for case in cases if case["evidence_recall"] is not None]
    precisions = [
        case["evidence_precision"] for case in cases if case["evidence_precision"] is not None
    ]
    severities = [case["severity_hit"] for case in faults if case["severity_hit"] is not None]
    verdicts = ("FAULT", "NOT_A_FAULT", "UNKNOWN")
    payload: dict[str, Any] = {
        "agent": agent,
        "agent_version": agent_version,
    }
    if model is not None:
        payload["model"] = model
    payload.update(
        {
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
    )
    return payload


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


def _m3_case(scenario: Scenario, result: SingleShotResult) -> dict[str, Any]:
    expected = scenario.ground_truth
    report = result.report
    if report is None:
        is_fault = expected.verdict == "FAULT"
        has_keys = bool(expected.key_evidence_keys)
        return {
            "scenario_id": scenario.id,
            "holdout": scenario.holdout,
            "status": result.status,
            "expected_verdict": expected.verdict.value,
            "expected_cause": None if expected.cause is None else expected.cause.value,
            "predicted_verdict": None,
            "predicted_top_cause": None,
            "predicted_severity": None,
            "verdict_hit": False,
            "top1_hit": False if is_fault else None,
            "top3_hit": False if is_fault else None,
            "severity_hit": False if is_fault else None,
            "evidence_recall": 0.0 if has_keys else None,
            "evidence_precision": 0.0 if has_keys else None,
            "swept_services": list(result.swept_services),
            "tool_calls": result.tool_calls,
            "llm_attempts": result.llm_attempts,
            "first_output_illegal_citation": result.first_output_illegal_citation,
            "first_output_illegal_citation_rate": result.first_output_illegal_citation_rate,
            "accepted_illegal_citation_rate": result.accepted_illegal_citation_rate,
        }

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
        "status": result.status,
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
        "swept_services": list(result.swept_services),
        "tool_calls": result.tool_calls,
        "llm_attempts": result.llm_attempts,
        "first_output_illegal_citation": result.first_output_illegal_citation,
        "first_output_illegal_citation_rate": result.first_output_illegal_citation_rate,
        "accepted_illegal_citation_rate": result.accepted_illegal_citation_rate,
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
