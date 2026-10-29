import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.incidents import router as incidents_router
from app.api.runs import router as runs_router
from app.api.scenarios import router as scenarios_router
from app.core.telemetry import configure_telemetry, flush_telemetry
from app.persistence.session import dispose_engine, ensure_ready
from app.persistence.store import fail_runs_after_restart


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_telemetry()
    await ensure_ready()
    await fail_runs_after_restart()
    try:
        yield
    finally:
        await dispose_engine()
        await asyncio.to_thread(flush_telemetry)


app = FastAPI(
    title="AgentOps",
    description="Read-only incident diagnosis API.",
    version="0.1.0",
    lifespan=lifespan,
)
register_exception_handlers(app)
app.include_router(scenarios_router)
app.include_router(incidents_router)
app.include_router(runs_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "AgentOps API is running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}
