from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.scenarios import router as scenarios_router

app = FastAPI(
    title="AgentOps",
    description="Read-only incident diagnosis API.",
    version="0.1.0",
)
register_exception_handlers(app)
app.include_router(scenarios_router)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "AgentOps API is running"}


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}
