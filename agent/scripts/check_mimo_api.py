"""MiMo contract checks: all HTTP is mocked; settings and usage stay in a D-drive fixture."""
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from pydantic import BaseModel

from inkflow.config import Settings, api_key_status
from inkflow.errors import ConfigurationError, ProviderError
from inkflow.model_usage import normalized_usage
from inkflow.provider import create_provider
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.utils import estimate_tokens


class Output(BaseModel):
    answer: str


async def check_http(settings, ledger):
    seen = []
    replies = []

    async def respond(request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content) if request.content else None))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "mimo-v2.6-flash"}, {"id": "mimo-v2.6-pro"}]})
        return replies.pop(0) if replies else httpx.Response(200, json={"model": "mimo-v2.6-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}', "reasoning_content": "offline reasoning"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 60}, "completion_tokens_details": {"reasoning_tokens": 3}}})

    client = httpx.AsyncClient
    with patch("inkflow.provider.httpx.AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw)), \
         patch.object(Settings, "get_api_key", return_value="sk-offline-placeholder"):
        provider = create_provider(settings)
        assert provider.capabilities()["thinking"] and not provider.capabilities()["reasoning_effort"]
        assert not provider.capabilities()["top_k"] and not provider.capabilities()["balance"]
        assert await provider.list_models() == ["mimo-v2.6-flash", "mimo-v2.6-pro"]
        assert seen[-1][0] == "https://api.xiaomimimo.com/v1/models"
        result = await provider.generate_json(system_prompt="规则", user_prompt="本章任务", output_model=Output,
                                             agent_role="writer", max_tokens=131_072)
        url, headers, body = seen[-1]
        assert url.endswith("/v1/chat/completions") and headers["authorization"] == "Bearer sk-offline-placeholder"
        assert body["max_completion_tokens"] == 131_072 and body["thinking"] == {"type": "enabled"}
        assert body["temperature"] == 1.0 and body["top_p"] == 0.95
        assert not {"max_tokens", "reasoning_effort", "top_k", "prompt_cache_key"}.intersection(body)
        assert body["response_format"] == {"type": "json_object"} and result.reasoning_content == "offline reasoning"
        assert normalized_usage(result.usage)["cache_hit_tokens"] == 60
        await provider.generate_json(system_prompt="检查", user_prompt="短回复", output_model=Output, max_tokens=160,
                                     thinking=False, agent_role="writer")
        body = seen[-1][2]
        assert body["max_completion_tokens"] == 160 and body["thinking"] == {"type": "disabled"}
        assert body["temperature"] == settings.generation_for("writer")["temperature"]

        before = len(seen)
        replies.extend([httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 60}}})])
        await provider.generate_json(system_prompt="规则", user_prompt="原任务", output_model=Output, agent_role="writer", max_tokens=500)
        assert len(seen) == before + 2
        assert seen[-1][2]["thinking"] == {"type": "disabled"}
        assert seen[-1][2]["max_completion_tokens"] <= seen[-2][2]["max_completion_tokens"] == 500
        assert seen[-1][2]["response_format"] == {"type": "json_object"}
        assert "原任务" in seen[-1][2]["messages"][1]["content"]

        before = len(seen)
        replies.append(httpx.Response(400, json={"error": {"message": "unknown model"}}))
        try:
            await provider.generate_json(system_prompt="规则", user_prompt="任务", output_model=Output)
        except ProviderError:
            pass
        else:
            raise AssertionError("MiMo invalid requests must stop without JSON-mode downgrade")
        assert len(seen) == before + 1

        for updates in ({"agent_generation": {"writer": {"temperature": 1.6}}},
                        {"agent_generation": {"writer": {"top_p": 0.005}}},
                        {"context_soft_tokens": 50, "context_hard_tokens": 50, "max_output_tokens": 10}):
            invalid = Settings.from_mapping({**settings.to_mapping(), **updates})
            before = len(seen)
            try:
                await create_provider(invalid).generate_json(system_prompt="规则", user_prompt="中" * 500,
                    output_model=Output, agent_role="writer", thinking=False)
            except ConfigurationError:
                pass
            else:
                raise AssertionError("Invalid sampling or exhausted complete input must stop before HTTP")
            assert len(seen) == before

        bounded = Settings.from_mapping({**settings.to_mapping(), "context_soft_tokens": 400, "context_hard_tokens": 400,
            "max_output_tokens": 300})
        await create_provider(bounded).generate_json(system_prompt="规则", user_prompt="任务", output_model=Output, max_tokens=300)
        body = seen[-1][2]
        assert body["max_completion_tokens"] == min(300, 400 - estimate_tokens(json.dumps(body["messages"], ensure_ascii=False)))

        runtime = RunRuntime(publish=lambda event: None, max_calls=1)
        token = active_runtime.set(runtime)
        before = len(seen)
        replies.append(httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}}))
        try:
            try:
                await provider.generate_json(system_prompt="规则", user_prompt="任务", output_model=Output)
            except ProviderError:
                pass
            else:
                raise AssertionError("Format recovery must obey the original call budget")
        finally:
            active_runtime.reset(token)
        assert len(seen) == before + 1 and runtime.calls == 1

    db = sqlite3.connect(ledger)
    try:
        assert db.execute("SELECT COUNT(*) FROM model_attempts WHERE request_sent=1").fetchone()[0] == len(seen) - 1
        assert db.execute("SELECT SUM(cache_hit_tokens) FROM model_attempts").fetchone()[0] == 300
    finally:
        db.close()
    print(f"MiMo checks passed: {len(seen)} mocked HTTP exchanges; original budgets, fields, cache and stop conditions verified.")


def check():
    settings = Settings.from_mapping({"provider_kind": "mimo", "max_output_tokens": 131_072,
        "agent_generation": {"writer": {"top_k": 50}}})
    assert settings.base_url == "https://api.xiaomimimo.com/v1" and settings.model == "mimo-v2.6-flash"
    assert settings.model_limits()["context_tokens"] == 1_000_000
    for host, model in (("api.xiaomimimo.com", "mimo-v2.6-pro"), ("token-plan-cn.xiaomimimo.com", "mimo-v2.5")):
        known = Settings.from_mapping({**settings.to_mapping(), "base_url": f"https://{host}/v1", "model": model})
        assert known.model_limits()["max_output_tokens"] == 131_072
    for updates in ({"provider_kind": "custom", "base_url": "https://api.xiaomimimo.com.evil.test/v1"},
                    {"base_url": "https://proxy.example/v1"}, {"model": "mimo-future-unknown"}):
        assert Settings.from_mapping({**settings.to_mapping(), **updates}).model_limits() == {}
    try:
        Settings.from_mapping({**settings.to_mapping(), "max_output_tokens": 131_073})
    except ConfigurationError:
        pass
    else:
        raise AssertionError("Known output limit must be enforced")
    assert normalized_usage({"prompt_tokens": 100, "completion_tokens": 10})["cache_hit_tokens"] is None
    with patch.dict(os.environ, {"MIMO_API_KEY": "sk-offline-placeholder"}, clear=True):
        assert settings.get_api_key() == "sk-offline-placeholder"
        assert api_key_status("mimo")["api_key_storage"] == "environment:MIMO_API_KEY"
    for host, key in (("api.xiaomimimo.com", "tp-offline-placeholder"),
                      ("api.xiaomimimo.com", "ttp-offline-placeholder"),
                      ("token-plan-cn.xiaomimimo.com", "sk-offline-placeholder")):
        selected = Settings.from_mapping({**settings.to_mapping(), "base_url": f"https://{host}/v1"})
        with patch.object(Settings, "get_api_key", return_value=key):
            try:
                selected.require_api_key()
            except ConfigurationError as error:
                assert key not in str(error)
            else:
                raise AssertionError("Mixed MiMo credentials must fail before HTTP")
    Path("D:/墨流/cache").mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="mimo-contract-", dir="D:/墨流/cache") as directory:
        ledger = Path(directory) / "usage.sqlite"
        with patch("inkflow.model_usage.usage_ledger_path", return_value=ledger), \
             patch.dict(os.environ, {"INKFLOW_SETTINGS_PATH": str(Path(directory) / "settings.json")}):
            asyncio.run(check_http(settings, ledger))


if __name__ == "__main__":
    check()
