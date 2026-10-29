"""Async engine shared by the API and the in-process run executor.

SQLite is the zero-dependency default so the CLI can run without Docker.
Postgres is selected with DATABASE_URL and created by Alembic.
"""

import asyncio
import os

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.persistence.models import Base

DEFAULT_URL = "sqlite+aiosqlite:///./.agentops.sqlite"

_engine: AsyncEngine | None = None
_engine_url: str | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_ready: set[int] = set()
_lock: asyncio.Lock | None = None


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def _build(url: str) -> AsyncEngine:
    engine = create_async_engine(url, pool_pre_ping=not url.startswith("sqlite"))
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    global _engine, _engine_url, _sessionmaker
    url = database_url()
    if _engine is not None and _engine_url == url:
        return _engine
    _engine = _build(url)
    _engine_url = url
    _sessionmaker = None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    engine = get_engine()
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return _sessionmaker


async def ensure_ready() -> None:
    """Create SQLite tables on first use. Postgres comes from Alembic."""

    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        engine = get_engine()
        marker = id(engine)
        if marker in _ready:
            return
        if engine.url.get_backend_name() == "sqlite":
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        _ready.add(marker)


def reset_engine() -> None:
    """Drop the cached engine so tests can point at a new database."""

    global _engine, _engine_url, _sessionmaker, _lock
    engine = _engine
    _engine = None
    _engine_url = None
    _sessionmaker = None
    _ready.clear()
    _lock = None
    if engine is not None:
        try:
            asyncio.run(engine.dispose())
        except RuntimeError:
            return


async def dispose_engine() -> None:
    global _engine, _engine_url, _sessionmaker, _lock
    engine = _engine
    _engine = None
    _engine_url = None
    _sessionmaker = None
    _ready.clear()
    _lock = None
    if engine is not None:
        await engine.dispose()
