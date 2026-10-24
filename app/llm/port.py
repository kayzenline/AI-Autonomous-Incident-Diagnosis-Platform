"""LLMPort contract.

All calls are async. Synchronous provider SDKs must run in a worker thread so
they never block the single-process event loop.
"""

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[LLMMessage] = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    response_schema: dict[str, Any] | None = None


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    model: str
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)


class LLMPort(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate a completion without blocking the event loop."""
