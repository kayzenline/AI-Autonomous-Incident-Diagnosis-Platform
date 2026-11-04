"""M7 LangGraph-orchestrated investigation loop with step checkpoints (FR-4, FR-5, §4.4, §6 M7).

Compiles an explicit `StateGraph` over:
  triage -> recall -> investigate_step <-> tool_dispatch -> verify -> finalize

Supports step-level checkpoint persistence (`checkpoint_saver`) and checkpoint
recovery (`resume_checkpoint`) without re-executing completed tool calls, while
preserving zero regression against the M4 hand-written loop.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.investigator import (
    INVESTIGATOR_SYSTEM_PROMPT,
    InvestigationBudget,
    InvestigationResult,
    InvestigationStatus,
    _apply_hypothesis_updates,
    _build_partial_report,
    _dispatch_search_memory,
    _dispatch_tool,
    _finalize_memory_citations,
    _format_memory_clue,
    _parse_inferred_service,
    _parse_json_dict,
    _to_recall_record,
    investigation_contract,
)
from app.agent.ledger import (
    EvidenceLedger,
    HypothesisBoard,
    HypothesisStatus,
    TraceEvent,
    TraceEventType,
)
from app.agent.verifier import ReadOnlyVerifier
from app.core.clock import ClockPort, WallClock
from app.core.report import DiagnosisReport
from app.core.taxonomy import CAUSE_CATEGORY, Cause, Verdict
from app.core.telemetry import traced
from app.core.window import TimeRange, default_window
from app.llm.port import LLMMessage, LLMPort, LLMRequest
from app.memory.models import (
    MemoryLayer,
    MemoryQuery,
    MemoryRecallRecord,
    PriorRunContext,
)
from app.memory.port import MemoryPort
from app.schemas.incident import IncidentCreate
from app.tools.port import AsyncToolPort, ToolPort, ensure_async_tools

CheckpointSaver = Callable[[int, str, dict[str, Any]], Awaitable[None]]


class InterruptedAfterCheckpointError(RuntimeError):
    """Raised in controlled recovery experiments after persisting step N's checkpoint."""

    def __init__(self, step_index: int) -> None:
        self.step_index = step_index
        super().__init__(f"Interrupted after saving checkpoint at step_index={step_index}.")


class GraphState(TypedDict, total=False):
    resolved_service: str | None
    inferred_by_model: bool
    window: TimeRange | None
    messages: list[LLMMessage]
    tokens_in: int
    tokens_out: int
    model_rounds: int
    no_evidence_streak: int
    next_route: str
    pending_tool_name: str
    pending_tool_args: dict[str, Any]
    raw_final_report: str | None
    explicit_cited_memory_ids: list[str] | None
    terminal_status: InvestigationStatus
    final_report: DiagnosisReport | None
    failure_reason: str | None
    verifier_runs: int
    verifier_tool_calls: int
    first_output_illegal_citation: bool
    first_output_illegal_citation_rate: float
    accepted_illegal_citation_rate: float
    final_recalls: tuple[MemoryRecallRecord, ...]
    cited_memory_ids: tuple[str, ...]
    result: InvestigationResult


def serialize_checkpoint_state(
    *,
    resolved_service: str | None,
    inferred_by_model: bool,
    window: TimeRange | None,
    messages: Sequence[LLMMessage],
    ledger: EvidenceLedger,
    board: HypothesisBoard,
    recall_map: dict[str, MemoryRecallRecord],
    tokens_in: int,
    tokens_out: int,
    model_rounds: int,
    no_evidence_streak: int,
    events: Sequence[TraceEvent],
) -> dict[str, Any]:
    return {
        "resolved_service": resolved_service,
        "inferred_by_model": inferred_by_model,
        "window": None if window is None else window.model_dump(mode="json"),
        "messages": [msg.model_dump(mode="json") for msg in messages],
        "ledger": ledger.to_checkpoint(),
        "board": board.to_checkpoint(),
        "recalls": [rec.model_dump(mode="json") for rec in recall_map.values()],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "model_rounds": model_rounds,
        "no_evidence_streak": no_evidence_streak,
        "events": [ev.model_dump(mode="json") for ev in events],
    }


@traced("agentops.investigate_langgraph")
async def investigate_with_langgraph(
    incident: IncidentCreate,
    tools: ToolPort | AsyncToolPort,
    llm: LLMPort,
    *,
    budget: InvestigationBudget | None = None,
    wall_clock: ClockPort | None = None,
    scenario_clock: ClockPort | None = None,
    known_services: Sequence[str] | None = None,
    memory: MemoryPort | None = None,
    memory_layers: Sequence[MemoryLayer] | None = None,
    service_version: str | None = None,
    prior_context: PriorRunContext | None = None,
    is_eval: bool = False,
    on_event: Callable[[TraceEvent], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
    checkpoint_saver: CheckpointSaver | None = None,
    resume_checkpoint: dict[str, Any] | None = None,
    stop_after_tool_calls: int | None = None,
) -> InvestigationResult:
    cfg = budget or InvestigationBudget()
    w_clock = wall_clock or WallClock()
    started_wall = w_clock.now()
    async_tools = ensure_async_tools(tools)

    inherited_causes: tuple[Cause, ...] = (
        () if is_eval or prior_context is None else tuple(prior_context.rejected_causes)
    )
    events: list[TraceEvent] = []
    ledger = EvidenceLedger()
    board = HypothesisBoard(initial_rejected=set(inherited_causes))
    recall_map: dict[str, MemoryRecallRecord] = {}

    if resume_checkpoint is not None:
        ledger = EvidenceLedger.from_checkpoint(resume_checkpoint.get("ledger") or {})
        board = HypothesisBoard.from_checkpoint(
            resume_checkpoint.get("board") or [],
            initial_rejected=set(inherited_causes),
        )
        for raw_rec in resume_checkpoint.get("recalls") or []:
            rec = MemoryRecallRecord.model_validate(raw_rec)
            recall_map[rec.memory_id] = rec
        for raw_ev in resume_checkpoint.get("events") or []:
            events.append(TraceEvent.model_validate(raw_ev))

    topology = tuple(dict.fromkeys(known_services or getattr(async_tools, "services", ())))
    detected_at = incident.detected_at or (
        scenario_clock.now() if scenario_clock is not None else None
    )

    active_layers: tuple[MemoryLayer, ...] = ()
    if memory is not None:
        active_layers = tuple(
            memory_layers if memory_layers is not None else memory.config.enabled_layers
        )
    memory_enabled = memory is not None and bool(active_layers)
    resolved_version = service_version
    if resolved_version is None and isinstance(incident.metadata, dict):
        raw_ver = incident.metadata.get("service_version")
        if isinstance(raw_ver, str) and raw_ver:
            resolved_version = raw_ver

    async def _emit(event_type: TraceEventType, summary: str, **payload: Any) -> None:
        event = TraceEvent(
            seq=len(events),
            type=event_type,
            created_at=w_clock.now(),
            summary=summary,
            payload=payload,
        )
        events.append(event)
        if on_event is not None:
            await on_event(event)

    def _cost(tokens_in: int, tokens_out: int) -> float:
        return round(((tokens_in + tokens_out) / 1000.0) * cfg.cost_per_1k_tokens_usd, 6)

    def _wall_exceeded() -> bool:
        elapsed = (w_clock.now() - started_wall).total_seconds()
        return elapsed > cfg.wall_timeout_seconds

    def _token_or_cost_exceeded(tokens_in: int, tokens_out: int) -> str | None:
        if (tokens_in + tokens_out) > cfg.max_tokens:
            return "token_budget_exceeded"
        if _cost(tokens_in, tokens_out) > cfg.max_cost_usd:
            return "cost_budget_exceeded"
        return None

    async def _save_checkpoint(step_index: int, node_name: str, state: GraphState) -> None:
        if checkpoint_saver is None:
            return
        snapshot = serialize_checkpoint_state(
            resolved_service=state.get("resolved_service"),
            inferred_by_model=bool(state.get("inferred_by_model", False)),
            window=state.get("window"),
            messages=state.get("messages") or [],
            ledger=ledger,
            board=board,
            recall_map=recall_map,
            tokens_in=int(state.get("tokens_in", 0)),
            tokens_out=int(state.get("tokens_out", 0)),
            model_rounds=int(state.get("model_rounds", 0)),
            no_evidence_streak=int(state.get("no_evidence_streak", 0)),
            events=events,
        )
        await checkpoint_saver(step_index, node_name, snapshot)

    async def triage_node(state: GraphState) -> GraphState:
        resolved_service = incident.service
        inferred_by_model = False
        tokens_in = int(state.get("tokens_in", 0))
        tokens_out = int(state.get("tokens_out", 0))
        model_rounds = int(state.get("model_rounds", 0))
        window = default_window(incident.occurred_at, detected_at)

        if resolved_service is None:
            triage_prompt = {
                "task": "triage_infer_service",
                "title": incident.title,
                "description": incident.description,
                "known_services": list(topology),
            }
            triage_resp = await llm.generate(
                LLMRequest(
                    messages=[
                        LLMMessage(
                            role="system",
                            content='Infer the incident service as JSON: {"service": "<name>"}.',
                        ),
                        LLMMessage(role="user", content=json.dumps(triage_prompt, sort_keys=True)),
                    ],
                    temperature=0.0,
                )
            )
            model_rounds += 1
            tokens_in += triage_resp.tokens_in
            tokens_out += triage_resp.tokens_out
            resolved_service = _parse_inferred_service(triage_resp.content, topology)
            inferred_by_model = resolved_service is not None
            await _emit(
                TraceEventType.LLM_CALL,
                f"Model inferred missing service as {resolved_service!r}.",
                inferred_by_model=True,
                service=resolved_service,
            )

        await _emit(
            TraceEventType.RUN_STARTED,
            "Investigation started.",
            service=resolved_service,
            service_inferred_by_model=inferred_by_model,
            window=None if window is None else window.model_dump(mode="json"),
        )

        for rej_cause in inherited_causes:
            await _emit(
                TraceEventType.HYPOTHESIS_UPDATED,
                f"Inherited rejected hypothesis {rej_cause.value} from prior run.",
                cause=rej_cause.value,
                cause_summary="Previously rejected hypothesis.",
                category=CAUSE_CATEGORY[rej_cause].value,
                confidence=0.0,
                status=HypothesisStatus.REJECTED.value,
                version=1,
                inherited=True,
            )

        if resolved_service is None or window is None:
            unknown_report = DiagnosisReport(
                verdict=Verdict.UNKNOWN,
                unresolved_questions=["Incident is missing service or valid time window."],
            )
            await _emit(
                TraceEventType.FINAL_REPORT,
                "Returned UNKNOWN due to missing service or window.",
            )
            return {
                "resolved_service": resolved_service,
                "inferred_by_model": inferred_by_model,
                "window": window,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model_rounds": model_rounds,
                "terminal_status": "COMPLETED",
                "final_report": unknown_report,
                "next_route": "finalize",
            }

        return {
            "resolved_service": resolved_service,
            "inferred_by_model": inferred_by_model,
            "window": window,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model_rounds": model_rounds,
            "next_route": "recall",
        }

    async def recall_node(state: GraphState) -> GraphState:
        resolved_service = state["resolved_service"]
        window = state["window"]
        assert resolved_service is not None and window is not None

        if memory_enabled and memory is not None:
            initial_scored = await memory.search(
                MemoryQuery(
                    query=f"{incident.title} {incident.description}",
                    service=resolved_service,
                    layers=active_layers,
                    include_candidate=True,
                    service_version=resolved_version,
                    reference_time=detected_at,
                    limit=5,
                )
            )
            for sm in initial_scored:
                recall_map[sm.item.id] = _to_recall_record(sm, injected=True)

        initial_payload: dict[str, Any] = {
            "task": "investigate",
            **investigation_contract(include_search_memory=memory_enabled),
            "known_services": list(topology),
            "incident": {
                "title": incident.title,
                "description": incident.description,
                "service": resolved_service,
                "occurred_at": (
                    None if incident.occurred_at is None else incident.occurred_at.isoformat()
                ),
                "detected_at": None if detected_at is None else detected_at.isoformat(),
            },
            "default_window": window.model_dump(mode="json"),
            "max_tool_calls": cfg.max_tool_calls,
        }
        if memory_enabled:
            initial_payload["memory_policy"] = (
                "Memories are clues only and must be verified against current tool evidence. "
                "Never diagnose a fault without corroborating current tool evidence in "
                "EvidenceLedger."
            )
            initial_payload["service_version"] = resolved_version
            initial_payload["recalled_memories"] = [
                _format_memory_clue(rec) for rec in recall_map.values()
            ]
        if (
            not is_eval
            and prior_context is not None
            and (prior_context.rejected_causes or prior_context.evidence_summaries)
        ):
            initial_payload["inherited_prior_runs"] = {
                "prior_run_ids": list(prior_context.prior_run_ids),
                "rejected_causes": [c.value for c in prior_context.rejected_causes],
                "evidence_summaries": list(prior_context.evidence_summaries),
            }

        messages = [
            LLMMessage(role="system", content=INVESTIGATOR_SYSTEM_PROMPT),
            LLMMessage(role="user", content=json.dumps(initial_payload, sort_keys=True)),
        ]
        updated: GraphState = {
            **state,
            "messages": messages,
            "no_evidence_streak": 0,
            "next_route": "investigate_step",
        }
        await _save_checkpoint(0, "recall", updated)
        return updated

    async def investigate_step_node(state: GraphState) -> GraphState:
        messages = list(state.get("messages") or [])
        tokens_in = int(state.get("tokens_in", 0))
        tokens_out = int(state.get("tokens_out", 0))
        model_rounds = int(state.get("model_rounds", 0))

        if cancel_event is not None and cancel_event.is_set():
            await _emit(TraceEventType.ERROR, "Run cancelled.", reason="cancelled")
            return {
                "terminal_status": "FAILED",
                "final_report": None,
                "failure_reason": "cancelled",
                "next_route": "finalize",
            }

        if _wall_exceeded():
            await _emit(
                TraceEventType.GUARDRAIL_TRIGGERED,
                "Wall-clock timeout exceeded.",
                reason="wall_clock_timeout",
            )
            partial = _build_partial_report(board, "Wall-clock timeout reached before completion.")
            await _emit(TraceEventType.FINAL_REPORT, "Emitted partial report on timeout.")
            return {
                "terminal_status": "TIMED_OUT",
                "final_report": partial,
                "next_route": "finalize",
            }

        budget_reason = _token_or_cost_exceeded(tokens_in, tokens_out)
        if budget_reason is not None:
            await _emit(
                TraceEventType.GUARDRAIL_TRIGGERED,
                f"Budget guardrail triggered: {budget_reason}.",
                reason=budget_reason,
            )
            partial = _build_partial_report(board, f"Run stopped early: {budget_reason}.")
            await _emit(TraceEventType.FINAL_REPORT, "Emitted partial report on budget exhaustion.")
            return {
                "terminal_status": "BUDGET_EXCEEDED",
                "final_report": partial,
                "next_route": "finalize",
            }

        if model_rounds >= cfg.max_model_rounds:
            await _emit(
                TraceEventType.GUARDRAIL_TRIGGERED,
                "Maximum model rounds reached.",
                reason="max_model_rounds",
            )
            partial = _build_partial_report(board, "Maximum model rounds reached.")
            await _emit(TraceEventType.FINAL_REPORT, "Emitted partial report on max model rounds.")
            return {
                "terminal_status": "BUDGET_EXCEEDED",
                "final_report": partial,
                "next_route": "finalize",
            }

        step_resp = await llm.generate(
            LLMRequest(messages=messages, temperature=0.0, response_schema={"type": "object"})
        )
        model_rounds += 1
        tokens_in += step_resp.tokens_in
        tokens_out += step_resp.tokens_out
        messages.append(LLMMessage(role="assistant", content=step_resp.content))

        step_data = _parse_json_dict(step_resp.content)
        action = step_data.get("action") if step_data else None
        decision_summary = str((step_data or {}).get("summary") or "Model step completed.")
        await _emit(
            TraceEventType.LLM_CALL,
            decision_summary,
            action=action,
            round=model_rounds,
        )

        if step_data and isinstance(step_data.get("hypotheses"), list):
            await _apply_hypothesis_updates(board, step_data["hypotheses"], _emit)

        explicit_cited_memory_ids = state.get("explicit_cited_memory_ids")
        if action == "final_report" and step_data is not None and "report" in step_data:
            if isinstance(step_data.get("cited_memory_ids"), list):
                explicit_cited_memory_ids = [
                    str(mid) for mid in step_data["cited_memory_ids"] if isinstance(mid, str)
                ]
            report_obj = step_data["report"]
            raw_final_report = report_obj if isinstance(report_obj, str) else json.dumps(report_obj)
            return {
                "messages": messages,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model_rounds": model_rounds,
                "raw_final_report": raw_final_report,
                "explicit_cited_memory_ids": explicit_cited_memory_ids,
                "next_route": "verify",
            }

        if action != "call_tool" or step_data is None:
            return {
                "messages": messages,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model_rounds": model_rounds,
                "raw_final_report": step_resp.content,
                "explicit_cited_memory_ids": explicit_cited_memory_ids,
                "next_route": "verify",
            }

        tool_name = str(step_data.get("tool") or "")
        raw_args = step_data.get("arguments")
        args_dict = raw_args if isinstance(raw_args, dict) else {}
        return {
            "messages": messages,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model_rounds": model_rounds,
            "pending_tool_name": tool_name,
            "pending_tool_args": args_dict,
            "next_route": "tool_dispatch",
        }

    async def tool_dispatch_node(state: GraphState) -> GraphState:
        messages = list(state.get("messages") or [])
        tool_name = str(state.get("pending_tool_name") or "")
        args_dict = dict(state.get("pending_tool_args") or {})
        resolved_service = state["resolved_service"]
        window = state["window"]
        no_evidence_streak = int(state.get("no_evidence_streak", 0))
        assert resolved_service is not None and window is not None

        cached = ledger.find_cached_call(tool_name, args_dict)
        if cached is not None:
            await _emit(
                TraceEventType.GUARDRAIL_TRIGGERED,
                f"Duplicate tool call suppressed: {tool_name}.",
                reason="duplicate_tool_call",
                tool=tool_name,
            )
            messages.append(
                LLMMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "DuplicateToolCall": True,
                            "cached_result": cached.model_dump(mode="json"),
                            "ledger_keys": list(ledger.keys()),
                            "remaining_tool_calls": cfg.max_tool_calls - len(ledger.tool_calls),
                        },
                        sort_keys=True,
                    ),
                )
            )
            return {"messages": messages, "next_route": "investigate_step"}

        if len(ledger.tool_calls) >= cfg.max_tool_calls:
            await _emit(
                TraceEventType.GUARDRAIL_TRIGGERED,
                "Maximum tool call budget reached.",
                reason="max_tool_calls",
                max_tool_calls=cfg.max_tool_calls,
            )
            partial = _build_partial_report(
                board,
                f"Reached maximum of {cfg.max_tool_calls} tool calls before final report.",
            )
            await _emit(
                TraceEventType.FINAL_REPORT,
                "Emitted partial report on tool budget exhaustion.",
            )
            return {
                "terminal_status": "BUDGET_EXCEEDED",
                "final_report": partial,
                "next_route": "finalize",
            }

        await _emit(
            TraceEventType.TOOL_CALL,
            f"Calling {tool_name}.",
            tool=tool_name,
            arguments=args_dict,
        )
        started_call = time.perf_counter()
        if tool_name == "search_memory" and memory_enabled and memory is not None:
            tool_result, service_used, range_used = await _dispatch_search_memory(
                args_dict=args_dict,
                default_service=resolved_service,
                default_range=window,
                memory=memory,
                active_layers=active_layers,
                service_version=resolved_version,
                reference_time=detected_at,
                recall_map=recall_map,
            )
        else:
            tool_result, service_used, range_used = await _dispatch_tool(
                tool_name=tool_name,
                args_dict=args_dict,
                default_service=resolved_service,
                default_range=window,
                tools=async_tools,
            )
        latency_ms = round((time.perf_counter() - started_call) * 1000, 3)
        added_items = ledger.record_tool_call(
            tool=tool_name,
            service=service_used,
            time_range=range_used,
            arguments=args_dict,
            result=tool_result,
        )
        await _emit(
            TraceEventType.TOOL_RESULT,
            tool_result.summary,
            tool=tool_name,
            arguments=args_dict,
            status=tool_result.status.value,
            evidence_keys=list(tool_result.evidence_keys),
            truncated=tool_result.truncated,
            output=tool_result.model_dump(mode="json"),
            service=service_used,
            time_range=range_used.model_dump(mode="json"),
            latency_ms=latency_ms,
        )
        for item in added_items:
            await _emit(
                TraceEventType.EVIDENCE_ADDED,
                f"Added evidence key {item.evidence_key}.",
                evidence_key=item.evidence_key,
                tool=item.tool,
                service=item.service,
            )

        if added_items:
            no_evidence_streak = 0
        else:
            no_evidence_streak += 1

        force_converge = (
            len(ledger.tool_calls) >= cfg.max_tool_calls
            or no_evidence_streak >= cfg.max_no_evidence_steps
        )
        if no_evidence_streak >= cfg.max_no_evidence_steps:
            await _emit(
                TraceEventType.GUARDRAIL_TRIGGERED,
                "Consecutive steps yielded no new evidence keys; forcing convergence.",
                reason="no_new_evidence_convergence",
                streak=no_evidence_streak,
            )

        observation_msg: dict[str, Any] = {
            "tool": tool_name,
            "arguments": args_dict,
            "result": tool_result.model_dump(mode="json"),
            "ledger_keys": list(ledger.keys()),
            "tool_calls_used": len(ledger.tool_calls),
            "remaining_tool_calls": cfg.max_tool_calls - len(ledger.tool_calls),
            "must_emit_final_report_now": force_converge,
        }
        messages.append(
            LLMMessage(role="user", content=json.dumps(observation_msg, sort_keys=True))
        )
        step_idx = len(ledger.tool_calls)
        updated: GraphState = {
            **state,
            "messages": messages,
            "no_evidence_streak": no_evidence_streak,
            "next_route": "investigate_step",
        }
        await _save_checkpoint(step_idx, "tool_dispatch", updated)
        if stop_after_tool_calls is not None and step_idx >= stop_after_tool_calls:
            raise InterruptedAfterCheckpointError(step_idx)
        return updated

    async def verify_node(state: GraphState) -> GraphState:
        raw_final_report = state.get("raw_final_report") or ""
        explicit_cited_memory_ids = state.get("explicit_cited_memory_ids")
        messages = list(state.get("messages") or [])
        tokens_in = int(state.get("tokens_in", 0))
        tokens_out = int(state.get("tokens_out", 0))
        model_rounds = int(state.get("model_rounds", 0))

        pre_verify_tool_calls = len(ledger.tool_calls)
        verifier = ReadOnlyVerifier(ledger, board)
        first_check = verifier.verify_raw(raw_final_report)
        verifier_runs = 1
        first_has_illegal = bool(first_check.illegal_keys)
        first_illegal_rate = first_check.illegal_citation_rate

        if first_check.valid and first_check.report is not None:
            final_recalls, cited_ids = _finalize_memory_citations(
                recall_map,
                first_check.report,
                board,
                explicit_cited_memory_ids,
            )
            await _emit(
                TraceEventType.FINAL_REPORT,
                f"Final report verified with verdict {first_check.report.verdict.value}.",
                verdict=first_check.report.verdict.value,
                attempts=1,
                cited_memory_ids=list(cited_ids),
            )
            return {
                "terminal_status": "COMPLETED",
                "final_report": first_check.report,
                "verifier_runs": verifier_runs,
                "verifier_tool_calls": len(ledger.tool_calls) - pre_verify_tool_calls,
                "first_output_illegal_citation": False,
                "first_output_illegal_citation_rate": 0.0,
                "accepted_illegal_citation_rate": 0.0,
                "final_recalls": final_recalls,
                "cited_memory_ids": cited_ids,
                "next_route": "finalize",
            }

        await _emit(
            TraceEventType.GUARDRAIL_TRIGGERED,
            f"First report rejected by verifier: {first_check.error}",
            reason="report_verification_failed",
            illegal_keys=list(first_check.illegal_keys),
        )
        retry_messages = [
            *messages,
            LLMMessage(
                role="user",
                content=json.dumps(
                    {
                        "task": "regenerate_final_report",
                        "report_schema": DiagnosisReport.model_json_schema(),
                        "instruction": "Return only a DiagnosisReport JSON object. No tool calls.",
                        "error": first_check.error,
                        "allowed_evidence_keys": list(ledger.keys()),
                    },
                    sort_keys=True,
                ),
            ),
        ]
        retry_resp = await llm.generate(
            LLMRequest(
                messages=retry_messages,
                temperature=0.0,
                response_schema=DiagnosisReport.model_json_schema(),
            )
        )
        model_rounds += 1
        tokens_in += retry_resp.tokens_in
        tokens_out += retry_resp.tokens_out
        await _emit(
            TraceEventType.LLM_CALL,
            "Regenerated final report after verifier rejection.",
            action="regenerate_final_report",
            round=model_rounds,
        )

        retry_parsed = _parse_json_dict(retry_resp.content)
        if (
            retry_parsed
            and retry_parsed.get("action") == "final_report"
            and "report" in retry_parsed
        ):
            if isinstance(retry_parsed.get("cited_memory_ids"), list):
                explicit_cited_memory_ids = [
                    str(mid) for mid in retry_parsed["cited_memory_ids"] if isinstance(mid, str)
                ]
            rep_val = retry_parsed["report"]
            retry_raw = rep_val if isinstance(rep_val, str) else json.dumps(rep_val)
        else:
            retry_raw = retry_resp.content

        second_check = verifier.verify_raw(retry_raw)
        if second_check.valid and second_check.report is not None:
            final_recalls, cited_ids = _finalize_memory_citations(
                recall_map,
                second_check.report,
                board,
                explicit_cited_memory_ids,
            )
            await _emit(
                TraceEventType.FINAL_REPORT,
                f"Retry report verified with verdict {second_check.report.verdict.value}.",
                verdict=second_check.report.verdict.value,
                attempts=2,
                cited_memory_ids=list(cited_ids),
            )
            return {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "model_rounds": model_rounds,
                "terminal_status": "COMPLETED",
                "final_report": second_check.report,
                "verifier_runs": verifier_runs,
                "verifier_tool_calls": len(ledger.tool_calls) - pre_verify_tool_calls,
                "first_output_illegal_citation": first_has_illegal,
                "first_output_illegal_citation_rate": first_illegal_rate,
                "accepted_illegal_citation_rate": 0.0,
                "final_recalls": final_recalls,
                "cited_memory_ids": cited_ids,
                "next_route": "finalize",
            }

        await _emit(
            TraceEventType.ERROR,
            f"Second report failed verification: {second_check.error}",
            error=second_check.error,
        )
        return {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model_rounds": model_rounds,
            "terminal_status": "FAILED",
            "final_report": None,
            "failure_reason": second_check.error or first_check.error,
            "verifier_runs": verifier_runs,
            "verifier_tool_calls": len(ledger.tool_calls) - pre_verify_tool_calls,
            "first_output_illegal_citation": first_has_illegal,
            "first_output_illegal_citation_rate": first_illegal_rate,
            "accepted_illegal_citation_rate": 0.0,
            "final_recalls": tuple(recall_map.values()),
            "cited_memory_ids": (),
            "next_route": "finalize",
        }

    async def finalize_node(state: GraphState) -> GraphState:
        tokens_in = int(state.get("tokens_in", 0))
        tokens_out = int(state.get("tokens_out", 0))
        recalls = state.get("final_recalls")
        if recalls is None:
            recalls = tuple(recall_map.values())
        result = InvestigationResult(
            status=state.get("terminal_status", "FAILED"),
            report=state.get("final_report"),
            ledger_keys=ledger.keys(),
            tool_calls=len(ledger.tool_calls),
            model_rounds=int(state.get("model_rounds", 0)),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=_cost(tokens_in, tokens_out),
            service_inferred_by_model=bool(state.get("inferred_by_model", False)),
            resolved_service=state.get("resolved_service"),
            verifier_runs=int(state.get("verifier_runs", 0)),
            verifier_tool_calls=int(state.get("verifier_tool_calls", 0)),
            first_output_illegal_citation=bool(state.get("first_output_illegal_citation", False)),
            first_output_illegal_citation_rate=float(
                state.get("first_output_illegal_citation_rate", 0.0)
            ),
            accepted_illegal_citation_rate=float(state.get("accepted_illegal_citation_rate", 0.0)),
            recalled_memories=recalls,
            cited_memory_ids=state.get("cited_memory_ids", ()),
            inherited_rejected_causes=inherited_causes,
            events=tuple(events),
            failure_reason=state.get("failure_reason"),
        )
        return {"result": result}

    def _route_from_state(state: GraphState) -> str:
        return str(state.get("next_route") or "finalize")

    graph = StateGraph(GraphState)
    graph.add_node("triage", triage_node)
    graph.add_node("recall", recall_node)
    graph.add_node("investigate_step", investigate_step_node)
    graph.add_node("tool_dispatch", tool_dispatch_node)
    graph.add_node("verify", verify_node)
    graph.add_node("finalize", finalize_node)

    if resume_checkpoint is not None:
        graph.add_edge(START, "investigate_step")
    else:
        graph.add_edge(START, "triage")

    graph.add_conditional_edges(
        "triage",
        _route_from_state,
        {"recall": "recall", "finalize": "finalize"},
    )
    graph.add_edge("recall", "investigate_step")
    graph.add_conditional_edges(
        "investigate_step",
        _route_from_state,
        {
            "tool_dispatch": "tool_dispatch",
            "verify": "verify",
            "finalize": "finalize",
        },
    )
    graph.add_conditional_edges(
        "tool_dispatch",
        _route_from_state,
        {
            "investigate_step": "investigate_step",
            "finalize": "finalize",
        },
    )
    graph.add_edge("verify", "finalize")
    graph.add_edge("finalize", END)

    compiled = graph.compile()

    initial_state: GraphState = {
        "tokens_in": 0,
        "tokens_out": 0,
        "model_rounds": 0,
        "no_evidence_streak": 0,
    }
    if resume_checkpoint is not None:
        win_raw = resume_checkpoint.get("window")
        initial_state = {
            "resolved_service": resume_checkpoint.get("resolved_service"),
            "inferred_by_model": bool(resume_checkpoint.get("inferred_by_model", False)),
            "window": None if win_raw is None else TimeRange.model_validate(win_raw),
            "messages": [
                LLMMessage.model_validate(m) for m in (resume_checkpoint.get("messages") or [])
            ],
            "tokens_in": int(resume_checkpoint.get("tokens_in", 0)),
            "tokens_out": int(resume_checkpoint.get("tokens_out", 0)),
            "model_rounds": int(resume_checkpoint.get("model_rounds", 0)),
            "no_evidence_streak": int(resume_checkpoint.get("no_evidence_streak", 0)),
        }
        await _emit(
            TraceEventType.CHECKPOINT_RESUMED,
            f"Resumed run from checkpoint with {len(ledger.tool_calls)} completed tool call(s).",
            tool_calls_restored=len(ledger.tool_calls),
            model_rounds_restored=initial_state["model_rounds"],
        )

    final_state = await compiled.ainvoke(
        initial_state,
        config={"recursion_limit": max(100, cfg.max_model_rounds * 4)},
    )
    return final_state["result"]
