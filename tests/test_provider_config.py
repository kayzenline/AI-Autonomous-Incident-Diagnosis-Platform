import asyncio
from types import SimpleNamespace

import pytest
from app.llm.adapters.openai import OpenAIAdapter
from app.llm.port import LLMMessage, LLMRequest


def test_deepseek_settings_reach_compatible_client(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_THINKING", "disabled")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"UNKNOWN"}'))],
            model="served-model-version",
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    adapter = OpenAIAdapter(model="deepseek-flash", client=client)
    result = asyncio.run(
        adapter.generate(
            LLMRequest(
                messages=[LLMMessage(role="user", content="Return json.")],
                response_schema={"type": "object"},
            )
        )
    )
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[0]["temperature"] == 0
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert calls[0]["model"] == "deepseek-flash"
    assert result.tokens_in == 20
    assert result.model == "served-model-version"
    assert adapter.observed_models == {"served-model-version"}


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [("https://api.deepseek.com", "https://api.deepseek.com"), ("", None)],
)
def test_compatible_endpoint_and_key_are_used(monkeypatch, base_url, expected) -> None:
    from app.llm.adapters import openai as module

    captured = {}
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.delenv("DEEPSEEK_THINKING", raising=False)

    def client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(module, "OpenAI", client)
    OpenAIAdapter()._get_client()
    assert captured == {"api_key": "test-placeholder", "base_url": expected}


def test_invalid_thinking_setting_fails_before_request(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_THINKING", "typo")
    with pytest.raises(ValueError, match="enabled or disabled"):
        OpenAIAdapter()
