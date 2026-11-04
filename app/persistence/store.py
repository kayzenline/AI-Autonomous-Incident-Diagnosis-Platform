"""Incident, run, hypothesis, feedback, and memory writes.

A second active run, and a run on an archived incident, are conflicts.
The same idempotency key returns the row that already exists.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.ledger import TraceEvent
from app.core.clock import WallClock
from app.core.taxonomy import CAUSE_CATEGORY, CAUSE_SUMMARY, Category, Cause
from app.core.window import _as_utc
from app.memory.models import (
    FeedbackVerdict,
    MemoryConfig,
    MemoryItem,
    MemoryLayer,
    MemoryQuery,
    MemoryRecallRecord,
    MemoryWrite,
    PriorRunContext,
    ScoredMemory,
    TrustLevel,
)
from app.memory.retrieval import embed_text, rank_memories, text_similarity
from app.persistence.models import (
    ActionApprovalRow,
    EvidenceRow,
    FeedbackRow,
    HypothesisEvidenceRow,
    HypothesisRow,
    IncidentRow,
    MemoryItemRow,
    MemoryRecallRow,
    RunCheckpointRow,
    RunEventRow,
    RunRow,
    ToolCallRow,
)
from app.persistence.outcomes import validate_outcome
from app.persistence.session import ensure_ready, get_sessionmaker
from app.schemas.incident import IncidentCreate
from app.tools.redact import redact_text

AGENT_VERSION = "m4"
PROMPT_VERSION = "m4-investigator-v1"
MODEL_NAME = "fake-m4-investigator-v1"


class StoreError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _stamp(moment: object) -> object:
    if moment is None:
        return None
    return _as_utc(moment)  # type: ignore[arg-type]


async def create_incident(body: IncidentCreate) -> IncidentRow:
    await ensure_ready()
    now = WallClock().now()
    row = IncidentRow(
        id=str(uuid4()),
        title=body.title,
        description=body.description,
        service=body.service,
        occurred_at=_stamp(body.occurred_at),
        detected_at=_stamp(body.detected_at),
        status="OPEN",
        user_severity=body.user_severity,
        details=dict(body.metadata),
        archived_at=None,
        created_at=now,
        updated_at=now,
    )
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            session.add(row)
        return row


async def list_incidents(limit: int, offset: int) -> list[IncidentRow]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        rows = await session.scalars(
            select(IncidentRow)
            .where(IncidentRow.archived_at.is_(None))
            .order_by(IncidentRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(rows)


async def get_incident(incident_id: str) -> IncidentRow:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        row = await session.get(IncidentRow, incident_id)
        if row is None:
            raise StoreError(404, "Incident not found.")
        return row


async def patch_incident(incident_id: str, changes: dict[str, Any]) -> IncidentRow:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            row = await session.get(IncidentRow, incident_id)
            if row is None:
                raise StoreError(404, "Incident not found.")
            if row.archived_at is not None:
                raise StoreError(409, "Incident is archived.")
            for key, value in changes.items():
                if key == "metadata":
                    row.details = dict(value)
                elif key in {"occurred_at", "detected_at"}:
                    setattr(row, key, _stamp(value))
                else:
                    setattr(row, key, value)
            row.updated_at = now
            return row


async def archive_incident(incident_id: str) -> None:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            row = await session.get(IncidentRow, incident_id)
            if row is None:
                raise StoreError(404, "Incident not found.")
            if row.archived_at is None:
                row.archived_at = now
                row.updated_at = now


async def start_run(
    incident_id: str,
    idempotency_key: str | None,
    scenario_id: str | None,
    *,
    inherit_prior_runs: bool = True,
    is_eval: bool = False,
    executor: str = "m7",
    checkpoint_policy: str = "fail",
) -> tuple[RunRow, bool]:
    await ensure_ready()
    factory = get_sessionmaker()
    try:
        async with factory() as session:
            async with session.begin():
                return await _insert_run(
                    session,
                    incident_id,
                    idempotency_key,
                    scenario_id,
                    inherit_prior_runs=inherit_prior_runs,
                    is_eval=is_eval,
                    executor=executor,
                    checkpoint_policy=checkpoint_policy,
                )
    except IntegrityError:
        return await _resolve_race(incident_id, idempotency_key)


async def _insert_run(
    session: object,
    incident_id: str,
    idempotency_key: str | None,
    scenario_id: str | None,
    *,
    inherit_prior_runs: bool = True,
    is_eval: bool = False,
    executor: str = "m7",
    checkpoint_policy: str = "fail",
) -> tuple[RunRow, bool]:
    assert isinstance(session, AsyncSession)
    incident = await session.get(IncidentRow, incident_id)
    if incident is None:
        raise StoreError(404, "Incident not found.")
    if idempotency_key:
        existing = await session.scalar(
            select(RunRow).where(
                RunRow.incident_id == incident_id,
                RunRow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing, False
    if incident.archived_at is not None:
        raise StoreError(409, "Incident is archived.")
    active = await session.scalar(
        select(RunRow).where(
            RunRow.incident_id == incident_id,
            RunRow.status.in_(("PENDING", "RUNNING")),
        )
    )
    if active is not None:
        raise StoreError(409, "Another run is still active.")
    now = WallClock().now()
    resolved_executor = executor if executor in {"m4", "m7"} else "m7"
    resolved_policy = checkpoint_policy if checkpoint_policy in {"fail", "resume"} else "fail"
    config_payload: dict[str, Any] = {
        "executor": resolved_executor,
        "checkpoint_policy": resolved_policy,
        "scenario_id": scenario_id,
        "llm": MODEL_NAME,
        "timeout_seconds": 60,
    }
    if not inherit_prior_runs or is_eval:
        config_payload["inherit_prior_runs"] = False
    if is_eval:
        config_payload["is_eval"] = True
    run = RunRow(
        id=str(uuid4()),
        incident_id=incident_id,
        idempotency_key=idempotency_key,
        status="PENDING",
        agent_version=resolved_executor,
        model=MODEL_NAME,
        prompt_version=PROMPT_VERSION,
        config=config_payload,
        started_at=None,
        ended_at=None,
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        final_report=None,
        failure_reason=None,
        created_at=now,
    )
    session.add(run)
    await session.flush()
    return run, True


async def _resolve_race(incident_id: str, idempotency_key: str | None) -> tuple[RunRow, bool]:
    factory = get_sessionmaker()
    async with factory() as session:
        if idempotency_key:
            existing = await session.scalar(
                select(RunRow).where(
                    RunRow.incident_id == incident_id,
                    RunRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing, False
        active = await session.scalar(
            select(RunRow).where(
                RunRow.incident_id == incident_id,
                RunRow.status.in_(("PENDING", "RUNNING")),
            )
        )
        if active is not None:
            raise StoreError(409, "Another run is still active.")
    raise StoreError(409, "Run was not created.")


async def get_run(run_id: str) -> RunRow:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        row = await session.get(RunRow, run_id)
        if row is None:
            raise StoreError(404, "Run not found.")
        return row


async def mark_running(run_id: str) -> bool:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None or run.status != "PENDING":
                return False
            run.status = "RUNNING"
            run.started_at = now
            incident = await session.get(IncidentRow, run.incident_id)
            if incident is not None and incident.status == "OPEN":
                incident.status = "INVESTIGATING"
                incident.updated_at = now
            return True


async def append_event(run_id: str, event: TraceEvent) -> None:
    """Write one timeline row. Tool results and hypothesis updates write linked rows in-tx."""

    await ensure_ready()
    payload = {"summary": event.summary, **event.payload}
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            last = await session.scalar(
                select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)
            )
            seq = max(event.seq, 0 if last is None else int(last) + 1)
            row = RunEventRow(
                id=str(uuid4()),
                run_id=run_id,
                seq=seq,
                type=event.type.value,
                payload=payload,
                created_at=event.created_at,
            )
            session.add(row)
            if event.type.value == "HYPOTHESIS_UPDATED":
                await _upsert_hypothesis(session, run_id, payload)
                return
            if event.type.value != "TOOL_RESULT":
                return
            await session.flush()
            arguments = payload.get("arguments")
            output = payload.get("output")
            call = ToolCallRow(
                id=str(uuid4()),
                run_id=run_id,
                event_id=row.id,
                tool=str(payload.get("tool") or ""),
                input=arguments if isinstance(arguments, dict) else {},
                output=output if isinstance(output, dict) else {},
                status=str(payload.get("status") or ""),
                latency_ms=_latency(payload.get("latency_ms")),
            )
            session.add(call)
            await session.flush()
            keys = payload.get("evidence_keys")
            if not isinstance(keys, list):
                return
            window = payload.get("time_range")
            summary = str(event.summary)[:500]
            for key in keys:
                if not isinstance(key, str) or not key:
                    continue
                session.add(
                    EvidenceRow(
                        id=str(uuid4()),
                        run_id=run_id,
                        tool_call_id=call.id,
                        evidence_key=key,
                        summary=summary,
                        excerpt=summary,
                        time_range=window if isinstance(window, dict) else None,
                    )
                )


async def _upsert_hypothesis(
    session: AsyncSession,
    run_id: str,
    payload: dict[str, Any],
) -> None:
    cause_raw = str(payload.get("cause") or "")
    try:
        cause = Cause(cause_raw)
    except ValueError:
        return
    status = str(payload.get("status") or "PROPOSED")
    version = int(payload.get("version") or 1)
    confidence = float(payload.get("confidence") or 0.0)
    cause_summary = str(payload.get("cause_summary") or CAUSE_SUMMARY[cause])
    category = str(payload.get("category") or CAUSE_CATEGORY[cause].value)

    existing = await session.scalar(
        select(HypothesisRow).where(
            HypothesisRow.run_id == run_id,
            HypothesisRow.cause == cause.value,
        )
    )
    if existing is None:
        hyp = HypothesisRow(
            id=str(uuid4()),
            run_id=run_id,
            cause=cause.value,
            cause_summary=cause_summary,
            category=category,
            confidence=confidence,
            status=status,
            version=version,
        )
        session.add(hyp)
        await session.flush()
    else:
        existing.cause_summary = cause_summary
        existing.category = category
        existing.confidence = confidence
        existing.status = status
        existing.version = version
        hyp = existing
        await session.flush()

    ev_rows = list(await session.scalars(select(EvidenceRow).where(EvidenceRow.run_id == run_id)))
    ev_by_key = {ev.evidence_key: ev.id for ev in ev_rows}
    existing_links = set(
        await session.scalars(
            select(HypothesisEvidenceRow.evidence_id).where(
                HypothesisEvidenceRow.hypothesis_id == hyp.id
            )
        )
    )
    for field_name, relation in (
        ("supporting_evidence", "supports"),
        ("contradicting_evidence", "contradicts"),
    ):
        raw_keys = payload.get(field_name)
        if not isinstance(raw_keys, list):
            continue
        for key in raw_keys:
            ev_id = ev_by_key.get(str(key))
            if ev_id is not None and ev_id not in existing_links:
                session.add(
                    HypothesisEvidenceRow(
                        id=str(uuid4()),
                        hypothesis_id=hyp.id,
                        evidence_id=ev_id,
                        relation=relation,
                    )
                )
                existing_links.add(ev_id)


def _latency(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


async def events_after(run_id: str, seq: int) -> list[RunEventRow]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        rows = await session.scalars(
            select(RunEventRow)
            .where(RunEventRow.run_id == run_id, RunEventRow.seq > seq)
            .order_by(RunEventRow.seq)
        )
        return list(rows)


async def finish_run(
    run_id: str,
    status: str,
    report: dict[str, Any] | None,
    failure_reason: str | None,
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    recalled_memories: Sequence[MemoryRecallRecord] | None = None,
    create_episodic_memory: bool = False,
) -> None:
    validate_outcome(status, report)
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None or run.status not in {"PENDING", "RUNNING"}:
                return
            run.status = status
            run.final_report = report
            run.failure_reason = failure_reason
            run.ended_at = now
            run.tokens_in = tokens_in
            run.tokens_out = tokens_out
            run.cost_usd = cost_usd
            if recalled_memories:
                await _record_recalls_in_session(session, run_id, recalled_memories, now)
            if isinstance(report, dict):
                await _seed_action_approvals_in_session(session, run_id, report, now)
            incident = await session.get(IncidentRow, run.incident_id)
            if (
                create_episodic_memory
                and status == "COMPLETED"
                and isinstance(report, dict)
                and not bool((run.config or {}).get("is_eval", False))
                and incident is not None
            ):
                await _create_episodic_memory_in_session(session, run, incident, report)
            if incident is None or incident.status == "RESOLVED":
                return
            if report is not None and status in {"COMPLETED", "TIMED_OUT", "BUDGET_EXCEEDED"}:
                incident.status = "DIAGNOSED"
                incident.updated_at = now


async def cancel_run(run_id: str) -> RunRow:
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None:
                raise StoreError(404, "Run not found.")
            if run.status not in {"PENDING", "RUNNING"}:
                return run
            run.status = "CANCELLED"
            run.final_report = None
            run.failure_reason = "cancelled"
            run.ended_at = now
            last = await session.scalar(
                select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)
            )
            session.add(
                RunEventRow(
                    id=str(uuid4()),
                    run_id=run_id,
                    seq=0 if last is None else int(last) + 1,
                    type="ERROR",
                    payload={"summary": "Run cancelled.", "reason": "cancelled"},
                    created_at=now,
                )
            )
            return run


async def fail_runs_after_restart() -> int:
    """Mark leftover active runs failed. A dead process will not resume them."""
    from app.executor import service as executor

    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            rows = list(
                await session.scalars(
                    select(RunRow).where(RunRow.status.in_(("PENDING", "RUNNING")))
                )
            )
            for run in rows:
                executor.cancel_task(run.id)
                run.status = "FAILED"
                run.failure_reason = "restart"
                run.ended_at = now
                run.final_report = None
                last = await session.scalar(
                    select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run.id)
                )
                session.add(
                    RunEventRow(
                        id=str(uuid4()),
                        run_id=run.id,
                        seq=0 if last is None else int(last) + 1,
                        type="ERROR",
                        payload={"summary": "Process restarted.", "reason": "restart"},
                        created_at=now,
                    )
                )
            return len(rows)


async def get_prior_run_context(incident_id: str, exclude_run_id: str) -> PriorRunContext:
    """Load rejected hypotheses and evidence summaries from prior runs on the same incident."""
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        prior_runs = list(
            await session.scalars(
                select(RunRow)
                .where(
                    RunRow.incident_id == incident_id,
                    RunRow.id != exclude_run_id,
                    RunRow.status.not_in(("PENDING", "RUNNING")),
                )
                .order_by(RunRow.created_at.asc())
            )
        )
        if not prior_runs:
            return PriorRunContext()
        run_ids = [r.id for r in prior_runs]
        hyp_rows = list(
            await session.scalars(
                select(HypothesisRow).where(
                    HypothesisRow.run_id.in_(run_ids),
                    HypothesisRow.status == "REJECTED",
                )
            )
        )
        rejected: list[Cause] = []
        for h in hyp_rows:
            try:
                cause = Cause(h.cause)
            except ValueError:
                continue
            if cause not in rejected:
                rejected.append(cause)

        ev_rows = list(
            await session.scalars(select(EvidenceRow).where(EvidenceRow.run_id.in_(run_ids)))
        )
        summaries: list[dict[str, str]] = []
        seen_keys: set[str] = set()
        for ev in ev_rows:
            if ev.evidence_key in seen_keys:
                continue
            seen_keys.add(ev.evidence_key)
            summaries.append(
                {
                    "evidence_key": ev.evidence_key,
                    "summary": ev.summary,
                }
            )
        return PriorRunContext(
            prior_run_ids=tuple(run_ids),
            rejected_causes=tuple(rejected),
            evidence_summaries=tuple(summaries),
        )


def _row_to_memory_item(row: MemoryItemRow) -> MemoryItem:
    cat = None
    if row.category:
        try:
            cat = Category(row.category)
        except ValueError:
            cat = None
    cause = None
    if row.cause:
        try:
            cause = Cause(row.cause)
        except ValueError:
            cause = None
    return MemoryItem(
        id=row.id,
        layer=MemoryLayer(row.layer),
        content=row.content,
        summary=row.summary,
        service=row.service,
        category=cat,
        cause=cause,
        trust_level=TrustLevel(row.trust_level),
        source_run_id=row.source_run_id,
        source_feedback_id=row.source_feedback_id,
        embedding=list(row.embedding or []),
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        service_version=row.service_version,
        usage_count=row.usage_count,
        helpful_count=row.helpful_count,
        last_used_at=row.last_used_at,
        superseded_by=row.superseded_by,
        created_at=row.created_at,
    )


async def _link_producing_run(
    session: AsyncSession, source_run_id: str | None, memory_id: str
) -> None:
    if not source_run_id:
        return
    producing_run = await session.get(RunRow, source_run_id)
    if producing_run is None:
        return
    cfg_dict = dict(producing_run.config or {})
    produced_ids = [
        str(mid)
        for mid in (cfg_dict.get("_produced_memory_ids") or [])
        if isinstance(mid, str) and mid
    ]
    if memory_id not in produced_ids:
        produced_ids.append(memory_id)
    cfg_dict["_produced_memory_ids"] = produced_ids
    producing_run.config = cfg_dict


async def _write_memory_in_session(
    session: AsyncSession,
    body: MemoryWrite,
    cfg: MemoryConfig,
) -> tuple[MemoryItem, bool]:
    aliases: dict[str, str] = {}
    clean_summary = redact_text(body.summary, aliases)
    clean_content = redact_text(body.content, aliases)
    combined = f"{clean_summary} {clean_content}"
    embedding = embed_text(combined)
    now = _as_utc(body.created_at) if body.created_at is not None else WallClock().now()
    cat_val = body.category.value if body.category is not None else None
    cause_val = body.cause.value if body.cause is not None else None

    candidates = list(
        await session.scalars(
            select(MemoryItemRow).where(
                MemoryItemRow.service == body.service,
                MemoryItemRow.layer == body.layer.value,
                MemoryItemRow.category == cat_val,
                MemoryItemRow.service_version == body.service_version,
                MemoryItemRow.superseded_by.is_(None),
                MemoryItemRow.trust_level != TrustLevel.REJECTED.value,
            )
        )
    )
    for existing in candidates:
        sim = text_similarity(
            combined,
            f"{existing.summary} {existing.content}",
            embedding,
            existing.embedding,
        )
        if sim >= cfg.similarity_dedup_threshold:
            existing.usage_count += 1
            existing.last_used_at = now
            if body.trust_level == TrustLevel.CONFIRMED:
                existing.trust_level = TrustLevel.CONFIRMED.value
            await _link_producing_run(session, body.source_run_id, existing.id)
            await session.flush()
            return _row_to_memory_item(existing), False

    row = MemoryItemRow(
        id=str(uuid4()),
        layer=body.layer.value,
        content=clean_content,
        summary=clean_summary,
        service=body.service,
        category=cat_val,
        cause=cause_val,
        trust_level=body.trust_level.value,
        source_run_id=body.source_run_id,
        source_feedback_id=body.source_feedback_id,
        embedding=embedding,
        valid_from=_stamp(body.valid_from),  # type: ignore[arg-type]
        valid_until=_stamp(body.valid_until),  # type: ignore[arg-type]
        service_version=body.service_version,
        usage_count=1,
        helpful_count=0,
        last_used_at=None,
        superseded_by=None,
        created_at=now,
    )
    session.add(row)
    await _link_producing_run(session, body.source_run_id, row.id)
    await session.flush()
    return _row_to_memory_item(row), True


async def write_memory_item(
    body: MemoryWrite,
    config: MemoryConfig | None = None,
) -> tuple[MemoryItem, bool]:
    """Redact, deduplicate, and persist a MemoryItemRow."""
    await ensure_ready()
    cfg = config or MemoryConfig()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            return await _write_memory_in_session(session, body, cfg)


async def search_memory_items(
    query: MemoryQuery,
    config: MemoryConfig | None = None,
) -> list[ScoredMemory]:
    await ensure_ready()
    cfg = config or MemoryConfig()
    factory = get_sessionmaker()
    async with factory() as session:
        stmt = select(MemoryItemRow).where(MemoryItemRow.superseded_by.is_(None))
        if query.service is not None:
            stmt = stmt.where(MemoryItemRow.service == query.service)
        rows = list(await session.scalars(stmt))
    items = [_row_to_memory_item(row) for row in rows]
    return rank_memories(items, query, cfg)


async def _record_recalls_in_session(
    session: AsyncSession,
    run_id: str,
    recalls: Sequence[MemoryRecallRecord],
    now: datetime,
) -> None:
    for rec in recalls:
        item_row = await session.get(MemoryItemRow, rec.memory_id)
        if item_row is None:
            continue
        item_row.usage_count += 1
        item_row.last_used_at = now
        existing = await session.scalar(
            select(MemoryRecallRow).where(
                MemoryRecallRow.run_id == run_id,
                MemoryRecallRow.memory_id == rec.memory_id,
            )
        )
        if existing is None:
            session.add(
                MemoryRecallRow(
                    id=str(uuid4()),
                    run_id=run_id,
                    memory_id=rec.memory_id,
                    score=rec.score,
                    injected=rec.injected,
                    cited=rec.cited,
                )
            )
        else:
            existing.score = max(existing.score, rec.score)
            existing.injected = existing.injected or rec.injected
            existing.cited = existing.cited or rec.cited


async def record_memory_recalls(
    run_id: str,
    recalls: Sequence[MemoryRecallRecord],
) -> None:
    if not recalls:
        return
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            await _record_recalls_in_session(session, run_id, recalls, now)


async def get_run_memory_recalls(run_id: str) -> list[MemoryRecallRow]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        rows = await session.scalars(
            select(MemoryRecallRow).where(MemoryRecallRow.run_id == run_id)
        )
        return list(rows)


async def _create_episodic_memory_in_session(
    session: AsyncSession,
    run: RunRow,
    incident: IncidentRow,
    report: dict[str, Any],
    config: MemoryConfig | None = None,
) -> MemoryItem | None:
    if not incident.service:
        return None
    verdict = report.get("verdict")
    hyps = report.get("hypotheses") or []
    if verdict != "FAULT" or not hyps or not isinstance(hyps[0], dict):
        return None
    top = hyps[0]
    try:
        cause = Cause(str(top.get("cause") or ""))
        category = Category(str(top.get("category") or CAUSE_CATEGORY[cause].value))
    except ValueError:
        return None
    ev_keys = [str(k) for k in top.get("supporting_evidence", []) if isinstance(k, str)]
    svc_version = None
    if isinstance(incident.details, dict) and isinstance(
        incident.details.get("service_version"), str
    ):
        svc_version = str(incident.details["service_version"])

    summary = f"{incident.title}: diagnosed {cause.value} on {incident.service}"
    content = (
        f"Incident: {incident.title}. Description: {incident.description}. "
        f"Diagnosed cause: {cause.value} ({category.value}). "
        f"Evidence keys: {', '.join(ev_keys)}."
    )
    item, _created = await _write_memory_in_session(
        session,
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            summary=summary,
            content=content,
            service=incident.service,
            category=category,
            cause=cause,
            trust_level=TrustLevel.CANDIDATE,
            source_run_id=run.id,
            service_version=svc_version,
        ),
        config or MemoryConfig(),
    )
    return item


async def create_episodic_memory_for_run(
    run_id: str,
    config: MemoryConfig | None = None,
) -> MemoryItem | None:
    """Create a CANDIDATE episodic MemoryItem when a non-evaluation run completes."""
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None or run.status != "COMPLETED" or not isinstance(run.final_report, dict):
                return None
            if bool((run.config or {}).get("is_eval", False)):
                return None
            incident = await session.get(IncidentRow, run.incident_id)
            if incident is None:
                return None
            return await _create_episodic_memory_in_session(
                session, run, incident, run.final_report, config
            )


async def record_run_feedback(
    run_id: str,
    verdict: FeedbackVerdict,
    true_cause: Cause | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record user feedback and update trust_level ONLY on cited memories (FR-7, FR-9)."""
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.get(RunRow, run_id)
            if run is None:
                raise StoreError(404, "Run not found.")
            feedback = FeedbackRow(
                id=str(uuid4()),
                run_id=run_id,
                verdict=verdict.value,
                true_cause=None if true_cause is None else true_cause.value,
                notes=notes,
                created_at=now,
            )
            session.add(feedback)
            await session.flush()

            recalls = list(
                await session.scalars(
                    select(MemoryRecallRow).where(MemoryRecallRow.run_id == run_id)
                )
            )
            promoted_ids: list[str] = []
            rejected_ids: list[str] = []
            unchanged_uncited_ids: list[str] = []

            for rec in recalls:
                if not rec.cited:
                    unchanged_uncited_ids.append(rec.memory_id)
                    continue
                mem_row = await session.get(MemoryItemRow, rec.memory_id)
                if mem_row is None:
                    continue
                if verdict == FeedbackVerdict.CORRECT:
                    mem_row.trust_level = TrustLevel.CONFIRMED.value
                    mem_row.helpful_count += 1
                    mem_row.source_feedback_id = feedback.id
                    promoted_ids.append(mem_row.id)
                elif verdict == FeedbackVerdict.INCORRECT:
                    mem_row.trust_level = TrustLevel.REJECTED.value
                    mem_row.source_feedback_id = feedback.id
                    rejected_ids.append(mem_row.id)
                else:
                    unchanged_uncited_ids.append(mem_row.id)

            # Also update episodic memory generated directly from this run (including deduplicated)
            generated_by_query = list(
                await session.scalars(
                    select(MemoryItemRow).where(MemoryItemRow.source_run_id == run_id)
                )
            )
            generated_map: dict[str, MemoryItemRow] = {row.id: row for row in generated_by_query}
            linked_ids = (run.config or {}).get("_produced_memory_ids") or []
            if isinstance(linked_ids, list):
                for mid in linked_ids:
                    if isinstance(mid, str) and mid not in generated_map:
                        linked_row = await session.get(MemoryItemRow, mid)
                        if linked_row is not None:
                            generated_map[linked_row.id] = linked_row
            generated = list(generated_map.values())
            for gen_row in generated:
                if gen_row.id in promoted_ids or gen_row.id in rejected_ids:
                    continue
                if verdict == FeedbackVerdict.CORRECT:
                    gen_row.trust_level = TrustLevel.CONFIRMED.value
                    gen_row.helpful_count += 1
                    gen_row.source_feedback_id = feedback.id
                    promoted_ids.append(gen_row.id)
                elif verdict == FeedbackVerdict.INCORRECT:
                    gen_row.trust_level = TrustLevel.REJECTED.value
                    gen_row.source_feedback_id = feedback.id
                    if true_cause is not None:
                        gen_row.cause = true_cause.value
                        gen_row.category = CAUSE_CATEGORY[true_cause].value
                    rejected_ids.append(gen_row.id)

            return {
                "id": feedback.id,
                "run_id": run_id,
                "verdict": verdict.value,
                "true_cause": None if true_cause is None else true_cause.value,
                "notes": notes,
                "promoted_memory_ids": promoted_ids,
                "rejected_memory_ids": rejected_ids,
                "unchanged_uncited_memory_ids": unchanged_uncited_ids,
                "created_at": now.isoformat(),
            }


class DatabaseMemoryAdapter:
    """MemoryPort backed by SQLAlchemy (SQLite / PostgreSQL)."""

    def __init__(self, config: MemoryConfig | None = None) -> None:
        self._config = config or MemoryConfig()

    @property
    def config(self) -> MemoryConfig:
        return self._config

    async def search(self, query: MemoryQuery) -> list[ScoredMemory]:
        return await search_memory_items(query, self._config)

    async def write(self, body: MemoryWrite) -> tuple[MemoryItem, bool]:
        return await write_memory_item(body, self._config)


async def _seed_action_approvals_in_session(
    session: AsyncSession,
    run_id: str,
    report: dict[str, Any],
    now: datetime,
) -> None:
    recs = report.get("recommendations")
    if not isinstance(recs, list):
        return
    for idx, rec in enumerate(recs):
        if not isinstance(rec, dict):
            continue
        action_text = str(rec.get("action") or "").strip()
        if not action_text:
            continue
        risk = str(rec.get("risk") or "low")
        requires_approval = bool(rec.get("requires_approval", True))
        existing = await session.scalar(
            select(ActionApprovalRow).where(
                ActionApprovalRow.run_id == run_id,
                ActionApprovalRow.action_index == idx,
            )
        )
        if existing is None:
            session.add(
                ActionApprovalRow(
                    id=str(uuid4()),
                    run_id=run_id,
                    action_index=idx,
                    action_text=action_text,
                    risk=risk,
                    requires_approval=requires_approval,
                    status="PENDING" if requires_approval else "APPROVED",
                    actor=None,
                    notes=None,
                    created_at=now,
                    updated_at=now,
                )
            )


def _approval_dict(row: ActionApprovalRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "action_index": row.action_index,
        "action": row.action_text,
        "risk": row.risk,
        "requires_approval": row.requires_approval,
        "status": row.status,
        "actor": row.actor,
        "notes": row.notes,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def list_run_approvals(run_id: str) -> list[dict[str, Any]]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        run = await session.get(RunRow, run_id)
        if run is None:
            raise StoreError(404, "Run not found.")
        rows = list(
            await session.scalars(
                select(ActionApprovalRow)
                .where(ActionApprovalRow.run_id == run_id)
                .order_by(ActionApprovalRow.action_index.asc())
            )
        )
        return [_approval_dict(row) for row in rows]


async def record_action_approval(
    run_id: str,
    *,
    action_index: int = 0,
    decision: str,
    actor: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Record a human approval or rejection for a recommended action (FR-5, §4.5, §6 M7)."""
    await ensure_ready()
    normalized = decision.strip().upper()
    if normalized not in {"APPROVED", "REJECTED"}:
        raise StoreError(422, "decision must be 'approved' or 'rejected'.")
    factory = get_sessionmaker()
    for attempt in range(3):
        now = WallClock().now()
        try:
            async with factory() as session:
                async with session.begin():
                    run = await session.scalar(
                        select(RunRow).where(RunRow.id == run_id).with_for_update()
                    )
                    if run is None:
                        raise StoreError(404, "Run not found.")
                    # Touch updated state on parent run row to serialize sequence allocation
                    run.config = dict(run.config or {})
                    row = await session.scalar(
                        select(ActionApprovalRow)
                        .where(
                            ActionApprovalRow.run_id == run_id,
                            ActionApprovalRow.action_index == action_index,
                        )
                        .with_for_update()
                    )
                    if row is None:
                        raise StoreError(404, f"Action approval index {action_index} not found.")
                    row.status = normalized
                    row.actor = actor
                    row.notes = notes
                    row.updated_at = now

                    last = await session.scalar(
                        select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)
                    )
                    event_type = (
                        "RECOMMENDATION_APPROVED"
                        if normalized == "APPROVED"
                        else "RECOMMENDATION_REJECTED"
                    )
                    summary = (
                        f"Recommendation #{action_index} {normalized.lower()}: "
                        f"{row.action_text[:120]}"
                    )
                    session.add(
                        RunEventRow(
                            id=str(uuid4()),
                            run_id=run_id,
                            seq=0 if last is None else int(last) + 1,
                            type=event_type,
                            payload={
                                "summary": summary,
                                "action_index": action_index,
                                "action": row.action_text,
                                "risk": row.risk,
                                "status": normalized,
                                "actor": actor,
                                "notes": notes,
                            },
                            created_at=now,
                        )
                    )
                    await session.flush()
                    return _approval_dict(row)
        except IntegrityError:
            if attempt == 2:
                raise
    raise StoreError(409, "Could not record action approval due to concurrent conflict.")


async def save_run_checkpoint(
    run_id: str,
    step_index: int,
    node_name: str,
    state_json: dict[str, Any],
) -> RunCheckpointRow:
    """Persist a LangGraph step checkpoint for a run."""
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            existing = await session.scalar(
                select(RunCheckpointRow).where(
                    RunCheckpointRow.run_id == run_id,
                    RunCheckpointRow.step_index == step_index,
                )
            )
            if existing is not None:
                existing.node_name = node_name
                existing.state_json = state_json
                existing.created_at = now
                await session.flush()
                return existing
            row = RunCheckpointRow(
                id=str(uuid4()),
                run_id=run_id,
                step_index=step_index,
                node_name=node_name,
                state_json=state_json,
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return row


async def get_latest_run_checkpoint(run_id: str) -> RunCheckpointRow | None:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        return await session.scalar(
            select(RunCheckpointRow)
            .where(RunCheckpointRow.run_id == run_id)
            .order_by(RunCheckpointRow.step_index.desc())
            .limit(1)
        )


async def list_run_checkpoints(run_id: str) -> list[RunCheckpointRow]:
    await ensure_ready()
    factory = get_sessionmaker()
    async with factory() as session:
        rows = await session.scalars(
            select(RunCheckpointRow)
            .where(RunCheckpointRow.run_id == run_id)
            .order_by(RunCheckpointRow.step_index.asc())
        )
        return list(rows)


async def prepare_run_checkpoint_resume(run_id: str) -> tuple[RunRow, RunCheckpointRow]:
    """Validate that a run opted into M7 checkpoint_policy='resume' and transition it to RUNNING."""
    await ensure_ready()
    now = WallClock().now()
    factory = get_sessionmaker()
    async with factory() as session:
        async with session.begin():
            run = await session.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
            if run is None:
                raise StoreError(404, "Run not found.")
            cfg = dict(run.config or {})
            policy = str(cfg.get("checkpoint_policy") or "fail")
            if policy != "resume":
                raise StoreError(
                    409,
                    "Run uses M2 restart-fail policy (checkpoint_policy='fail'); "
                    "set checkpoint_policy='resume' to replace M2 restart-fail with "
                    "checkpoint recovery.",
                )
            if run.status in {"PENDING", "RUNNING"}:
                raise StoreError(409, "Run is already active.")
            if run.status != "FAILED" or run.failure_reason != "restart":
                raise StoreError(
                    409,
                    f"Run in status {run.status} (reason={run.failure_reason!r}) "
                    "cannot be resumed; only runs interrupted by restart can be resumed.",
                )
            checkpoint = await session.scalar(
                select(RunCheckpointRow)
                .where(RunCheckpointRow.run_id == run_id)
                .order_by(RunCheckpointRow.step_index.desc())
                .limit(1)
            )
            if checkpoint is None:
                raise StoreError(409, "No saved checkpoint exists for this run.")
            active_other = await session.scalar(
                select(RunRow).where(
                    RunRow.incident_id == run.incident_id,
                    RunRow.id != run.id,
                    RunRow.status.in_(("PENDING", "RUNNING")),
                )
            )
            if active_other is not None:
                raise StoreError(409, "Another run is still active on this incident.")
            cfg["resumed_from_checkpoint_step"] = checkpoint.step_index
            started_at = run.started_at if run.started_at is not None else now
            transition = await session.execute(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status == "FAILED",
                    RunRow.failure_reason == "restart",
                )
                .values(
                    status="RUNNING",
                    failure_reason=None,
                    ended_at=None,
                    started_at=started_at,
                    config=cfg,
                )
            )
            if (transition.rowcount or 0) != 1:
                raise StoreError(409, "Run was already resumed by another request.")
            run.status = "RUNNING"
            run.failure_reason = None
            run.ended_at = None
            run.started_at = started_at
            run.config = cfg
            await session.flush()
            return run, checkpoint
