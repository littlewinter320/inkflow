"""Official V4 sampling constraints affect the wire, never the saved profile."""
import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.provider import DeepSeekProvider


class SamplingAnswer(BaseModel):
    answer: str


@pytest.mark.parametrize("thinking", [True, False])
def test_official_sampling_modes_use_selected_role_model_and_preserve_profile(tmp_path, monkeypatch, thinking):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 5}})
    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(quality_preset="balanced" if thinking else "economy",
        model="deepseek-flash", role_models={"writer": "deepseek-v4-pro"},
        agent_generation={"writer": {"temperature": .8, "top_p": .7, "top_k": None}})
    original = settings.generation_for("writer")
    response = asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="检查参数", user_prompt="离线用例",
        output_model=SamplingAnswer, agent_role="writer", thinking=thinking, max_tokens=100))
    assert response.data.answer == "ok" and len(requests) == 1
    assert requests[0]["model"] == "deepseek-v4-pro"
    assert requests[0]["top_p"] == (.95 if thinking else 1.0)
    assert ("temperature" not in requests[0]) if thinking else requests[0]["temperature"] == .8
    assert settings.generation_for("writer") == original
    for model in ("deepseek-chat", "deepseek-reasoner", "unknown-future-model"):
        assert "thinking_top_p_min" not in settings.parameter_capabilities(model)["sampling_constraints"]
    proxy = Settings(provider_kind="deepseek", base_url="https://proxy.invalid/v1", model="deepseek-flash")
    assert "thinking_top_p_min" not in proxy.parameter_capabilities()["sampling_constraints"]
