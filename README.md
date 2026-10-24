# AgentOps

Read-only incident diagnosis API. This repository is at milestone M0: the FastAPI app, the FR-1 incident schema, one error response shape, and offline tests.

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

The M1 rule baseline calls the tool port only. It does not read `ground_truth`. Frozen scenarios live in `scenarios/frozen/`. Six of them are holdouts for later prompt work. The committed score is `evals/baselines/m1-rule.json`.

```bash
python -m app.eval check
python -m app.eval write
python -m app.eval compare evals/baselines/m1-rule.json evals/baselines/m1-rule.json
```

`check` fails when a fresh run does not match the committed score or the scenario hashes.

## Scenarios API

`GET /v1/scenarios` returns id, title, service, and tags. It does not return `ground_truth`. Send the shared key in `X-API-Key`. `/health` stays open.

```bash
export API_KEY=dev-key
uvicorn app.main:app --reload
curl -H "X-API-Key: $API_KEY" http://127.0.0.1:8000/v1/scenarios
```
