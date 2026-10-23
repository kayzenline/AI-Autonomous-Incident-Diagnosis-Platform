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
