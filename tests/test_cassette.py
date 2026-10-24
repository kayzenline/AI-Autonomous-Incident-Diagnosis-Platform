"""Tests for M4 cassette recording, replay, and drift detection (#28)."""

import asyncio

import pytest
from app.core.report import DiagnosisReport, Hypothesis, Recommendation
from app.core.taxonomy import Category, Cause, Verdict
from app.eval.cassette import (
    CassetteMismatchError,
    load_cassette,
    record_cassette_entry_async,
    replay_cassette_entry_async,
    replay_m4_cassette,
)
from app.eval.runner import main as eval_main
from app.eval.scenario import load_frozen_scenarios
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM


def _scenario_by_id(scenario_id: str):
    for scenario in load_frozen_scenarios():
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def test_committed_m4_cassette_replays_cleanly() -> None:
    cassette = load_cassette()
    results = replay_m4_cassette(cassette)
    assert len(results) == 29
    assert all(r.status == "COMPLETED" for r in results)
    assert all(r.tool_calls <= 8 for r in results)


def test_cassette_replay_preserves_first_output_illegal_citation_rate() -> None:
    scenario = _scenario_by_id("dns-nxdomain-edge")
    bad_first_report = DiagnosisReport(
        hypotheses=[
            Hypothesis(
                cause=Cause.DNS_FAILURE,
                cause_summary="DNS lookup failed for an upstream host.",
                category=Category.NETWORK,
                confidence=0.9,
                supporting_evidence=["log:dns_nxdomain", "log:hallucinated_key"],
            )
        ],
        verdict=Verdict.FAULT,
        severity="high",
        recommendations=[
            Recommendation(
                action="Inspect DNS records.",
                risk="low",
                requires_approval=True,
            )
        ],
    )
    scripted = InvestigativeFakeLLM(
        responses=[
            {
                "action": "call_tool",
                "summary": "Search error logs on edge.",
                "tool": "search_logs",
                "arguments": {"service": "edge", "min_level": "ERROR"},
            },
            {
                "action": "final_report",
                "summary": "First report with 1 valid and 1 illegal citation.",
                "report": bad_first_report.model_dump(mode="json"),
            },
        ]
    )
    entry = asyncio.run(record_cassette_entry_async(scenario, llm=scripted))
    assert entry.expected_first_output_illegal_citation is True
    assert entry.expected_first_output_illegal_citation_rate == 0.5
    assert entry.expected_accepted_illegal_citation_rate == 0.0

    # Replaying the exact recorded entry preserves the 0.5 first-output illegal citation rate
    replayed = asyncio.run(replay_cassette_entry_async(entry, scenario))
    assert replayed.first_output_illegal_citation_rate == 0.5

    # If a cassette attempts to rewrite first_output_illegal_citation_rate to 0.0, replay fails!
    rewritten = entry.model_copy(update={"expected_first_output_illegal_citation_rate": 0.0})
    with pytest.raises(CassetteMismatchError, match="rewrote first_output_illegal_citation_rate"):
        asyncio.run(replay_cassette_entry_async(rewritten, scenario))


def test_cassette_mismatch_on_tampered_report_or_ledger_fails() -> None:
    scenario = _scenario_by_id("dns-nxdomain-edge")
    entry = asyncio.run(record_cassette_entry_async(scenario))

    tampered_ledger = entry.model_copy(update={"expected_ledger_keys": ["wrong:key"]})
    with pytest.raises(CassetteMismatchError, match="ledger_keys mismatch"):
        asyncio.run(replay_cassette_entry_async(tampered_ledger, scenario))

    tampered_status = entry.model_copy(update={"expected_status": "FAILED"})
    with pytest.raises(CassetteMismatchError, match="status mismatch"):
        asyncio.run(replay_cassette_entry_async(tampered_status, scenario))


def test_eval_cli_replay_and_inspect_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert eval_main(["replay"]) == 0
    out_replay = capsys.readouterr().out
    assert '"replayed_scenarios": 29' in out_replay

    assert eval_main(["inspect", "drilldown-truncated-dns", "--mode", "m4"]) == 0
    out_inspect = capsys.readouterr().out
    assert "log:late_nxdomain" in out_inspect
    assert "Budget Usage   : 3/8 tool calls" in out_inspect
