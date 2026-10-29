"""Run the M4 investigator without blocking the API loop.

Tool IO goes through ThreadedToolPort inside investigate(). Events are written
as they are emitted. A client disconnect does not cancel the task.
"""

import asyncio

from app.agent.investigator import InvestigationResult, investigate
from app.agent.ledger import TraceEvent
from app.core.clock import ScenarioClock
from app.core.telemetry import traced
from app.eval.scenario import frozen_dir, load_scenario
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.persistence.models import IncidentRow
from app.persistence.store import (
    append_event,
    finish_run,
    get_incident,
    get_run,
    mark_running,
)
from app.schemas.incident import IncidentCreate
from app.tools.empty import EmptyTools
from app.tools.simulator.adapter import SimulatorTools

pause_runs = False
_tasks: dict[str, asyncio.Task[None]] = {}
_cancel_events: dict[str, asyncio.Event] = {}


def schedule(run_id: str) -> None:
    _cancel_events[run_id] = asyncio.Event()
    task = asyncio.create_task(execute(run_id), name=f"run-{run_id}")
    _tasks[run_id] = task

    def _done(finished: asyncio.Task[None]) -> None:
        del finished
        _tasks.pop(run_id, None)
        _cancel_events.pop(run_id, None)

    task.add_done_callback(_done)


def request_cancel(run_id: str) -> None:
    event = _cancel_events.get(run_id)
    if event is not None:
        event.set()


@traced("agentops.run")
async def execute(run_id: str) -> None:
    started = await mark_running(run_id)
    if not started:
        return
    while pause_runs:
        current = await get_run(run_id)
        if current.status not in {"PENDING", "RUNNING"}:
            return
        await asyncio.sleep(0.01)
    try:
        run = await get_run(run_id)
        incident = await get_incident(run.incident_id)
        result = await _investigate(run_id, incident)
    except asyncio.CancelledError:
        await finish_run(run_id, "CANCELLED", None, "cancelled")
        raise
    except Exception:
        await finish_run(run_id, "FAILED", None, "executor_error")
        return
    status, report, reason = _outcome(result)
    await finish_run(
        run_id,
        status,
        report,
        reason,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost_usd=result.cost_usd,
    )


async def _investigate(run_id: str, incident_row: IncidentRow) -> InvestigationResult:
    run = await get_run(run_id)
    scenario_id = run.config.get("scenario_id")
    tools, services = _tools(scenario_id if isinstance(scenario_id, str) else None)
    incident = IncidentCreate(
        title=incident_row.title,
        description=incident_row.description,
        service=incident_row.service,
        occurred_at=incident_row.occurred_at,
        detected_at=incident_row.detected_at,
        user_severity=incident_row.user_severity,
        metadata=dict(incident_row.details or {}),
    )
    moment = incident.detected_at or incident.occurred_at
    scenario_clock = ScenarioClock(moment) if moment is not None else None

    async def on_event(event: TraceEvent) -> None:
        await append_event(run_id, event)

    return await investigate(
        incident,
        tools,
        InvestigativeFakeLLM(),
        known_services=services,
        scenario_clock=scenario_clock,
        on_event=on_event,
        cancel_event=_cancel_events.get(run_id),
    )


def _tools(scenario_id: str | None) -> tuple[EmptyTools | SimulatorTools, tuple[str, ...]]:
    if not scenario_id:
        return EmptyTools(), ()
    scenario = load_scenario(frozen_dir() / f"{scenario_id}.yaml")
    return SimulatorTools(scenario.world), tuple(scenario.world.services)


def _outcome(result: InvestigationResult) -> tuple[str, dict[str, object] | None, str | None]:
    if result.failure_reason == "cancelled":
        return "CANCELLED", None, "cancelled"
    report = None if result.report is None else result.report.model_dump(mode="json")
    if result.status == "COMPLETED":
        return "COMPLETED", report, None
    if result.status == "TIMED_OUT":
        return "TIMED_OUT", report, "wall_clock_timeout"
    if result.status == "BUDGET_EXCEEDED":
        return "BUDGET_EXCEEDED", report, "budget_exceeded"
    return "FAILED", None, result.failure_reason or "investigation_failed"
