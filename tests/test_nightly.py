import asyncio
import json

import pytest
from app.eval import nightly
from app.eval.runner import main as eval_main
from app.eval.scenario import load_frozen_scenarios
from app.llm.port import LLMRequest, LLMResponse


def test_agreement_measures_consistency_not_correctness() -> None:
    wrong = {
        "verdict": "FAULT",
        "hypotheses": [{"category": "CPU", "cause": "wrong", "confidence": 0.1}],
    }
    same = {**wrong, "severity": "high"}
    other = {"verdict": "UNKNOWN", "hypotheses": []}
    assert nightly.agreement([wrong, same, other]) == 2 / 3
    assert nightly.agreement([wrong, wrong, wrong]) == 1
    assert nightly.agreement([None, None, None]) == 0
    assert nightly.agreement([wrong, None]) == 0.5
    assert nightly.answer_key(other) == nightly.answer_key(
        {"verdict": "UNKNOWN", "hypotheses": [{"cause": "ignored"}]}
    )


def test_nightly_fake_records_repeats_and_headline(tmp_path) -> None:
    scenarios = load_frozen_scenarios()[:2]
    report = asyncio.run(
        nightly.run_nightly(repeats=2, provider="fake", output=tmp_path, scenarios=scenarios)
    )
    assert report["status"] == "completed"
    assert report["measurement"] == "offline-harness"
    assert report["judge"]["status"] == "disabled"
    assert report["headline"]["value"] == report["comparison"]["top1_delta"]
    assert json.loads((tmp_path / "report.json").read_text()) == report
    for version in ("m3", "m4"):
        assert len(report["runs"][version]) == 2
        summary = report["summary"][version]
        assert summary["observation_count"] == 4
        assert summary["agreement@k"] == 1
        assert "severity_accuracy" in summary
        assert "evidence_precision" in summary
        assert "first_output_illegal_citation_rate" in summary
        assert all("final_report" in case for case in report["runs"][version][0]["cases"])


def test_nightly_failure_retains_completed_repeats(tmp_path, monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise RuntimeError("sensitive provider response")

    monkeypatch.setattr(nightly, "build_m4_report_async", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(
            nightly.run_nightly(
                repeats=2, provider="fake", output=tmp_path, scenarios=load_frozen_scenarios()[:1]
            )
        )
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["status"] == "failed"
    assert len(saved["runs"]["m3"]) == 1
    assert saved["runs"]["m4"] == []
    assert "sensitive provider response" not in (tmp_path / "report.json").read_text()


def test_judge_is_commentary_and_failure_does_not_replace_scores(tmp_path) -> None:
    class BrokenJudge:
        async def generate(self, request: LLMRequest) -> LLMResponse:
            raise ValueError("unavailable")

    report = asyncio.run(
        nightly.run_nightly(
            repeats=2,
            provider="fake",
            output=tmp_path,
            scenarios=load_frozen_scenarios()[:1],
            judge=BrokenJudge(),
        )
    )
    assert report["status"] == "completed"
    assert report["judge"] == {"status": "failed", "error_type": "ValueError"}
    assert "top1" in report["summary"]["m4"]


def test_judge_success_keeps_text_separate() -> None:
    class Judge:
        model = "test-judge"

        async def generate(self, request: LLMRequest) -> LLMResponse:
            assert "untrusted" in request.messages[0].content
            return LLMResponse(content="Commentary only.", model=self.model)

    runs = {"m4": [{"cases": [{"scenario_id": "case", "final_report": None}]}]}
    result = asyncio.run(nightly.judge_commentary(runs, Judge()))
    assert result["cases"][0]["text"] == "Commentary only."
    assert result["model"] == "test-judge"
    assert "top1" not in result


def test_nightly_validates_repeat_count_before_running(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        asyncio.run(nightly.run_nightly(repeats=1, provider="fake", output=tmp_path))


def test_compare_writes_a_report(tmp_path) -> None:
    left = tmp_path / "left.json"
    right = tmp_path / "right.json"
    output = tmp_path / "comparison.json"
    left.write_text(json.dumps({"agent_version": "m3", "top1": 0.5}))
    right.write_text(json.dumps({"agent_version": "m4", "top1": 0.75}))
    assert eval_main(["compare", str(left), str(right), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["top1_delta"] == 0.25


def test_baseline_gate_excludes_only_severity_metrics(tmp_path) -> None:
    from app.eval.runner import _expect

    path = tmp_path / "baseline.json"
    original = {"severity_accuracy": 1, "cases": [{"severity_hit": True, "top1_hit": True}]}
    changed = {"severity_accuracy": 0, "cases": [{"severity_hit": False, "top1_hit": True}]}
    path.write_text(json.dumps(original))
    _expect(path, changed, ignore_severity=True)
    changed["cases"][0]["top1_hit"] = False
    with pytest.raises(SystemExit, match="does not match"):
        _expect(path, changed, ignore_severity=True)
