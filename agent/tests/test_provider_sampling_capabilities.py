import asyncio
import json

import httpx
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.provider import DeepSeekProvider


class SamplingProbe(BaseModel):
    answer: str


def test_disabled_top_k_is_retained_but_never_sent_or_retried(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "top_k" in payload:
            return httpx.Response(400, json={"error": {"message": "top_k is unsupported"}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}],
                                         "usage": {"prompt_tokens": 30, "completion_tokens": 5}})

    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(quality_preset="economy", agent_generation={"writer": {"temperature": .8, "top_p": .9, "top_k": 17}})
    result = asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="离线检查", user_prompt="检查参数",
        output_model=SamplingProbe, agent_role="writer", thinking=False))
    assert result.data.answer == "ok" and len(requests) == 1
    assert "top_k" not in requests[0] and requests[0]["temperature"] == .8 and requests[0]["top_p"] == 1.0
    assert settings.generation_for("writer")["top_k"] == 17
