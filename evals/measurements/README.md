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
