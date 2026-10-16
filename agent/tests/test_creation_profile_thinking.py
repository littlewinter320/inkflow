"""Thinking presets reach the wire and survive bounded format recovery."""
import asyncio
import json

import httpx
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.provider import DeepSeekProvider


class ProfileAnswer(BaseModel):
    answer: str


def test_thinking_profiles_and_retry_preserve_actual_role_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    sent, fail_next = [], [False]

    def respond(request):
        sent.append(json.loads(request.content))
        content = "{}" if fail_next[0] else '{"answer":"ok"}'
        fail_next[0] = False
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                      "prompt_tokens_details": {"cached_tokens": 80}}})

    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(respond), **kwargs))
    roles = ("coordinator", "writer", "editor", "reviewer", "memory_keeper")
    for kind, model, url in (("deepseek", "deepseek-flash", "https://api.deepseek.com/v1"),
                             ("mimo", "mimo-v2.6-flash", "https://token-plan-cn.xiaomimimo.com/v1")):
        for preset in ("economy", "balanced", "quality"):
            settings = Settings(provider_kind=kind, model=model, base_url=url, quality_preset=preset,
                                workspace_root=tmp_path, max_output_tokens=6000)
            for role in roles:
                wanted = not (preset == "economy" and role in {"coordinator", "writer"})
                before = len(sent)
                result = asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="只返回结构化结论",
                    user_prompt="本次职责不变", output_model=ProfileAnswer, max_tokens=1600,
                    agent_role=role, thinking=not wanted))
                assert result.data.answer == "ok" and len(sent) == before + 1
                payload = sent[-1]
                assert payload["thinking"]["type"] == ("enabled" if wanted else "disabled")
                token_field = "max_completion_tokens" if kind == "mimo" else "max_tokens"
                assert payload[token_field] == (6000 if wanted else 1600)
                if kind == "mimo" and wanted:
                    assert payload["temperature"] == 1 and payload["top_p"] == .95
                elif kind == "deepseek" and wanted:
                    assert "temperature" not in payload and payload["top_p"] >= .95
                assert result.usage["cache_prefix_diagnostic"]["thinking"] == payload["thinking"]["type"]
            fail_next[0] = True
            before = len(sent)
            result = asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="审查职责",
                user_prompt="只修结构，不更改职责", output_model=ProfileAnswer, agent_role="editor",
                max_tokens=1600, thinking=False))
            assert result.data.answer == "ok" and len(sent) == before + 2
            assert all(item["thinking"]["type"] == "enabled" for item in sent[-2:])
            assert "此轮不展开推理" not in sent[-1]["messages"][-1]["content"]
    unsupported = Settings(provider_kind="anthropic", model="claude-sonnet-4-6", base_url="https://api.anthropic.com/v1")
    assert unsupported.thinking_for("editor", False) is False
    assert unsupported.generation_output_budget("editor", 1600, True) == 1600


def test_native_thinking_uses_saved_40000_ceiling_and_clips_remaining_context(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    sent = []
    client = httpx.AsyncClient
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(respond), **kwargs))
    for kind, model, url in (("mimo", "mimo-v2.6-flash", "https://token-plan-cn.xiaomimimo.com/v1"),
                             ("deepseek", "deepseek-flash", "https://api.deepseek.com/v1")):
        settings = Settings(provider_kind=kind, model=model, base_url=url, quality_preset="balanced",
            workspace_root=tmp_path, max_output_tokens=40000)
        for role in ("writer", "coordinator", "editor", "reviewer", "memory_keeper"):
            asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="本次合同",
                user_prompt="仅本地验证", output_model=ProfileAnswer, agent_role=role, max_tokens=1600, thinking=False))
            field = "max_completion_tokens" if kind == "mimo" else "max_tokens"
            assert sent[-1][field] == 40000 and sent[-1]["thinking"] == {"type": "enabled"}
            assert settings.generation_output_budget(role, 40000, True) == 40000
        project = SimpleNamespace(db=SimpleNamespace(get_brief=lambda: SimpleNamespace(target_chapter_words=2900)))
        assert InkFlowEngine(None, settings)._context_builder(project, "writer").output_reserve_tokens == 40000
        assert replace(settings, quality_preset="economy").generation_output_budget("writer", 1600, False) == 1600
        limited = replace(settings, context_budget_mode="custom", agent_context_budgets={**settings.agent_context_budgets,
            "writer": {"soft": 41000, "hard": 41000}})
        asyncio.run(DeepSeekProvider(limited).generate_json(system_prompt="本次合同",
            user_prompt="本次资料" * 3000, output_model=ProfileAnswer, agent_role="writer", max_tokens=1600))
        assert 0 < sent[-1][field] < 40000
