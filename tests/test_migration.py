import sqlite3

from alembic import command
from alembic.config import Config


def test_alembic_upgrade_creates_the_m2_tables(tmp_path: object, monkeypatch: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    database = root / "migrated.db"
    url = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setenv("DATABASE_URL", url)  # type: ignore[attr-defined]
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")

    with sqlite3.connect(database) as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "incidents",
        "runs",
        "run_events",
        "tool_calls",
        "evidence",
    } <= names
