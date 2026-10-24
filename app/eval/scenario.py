"""Frozen scenario files.

The scorer is the only reader of ground_truth. Tool code receives the nested
world, which rejects that field.
"""

import re
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.taxonomy import CAUSE_CATEGORY, Category, Cause, Verdict
from app.schemas.incident import IncidentCreate
from app.tools.simulator.models import World

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KEY = re.compile(r"^[a-z]+:[a-z0-9_]+$")
Severity = Literal["low", "medium", "high"]


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    category: Category | None = None
    cause: Cause | None = None
    acceptable_alternatives: list[Cause] = Field(default_factory=list)
    key_evidence_keys: list[str] = Field(default_factory=list)
    severity: Severity | None = None

    @model_validator(mode="after")
    def shape(self) -> Self:
        for key in self.key_evidence_keys:
            if _KEY.fullmatch(key) is None:
                raise ValueError(f"invalid evidence key: {key}")
        if self.verdict != Verdict.FAULT:
            if (
                self.category is not None
                or self.cause is not None
                or self.severity is not None
                or self.acceptable_alternatives
            ):
                raise ValueError("non-fault ground truth leaves cause fields empty")
            return self
        if self.category is None or self.cause is None or self.severity is None:
            raise ValueError("fault ground truth needs category, cause, and severity")
        if not self.key_evidence_keys:
            raise ValueError("fault ground truth needs evidence keys")
        if CAUSE_CATEGORY[self.cause] != self.category:
            raise ValueError("cause does not pair with category")
        for alternative in self.acceptable_alternatives:
            if CAUSE_CATEGORY[alternative] != self.category:
                raise ValueError("alternative cause must share the expected category")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1)
    service: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    holdout: bool = False
    incident: IncidentCreate
    world: World
    ground_truth: GroundTruth

    @model_validator(mode="after")
    def frozen_case_is_complete(self) -> Self:
        if _ID.fullmatch(self.id) is None:
            raise ValueError(f"invalid scenario id: {self.id}")
        if self.incident.service != self.service:
            raise ValueError("incident.service must match the scenario service")
        if self.service not in self.world.services:
            raise ValueError("scenario service is missing from the world")
        if self.incident.occurred_at is None or self.incident.detected_at is None:
            raise ValueError("frozen incidents require occurred_at and detected_at")
        return self


def frozen_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "scenarios" / "frozen"


def load_scenario(path: Path) -> Scenario:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must be a mapping")
    incident = loaded.get("incident")
    if isinstance(incident, dict):
        unknown = set(incident) - set(IncidentCreate.model_fields)
        if unknown:
            raise ValueError(f"{path.name} has unknown incident fields: {sorted(unknown)}")
    scenario = Scenario.model_validate(loaded)
    if scenario.id != path.stem:
        raise ValueError(f"{path.name} id is {scenario.id}")
    return scenario


def load_frozen_scenarios(directory: Path | None = None) -> list[Scenario]:
    root = directory or frozen_dir()
    paths = sorted(root.glob("*.yaml"))
    scenarios = [load_scenario(path) for path in paths]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario id")
    return scenarios
