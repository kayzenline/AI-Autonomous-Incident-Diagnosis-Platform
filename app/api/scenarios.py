"""Public scenario catalog.

The response is id, title, service, and tags. ground_truth stays in the file
the scorer reads.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import require_api_key
from app.eval.scenario import load_frozen_scenarios

router = APIRouter(prefix="/v1", tags=["scenarios"])


class ScenarioPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    service: str
    tags: list[str] = Field(default_factory=list)


@router.get("/scenarios", response_model=list[ScenarioPublic])
def list_scenarios(_: None = Depends(require_api_key)) -> list[ScenarioPublic]:
    return [
        ScenarioPublic(
            id=scenario.id,
            title=scenario.title,
            service=scenario.service,
            tags=list(scenario.tags),
        )
        for scenario in load_frozen_scenarios()
    ]
