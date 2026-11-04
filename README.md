# AgentOps

[![CI](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/workflows/ci.yml)
[![Nightly Eval](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/workflows/nightly-eval.yml/badge.svg)](https://github.com/kayzenline/AI-Autonomous-Incident-Diagnosis-Platform/actions/workflows/nightly-eval.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)
![Database](https://img.shields.io/badge/database-PostgreSQL%2016%20%7C%20SQLite-336791.svg)
![Safety](https://img.shields.io/badge/tools-read--only%20%2B%20redacted-success.svg)

## Overview

**AgentOps** is an evidence-grounded, read-only autonomous incident diagnosis platform. Given an incident alert, it investigates service metrics, logs, deployments, dependencies, and database connection pools through a hexagonal port boundary, tracks competing hypotheses on a structured **Hypothesis Board**, verifies every cited claim against an immutable **Evidence Ledger**, and streams its step-by-step investigation timeline over Server-Sent Events (SSE).

### Key Capabilities

- **Evidence-Grounded Diagnosis (`EvidenceLedger` + `ReadOnlyVerifier`)**: Every hypothesis in the final `DiagnosisReport` (`FAULT` | `NOT_A_FAULT` | `UNKNOWN`) must cite verifiable `evidence_keys` produced by tool calls in that run. A zero-tool `ReadOnlyVerifier` checks citation existence, cross-tool corroboration, and contradicting signals before any report is accepted.
- **Bounded LangGraph Orchestration**: Executes an explicit `StateGraph` (`triage -> recall -> investigate_step <-> tool_dispatch -> verify -> finalize`) under strict guardrails (`<= 8` tool calls, `60s` wall-clock timeout, duplicate-call caching, and graceful partial reports on timeout/budget exhaustion).
- **Evidence-Checked Episodic & Semantic Memory**: Hybrid vector + Okapi BM25 retrieval with exponential time decay, `service_version` staleness checks, write-time secret/IP redaction, and citation-only feedback trust transitions (`CANDIDATE -> CONFIRMED | QUARANTINED`).
- **Step Checkpoints & Human-in-the-Loop (HITL) Approvals**: Persists per-step LangGraph checkpoints (`run_checkpoints`) for opt-in crash recovery (`POST /v1/runs/{id}/resume`) and tracks operator approvals/rejections on recommended actions (`GET /v1/runs/{id}/approvals`, `POST /v1/runs/{id}/approve`).

### Documentation

- **Engineering Blog (M1–M7 Workflow & Quantitative Results)**: [`docs/blog-m1-to-m7.md`](docs/blog-m1-to-m7.md)
- **Engineering Handoff & Architecture Rationale**: [`docs/handoff.md`](docs/handoff.md)
- **System Requirements Specification**: [`docs/requirements.md`](docs/requirements.md)
- **Live Model Measurements Archive**: [`evals/measurements/README.md`](evals/measurements/README.md)

---

## Architecture

AgentOps uses a **hexagonal (ports-and-adapters) architecture** where the diagnosis runtime depends only on abstract ports (`ToolPort`, `LLMPort`, `ClockPort`, `MemoryPort`). `ground_truth` is strictly isolated to the evaluation scorer (`app/eval/fairness.py`).

```mermaid
flowchart TB
    subgraph Clients["Clients & Entrypoints"]
        CLI["CLI / HTTP Client (agentops diagnose)"]
        EvalCLI["Eval Runner (python -m app.eval)"]
    end

    subgraph API["API & Service Layer (FastAPI)"]
        REST["REST + SSE Endpoints (/health, /v1/scenarios, /v1/incidents, /v1/runs, /v1/memory)"]
        Exec["In-Process Asyncio Run Executor"]
    end

    subgraph Domain["Agent Runtime & Domain Core (LangGraph StateGraph)"]
        direction TB
        Triage["1. Triage (Code: service + default time window)"]
        Recall["2. Recall (Episodic + Semantic Memory Cues)"]
        Investigate["3. Investigate & Tool Dispatch (Select read-only tools)"]
        Verifier["4. Read-Only Verifier (0 tool calls: ledger & contradiction check)"]
        Report["5. Finalize Report (FAULT | NOT_A_FAULT | UNKNOWN + HITL Approvals)"]

        Ledger["Evidence Ledger & Hypothesis Board"]
        Guard["Budget & Guardrails (<=8 tool calls, 60s wall-clock, dedup, checkpoints)"]

        Triage --> Recall --> Investigate --> Verifier --> Report
        Investigate <--> Ledger
        Verifier --> Ledger
        Guard -. "enforces limits & saves checkpoints" .-> Investigate
    end

    subgraph Ports["Hexagonal Ports"]
        ToolPort["ToolPort (6 Read-Only Tools + Redaction + Untrusted Wrap)"]
        LLMPort["LLMPort (Async)"]
        ClockPort["ClockPort (WallClock + ScenarioClock)"]
        MemoryPort["MemoryPort (Episodic + Semantic)"]
    end

    subgraph Adapters["Adapters & Storage"]
        Sim["Scenario Simulator (YAML Topologies, Metrics, Logs, Changes)"]
        LLMProv["OpenAI / DeepSeek Adapter + Fake & Cassette Replay"]
        PG["PostgreSQL 16 / SQLite (Incidents, RunEvents, Memory, Checkpoints, Approvals)"]
    end

    CLI -- "X-API-Key" --> REST --> Exec --> Domain
    EvalCLI --> Domain
    Domain --> ToolPort --> Sim
    Domain --> LLMPort --> LLMProv
    Domain --> ClockPort
    Domain --> MemoryPort -.-> PG
    Exec -.-> PG
```

### Read-Only Diagnostic Tools (`ToolPort`)

| Tool | Input Schema | Purpose | Production Counterpart |
| :--- | :--- | :--- | :--- |
| `query_metrics` | `MetricsQuery` (`service`, `time_range`, `metric`) | Time-series metrics (`cpu`, `memory`, `disk`, `latency_p99`, `error_rate`, `db_connections`) | Prometheus / Datadog |
| `search_logs` | `LogQuery` (`service`, `time_range`, `min_level`, `contains`, `offset`) | Paginated log search (max 20 lines/call, redacted & wrapped in `<untrusted_data>`) | Grafana Loki / ELK |
| `get_service_status` | `ServiceQuery` (`service`, `time_range`) | Instance health and restart status | Kubernetes API |
| `get_recent_changes` | `ServiceQuery` (`service`, `time_range`) | Code deploys, rollbacks, and config flag changes in window | ArgoCD / GitHub Actions |
| `get_dependency_health` | `ServiceQuery` (`service`, `time_range`) | Downstream service topology and dependency health | Service Mesh / APM |
| `query_db_health` | `ServiceQuery` (`service`, `time_range`) | Connection pool usage, max connections, and queue depth | RDS / PgBouncer |

---

## Quick Start & Usage

### 1. Requirements & Installation

Requires **Python 3.12+**.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Credentials & Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `API_KEY` | `dev-key` | Shared secret required in the `X-API-Key` HTTP header for all `/v1/*` endpoints (`/health` is open). |
| `DATABASE_URL` | `sqlite+aiosqlite:///./.agentops.sqlite` | Database connection URL. Omit for zero-dependency local SQLite, or set to `postgresql+asyncpg://...` for PostgreSQL 16. |
| `OPENAI_API_KEY` | *(unset)* | API credential for live LLM evaluation (`--provider openai`, supports OpenAI and DeepSeek). |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint base URL (e.g. `https://api.deepseek.com` for DeepSeek). |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name for live runs (e.g. `deepseek-flash`). |
| `DEEPSEEK_THINKING` | *(unset)* | Set to `disabled` when using DeepSeek with `temperature=0` so deterministic temperature is respected. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)* | Optional OTLP HTTP/protobuf collector URL for OpenTelemetry trace export (`OTEL_SDK_DISABLED=true` disables tracing). |

### 3. Run the API Server

#### Option A: Zero-Dependency Local SQLite

```bash
export API_KEY=dev-key
uvicorn app.main:app --reload
```

#### Option B: PostgreSQL 16 + Alembic Migrations

```bash
docker compose up -d --wait
export DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5432/agentops
alembic upgrade head
export API_KEY=dev-key
uvicorn app.main:app --reload
```

- Health check: <http://127.0.0.1:8000/health>
- OpenAPI docs: <http://127.0.0.1:8000/docs>

### 4. Diagnose an Incident via CLI or HTTP API

Submit an incident, stream its real-time SSE investigation events, and print the structured `DiagnosisReport` in one command:

```bash
agentops diagnose --scenario cpu-saturation-checkout --api-key "$API_KEY"
```

Or interact directly with the REST + SSE endpoints:

```bash
# 1. List available frozen scenarios (ground_truth is never exposed)
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/v1/scenarios

# 2. Create an incident
INCIDENT_ID=$(curl -s -X POST http://127.0.0.1:8000/v1/incidents \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"title":"Checkout latency spike","description":"p99 latency above 1.5s","service":"checkout"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 3. Start an async diagnosis run (optional: checkpoint_policy="resume" for M7 checkpoint recovery)
RUN_ID=$(curl -s -X POST "http://127.0.0.1:8000/v1/incidents/$INCIDENT_ID/runs" \
  -H "X-API-Key: $API_KEY" -H "Idempotency-Key: run-1" -H "Content-Type: application/json" \
  -d '{"scenario_id":"cpu-saturation-checkout","checkpoint_policy":"resume"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

# 4. Stream live investigation events over SSE (supports Last-Event-ID resumption)
curl -N -H "X-API-Key: $API_KEY" "http://127.0.0.1:8000/v1/runs/$RUN_ID/events"

# 5. Inspect HITL action approvals and record an operator decision
curl -H "X-API-Key: $API_KEY" "http://127.0.0.1:8000/v1/runs/$RUN_ID/approvals"
curl -X POST "http://127.0.0.1:8000/v1/runs/$RUN_ID/approve" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"action_index":0,"decision":"approved","actor":"sre-oncall","notes":"Verified on metrics"}'

# 6. Submit post-incident feedback (updates trust levels ONLY on cited memories)
curl -X POST "http://127.0.0.1:8000/v1/runs/$RUN_ID/feedback" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"verdict":"correct","notes":"Root cause confirmed"}'
```

### 5. Testing & Evaluation CLI

Run lint, formatting, unit/integration tests, and the offline evaluation gate:

```bash
ruff check app tests
ruff format --check app tests
pytest -q
python -m app.eval check
```

Inspect individual scenario traces, replay cassettes, or run milestone evaluations:

```bash
# Inspect step-by-step timeline, evidence ledger, hypothesis board, and verifier output
python -m app.eval inspect drilldown-truncated-dns --mode m7

# Replay all 29 recorded M4 cassettes and verify zero drift
python -m app.eval replay

# Run deterministic offline baselines or compare two reports
python -m app.eval run-m3 --provider fake
python -m app.eval run-m4 --provider fake
python -m app.eval run-m6-ablation --provider fake
python -m app.eval run-m7 --provider fake
python -m app.eval compare evals/baselines/m4-agent.json evals/baselines/m7-langgraph.json

# Run repeated live-model nightly evaluation (with optional M6 3-way memory ablation)
python -m app.eval.nightly --provider openai --repeats 3 \
  --judge-model "$EVAL_JUDGE_MODEL" --include-m6 --output work/eval-live
```
