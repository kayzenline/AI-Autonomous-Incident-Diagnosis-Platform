"""OpenAI adapter for LLMPort.

Reads credentials from environment variables (`OPENAI_API_KEY`, optional
`OPENAI_BASE_URL` and `OPENAI_MODEL`). Uses the synchronous OpenAI SDK inside
`asyncio.to_thread` so blocking network IO stays off the event loop.
"""

import asyncio
import os
from collections.abc import Callable
from typing import Any

from openai import OpenAI

from app.llm.port import LLMRequest, LLMResponse

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class OpenAIAdapter:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        sync_fn: Callable[[LLMRequest], LLMResponse] | None = None,
    ) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        self._api_key = api_key
        self._base_url = base_url
        self._client = client
        self._sync_fn = sync_fn

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return await asyncio.to_thread(self._complete_sync, request)

    def _complete_sync(self, request: LLMRequest) -> LLMResponse:
        if self._sync_fn is not None:
            return self._sync_fn(request)
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in request.messages],
            "temperature": request.temperature,
        }
        if request.response_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        completion = client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        content = choice.message.content or ""
        usage = getattr(completion, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        model_name = str(getattr(completion, "model", None) or self.model)
        return LLMResponse(
            content=content,
            model=model_name,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        key = self._api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set in the environment.")
        base_url = self._base_url or os.environ.get("OPENAI_BASE_URL")
        self._client = OpenAI(api_key=key, base_url=base_url)
        return self._client
