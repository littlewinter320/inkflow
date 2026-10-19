"""Retry cache statistics use complete attempt-level numerators and denominators."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from pydantic import BaseModel

from inkflow.app_server import _usage_overview
from inkflow.config import Settings
from inkflow.database import ProjectDatabase
from inkflow.model_usage import UsageLedger, aggregate_usage
from inkflow.provider import DeepSeekProvider


class CacheAnswer(BaseModel):
    answer: str


def test_mimo_retry_cache_aggregation_and_unknown_history(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    monkeypatch.setattr("inkflow.provider.asyncio.sleep", AsyncMock())
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 3:
            raise httpx.ReadError("offline interrupted response", request=request)
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": "{}" if len(requests) == 1 else '{"answer":"ok"}'}}],
            "usage": {"prompt_tokens": 100 if len(requests) <= 2 else 20,
                      "completion_tokens": 2,
                      "prompt_tokens_details": {"cached_tokens": {1: 80, 2: 90, 4: 10}[len(requests)]}},
        })

    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
                        client(transport=httpx.MockTransport(respond), **kwargs))
    settings = Settings(provider_kind="mimo", model="mimo-v2.6-flash",
                        base_url="https://token-plan-cn.xiaomimimo.com/v1")

    def generate():
        return asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="离线核对", user_prompt="缓存统计",
                           output_model=CacheAnswer, agent_role="writer", thinking=False, max_tokens=100))

    result = generate()
    assert result.data.answer == "ok" and len(requests) == 2
    assert result.usage["prompt_tokens"] == 200
    assert result.usage["prompt_cache_hit_tokens"] == result.usage["prompt_tokens_details"]["cached_tokens"] == 170
    assert result.usage["prompt_cache_miss_tokens"] == 30
    assert [item["usage"]["prompt_tokens_details"]["cached_tokens"] for item in result.usage["attempt_usage"]] == [80, 90]
    unknown = generate()
    assert unknown.data.answer == "ok" and len(requests) == 4
    assert unknown.usage["prompt_tokens"] is None and unknown.usage["prompt_cache_hit_tokens"] is None
    assert unknown.usage["attempt_usage"][0]["usage"] == {}
    assert all(item["request_sent"] for item in unknown.usage["attempt_usage"])
    ledger = UsageLedger().summary()
    assert ledger["calls"] == 4 and ledger["unknown_usage_calls"] == 1 and ledger["input_tokens"] == 220

    # Existing history has a summed input but only the last nested hit count.
    legacy = copy.deepcopy(result.usage)
    legacy.pop("prompt_cache_hit_tokens")
    legacy.pop("prompt_cache_miss_tokens")
    legacy["prompt_tokens_details"]["cached_tokens"] = 90
    partial = {"prompt_tokens": 200, "prompt_tokens_details": {"cached_tokens": None}, "attempt_usage": [
        {"attempt": 1, "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                   "prompt_tokens_details": {"cached_tokens": 60}}},
        {"attempt": 2, "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                   "prompt_tokens_details": None}},
    ]}
    partial_before = copy.deepcopy(partial)
    combined = aggregate_usage(partial)
    assert combined["prompt_tokens"] == 200 and combined["prompt_cache_hit_tokens"] is None
    assert combined["prompt_tokens_details"]["cached_tokens"] is None and partial == partial_before
    legacy_transport = {"prompt_tokens": 50, "prompt_tokens_details": {"cached_tokens": 40},
        "attempt_usage": [{"attempt": 2, "usage": {"prompt_tokens": 50, "completion_tokens": 5,
                                                  "prompt_tokens_details": {"cached_tokens": 40}}}],
        "recovery_events": [{"attempt": 1, "kind": "transport", "usage_unknown": True}]}
    run = tmp_path / "runs" / "attempts"
    run.mkdir(parents=True)
    events = run / "events.jsonl"
    events.write_text("\n".join(json.dumps({"stage": "writer.model", "metadata": {
        "model": settings.model, "agent_role": "writer", "usage": usage}})
        for usage in (legacy, partial, legacy_transport, unknown.usage)), encoding="utf-8")
    original_events = events.read_bytes()
    project = SimpleNamespace(internal=tmp_path, db=ProjectDatabase(tmp_path / ".inkflow" / "inkflow.db"),
                              project_id="offline-attempts")
    overview = _usage_overview(project, settings)
    assert overview["calls"] == 8 and overview["prompt_tokens"] == 470
    assert overview["prompt_cache_hit_tokens"] == 280 and overview["prompt_cache_miss_tokens"] == 90
    assert overview["prompt_cache_hit_rate"] == round(280 / 370, 4)
    assert overview["cache_reported_calls"] == 5 and overview["cache_unknown_calls"] == 3
    assert overview["cache_unknown_prompt_tokens"] == 100 and overview["input_unknown_calls"] == 2
    assert overview["estimated_cost"] is None
    assert overview["by_agent_role"]["writer"]["prompt_cache_hit_rate"] == overview["prompt_cache_hit_rate"]
    assert events.read_bytes() == original_events
