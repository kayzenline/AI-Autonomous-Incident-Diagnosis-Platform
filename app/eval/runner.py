"""Run the frozen M1 rule baseline, M3 single-shot LLM, and M4 investigator loop.

Commands:
- `check`: recompute M1/M3/M4 reports, compare deltas, and replay M4 cassettes.
- `write`: refresh committed baseline JSON files and M4 cassette.
- `run-m3` / `run-m4`: run M3 or M4 with `--provider fake|openai`.
- `replay`: replay `evals/cassettes/m4-frozen.json` through the parser, ledger, and orchestrator.
- `inspect <scenario_id> [--mode m1|m3|m4]`: print a step-by-step SRE diagnostic trace.
- `compare <left> <right>`: compare any two evaluation report JSON files.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from app.agent.baseline import diagnose
from app.agent.investigator import InvestigationResult, investigate
from app.agent.single_shot import SingleShotResult, diagnose_single_shot
from app.eval.cassette import (
    load_cassette,
    record_m4_cassette,
    replay_m4_cassette,
    save_cassette,
)
from app.eval.fairness import assert_baseline_is_isolated
from app.eval.scenario import Scenario, frozen_dir, load_frozen_scenarios
from app.eval.score import cited_keys, evidence_precision, evidence_recall, top_hit
from app.llm.adapters.fake import FakeLLMAdapter
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.llm.adapters.openai import OpenAIAdapter
from app.llm.port import LLMPort
from app.tools.simulator.adapter import SimulatorTools

REPORT_NAME = "m1-rule.json"
M3_REPORT_NAME = "m3-single-shot.json"
M3_COMPARE_NAME = "m3-vs-m1.json"
M4_REPORT_NAME = "m4-agent.json"
M4_COMPARE_NAME = "m4-vs-m3.json"
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
    include_reports: bool = False,
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
        row = _m3_case(scenario, result)
        if include_reports:
            row["final_report"] = (
                None if result.report is None else result.report.model_dump(mode="json")
            )
        cases.append(row)

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
    if include_reports:
        report["response_models"] = sorted(getattr(adapter, "observed_models", ()))
    return report


def build_m3_report(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> dict[str, Any]:
    return asyncio.run(build_m3_report_async(scenarios, llm=llm))


async def build_m4_report_async(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
    include_reports: bool = False,
) -> dict[str, Any]:
    assert_baseline_is_isolated()
    adapter = llm or InvestigativeFakeLLM()
    ordered = sorted(scenarios or load_frozen_scenarios(), key=lambda item: item.id)
    cases: list[dict[str, Any]] = []
    for scenario in ordered:
        result = await investigate(
            scenario.incident,
            SimulatorTools(scenario.world),
            adapter,
            known_services=scenario.world.services,
        )
        row = _m4_case(scenario, result)
        if include_reports:
            row["final_report"] = (
                None if result.report is None else result.report.model_dump(mode="json")
            )
        cases.append(row)

    report = _aggregate_report(
        agent="investigation-loop",
        agent_version="m4",
        cases=cases,
        model=getattr(adapter, "model", "unknown"),
    )
    report["avg_tool_calls"] = _mean([float(case["tool_calls"]) for case in cases])
    report["max_tool_calls_used"] = max((int(case["tool_calls"]) for case in cases), default=0)
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
    if include_reports:
        report["response_models"] = sorted(getattr(adapter, "observed_models", ()))
    return report


def build_m4_report(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> dict[str, Any]:
    return asyncio.run(build_m4_report_async(scenarios, llm=llm))


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
    m4_report = build_m4_report()
    _dump(directory / REPORT_NAME, m1_report)
    _dump(directory / M3_REPORT_NAME, m3_report)
    _dump(directory / M3_COMPARE_NAME, compare_reports(m1_report, m3_report))
    _dump(directory / M4_REPORT_NAME, m4_report)
    _dump(directory / M4_COMPARE_NAME, compare_reports(m3_report, m4_report))
    _dump(directory / MANIFEST_NAME, build_manifest())
    save_cassette(record_m4_cassette())


def check_baselines() -> None:
    directory = baselines_dir()
    m1_report = build_report()
    m3_report = build_m3_report()
    m4_report = build_m4_report()
    _expect(directory / REPORT_NAME, m1_report, ignore_severity=True)
    _expect(directory / M3_REPORT_NAME, m3_report, ignore_severity=True)
    _expect(directory / M3_COMPARE_NAME, compare_reports(m1_report, m3_report))
    _expect(directory / M4_REPORT_NAME, m4_report, ignore_severity=True)
    _expect(directory / M4_COMPARE_NAME, compare_reports(m3_report, m4_report))
    _expect(directory / MANIFEST_NAME, build_manifest())
    replay_m4_cassette(load_cassette())


def format_inspection(scenario: Scenario, mode: str = "m4", *, as_json: bool = False) -> str:
    tools = SimulatorTools(scenario.world)
    expected = scenario.ground_truth

    if mode == "m1":
        rep = diagnose(scenario.incident, tools)
        payload = {
            "scenario_id": scenario.id,
            "mode": "m1",
            "verdict": rep.verdict.value,
            "verdict_hit": rep.verdict == expected.verdict,
            "top1_hit": top_hit(rep, expected, limit=1),
            "report": rep.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2)

    if mode == "m3":
        m3_res = asyncio.run(
            diagnose_single_shot(
                scenario.incident,
                tools,
                FakeLLMAdapter(),
                services=scenario.world.services,
            )
        )
        payload = {
            "scenario_id": scenario.id,
            "mode": "m3",
            "status": m3_res.status,
            "tool_calls": m3_res.tool_calls,
            "swept_services": list(m3_res.swept_services),
            "report": None if m3_res.report is None else m3_res.report.model_dump(mode="json"),
        }
        return json.dumps(payload, indent=2)

    m4_res = asyncio.run(
        investigate(
            scenario.incident,
            tools,
            InvestigativeFakeLLM(),
            known_services=scenario.world.services,
        )
    )
    if as_json:
        return json.dumps(m4_res.model_dump(mode="json"), indent=2)

    lines: list[str] = [
        f"=== SRE Incident Investigation Trace: {scenario.id} (mode=m4) ===",
        f"Incident Title : {scenario.incident.title}",
        (
            f"Service        : {m4_res.resolved_service} "
            f"(inferred_by_model={m4_res.service_inferred_by_model})"
        ),
        f"Status         : {m4_res.status}",
        f"Budget Usage   : {m4_res.tool_calls}/8 tool calls | {m4_res.model_rounds} model rounds | "
        f"{m4_res.tokens_in + m4_res.tokens_out} tokens (${m4_res.cost_usd:.6f})",
        f"Verifier       : runs={m4_res.verifier_runs}, tool_calls={m4_res.verifier_tool_calls}, "
        f"first_illegal_rate={m4_res.first_output_illegal_citation_rate:.2f}, "
        f"accepted_illegal_rate={m4_res.accepted_illegal_citation_rate:.2f}",
        "",
        "--- Step-by-Step Timeline ---",
    ]
    for ev in m4_res.events:
        lines.append(f"[{ev.seq:02d}] {ev.type.value:20s} | {ev.summary}")
    lines.append("")
    lines.append(f"--- Evidence Ledger ({len(m4_res.ledger_keys)} keys) ---")
    lines.append(", ".join(m4_res.ledger_keys) if m4_res.ledger_keys else "(none)")
    lines.append("")
    lines.append("--- Final Diagnosis Report ---")
    if m4_res.report is None:
        lines.append(f"FAILED: {m4_res.failure_reason}")
    else:
        rep = m4_res.report
        lines.append(f"Verdict  : {rep.verdict.value} (expected={expected.verdict.value})")
        lines.append(f"Severity : {rep.severity} (expected={expected.severity})")
        for idx, hyp in enumerate(rep.hypotheses, start=1):
            lines.append(
                f"  #{idx} [{hyp.category.value}] {hyp.cause.value} "
                f"(confidence={hyp.confidence:.2f}) -> evidence={hyp.supporting_evidence}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.eval")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check")
    subparsers.add_parser("write")
    subparsers.add_parser("replay")

    m3_parser = subparsers.add_parser("run-m3")
    m3_parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    m3_parser.add_argument("--output", type=Path, default=None)

    m4_parser = subparsers.add_parser("run-m4")
    m4_parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    m4_parser.add_argument("--output", type=Path, default=None)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("scenario_id")
    inspect_parser.add_argument("--mode", choices=("m1", "m3", "m4"), default="m4")
    inspect_parser.add_argument("--json", action="store_true")

    compare = subparsers.add_parser("compare")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "write":
        write_baselines()
        return 0
    if args.command == "check":
        check_baselines()
        return 0
    if args.command == "replay":
        cassette = load_cassette()
        results = replay_m4_cassette(cassette)
        print(
            json.dumps(
                {
                    "cassette_version": cassette.version,
                    "replayed_scenarios": len(results),
                    "all_completed": all(r.status == "COMPLETED" for r in results),
                },
                indent=2,
            )
        )
        return 0
    if args.command == "run-m3":
        llm_m3: LLMPort = OpenAIAdapter() if args.provider == "openai" else FakeLLMAdapter()
        report = build_m3_report(llm=llm_m3)
        if args.output is not None:
            _dump(args.output, report)
        else:
            print(json.dumps(report, indent=2))
        return 0
    if args.command == "run-m4":
        llm_m4: LLMPort = OpenAIAdapter() if args.provider == "openai" else InvestigativeFakeLLM()
        report = build_m4_report(llm=llm_m4)
        if args.output is not None:
            _dump(args.output, report)
        else:
            print(json.dumps(report, indent=2))
        return 0
    if args.command == "inspect":
        scenarios = {s.id: s for s in load_frozen_scenarios()}
        if args.scenario_id not in scenarios:
            raise SystemExit(f"Unknown scenario_id: {args.scenario_id!r}")
        print(
            format_inspection(
                scenarios[args.scenario_id],
                mode=args.mode,
                as_json=bool(args.json),
            )
        )
        return 0
    left = json.loads(args.left.read_text(encoding="utf-8"))
    right = json.loads(args.right.read_text(encoding="utf-8"))
    comparison = compare_reports(left, right)
    if args.output is not None:
        _dump(args.output, comparison)
    else:
        print(json.dumps(comparison, indent=2))
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


def _m4_case(scenario: Scenario, result: InvestigationResult) -> dict[str, Any]:
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
            "tool_calls": result.tool_calls,
            "model_rounds": result.model_rounds,
            "verifier_runs": result.verifier_runs,
            "verifier_tool_calls": result.verifier_tool_calls,
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
        "tool_calls": result.tool_calls,
        "model_rounds": result.model_rounds,
        "verifier_runs": result.verifier_runs,
        "verifier_tool_calls": result.verifier_tool_calls,
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


def _without_severity(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_severity(item)
            for key, item in value.items()
            if key not in {"predicted_severity", "severity_hit", "severity_accuracy"}
        }
    if isinstance(value, list):
        return [_without_severity(item) for item in value]
    return value


def _expect(path: Path, payload: dict[str, Any], *, ignore_severity: bool = False) -> None:
    if not path.exists():
        raise SystemExit(f"missing {path}")
    current = json.loads(path.read_text(encoding="utf-8"))
    if ignore_severity:
        current, payload = _without_severity(current), _without_severity(payload)
    if current != payload:
        raise SystemExit(f"{path.name} does not match a fresh baseline run")
