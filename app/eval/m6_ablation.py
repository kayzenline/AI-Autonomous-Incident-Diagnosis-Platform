"""M6 3-way memory ablation runner: no_memory vs episodic vs episodic_plus_semantic (FR-7, FR-8).

Evaluates the versioned `scenarios/m6/*.yaml` suite (including memory contamination
and stale-service scenarios) under identical model and repeat settings, records
retrieval quality and diagnosis accuracy, and disables any memory layer with no
demonstrated benefit.

Evaluation isolation (FR-8):
- Every evaluation run uses an isolated read-only catalog (`is_eval=True`).
- Cross-run inheritance is disabled (`prior_context=None`).
- Evaluation runs never write new `MemoryItem` records.
- Scores from `m6-v1` must not be directly subtracted from published `frozen-v1` M3/M4 scores.
"""

import asyncio
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.investigator import InvestigationResult, investigate
from app.agent.ledger import HypothesisStatus
from app.core.taxonomy import Category, Cause
from app.eval.fairness import assert_baseline_is_isolated
from app.eval.scenario import Scenario, frozen_dir, load_scenario
from app.eval.score import cited_keys, evidence_precision, evidence_recall, top_hit
from app.llm.adapters.investigator_fake import InvestigativeFakeLLM
from app.llm.port import LLMPort
from app.memory.models import (
    MemoryConfig,
    MemoryItem,
    MemoryLayer,
    MemoryWrite,
    TrustLevel,
)
from app.memory.port import InMemoryCatalog
from app.tools.simulator.adapter import SimulatorTools

M6_MANIFEST_NAME = "m6-manifest.json"
M6_ABLATION_NAME = "m6-ablation.json"

CONTAMINATED_MEMORY_CAUSES: dict[str, Cause] = {
    "memory-contamination-bad-deploy": Cause.BAD_DEPLOY,
    "memory-contamination-cpu-herring": Cause.CPU_SATURATION,
    "stale-service-checkout-v2-pool": Cause.CONFIG_ERROR,
    "stale-service-gateway-v3-dns": Cause.BAD_DEPLOY,
}

HELPFUL_MEMORY_SCENARIOS: dict[str, set[MemoryLayer]] = {
    "stale-service-checkout-v2-pool": {MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC},
    "stale-service-gateway-v3-dns": {MemoryLayer.SEMANTIC},
    "episodic-multidep-inventory-pool": {MemoryLayer.EPISODIC},
    "semantic-runbook-cert-config": {MemoryLayer.SEMANTIC},
}


def m6_dir() -> Path:
    return frozen_dir().parent / "m6"


def load_m6_scenarios(directory: Path | None = None) -> list[Scenario]:
    root = directory or m6_dir()
    paths = sorted(root.glob("*.yaml"))
    scenarios = [load_scenario(path) for path in paths]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate M6 scenario id")
    return scenarios


def build_m6_manifest(directory: Path | None = None) -> dict[str, Any]:
    root = directory or m6_dir()
    scenarios = {s.id: s for s in load_m6_scenarios(root)}
    rows = []
    for path in sorted(root.glob("*.yaml")):
        scenario = scenarios[path.stem]
        rows.append(
            {
                "id": scenario.id,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "tags": list(scenario.tags),
                "holdout": scenario.holdout,
            }
        )
    return {"set": "m6-v1", "scenarios": rows}


async def build_seed_catalog(config: MemoryConfig | None = None) -> InMemoryCatalog:
    """Construct the deterministic seed memory catalog for M6 ablation experiments."""
    catalog = InMemoryCatalog(config=config)
    t_old = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
    t_recent = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)

    seeds = (
        # 1. Contaminated episodic memory for checkout (claims bad_deploy on 5xx peak spike)
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            service="checkout",
            category=Category.DEPLOY,
            cause=Cause.BAD_DEPLOY,
            trust_level=TrustLevel.CANDIDATE,
            service_version="v2.0",
            summary="Checkout 5xx spike during peak traffic caused by bad_deploy",
            content=(
                "Past incident: Checkout 5xx spike during peak traffic window was attributed "
                "to a bad_deploy rollout on checkout."
            ),
            created_at=t_recent,
        ),
        # 2. Contaminated episodic memory for payments (claims cpu_saturation on webhook failure)
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            service="payments",
            category=Category.CPU,
            cause=Cause.CPU_SATURATION,
            trust_level=TrustLevel.CANDIDATE,
            service_version="v2.0",
            summary="Payments webhook delivery failing due to cpu_saturation",
            content=(
                "Past incident: Outbound payment settlement webhook delivery failing across "
                "workers was attributed to cpu_saturation on payments."
            ),
            created_at=t_recent,
        ),
        # 3a. Stale v1.4 episodic memory for checkout (claims local config_error in v1.4)
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            service="checkout",
            category=Category.CONFIG,
            cause=Cause.CONFIG_ERROR,
            trust_level=TrustLevel.CONFIRMED,
            service_version="v1.4",
            valid_until=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            summary="Checkout order submission failing in v1.4 due to local config_error",
            content=(
                "Legacy v1.4 incident: Checkout order submissions failing were caused by "
                "config_error on checkout monolithic config."
            ),
            created_at=t_old,
        ),
        # 3b. Fresh v2.0 episodic memory for checkout order submission -> downstream payments pool
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            service="checkout",
            category=Category.DATABASE,
            cause=Cause.DB_POOL_EXHAUSTED,
            trust_level=TrustLevel.CONFIRMED,
            service_version="v2.0",
            summary="Checkout order submission failing after v2.0 split traced to payments db pool",
            content=(
                "Confirmed v2.0 incident: Checkout order submissions failing with upstream 5xx "
                "in v2.0 were caused by db_pool_exhausted on downstream service payments."
            ),
            created_at=t_recent,
        ),
        # 4a. Stale v2.1 episodic memory for api-gateway (claims bad_deploy on legacy-billing)
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            service="api-gateway",
            category=Category.DEPLOY,
            cause=Cause.BAD_DEPLOY,
            trust_level=TrustLevel.CONFIRMED,
            service_version="v2.1",
            valid_until=datetime(2026, 9, 15, 0, 0, tzinfo=UTC),
            summary="Gateway checkout route failing in v2.1 due to bad_deploy on legacy-billing",
            content=(
                "Legacy v2.1 incident: API Gateway 502 errors on checkout routes were caused "
                "by bad_deploy on legacy-billing."
            ),
            created_at=t_old,
        ),
        # 4b. Fresh v3.0 semantic runbook for api-gateway -> settlement-router DNS check
        MemoryWrite(
            layer=MemoryLayer.SEMANTIC,
            service="api-gateway",
            category=Category.NETWORK,
            cause=None,
            trust_level=TrustLevel.CONFIRMED,
            service_version="v3.0",
            summary="Runbook: API Gateway v3.0 routes settlement traffic via settlement-router",
            content=(
                "Service knowledge (v3.0): API Gateway v3.0 routes all checkout and "
                "settlement requests to downstream service settlement-router. "
                "On 502 errors, inspect settlement-router error logs for internal DNS failures."
            ),
            created_at=t_recent,
        ),
        # 5. Confirmed episodic memory for order-service flash sale -> inventory-db-proxy pool
        MemoryWrite(
            layer=MemoryLayer.EPISODIC,
            service="order-service",
            category=Category.DATABASE,
            cause=Cause.DB_POOL_EXHAUSTED,
            trust_level=TrustLevel.CONFIRMED,
            service_version="v2.0",
            summary="Order reservation timeouts under flash sale caused by inventory-db-proxy pool",
            content=(
                "Confirmed v2.0 incident: Order reservation timeouts and 5xx under flash sale "
                "load on order-service were caused by db_pool_exhausted on downstream "
                "service inventory-db-proxy."
            ),
            created_at=t_recent,
        ),
        # 6. Confirmed semantic runbook for edge-proxy v2.2 -> cert-manager config
        MemoryWrite(
            layer=MemoryLayer.SEMANTIC,
            service="edge-proxy",
            category=Category.CONFIG,
            cause=None,
            trust_level=TrustLevel.CONFIRMED,
            service_version="v2.2",
            summary="Runbook: Edge proxy v2.2 upstream TLS handshake depends on cert-manager",
            content=(
                "Service knowledge (v2.2): Edge proxy v2.2 delegates mTLS bundle reloads "
                "to downstream service cert-manager. On 503 TLS handshake errors, inspect "
                "cert-manager recent config changes and error logs."
            ),
            created_at=t_recent,
        ),
    )
    for body in seeds:
        await catalog.write(body)
    return catalog


class ReadOnlyEvalMemoryPort:
    """Wraps a catalog for evaluation runs so writes are prohibited (FR-8)."""

    def __init__(self, inner: InMemoryCatalog) -> None:
        self._inner = inner
        self.write_attempts = 0

    @property
    def config(self) -> MemoryConfig:
        return self._inner.config

    async def search(self, query: Any) -> list[Any]:
        return await self._inner.search(query)

    async def write(self, body: MemoryWrite) -> tuple[MemoryItem, bool]:
        del body
        self.write_attempts += 1
        raise RuntimeError("Evaluation runs must not write MemoryItem records (FR-8).")


async def run_m6_ablation_async(
    scenarios: list[Scenario] | None = None,
    *,
    llm_factory: Any = None,
    repeats: int = 1,
) -> dict[str, Any]:
    assert_baseline_is_isolated()
    ordered = sorted(scenarios or load_m6_scenarios(), key=lambda item: item.id)
    modes: tuple[tuple[str, tuple[MemoryLayer, ...]], ...] = (
        ("no_memory", ()),
        ("episodic", (MemoryLayer.EPISODIC,)),
        ("episodic_plus_semantic", (MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC)),
    )

    mode_reports: dict[str, dict[str, Any]] = {}
    for mode_name, layers in modes:
        mode_reports[mode_name] = await _run_single_mode(
            mode_name=mode_name,
            layers=layers,
            scenarios=ordered,
            llm_factory=llm_factory,
            repeats=repeats,
        )

    no_mem_top1 = float(mode_reports["no_memory"]["top1"] or 0.0)
    ep_top1 = float(mode_reports["episodic"]["top1"] or 0.0)
    both_top1 = float(mode_reports["episodic_plus_semantic"]["top1"] or 0.0)

    ep_delta = round(ep_top1 - no_mem_top1, 4)
    sem_delta = round(both_top1 - ep_top1, 4)
    total_delta = round(both_top1 - no_mem_top1, 4)

    layer_selection = select_enabled_layers(
        episodic_top1_delta=ep_delta,
        semantic_top1_delta=sem_delta,
    )

    return {
        "experiment": "m6-memory-ablation",
        "scenario_set": "m6-v1",
        "scenario_count": len(ordered),
        "repeats_k": repeats,
        "isolation_policy": {
            "is_eval": True,
            "inherit_prior_runs": False,
            "writes_memory_items": False,
        },
        "comparability_notice": (
            "M6 scores are measured on the versioned m6-v1 suite with search_memory "
            "enabled and must NOT be directly subtracted from previously published "
            "frozen-v1 M3/M4 scores (FR-3, M6)."
        ),
        "modes": mode_reports,
        "comparisons": {
            "episodic_vs_no_memory_top1_delta": ep_delta,
            "semantic_vs_episodic_top1_delta": sem_delta,
            "episodic_plus_semantic_vs_no_memory_top1_delta": total_delta,
        },
        "layer_gating": layer_selection,
    }


def select_enabled_layers(
    *,
    episodic_top1_delta: float,
    semantic_top1_delta: float,
) -> dict[str, Any]:
    """Disable any memory layer that demonstrates no positive Top-1 benefit (FR-7)."""
    enabled: list[str] = []
    disabled: list[dict[str, Any]] = []

    if episodic_top1_delta > 0.0:
        enabled.append(MemoryLayer.EPISODIC.value)
    else:
        disabled.append(
            {
                "layer": MemoryLayer.EPISODIC.value,
                "top1_delta": episodic_top1_delta,
                "reason": "No Top-1 gain over no_memory baseline; disabled by default.",
            }
        )

    if semantic_top1_delta > 0.0:
        enabled.append(MemoryLayer.SEMANTIC.value)
    else:
        disabled.append(
            {
                "layer": MemoryLayer.SEMANTIC.value,
                "top1_delta": semantic_top1_delta,
                "reason": "No demonstrated Top-1 gain over episodic baseline; disabled by default.",
            }
        )

    return {
        "enabled_layers": enabled,
        "disabled_layers": disabled,
    }


async def _run_single_mode(
    *,
    mode_name: str,
    layers: tuple[MemoryLayer, ...],
    scenarios: list[Scenario],
    llm_factory: Any,
    repeats: int,
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    agreement_scores: list[float] = []
    model_name = "fake-m4-investigator-v1"

    for scenario in scenarios:
        signatures: list[tuple[str | None, str | None]] = []
        primary_row: dict[str, Any] | None = None

        for rep_idx in range(repeats):
            adapter: LLMPort = llm_factory() if llm_factory is not None else InvestigativeFakeLLM()
            model_name = getattr(adapter, "model", model_name)
            seed_catalog = await build_seed_catalog()
            initial_item_count = len(seed_catalog.all_items())
            eval_memory = ReadOnlyEvalMemoryPort(seed_catalog) if layers else None

            result = await investigate(
                scenario.incident,
                SimulatorTools(scenario.world),
                adapter,
                known_services=scenario.world.services,
                memory=eval_memory,
                memory_layers=layers,
                prior_context=None,
                is_eval=True,
            )
            if len(seed_catalog.all_items()) != initial_item_count:
                raise RuntimeError("Evaluation run mutated memory catalog items!")

            row = _score_m6_case(scenario, result, active_layers=layers)
            pred_v = row["predicted_verdict"]
            pred_c = row["predicted_top_cause"]
            signatures.append((pred_v, pred_c))
            if rep_idx == 0:
                primary_row = row

        assert primary_row is not None
        most_common = Counter(signatures).most_common(1)[0][1]
        agree = round(most_common / len(signatures), 4)
        primary_row["agreement_at_k"] = agree
        agreement_scores.append(agree)
        cases.append(primary_row)

    faults = [c for c in cases if c["expected_verdict"] == "FAULT"]
    recalls = [c["evidence_recall"] for c in cases if c["evidence_recall"] is not None]
    precisions = [c["evidence_precision"] for c in cases if c["evidence_precision"] is not None]
    severities = [c["severity_hit"] for c in faults if c["severity_hit"] is not None]
    contam_cases = [c for c in cases if c["contaminated_memory_present"]]
    helpful_cases = [c for c in cases if c["helpful_memory_expected"] is not None]
    cited_totals = [
        c["cited_memory_precision"] for c in cases if c["cited_memory_precision"] is not None
    ]

    return {
        "mode": mode_name,
        "enabled_layers": [layer.value for layer in layers],
        "model": model_name,
        "scenario_count": len(cases),
        "top1": _rate([c["top1_hit"] for c in faults]),
        "top3": _rate([c["top3_hit"] for c in faults]),
        "evidence_recall": _mean([float(v) for v in recalls]),
        "evidence_precision": _mean([float(v) for v in precisions]),
        "severity_accuracy": _rate(severities),
        "avg_tool_calls": _mean([float(c["tool_calls"]) for c in cases]),
        "accepted_illegal_citation_rate": _mean(
            [
                float(c["accepted_illegal_citation_rate"])
                for c in cases
                if c["status"] == "COMPLETED"
            ]
        ),
        "agreement_at_k": _mean(agreement_scores),
        "memory_recall_at_k": _rate([c["helpful_memory_recalled"] for c in helpful_cases]),
        "memory_citation_precision": _mean([float(v) for v in cited_totals]),
        "contamination_refusal_rate": _rate(
            [c["contaminated_memory_refuted"] for c in contam_cases]
        ),
        "cases": cases,
    }


def _score_m6_case(
    scenario: Scenario,
    result: InvestigationResult,
    *,
    active_layers: tuple[MemoryLayer, ...],
) -> dict[str, Any]:
    expected = scenario.ground_truth
    report = result.report
    cited = cited_keys(report) if report is not None else set()
    top_cause = (
        report.hypotheses[0].cause.value if (report is not None and report.hypotheses) else None
    )
    recall = evidence_recall(cited, expected.key_evidence_keys) if report is not None else 0.0
    precision = evidence_precision(cited, expected.key_evidence_keys) if report is not None else 0.0

    contam_cause = CONTAMINATED_MEMORY_CAUSES.get(scenario.id)
    contaminated_present = contam_cause is not None and bool(active_layers)
    contaminated_refuted: bool | None = None
    if contaminated_present and contam_cause is not None:
        rejected_in_events = any(
            ev.type.value == "HYPOTHESIS_UPDATED"
            and ev.payload.get("cause") == contam_cause.value
            and ev.payload.get("status") == HypothesisStatus.REJECTED.value
            for ev in result.events
        )
        not_adopted = top_cause != contam_cause.value
        contaminated_refuted = bool(not_adopted and rejected_in_events)

    expected_helpful_layers = HELPFUL_MEMORY_SCENARIOS.get(scenario.id)
    helpful_expected: bool | None = None
    helpful_recalled: bool | None = None
    if expected_helpful_layers is not None and bool(set(active_layers) & expected_helpful_layers):
        helpful_expected = True
        helpful_recalled = any(
            not rec.stale
            and rec.trust_level == TrustLevel.CONFIRMED
            and rec.layer in expected_helpful_layers
            for rec in result.recalled_memories
        )

    cited_mem_precision: float | None = None
    if result.cited_memory_ids:
        rec_by_id = {rec.memory_id: rec for rec in result.recalled_memories}
        good = 0
        for mid in result.cited_memory_ids:
            rec = rec_by_id.get(mid)
            if (
                rec is not None
                and not rec.stale
                and rec.trust_level != TrustLevel.REJECTED
                and (rec.cause is None or rec.cause == expected.cause)
            ):
                good += 1
        cited_mem_precision = round(good / len(result.cited_memory_ids), 4)

    return {
        "scenario_id": scenario.id,
        "status": result.status,
        "expected_verdict": expected.verdict.value,
        "expected_cause": None if expected.cause is None else expected.cause.value,
        "predicted_verdict": None if report is None else report.verdict.value,
        "predicted_top_cause": top_cause,
        "predicted_severity": None if report is None else report.severity,
        "verdict_hit": False if report is None else (report.verdict == expected.verdict),
        "top1_hit": False if report is None else top_hit(report, expected, limit=1),
        "top3_hit": False if report is None else top_hit(report, expected, limit=3),
        "severity_hit": (
            False
            if report is None
            else (report.severity == expected.severity if expected.verdict == "FAULT" else None)
        ),
        "evidence_recall": None if recall is None else round(recall, 4),
        "evidence_precision": None if precision is None else round(precision, 4),
        "tool_calls": result.tool_calls,
        "recalled_memory_count": len(result.recalled_memories),
        "cited_memory_count": len(result.cited_memory_ids),
        "cited_memory_precision": cited_mem_precision,
        "helpful_memory_expected": helpful_expected,
        "helpful_memory_recalled": helpful_recalled,
        "contaminated_memory_present": contaminated_present,
        "contaminated_memory_refuted": contaminated_refuted,
        "accepted_illegal_citation_rate": result.accepted_illegal_citation_rate,
    }


def run_m6_ablation(
    scenarios: list[Scenario] | None = None,
    *,
    llm_factory: Any = None,
    repeats: int = 1,
) -> dict[str, Any]:
    return asyncio.run(run_m6_ablation_async(scenarios, llm_factory=llm_factory, repeats=repeats))


def write_m6_baselines(directory: Path | None = None) -> None:
    target = directory or (frozen_dir().parents[1] / "evals" / "baselines")
    target.mkdir(parents=True, exist_ok=True)
    manifest = build_m6_manifest()
    ablation = run_m6_ablation()
    (target / M6_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (target / M6_ABLATION_NAME).write_text(json.dumps(ablation, indent=2) + "\n", encoding="utf-8")


def check_m6_baselines(directory: Path | None = None) -> None:
    target = directory or (frozen_dir().parents[1] / "evals" / "baselines")
    manifest_path = target / M6_MANIFEST_NAME
    ablation_path = target / M6_ABLATION_NAME
    if not manifest_path.exists():
        raise SystemExit(f"missing {manifest_path}")
    if not ablation_path.exists():
        raise SystemExit(f"missing {ablation_path}")
    if json.loads(manifest_path.read_text(encoding="utf-8")) != build_m6_manifest():
        raise SystemExit(f"{M6_MANIFEST_NAME} does not match a fresh M6 manifest run")
    if json.loads(ablation_path.read_text(encoding="utf-8")) != run_m6_ablation():
        raise SystemExit(f"{M6_ABLATION_NAME} does not match a fresh M6 ablation run")


def _rate(flags: list[Any]) -> float | None:
    valid = [f for f in flags if f is not None]
    if not valid:
        return None
    return round(sum(1 for flag in valid if flag) / len(valid), 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)
