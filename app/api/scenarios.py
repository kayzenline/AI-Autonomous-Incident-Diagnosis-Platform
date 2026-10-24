"""Public scenario catalog.

The response is id, title, service, and tags. ground_truth stays in the file
the scorer reads.
"""

import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.eval.scenario import load_frozen_scenarios

router = APIRouter(prefix="/v1", tags=["scenarios"])


class ScenarioPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    service: str
    tags: list[str] = Field(default_factory=list)


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("API_KEY", "")
    provided = x_api_key or ""
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


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
