# AgentOps

Read-only incident diagnosis API. Milestone M0 is done: the FastAPI app, the FR-1 incident schema, one error response shape, and CI. Milestone M1 is done: the scenario simulator, the read-only tool port, the frozen scenario set, and the rule baseline. Milestone M2 persists incidents and runs in SQLite or Postgres, executes the M4 investigator in-process, and streams `RunEvent` rows over SSE. Milestone M3 is done: `LLMPort` (`OpenAIAdapter` and `FakeLLMAdapter`), `ThreadedToolPort`, the single-shot code-swept LLM harness with ledger citation checks, and the M3-vs-M1 comparison report. Milestone M4 is done: the hand-written investigation loop (`Triage -> Hypothesize & Investigate -> ReadOnlyVerifier -> DiagnosisReport`), `EvidenceLedger`, `HypothesisBoard`, cassette replay (`evals/cassettes/m4-frozen.json`), and SRE CLI inspection (`python -m app.eval inspect`). CI eval gates (M5) are next. See [`docs/handoff.md`](docs/handoff.md) for the step-by-step engineering rationale and handoff guide.

## Architecture

AgentOps uses a **hexagonal (ports-and-adapters) architecture** where the diagnosis runtime depends only on abstract ports (`ToolPort`, `LLMPort`, `ClockPort`, `MemoryPort`). Every conclusion in the final report must cite stable `evidence_keys` recorded in the run's **Evidence Ledger**, and `ground_truth` is strictly isolated to the evaluation scorer.

```mermaid
flowchart TB
    subgraph Clients["Clients & Entrypoints"]
        CLI["CLI / HTTP Client"]
        EvalCLI["Eval Runner (python -m app.eval)"]
    end

    subgraph API["API & Service Layer (FastAPI)"]
        REST["REST + SSE Endpoints (/health, /v1/scenarios, /v1/incidents, /v1/runs)"]
        Exec["In-Process Asyncio Run Executor"]
    end

    subgraph Domain["Agent Runtime & Domain Core"]
        direction TB
        Triage["1. Triage (Code: service + default time window)"]
        Hypo["2. Hypothesize (Candidate causes)"]
        Investigate["3. Investigate Loop (Select read-only tools)"]
        Verifier["4. Read-Only Verifier (Single-pass ledger & contradiction check)"]
        Report["5. Structured Report (FAULT | NOT_A_FAULT | UNKNOWN)"]

        Ledger["Evidence Ledger & Hypothesis Board"]
        Guard["Budget & Guardrails (<=8 tool calls, 60s wall-clock, dedup)"]

        Triage --> Hypo --> Investigate --> Verifier --> Report
        Investigate <--> Ledger
        Verifier --> Ledger
        Guard -. "enforces limits" .-> Investigate
    end

    subgraph Modes["Controlled Experiment Modes"]
        M1["M1: Frozen Rule Baseline"]
        M3["M3: Single-Shot LLM (Code-swept evidence)"]
        M4["M4: Autonomous Investigation Agent"]
    end

    subgraph Ports["Hexagonal Ports"]
        ToolPort["ToolPort (6 Read-Only Tools + Redaction + Untrusted Wrap)"]
        LLMPort["LLMPort (Async)"]
        ClockPort["ClockPort (WallClock + ScenarioClock)"]
        MemoryPort["MemoryPort (Episodic + Semantic, M6)"]
    end

    subgraph Adapters["Adapters & Storage"]
        Sim["Scenario Simulator (YAML Topologies, Metrics, Logs, Changes)"]
        Prod["Production / MCP Adapters (Prometheus, M8)"]
        LLMProv["LLM Provider / Fake & Cassette Replay"]
        PG["PostgreSQL + pgvector (Incidents, RunEvents, MemoryItems, M2/M6)"]
    end

    subgraph EvalBox["Evaluation & Fairness Isolation"]
        Frozen["Frozen Scenarios (scenarios/frozen/*.yaml)"]
        Scorer["Deterministic Scorer & Fairness Check (app/eval)"]
    end

    CLI -- "X-API-Key" --> REST --> Exec --> Domain
    EvalCLI --> Scorer
    EvalCLI --> Modes
    Modes --> Domain

    Domain --> ToolPort
    Domain --> LLMPort
    Domain --> ClockPort
    Domain --> MemoryPort

    ToolPort --> Sim
    ToolPort -. "swap adapter" .-> Prod
    LLMPort --> LLMProv
    MemoryPort -.-> PG
    Exec -.-> PG

    Frozen -- "observable state only (no ground_truth)" --> Sim
    Frozen -- "ground_truth + expected keys" --> Scorer
    Report -- "hypotheses + cited evidence_keys" --> Scorer
```

### Read-Only Diagnostic Tools (`ToolPort`)

| Tool | Input Schema | Purpose | Production Counterpart |
| :--- | :--- | :--- | :--- |
| `query_metrics` | `MetricsQuery` (`service`, `time_range`, `metric`) | Time-series metrics (`cpu`, `memory`, `disk`, `latency_p99`, `error_rate`, `db_connections`) | Prometheus / Datadog |
| `search_logs` | `LogQuery` (`service`, `time_range`, `min_level`, `contains`, `offset`) | Paginated log search (max 20 lines/call, redacted & wrapped) | Grafana Loki / ELK |
| `get_service_status` | `ServiceQuery` (`service`, `time_range`) | Instance health and restart status | Kubernetes API |
| `get_recent_changes` | `ServiceQuery` (`service`, `time_range`) | Code deploys, rollbacks, and config flag changes in window | ArgoCD / GitHub Actions |
| `get_dependency_health` | `ServiceQuery` (`service`, `time_range`) | Downstream service topology and dependency health | Service Mesh / APM |
| `query_db_health` | `ServiceQuery` (`service`, `time_range`) | Connection pool usage, max connections, and queue depth | RDS / PgBouncer |

## Requirements

Python 3.12 or newer.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
uvicorn app.main:app --reload
```

- Health: <http://127.0.0.1:8000/health>
- API docs: <http://127.0.0.1:8000/docs>

The application entrypoint is `app.main:app`.

## Test

```bash
ruff check app tests
ruff format --check app tests
pytest
```

## Evaluate

The M1 rule baseline calls the tool port only. It does not read `ground_truth`. Frozen scenarios live in `scenarios/frozen/`. The committed score is `evals/baselines/m1-rule.json`, locked to `evals/baselines/frozen-manifest.json`.

The set has 29 scenarios. Nine are holdouts (`holdout: true`) for later prompt work. Beside the original single-service cases, the set includes:

- multi-hop faults, where the decisive evidence is on a downstream service
- drill-down faults, where the decisive evidence needs another service argument or a log window narrower than the default 20-line page
- one conflicting deploy-versus-config fault, where one sweep shows both signals
- one noisy `NOT_A_FAULT` case, where a pool phrase appears in a log and the host stays healthy

Sweeping every tool on the incident service, with the default window, does not return the decisive keys for the multi-hop and drill-down cases. Committed rule-baseline Top-1 is below 0.87, so M3 and M4 have room to move it. The number is in the baseline file.

Evidence precision is omitted from the average when the expected key set is empty and the report cites nothing. That is a correct `NOT_A_FAULT` or `UNKNOWN` abstention. Precision is 0 when keys were expected and the report cites nothing, and 0 when the report cites keys that were not expected.

The rule baseline and the original 23 scenarios landed in one commit (`f1eba32`). Git history does not show that the rules were frozen before those answers were written. The rules are generic thresholds. `RULE_ORDER` was not rearranged to recover the red-herring misses, and it was not rearranged when the later scenarios were added. Those later scenarios were written against the rules already in the tree.

```bash
python -m app.eval check
python -m app.eval write
python -m app.eval replay
python -m app.eval inspect drilldown-truncated-dns --mode m4
python -m app.eval run-m3 --provider fake
python -m app.eval run-m4 --provider fake
python -m app.eval compare evals/baselines/m3-single-shot.json evals/baselines/m4-agent.json
```

`check` fails when a fresh run does not match the committed M1/M3/M4 baseline files (`evals/baselines/m1-rule.json`, `evals/baselines/m3-single-shot.json`, `evals/baselines/m3-vs-m1.json`, `evals/baselines/m4-agent.json`, `evals/baselines/m4-vs-m3.json`), the scenario hashes (`evals/baselines/frozen-manifest.json`), or the M4 cassette replay (`evals/cassettes/m4-frozen.json`). To run M3 or M4 against a live OpenAI model, set `OPENAI_API_KEY` (and optional `OPENAI_MODEL` / `OPENAI_BASE_URL`) and pass `--provider openai`.

## Persist and diagnose

SQLite is the default database (`.agentops.sqlite` in the working directory). Postgres is the deployment database. List pagination is `limit` and `offset` because the requirements leave the page shape open. Archival sets `archived_at`; there is no `ARCHIVED` status. A restart marks leftover `PENDING` and `RUNNING` runs `FAILED` with reason `restart`, because this process is the only worker.

```bash
docker compose up -d
export DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5432/agentops
alembic upgrade head
export API_KEY=dev-key
uvicorn app.main:app --reload
```

One command submits an incident, follows the event stream, and prints the report. Without Postgres, omit `DATABASE_URL` and the API uses SQLite.

```bash
agentops diagnose --scenario cpu-saturation-checkout --api-key "$API_KEY"
```

`POST /v1/incidents/{id}/runs` returns 202. The same `Idempotency-Key` returns 202 while that run is active and 200 after it is terminal. A second run, or a run on an archived incident, returns 409. The API diagnosis uses the offline M4 investigator. `scenario_id` selects a frozen world. `ground_truth` stays in the scenario file.

## Scenarios API

`GET /v1/scenarios` returns id, title, service, and tags. It does not return `ground_truth`. Send the shared key in `X-API-Key`. `/health` stays open.

```bash
export API_KEY=dev-key
uvicorn app.main:app --reload
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/v1/scenarios
```

## M5 evaluation and tracing

PR CI runs unit tests and `python -m app.eval check` without live model credentials.
The gate checks frozen-file hashes, rule/fake-model baselines, citation validation,
and cassette replay. Severity metrics are retained in reports but excluded from
baseline score comparisons; cassette replay still checks exact recorded behavior.
A replay protects parsing, evidence handling and orchestration, not prompt quality.

Nightly measurements run in a separate scheduled/manual workflow, never on
`pull_request`. Enable it with repository variable `EVAL_NIGHTLY_ENABLED=true`,
secret `OPENAI_API_KEY`, and explicit variables `EVAL_MODEL`, `EVAL_JUDGE_MODEL`
and `EVAL_REPEATS` (integer >= 2). There is no assumed experiment repeat count.
The schedule is 16:17 UTC daily. Do not make the `measured-eval` job a required
branch check. Results are retained as GitHub Actions artifacts for 30 days,
including completed repeats if a later model call fails.

For an offline smoke test, or an explicitly configured live measurement:

```bash
python -m app.eval.nightly --provider fake --repeats 2 --output work/eval-smoke
# Set OPENAI_API_KEY and OPENAI_MODEL before using the live provider.
python -m app.eval.nightly --provider openai --repeats "$EVAL_REPEATS" \
  --judge-model "$EVAL_JUDGE_MODEL" --output work/eval-live
python -m app.eval compare old.json new.json --output comparison.json
```

The nightly JSON includes frozen hashes, revision (in Actions), model names,
every repeat's accepted report, Top-1/Top-3, evidence recall/precision,
first-output illegal-citation rate, verdict accuracy, FAULT severity and
`agreement@k`. The headline is mean M4 Top-1 minus mean M3 Top-1.
`agreement@k` is the modal valid answer count divided by k, averaged across
scenarios. The answer is verdict plus first category/cause for FAULT, verdict
alone otherwise; missing reports count only in the denominator. Consistently
wrong answers can have agreement 1. Confidence, prose and severity do not define
answer identity. Judge commentary examines first-repeat reports only, cannot
verify raw observations, and never changes deterministic scores.

Evidence precision follows the canonical `docs/requirements.md` FR-8: when
both expected and cited evidence are empty it is undefined and excluded from
the average; absent citations with expected evidence score zero. This is more
specific than issue #16's older acceptance wording. Existing frozen baselines
remain unchanged. Fake runs are labelled `offline-harness`; they are not real
model accuracy measurements.

OpenTelemetry traces cover API executor runs, M3/M4 diagnoses, real model calls,
and thread-offloaded tools. Export is disabled unless
`OTEL_EXPORTER_OTLP_ENDPOINT` (collector base URL) or
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (full traces URL) is set. Export uses
[OTLP HTTP/protobuf](https://opentelemetry.io/docs/languages/python/exporters/).
`OTEL_SDK_DISABLED=true` disables setup. Spans carry timing, outcome, token/tool
counts and error types; they do not attach incident text, prompts, tool data,
reports, credentials or exception messages.

### DeepSeek first measurement

DeepSeek uses the existing OpenAI-compatible adapter. For the agreed first
experiment, configure Actions variables:

| Variable | Value |
| --- | --- |
| EVAL_BASE_URL | https://api.deepseek.com |
| EVAL_MODEL | deepseek-flash |
| EVAL_JUDGE_MODEL | deepseek-flash |
| EVAL_REPEATS | 3 |
| EVAL_DEEPSEEK_THINKING | disabled |
| EVAL_NIGHTLY_ENABLED | false until the first manual report is checked |

Store the **DeepSeek** key in Actions secret OPENAI_API_KEY; that name is the
adapter's credential interface, not a requirement to use OpenAI's service.
Manual workflow dispatch works while the scheduled job is disabled.

[DeepSeek defaults to thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/),
which ignores temperature. This experiment explicitly disables it and requests
temperature 0 for both M3 and M4. Locally set OPENAI_BASE_URL and
DEEPSEEK_THINKING to the same values. Leave DEEPSEEK_THINKING unset for providers
that do not support DeepSeek's thinking parameter.

Reports retain the endpoint hostname, requested temperature/thinking mode, model
alias and model identifiers returned by the API. Alias versions can change, so
compare dated runs with the recorded settings and frozen manifest. No private
reasoning content is saved.
