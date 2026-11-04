# Live measurements

These are immutable observations, separate from deterministic fake baselines.
Do not replace earlier failures with corrected runs.

## Initial DeepSeek measurement (2026-10-29)

- [Actions run](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/runs/40804677634)
- Source revision: 3ca1d4589aa2abac2103f3e950bda074b8c1f3ab.
- Artifact copied without changing its JSON into 2026-10-29-deepseek-initial.json.
- Frozen-v1: 29 scenarios, 3 repeats per version, deepseek-flash, thinking disabled, temperature 0.
- FAULT Top-1: M3 83.33%, M4 0%; delta -83.33 percentage points.
- M3: 84 completed / 3 failed. M4: 8 completed / 79 failed.
- Judge commentary completed for 58 first-repeat reports. Judge success does not establish diagnosis correctness.
- This is evidence of a broken live M4 integration, not a usable claim that the agent improves on single-shot diagnosis.
- The first report omitted failure reasons. It cannot establish individual failure causes for all 79 failures.

## Diagnostic reproduction (2026-10-30)

- [Actions run](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/runs/40987692938)
- One non-holdout scenario, cpu-saturation-checkout, k=3; not a full-set score.
- M4 failed all three times: DiagnosisReport rejected invented top-level evidence/evidence_keys fields.
- Source inspection showed M4 omitted both the report schema and tool catalog from model messages. Fake-model tests masked this missing public contract.
- The repair supplies existing contract schemas and generic tool descriptions, without per-scenario hints or ground truth, and requests JSON mode.
- Preserve deterministic baseline files and scenario hashes. Re-run measurements under the repaired revision before starting M6.


## Repaired contract smoke (2026-10-30)

- [Actions run](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/runs/40987880238)
- Source revision: 1dde468; selected non-holdout scenarios cpu-saturation-checkout and unknown-latency-only, k=3.
- All 12 diagnoses (6 M3 and 6 M4) completed and matched the expected verdict/cause where applicable; no illegal accepted citations. Judge completed.
- M4 used 7–8 tool calls per diagnosis. These two cases confirm the contract integration works, not full-set accuracy or a gain over M3.
- Full frozen-set remeasurement is required before claiming improvement or starting memory ablations.

## Repaired full DeepSeek no-memory measurement (2026-10-30)

- [Actions run](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/runs/40988173433)
- Source revision: `d20db86e1556c3e39c17e39594804c3c7dae2d0c`.
- Artifact copied verbatim into `2026-10-30-deepseek-repaired.json` (SHA-256 `311965794c211176486e53a1dd08fe88e9acbd6336a6b9d6128ca99a83dc161b`).
- Settings: `frozen-v1` (29 scenarios, 9 holdouts), `k=3` repeats (87 observations per version), `deepseek-flash` via `https://api.deepseek.com`, `deepseek_thinking: disabled`, `temperature: 0.0`.
- Completion: M3 `86/87` completed (`1` failed on `db-pool-with-cpu-herring` repeat 3 after schema retry); M4 `87/87` completed (`0` failed, compared with `79/87` failed before contract repair).
- Headline (`M4 Top-1 minus M3 Top-1`): `-0.1167` (`M3 Top-1 = 0.8500`, `M4 Top-1 = 0.7333`).
- `Top-3`: M3 `0.9333`, M4 `0.7667` (`delta = -0.1666`).
- `Verdict accuracy`: M3 (`FAULT: 0.9333`, `NOT_A_FAULT: 0.9333`, `UNKNOWN: 0.7500`), M4 (`FAULT: 0.9333`, `NOT_A_FAULT: 0.8000`, `UNKNOWN: 0.7500`).
- `Evidence recall / precision`: M3 (`0.9333` / `0.7292`), M4 (`0.7472` / `0.6750`).
- `FAULT severity accuracy`: M3 `0.6333`, M4 `0.8333` (`+0.2000`).
- `Citation metrics`: `first_output_illegal_citation_rate` is `0.0000` (M3) and `0.0230` (M4); `accepted_illegal_citation_rate` is `0.0000` for both.
- `agreement@3`: M3 `0.9770`, M4 `0.9425`. Judge commentary status: `completed` (`58` first-repeat commentaries).
- Tool budget: M4 averaged `6.69` tool calls per run (max `8`), versus M3's code-swept `12.90` average (max `33`).
- Limitations & analysis (no-memory reference for M6):
  - M4 recovered `1/3` on `drilldown-truncated-dns` and `1/3` on `dependency-with-cpu-herring` (both `0/3` in M3), proving that multi-step window narrowing works on a live model.
  - However, M4 scored `0/3` on `drilldown-payments-pool`, `multihop-checkout-payments-pool`, and `multihop-checkout-pricing-dns` (all `3/3` in M3). M3's harness sweeps every service in `scenario.world.services` upfront (`22–33` tool calls), whereas no-memory M4 spent its `7–8` tool calls on local `checkout` metrics/logs and stopped at the first-hop `dependency_timeout` without pivoting to `payments` or `pricing`.
  - Recurring nightly runs remain disabled (`EVAL_NIGHTLY_ENABLED=false`) to prevent unattended API spend.
