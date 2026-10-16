"""A formatting fallback must adopt the saved sampling values once reasoning is off."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.provider import DeepSeekProvider


class FormatAnswer(BaseModel):
    answer: str


def test_modern_openai_format_fallback_restores_only_supported_sampling(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    monkeypatch.setattr("inkflow.provider.asyncio.sleep", AsyncMock())
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "{}" if len(requests) == 1 else '{"answer":"ok"}'}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 5}})
    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(provider_kind="openai", base_url="https://offline.invalid/v1", model="gpt-5.2",
        agent_generation={"writer": {"temperature": .8, "top_p": .9, "top_k": 17}})
    before = settings.generation_for("writer")
    result = asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="检查结构", user_prompt="离线检查",
        output_model=FormatAnswer, thinking=True, agent_role="writer", max_tokens=100))
    assert result.data.answer == "ok" and len(requests) == 2
    assert requests[0]["reasoning_effort"] == "high"
    assert "temperature" not in requests[0] and "top_p" not in requests[0]
    assert requests[1]["reasoning_effort"] == "none"
    assert requests[1]["temperature"] == .8 and requests[1]["top_p"] == .9
    assert "top_k" not in requests[0] and "top_k" not in requests[1]
    assert settings.generation_for("writer") == before
