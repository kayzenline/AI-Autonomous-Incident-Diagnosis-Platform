"""Cassette recording and deterministic replay for the M4 orchestrator (#28).

Replays recorded `LLMResponse` sequences through the real `investigate()`
orchestrator, step parser, `EvidenceLedger`, `HypothesisBoard`, and
`ReadOnlyVerifier` against `SimulatorTools` without a live model provider.
"""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.investigator import InvestigationResult, InvestigationStatus, investigate
from app.eval.scenario import Scenario, frozen_dir, load_frozen_scenarios
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.llm.port import LLMPort, LLMRequest, LLMResponse
from app.tools.simulator.adapter import SimulatorTools

CASSETTE_NAME = "m4-frozen.json"


class CassetteMismatchError(RuntimeError):
    """Raised when a cassette replay diverges from its recorded expectations."""


class CassetteEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    llm_responses: list[LLMResponse] = Field(default_factory=list)
    expected_status: InvestigationStatus
    expected_ledger_keys: list[str] = Field(default_factory=list)
    expected_tool_calls: int = Field(default=0, ge=0)
    expected_first_output_illegal_citation: bool = False
    expected_first_output_illegal_citation_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_accepted_illegal_citation_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    expected_report: dict[str, Any] | None = None


class Cassette(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "m4-cassette-v1"
    entries: list[CassetteEntry] = Field(default_factory=list)


class RecordingLLM:
    """Wraps an `LLMPort` and records every `LLMResponse` in call order."""

    def __init__(self, inner: LLMPort) -> None:
        self._inner = inner
        self.model = getattr(inner, "model", "recorded-llm")
        self.responses: list[LLMResponse] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        resp = await self._inner.generate(request)
        self.responses.append(resp)
        return resp


class ReplayLLM:
    """Replays a pre-recorded sequence of `LLMResponse` objects."""

    def __init__(self, responses: Sequence[LLMResponse], *, model: str = "cassette-replay") -> None:
        self._queue: list[LLMResponse] = list(responses)
        self.model = model

    @property
    def remaining(self) -> int:
        return len(self._queue)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        del request
        if not self._queue:
            raise CassetteMismatchError(
                "Cassette exhausted: orchestrator requested more LLM turns than recorded."
            )
        return self._queue.pop(0)


def cassettes_dir() -> Path:
    return frozen_dir().parents[1] / "evals" / "cassettes"


async def record_cassette_entry_async(
    scenario: Scenario,
    *,
    llm: LLMPort | None = None,
) -> CassetteEntry:
    recorder = RecordingLLM(llm or InvestigativeFakeLLM())
    result = await investigate(
        scenario.incident,
        SimulatorTools(scenario.world),
        recorder,
        known_services=scenario.world.services,
    )
    return CassetteEntry(
        scenario_id=scenario.id,
        llm_responses=recorder.responses,
        expected_status=result.status,
        expected_ledger_keys=list(result.ledger_keys),
        expected_tool_calls=result.tool_calls,
        expected_first_output_illegal_citation=result.first_output_illegal_citation,
        expected_first_output_illegal_citation_rate=result.first_output_illegal_citation_rate,
        expected_accepted_illegal_citation_rate=result.accepted_illegal_citation_rate,
        expected_report=(None if result.report is None else result.report.model_dump(mode="json")),
    )


async def record_m4_cassette_async(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> Cassette:
    ordered = sorted(scenarios or load_frozen_scenarios(), key=lambda item: item.id)
    entries: list[CassetteEntry] = []
    for scenario in ordered:
        entries.append(await record_cassette_entry_async(scenario, llm=llm))
    return Cassette(entries=entries)


def record_m4_cassette(
    scenarios: list[Scenario] | None = None,
    *,
    llm: LLMPort | None = None,
) -> Cassette:
    return asyncio.run(record_m4_cassette_async(scenarios, llm=llm))


async def replay_cassette_entry_async(
    entry: CassetteEntry,
    scenario: Scenario,
) -> InvestigationResult:
    if entry.scenario_id != scenario.id:
        raise CassetteMismatchError(
            f"Scenario ID mismatch: entry={entry.scenario_id!r} vs scenario={scenario.id!r}."
        )
    replay_llm = ReplayLLM(entry.llm_responses)
    result = await investigate(
        scenario.incident,
        SimulatorTools(scenario.world),
        replay_llm,
        known_services=scenario.world.services,
    )
    if replay_llm.remaining != 0:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} had {replay_llm.remaining} unconsumed LLM response(s)."
        )
    if result.status != entry.expected_status:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} status mismatch: "
            f"got {result.status!r}, expected {entry.expected_status!r}."
        )
    if list(result.ledger_keys) != entry.expected_ledger_keys:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} ledger_keys mismatch: "
            f"got {list(result.ledger_keys)!r}, expected {entry.expected_ledger_keys!r}."
        )
    if result.tool_calls != entry.expected_tool_calls:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} tool_calls mismatch: "
            f"got {result.tool_calls}, expected {entry.expected_tool_calls}."
        )
    if result.first_output_illegal_citation != entry.expected_first_output_illegal_citation:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} first_output_illegal_citation mismatch: "
            f"got {result.first_output_illegal_citation}, "
            f"expected {entry.expected_first_output_illegal_citation}."
        )
    if (
        result.first_output_illegal_citation_rate
        != entry.expected_first_output_illegal_citation_rate
    ):
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} rewrote first_output_illegal_citation_rate: "
            f"got {result.first_output_illegal_citation_rate}, "
            f"expected {entry.expected_first_output_illegal_citation_rate}."
        )
    if result.accepted_illegal_citation_rate != entry.expected_accepted_illegal_citation_rate:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} accepted_illegal_citation_rate mismatch: "
            f"got {result.accepted_illegal_citation_rate}, "
            f"expected {entry.expected_accepted_illegal_citation_rate}."
        )
    actual_report = None if result.report is None else result.report.model_dump(mode="json")
    if actual_report != entry.expected_report:
        raise CassetteMismatchError(
            f"Cassette {entry.scenario_id!r} report mismatch: "
            f"got {actual_report!r}, expected {entry.expected_report!r}."
        )
    return result


async def replay_m4_cassette_async(
    cassette: Cassette,
    scenarios: list[Scenario] | None = None,
) -> list[InvestigationResult]:
    scenario_map = {s.id: s for s in (scenarios or load_frozen_scenarios())}
    entry_ids = [e.scenario_id for e in cassette.entries]
    if sorted(entry_ids) != sorted(scenario_map):
        raise CassetteMismatchError(
            f"Cassette scenario set {sorted(entry_ids)} does not match {sorted(scenario_map)}."
        )
    results: list[InvestigationResult] = []
    for entry in cassette.entries:
        results.append(await replay_cassette_entry_async(entry, scenario_map[entry.scenario_id]))
    return results


def replay_m4_cassette(
    cassette: Cassette,
    scenarios: list[Scenario] | None = None,
) -> list[InvestigationResult]:
    return asyncio.run(replay_m4_cassette_async(cassette, scenarios))


def load_cassette(path: Path | None = None) -> Cassette:
    target = path or (cassettes_dir() / CASSETTE_NAME)
    if not target.exists():
        raise CassetteMismatchError(f"Missing cassette file: {target}")
    raw = json.loads(target.read_text(encoding="utf-8"))
    return Cassette.model_validate(raw)


def save_cassette(cassette: Cassette, path: Path | None = None) -> Path:
    target = path or (cassettes_dir() / CASSETTE_NAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(cassette.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return target
