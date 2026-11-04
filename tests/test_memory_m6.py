"""M6 tests for episodic memory, semantic knowledge, feedback trust rules, and ablations (#41)."""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

from app.agent.investigator import (
    INVESTIGATOR_SYSTEM_PROMPT,
    investigate,
    investigation_contract,
)
from app.core.taxonomy import Category, Cause, Verdict
from app.eval.m6_ablation import (
    M6_ABLATION_NAME,
    M6_MANIFEST_NAME,
    build_m6_manifest,
    build_seed_catalog,
    load_m6_scenarios,
    m6_dir,
    run_m6_ablation,
    select_enabled_layers,
)
from app.eval.runner import baselines_dir
from app.eval.scenario import load_scenario
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.main import app
from app.memory.models import (
    FeedbackVerdict,
    MemoryConfig,
    MemoryLayer,
    MemoryQuery,
    MemoryRecallRecord,
    MemoryWrite,
    TrustLevel,
)
from app.memory.port import InMemoryCatalog
from app.persistence.models import (
    HypothesisRow,
    MemoryItemRow,
)
from app.persistence.session import get_sessionmaker
from app.persistence.store import (
    create_episodic_memory_for_run,
    create_incident,
    finish_run,
    get_prior_run_context,
    mark_running,
    record_memory_recalls,
    record_run_feedback,
    start_run,
    write_memory_item,
)
from app.schemas.incident import IncidentCreate
from app.tools.simulator.adapter import SimulatorTools
from fastapi.testclient import TestClient
from sqlalchemy import select

HEADERS = {"X-API-Key": "dev-key"}


def _wait_terminal(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        res = client.get(f"/v1/runs/{run_id}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        if body["status"] not in {"PENDING", "RUNNING"}:
            return body
        time.sleep(0.02)
    raise AssertionError("Run did not reach terminal state")


def test_memory_redaction_and_configurable_deduplication() -> None:
    async def _run() -> None:
        cfg = MemoryConfig(similarity_dedup_threshold=0.75)
        item1, created1 = await write_memory_item(
            MemoryWrite(
                layer=MemoryLayer.SEMANTIC,
                service="checkout",
                category=Category.DATABASE,
                trust_level=TrustLevel.CONFIRMED,
                service_version="v2.0",
                summary="Contact sre@example.com for checkout pool on 10.20.30.40",
                content=(
                    "Runbook token=super-secret-token host 10.20.30.40 "
                    "and 10.20.30.40 pool saturation."
                ),
            ),
            config=cfg,
        )
        assert created1 is True
        assert "<REDACTED:email>" in item1.summary
        assert "sre@example.com" not in item1.summary
        assert "<REDACTED:secret>" in item1.content
        assert "super-secret-token" not in item1.content
        assert "<ip-1>" in item1.summary
        assert item1.content.count("<ip-1>") == 2
        assert item1.usage_count == 1

        # Near-duplicate write with same service, layer, category, and service_version merges
        item2, created2 = await write_memory_item(
            MemoryWrite(
                layer=MemoryLayer.SEMANTIC,
                service="checkout",
                category=Category.DATABASE,
                trust_level=TrustLevel.CANDIDATE,
                service_version="v2.0",
                summary="Contact sre@example.com for checkout pool on 10.20.30.40",
                content=(
                    "Runbook token=another-secret host 10.20.30.40 and 10.20.30.40 pool saturation."
                ),
            ),
            config=cfg,
        )
        assert created2 is False
        assert item2.id == item1.id
        assert item2.usage_count == 2
        assert item2.trust_level == TrustLevel.CONFIRMED

    asyncio.run(_run())


def test_hybrid_retrieval_ranking_trust_time_decay_and_service_version() -> None:
    async def _run() -> None:
        catalog = InMemoryCatalog()
        t_old = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
        t_recent = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)

        stale_item, _ = await catalog.write(
            MemoryWrite(
                layer=MemoryLayer.EPISODIC,
                service="checkout",
                category=Category.DATABASE,
                cause=Cause.DB_POOL_EXHAUSTED,
                trust_level=TrustLevel.CONFIRMED,
                service_version="v1.0",
                valid_until=datetime(2026, 8, 1, 0, 0, tzinfo=UTC),
                summary="Checkout database connection pool exhausted",
                content="Checkout database connection pool exhausted waiting on primary.",
                created_at=t_old,
            )
        )
        fresh_confirmed, _ = await catalog.write(
            MemoryWrite(
                layer=MemoryLayer.EPISODIC,
                service="checkout",
                category=Category.DATABASE,
                cause=Cause.DB_POOL_EXHAUSTED,
                trust_level=TrustLevel.CONFIRMED,
                service_version="v2.0",
                summary="Checkout database connection pool exhausted in v2.0",
                content="Checkout database connection pool exhausted waiting on payments primary.",
                created_at=t_recent,
            )
        )
        fresh_candidate, _ = await catalog.write(
            MemoryWrite(
                layer=MemoryLayer.EPISODIC,
                service="checkout",
                category=Category.CPU,
                cause=Cause.CPU_SATURATION,
                trust_level=TrustLevel.CANDIDATE,
                service_version="v2.0",
                summary="Checkout database connection pool CPU check",
                content="Checkout worker CPU spike during database connection pool traffic.",
                created_at=t_recent,
            )
        )

        # Default search excludes CANDIDATE
        confirmed_only = await catalog.search(
            MemoryQuery(
                query="checkout database connection pool exhausted",
                service="checkout",
                include_candidate=False,
                service_version="v2.0",
                reference_time=datetime(2026, 10, 29, 0, 0, tzinfo=UTC),
            )
        )
        returned_ids = [r.item.id for r in confirmed_only]
        assert fresh_candidate.id not in returned_ids
        assert returned_ids[0] == fresh_confirmed.id
        stale_scored = next(r for r in confirmed_only if r.item.id == stale_item.id)
        fresh_scored = next(r for r in confirmed_only if r.item.id == fresh_confirmed.id)
        assert stale_scored.stale is True
        assert fresh_scored.stale is False
        assert fresh_scored.score > stale_scored.score

        # Opt-in include_candidate=True returns CANDIDATE as well
        with_candidate = await catalog.search(
            MemoryQuery(
                query="checkout database connection pool exhausted",
                service="checkout",
                include_candidate=True,
                service_version="v2.0",
                reference_time=datetime(2026, 10, 29, 0, 0, tzinfo=UTC),
            )
        )
        assert fresh_candidate.id in {r.item.id for r in with_candidate}

    asyncio.run(_run())


def test_feedback_promotes_and_rejects_only_cited_memories() -> None:
    """FR-7 & FR-9: Feedback affects ONLY memories cited in the run's final report."""

    async def _run() -> None:
        cited_mem, _ = await write_memory_item(
            MemoryWrite(
                layer=MemoryLayer.EPISODIC,
                service="checkout",
                category=Category.DATABASE,
                cause=Cause.DB_POOL_EXHAUSTED,
                trust_level=TrustLevel.CANDIDATE,
                summary="Cited candidate memory for db_pool_exhausted",
                content="Checkout 5xx caused by db_pool_exhausted on primary.",
            )
        )
        uncited_mem, _ = await write_memory_item(
            MemoryWrite(
                layer=MemoryLayer.EPISODIC,
                service="checkout",
                category=Category.CPU,
                cause=Cause.CPU_SATURATION,
                trust_level=TrustLevel.CANDIDATE,
                summary="Retrieved but uncited candidate memory for cpu_saturation",
                content="Checkout latency coincided with cpu_saturation.",
            )
        )

        incident = await create_incident(
            IncidentCreate(
                title="Checkout DB pool errors",
                description="5xx errors on checkout",
                service="checkout",
            )
        )
        run, _ = await start_run(incident.id, "feedback-test-1", None, is_eval=True)
        await mark_running(run.id)
        await finish_run(
            run.id,
            "COMPLETED",
            {
                "verdict": "FAULT",
                "severity": "high",
                "hypotheses": [
                    {
                        "cause": "db_pool_exhausted",
                        "cause_summary": "Pool exhausted",
                        "category": "DATABASE",
                        "confidence": 0.9,
                        "supporting_evidence": ["log:pool_timeout"],
                        "contradicting_evidence": [],
                    }
                ],
                "recommendations": [],
                "unresolved_questions": [],
            },
            None,
        )
        await record_memory_recalls(
            run.id,
            [
                MemoryRecallRecord(
                    memory_id=cited_mem.id,
                    layer=MemoryLayer.EPISODIC,
                    service="checkout",
                    category=Category.DATABASE,
                    cause=Cause.DB_POOL_EXHAUSTED,
                    trust_level=TrustLevel.CANDIDATE,
                    score=0.91,
                    injected=True,
                    cited=True,
                ),
                MemoryRecallRecord(
                    memory_id=uncited_mem.id,
                    layer=MemoryLayer.EPISODIC,
                    service="checkout",
                    category=Category.CPU,
                    cause=Cause.CPU_SATURATION,
                    trust_level=TrustLevel.CANDIDATE,
                    score=0.52,
                    injected=True,
                    cited=False,
                ),
            ],
        )

        # 1. Partial feedback MUST NOT change trust_level on either memory
        partial_res = await record_run_feedback(
            run.id,
            FeedbackVerdict.PARTIAL,
            notes="Partially helpful diagnosis.",
        )
        assert partial_res["promoted_memory_ids"] == []
        assert partial_res["rejected_memory_ids"] == []
        async with get_sessionmaker()() as session:
            c_row = await session.get(MemoryItemRow, cited_mem.id)
            u_row = await session.get(MemoryItemRow, uncited_mem.id)
            assert c_row is not None and c_row.trust_level == "CANDIDATE"
            assert u_row is not None and u_row.trust_level == "CANDIDATE"

        # 2. Correct feedback promotes ONLY cited_mem; uncited_mem stays CANDIDATE
        correct_res = await record_run_feedback(
            run.id,
            FeedbackVerdict.CORRECT,
            notes="Confirmed DB pool exhaustion.",
        )
        assert correct_res["promoted_memory_ids"] == [cited_mem.id]
        assert correct_res["unchanged_uncited_memory_ids"] == [uncited_mem.id]
        async with get_sessionmaker()() as session:
            c_row = await session.get(MemoryItemRow, cited_mem.id)
            u_row = await session.get(MemoryItemRow, uncited_mem.id)
            assert c_row is not None and c_row.trust_level == "CONFIRMED"
            assert c_row.helpful_count == 1
            assert u_row is not None and u_row.trust_level == "CANDIDATE"
            assert u_row.helpful_count == 0

        # 3. Incorrect feedback marks ONLY cited_mem as REJECTED; uncited_mem stays CANDIDATE
        incorrect_res = await record_run_feedback(
            run.id,
            FeedbackVerdict.INCORRECT,
            true_cause=Cause.DNS_FAILURE,
            notes="Was actually DNS.",
        )
        assert incorrect_res["rejected_memory_ids"] == [cited_mem.id]
        assert incorrect_res["unchanged_uncited_memory_ids"] == [uncited_mem.id]
        async with get_sessionmaker()() as session:
            c_row = await session.get(MemoryItemRow, cited_mem.id)
            u_row = await session.get(MemoryItemRow, uncited_mem.id)
            assert c_row is not None and c_row.trust_level == "REJECTED"
            assert u_row is not None and u_row.trust_level == "CANDIDATE"

    asyncio.run(_run())


def test_memory_search_api_defaults_to_confirmed_and_feedback_endpoint() -> None:
    with TestClient(app) as client:
        # Import one CONFIRMED semantic memory and one CANDIDATE episodic memory
        r1 = client.post(
            "/v1/memory",
            headers=HEADERS,
            json={
                "layer": "semantic",
                "service": "checkout",
                "category": "DATABASE",
                "trust_level": "CONFIRMED",
                "service_version": "v2.0",
                "summary": "Confirmed checkout DB runbook",
                "content": "Verify payments DB pool waiters when checkout returns 5xx.",
            },
        )
        assert r1.status_code == 201
        confirmed_id = r1.json()["item"]["id"]

        r2 = client.post(
            "/v1/memory",
            headers=HEADERS,
            json={
                "layer": "episodic",
                "service": "checkout",
                "category": "CPU",
                "cause": "cpu_saturation",
                "trust_level": "CANDIDATE",
                "service_version": "v2.0",
                "summary": "Unconfirmed checkout CPU candidate memory",
                "content": "Candidate incident note for checkout CPU spike.",
            },
        )
        assert r2.status_code == 201
        candidate_id = r2.json()["item"]["id"]

        # Default GET /v1/memory/search returns ONLY CONFIRMED
        s_default = client.get(
            "/v1/memory/search",
            params={"service": "checkout", "q": "checkout"},
            headers=HEADERS,
        )
        assert s_default.status_code == 200
        ids_default = [item["id"] for item in s_default.json()["items"]]
        assert confirmed_id in ids_default
        assert candidate_id not in ids_default

        # Explicit opt-in include_candidate=true includes CANDIDATE
        s_opt_in = client.get(
            "/v1/memory/search",
            params={"service": "checkout", "q": "checkout", "include_candidate": "true"},
            headers=HEADERS,
        )
        assert s_opt_in.status_code == 200
        ids_opt_in = [item["id"] for item in s_opt_in.json()["items"]]
        assert confirmed_id in ids_opt_in
        assert candidate_id in ids_opt_in


def test_cross_run_inheritance_and_eval_isolation() -> None:
    """Non-eval runs inherit prior REJECTED hypotheses and write CANDIDATE episodic memory;
    eval runs disable inheritance and never write MemoryItem."""
    scenario_path = (
        Path(__file__).resolve().parents[1]
        / "scenarios"
        / "frozen"
        / "dependency-with-cpu-herring.yaml"
    )
    scenario = load_scenario(scenario_path)

    with TestClient(app) as client:
        inc_res = client.post(
            "/v1/incidents",
            headers=HEADERS,
            json=scenario.incident.model_dump(mode="json"),
        )
        assert inc_res.status_code == 201
        incident_id = inc_res.json()["id"]

        # Run 1 (normal production run: writes Hypothesis rows + CANDIDATE episodic MemoryItem)
        run1_res = client.post(
            f"/v1/incidents/{incident_id}/runs",
            headers=HEADERS,
            json={"scenario_id": scenario.id},
        )
        assert run1_res.status_code == 202
        run1_id = run1_res.json()["id"]
        finished1 = _wait_terminal(client, run1_id)
        assert finished1["status"] == "COMPLETED"

        # Check that Run 1 persisted a REJECTED hypothesis (`cpu_saturation` herring)
        # and created a CANDIDATE episodic MemoryItem
        async def _check_after_run1() -> tuple[int, list[Cause]]:
            prior = await get_prior_run_context(incident_id, exclude_run_id="none")
            async with get_sessionmaker()() as session:
                mem_rows = list(
                    await session.scalars(
                        select(MemoryItemRow).where(MemoryItemRow.source_run_id == run1_id)
                    )
                )
            return len(mem_rows), list(prior.rejected_causes)

        mem_count_run1, rejected_from_run1 = asyncio.run(_check_after_run1())
        assert mem_count_run1 == 1
        assert Cause.CPU_SATURATION in rejected_from_run1

        # Run 2 on the same incident with inherit_prior_runs=True inherits the REJECTED hypothesis
        run2_res = client.post(
            f"/v1/incidents/{incident_id}/runs",
            headers=HEADERS,
            json={"scenario_id": scenario.id, "inherit_prior_runs": True},
        )
        assert run2_res.status_code == 202
        run2_id = run2_res.json()["id"]
        finished2 = _wait_terminal(client, run2_id)
        assert finished2["status"] == "COMPLETED"

        events2 = client.get(f"/v1/runs/{run2_id}/events", headers=HEADERS).text
        assert "Inherited rejected hypothesis cpu_saturation" in events2

        # Run 3 with is_eval=True disables cross-run inheritance and writes NO MemoryItem
        run3_res = client.post(
            f"/v1/incidents/{incident_id}/runs",
            headers=HEADERS,
            json={"scenario_id": scenario.id, "is_eval": True},
        )
        assert run3_res.status_code == 202
        run3_id = run3_res.json()["id"]
        finished3 = _wait_terminal(client, run3_id)
        assert finished3["status"] == "COMPLETED"
        events3 = client.get(f"/v1/runs/{run3_id}/events", headers=HEADERS).text
        assert "Inherited rejected hypothesis" not in events3

        async def _check_after_run3() -> int:
            async with get_sessionmaker()() as session:
                mem_rows = list(
                    await session.scalars(
                        select(MemoryItemRow).where(MemoryItemRow.source_run_id == run3_id)
                    )
                )
                hyp_rows = list(
                    await session.scalars(
                        select(HypothesisRow).where(HypothesisRow.run_id == run1_id)
                    )
                )
                assert hyp_rows
                return len(mem_rows)

        assert asyncio.run(_check_after_run3()) == 0

        # Submit `correct` feedback on Run 1 -> promotes Run 1's episodic memory to CONFIRMED
        fb_res = client.post(
            f"/v1/runs/{run1_id}/feedback",
            headers=HEADERS,
            json={"verdict": "correct", "notes": "Verified dependency timeout."},
        )
        assert fb_res.status_code == 200
        assert len(fb_res.json()["promoted_memory_ids"]) >= 1

        # Now GET /v1/memory/search without include_candidate returns the promoted memory
        search_res = client.get(
            "/v1/memory/search",
            params={"service": scenario.service},
            headers=HEADERS,
        )
        assert search_res.status_code == 200
        assert any(
            item["id"] in fb_res.json()["promoted_memory_ids"]
            for item in search_res.json()["items"]
        )


def test_contamination_and_stale_memories_are_refuted_by_current_evidence() -> None:
    async def _run() -> None:
        catalog = await build_seed_catalog()
        scenarios = {s.id: s for s in load_m6_scenarios()}

        # 1. Contaminated bad_deploy memory on checkout -> refuted; diagnoses db_pool_exhausted
        s_deploy = scenarios["memory-contamination-bad-deploy"]
        res_deploy = await investigate(
            s_deploy.incident,
            SimulatorTools(s_deploy.world),
            InvestigativeFakeLLM(),
            known_services=s_deploy.world.services,
            memory=catalog,
            memory_layers=(MemoryLayer.EPISODIC,),
            is_eval=True,
        )
        assert res_deploy.status == "COMPLETED"
        assert res_deploy.report is not None
        assert res_deploy.report.verdict == Verdict.FAULT
        assert res_deploy.report.hypotheses[0].cause == Cause.DB_POOL_EXHAUSTED
        assert any(
            ev.type.value == "HYPOTHESIS_UPDATED"
            and ev.payload.get("cause") == "bad_deploy"
            and ev.payload.get("status") == "REJECTED"
            for ev in res_deploy.events
        )
        # Contaminated memory is never cited
        rec_by_id = {r.memory_id: r for r in res_deploy.recalled_memories}
        for mid in res_deploy.cited_memory_ids:
            assert rec_by_id[mid].cause != Cause.BAD_DEPLOY

        # 2. Stale v2.1 memory + v3.0 semantic runbook on api-gateway -> diagnoses dns_failure
        s_stale = scenarios["stale-service-gateway-v3-dns"]
        res_stale = await investigate(
            s_stale.incident,
            SimulatorTools(s_stale.world),
            InvestigativeFakeLLM(),
            known_services=s_stale.world.services,
            memory=catalog,
            memory_layers=(MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC),
            is_eval=True,
        )
        assert res_stale.status == "COMPLETED"
        assert res_stale.report is not None
        assert res_stale.report.hypotheses[0].cause == Cause.DNS_FAILURE
        assert any(r.stale for r in res_stale.recalled_memories)
        for mid in res_stale.cited_memory_ids:
            rec = next(r for r in res_stale.recalled_memories if r.memory_id == mid)
            assert rec.stale is False

    asyncio.run(_run())


def test_search_memory_tool_can_be_called_during_investigation() -> None:
    async def _run() -> None:
        catalog = await build_seed_catalog()
        s = load_scenario(m6_dir() / "episodic-multidep-inventory-pool.yaml")
        # Custom sequence that invokes search_memory before final report
        llm = InvestigativeFakeLLM(
            responses=[
                {
                    "action": "call_tool",
                    "tool": "search_memory",
                    "arguments": {
                        "service": "order-service",
                        "query": "flash sale reservation timeout",
                    },
                    "summary": "Query memory for order-service flash sale incidents.",
                },
                {
                    "action": "call_tool",
                    "tool": "query_db_health",
                    "arguments": {"service": "inventory-db-proxy"},
                    "summary": "Verify DB pool on inventory-db-proxy.",
                },
                {
                    "action": "final_report",
                    "summary": "Confirmed db_pool_exhausted on inventory-db-proxy.",
                    "report": {
                        "verdict": "FAULT",
                        "severity": "high",
                        "hypotheses": [
                            {
                                "cause": "db_pool_exhausted",
                                "cause_summary": "Pool exhausted on inventory-db-proxy",
                                "category": "DATABASE",
                                "confidence": 0.9,
                                "supporting_evidence": ["db:inv_proxy_pool"],
                                "contradicting_evidence": [],
                            }
                        ],
                        "recommendations": [
                            {
                                "action": "Scale inventory-db-proxy pool",
                                "risk": "low",
                                "requires_approval": True,
                            }
                        ],
                        "unresolved_questions": [],
                    },
                },
            ]
        )
        res = await investigate(
            s.incident,
            SimulatorTools(s.world),
            llm,
            known_services=s.world.services,
            memory=catalog,
            memory_layers=(MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC),
            is_eval=True,
        )
        assert res.status == "COMPLETED"
        assert res.tool_calls == 2
        assert any(
            ev.type.value == "TOOL_RESULT" and ev.payload.get("tool") == "search_memory"
            for ev in res.events
        )
        # Omitted cited_memory_ids must NEVER implicitly mark recalled memories as cited
        assert res.recalled_memories
        assert res.cited_memory_ids == ()
        assert all(not r.cited for r in res.recalled_memories)

    asyncio.run(_run())


def test_review_fixes_explicit_citations_default_ref_time_and_dedup_feedback() -> None:
    """Regression test for PR #44 review findings:
    1. Model contract exposes cited_memory_ids and omitted cited_memory_ids stays empty.
    2. /v1/memory/search defaults reference_time to current clock so expired items are stale.
    3. Deduplicated produced memories retain producing-run association for feedback."""
    assert "cited_memory_ids" in INVESTIGATOR_SYSTEM_PROMPT
    assert "cited_memory_ids_schema" not in investigation_contract(include_search_memory=False)
    assert "cited_memory_ids_schema" in investigation_contract(include_search_memory=True)

    with TestClient(app) as client:
        # Expired confirmed runbook with old created_at
        r_exp = client.post(
            "/v1/memory",
            headers=HEADERS,
            json={
                "layer": "semantic",
                "service": "billing-svc",
                "category": "CONFIG",
                "trust_level": "CONFIRMED",
                "summary": "Expired billing runbook",
                "content": "Legacy billing runbook valid until 2025.",
                "valid_until": "2025-01-01T00:00:00Z",
                "created_at": "2024-06-01T00:00:00Z",
            },
        )
        assert r_exp.status_code == 201
        exp_id = r_exp.json()["item"]["id"]

        s_res = client.get(
            "/v1/memory/search",
            params={"service": "billing-svc", "q": "billing runbook"},
            headers=HEADERS,
        )
        assert s_res.status_code == 200
        exp_item = next(it for it in s_res.json()["items"] if it["id"] == exp_id)
        assert exp_item["stale"] is True
        assert exp_item["version_weight"] < 1.0
        assert exp_item["time_decay_weight"] < 1.0

    async def _check_dedup_producing_run_feedback() -> None:
        report = {
            "verdict": "FAULT",
            "severity": "high",
            "hypotheses": [
                {
                    "cause": "db_pool_exhausted",
                    "cause_summary": "Pool exhausted",
                    "category": "DATABASE",
                    "confidence": 0.92,
                    "supporting_evidence": ["db:pool_1"],
                    "contradicting_evidence": [],
                }
            ],
            "recommendations": [],
            "unresolved_questions": [],
        }
        inc1 = await create_incident(
            IncidentCreate(
                title="Payments pool timeout",
                description="5xx on payments DB pool",
                service="payments-dedup",
            )
        )
        run1, _ = await start_run(inc1.id, "dedup-run-1", None, is_eval=False)
        await mark_running(run1.id)
        await finish_run(run1.id, "COMPLETED", report, None)
        mem1 = await create_episodic_memory_for_run(run1.id)
        assert mem1 is not None

        # Second incident/run produces a duplicate episodic memory without recalling/citing mem1
        inc2 = await create_incident(
            IncidentCreate(
                title="Payments pool timeout",
                description="5xx on payments DB pool",
                service="payments-dedup",
            )
        )
        run2, _ = await start_run(inc2.id, "dedup-run-2", None, is_eval=False)
        await mark_running(run2.id)
        await finish_run(run2.id, "COMPLETED", report, None)
        mem2 = await create_episodic_memory_for_run(run2.id)
        assert mem2 is not None
        assert mem2.id == mem1.id
        assert mem2.usage_count == 2

        # Feedback on run2 promotes the deduplicated memory even though source_run_id == run1.id
        fb2 = await record_run_feedback(run2.id, FeedbackVerdict.CORRECT)
        assert mem1.id in fb2["promoted_memory_ids"]
        async with get_sessionmaker()() as session:
            row = await session.get(MemoryItemRow, mem1.id)
            assert row is not None and row.trust_level == "CONFIRMED"

    asyncio.run(_check_dedup_producing_run_feedback())


def test_m6_ablation_and_automatic_layer_disabling() -> None:
    ablation = run_m6_ablation()
    assert ablation["scenario_set"] == "m6-v1"
    assert ablation["scenario_count"] == 6
    assert ablation["isolation_policy"] == {
        "is_eval": True,
        "inherit_prior_runs": False,
        "writes_memory_items": False,
    }
    assert "must NOT be directly subtracted" in ablation["comparability_notice"]

    no_mem = ablation["modes"]["no_memory"]
    ep = ablation["modes"]["episodic"]
    both = ablation["modes"]["episodic_plus_semantic"]

    assert no_mem["top1"] == 0.3333
    assert ep["top1"] == 0.6667
    assert both["top1"] == 1.0
    assert ep["contamination_refusal_rate"] == 1.0
    assert both["contamination_refusal_rate"] == 1.0
    assert both["memory_recall_at_k"] == 1.0
    assert both["memory_citation_precision"] == 1.0

    # Verify automatic layer disabling when a layer shows <= 0.0 Top-1 gain
    gated_none = select_enabled_layers(episodic_top1_delta=0.0, semantic_top1_delta=-0.05)
    assert gated_none["enabled_layers"] == []
    assert len(gated_none["disabled_layers"]) == 2

    gated_ep_only = select_enabled_layers(episodic_top1_delta=0.15, semantic_top1_delta=0.0)
    assert gated_ep_only["enabled_layers"] == ["episodic"]
    assert [d["layer"] for d in gated_ep_only["disabled_layers"]] == ["semantic"]

    # Committed baseline files match fresh computation
    assert (baselines_dir() / M6_MANIFEST_NAME).exists()
    assert (baselines_dir() / M6_ABLATION_NAME).exists()
    assert build_m6_manifest()["set"] == "m6-v1"
