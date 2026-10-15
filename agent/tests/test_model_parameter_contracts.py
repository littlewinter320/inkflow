import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from inkflow.config import Settings, save_user_settings
from inkflow.errors import ConfigurationError
from inkflow.provider import create_provider


class ParameterProbe(BaseModel):
    answer: str


@pytest.mark.parametrize("kind,model,thinking,expected,absent", [
    ("openai", "gpt-5", True, {"reasoning_effort": "high"}, {"temperature", "top_p", "top_k", "max_tokens"}),
    ("openai", "gpt-5", False, {"reasoning_effort": "minimal"}, {"temperature", "top_p", "top_k", "max_tokens"}),
    ("openai", "gpt-4.1", True, {"temperature": .8, "top_p": .9}, {"reasoning_effort", "top_k", "max_completion_tokens"}),
    ("openai", "gpt-5.2", True, {"reasoning_effort": "high"}, {"temperature", "top_p", "max_tokens"}),
    ("openai", "gpt-5.2", False, {"reasoning_effort": "none", "temperature": .8, "top_p": .9}, {"max_tokens"}),
    ("anthropic", "claude-sonnet-4-5", True, {"temperature": .8, "top_p": .9, "top_k": 17}, {"reasoning_effort"}),
    ("anthropic", "claude-sonnet-5", True, {}, {"temperature", "top_p", "top_k", "reasoning_effort"}),
    ("anthropic", "claude-opus-4-1", True, {"temperature": .8, "top_k": 17}, {"top_p", "reasoning_effort"}),
    ("gemini", "gemini-2.5-flash", False, {"reasoning_effort": "none", "temperature": .8}, {"top_k"}),
    ("gemini", "gemini-2.5-pro", False, {"reasoning_effort": "low"}, {"top_k"}),
    ("openrouter", "provider/example", True, {"reasoning_effort": "high", "top_k": 17}, set()),
    ("ollama", "gpt-oss:20b", True, {"reasoning_effort": "high"}, {"top_k"}),
])
def test_declared_parameter_contract_matches_wire(tmp_path, monkeypatch, kind, model, thinking, expected, absent):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": '{"answer":"ok"}'}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 30, "output_tokens": 5}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 5}})

    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(provider_kind=kind, model=model, base_url="https://offline.invalid/v1",
        agent_generation={"writer": {"temperature": .8, "top_p": .9, "top_k": 17}})
    adapter = create_provider(settings)
    result = asyncio.run(adapter.generate_json(system_prompt="离线检查", user_prompt="参数检查", output_model=ParameterProbe,
        agent_role="writer", effort="max", thinking=thinking, max_tokens=200))
    assert result.data.answer == "ok" and len(requests) == 1
    assert expected.items() <= requests[0].items()
    assert not absent.intersection(requests[0])
    assert adapter.capabilities()["reasoning_effort"] == ("reasoning_effort" in requests[0])
    if kind == "openai" and model.startswith("gpt-5"):
        assert requests[0]["max_completion_tokens"] == 200
    assert settings.generation_for("writer")["top_k"] == 17


def test_role_override_selects_its_own_capabilities():
    settings = Settings(provider_kind="openai", base_url="https://api.openai.com/v1", model="gpt-5", role_models={"writer": "gpt-4.1"})
    assert settings.parameter_capabilities()["sampling_parameters"]["temperature"] is False
    writer = settings.parameter_capabilities(settings.model_for("writer"))
    assert writer["sampling_parameters"]["temperature"] is True and writer["reasoning_effort"] is False


def test_anthropic_save_rejects_only_adopted_sampling_and_preserves_disk(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(path))
    save_user_settings({"provider_kind": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5"})
    original = path.read_bytes()
    with pytest.raises(ConfigurationError, match="创作温度须在 0～1"):
        save_user_settings({"agent_generation": {"writer": {"temperature": 1.5}}})
    assert path.read_bytes() == original
    saved = save_user_settings({"role_models": {"writer": "claude-sonnet-5"},
        "agent_generation": {"writer": {"temperature": 1.5, "top_k": 17}}})
    assert saved["agent_generation"]["writer"]["temperature"] == 1.5
    assert saved["agent_generation"]["writer"]["top_k"] == 17


def test_deepseek_effort_metadata_explains_accepted_medium_alias():
    capabilities = Settings().parameter_capabilities()
    assert capabilities["reasoning_effort_map"] == {"medium": "high"}
    assert capabilities["sampling_constraints"]["thinking_temperature_ignored"] is True
