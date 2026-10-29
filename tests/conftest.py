import pytest


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> object:
    from pathlib import Path

    from app.executor import service as executor
    from app.persistence.session import reset_engine

    root = Path(str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{root / 'test.db'}")
    monkeypatch.setenv("API_KEY", "dev-key")
    executor.pause_runs = False
    executor._tasks.clear()
    executor._cancel_events.clear()
    reset_engine()
    yield
    executor.pause_runs = False
    reset_engine()
