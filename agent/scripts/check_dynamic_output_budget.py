"""Focused offline output/context checks; mocked HTTP and an isolated D-drive ledger."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.app_server import InkFlowAppService
from inkflow.errors import ConfigurationError
from inkflow.engine import InkFlowEngine
from inkflow.provider import AnthropicProvider, DeepSeekProvider
from inkflow.utils import estimate_tokens


class Output(BaseModel):
    answer: str


def check():
    async def invalid_import_preserves_credentials():
        service = InkFlowAppService()
        async def emit(event):
            pass
        with patch("inkflow.config.load_user_settings", return_value={}), patch("inkflow.app_server.save_api_key_to_keyring") as store:
            try:
                await service.dispatch("provider.configure", {"provider_kind": "deepseek", "max_output_tokens": -1,
                    "api_key": "offline-not-a-real-key"}, emit)
            except ConfigurationError:
                pass
            else:
                raise AssertionError("Invalid settings must fail before saving credentials")
            store.assert_not_called()
            try:
                await service.dispatch("provider.configure", {"api_key": 123}, emit)
            except ConfigurationError:
                pass
            else:
                raise AssertionError("Non-text imported credentials must be rejected")
            store.assert_not_called()
    asyncio.run(invalid_import_preserves_credentials())
    settings = Settings.from_mapping({"max_output_tokens": 393_216})
    assert settings.effective_output_tokens(32_000) == 32_000
    assert settings.effective_output_tokens(500_000) == 393_216
    try:
        Settings.from_mapping({"max_output_tokens": 393_217})
    except ConfigurationError:
        pass
    else:
        raise AssertionError("Known model output limit must remain enforced")
    custom = Settings.from_mapping({"provider_kind": "custom", "base_url": "https://proxy.example/v1",
        "model": "unknown-large", "context_soft_tokens": 500_000, "context_hard_tokens": 1_800_000,
        "max_output_tokens": 500_000})
    assert custom.model_limits() == {}
    assert custom.effective_output_tokens(500_000, input_tokens=1_400_000) == 400_000
    roles = Settings.from_mapping({**custom.to_mapping(), "base_url": "https://api.deepseek.com",
        "role_models": {"writer": "deepseek-v4-pro"}, "context_budget_mode": "custom",
        "agent_context_budgets": {"writer": {"soft": 1_200_000, "hard": 1_400_000}}})
    assert roles.context_budget_for("writer") == (1_000_000, 1_000_000)
    assert roles.effective_output_tokens(500_000, "writer") == 393_216
    assert roles.effective_output_tokens(500_000, "writer", model_override="unknown-large") == 500_000
    frozen = Settings.from_mapping({"max_output_tokens": 16_000})
    assert frozen.effective_output_tokens(32_000) == 16_000
    large_output = Settings.from_mapping({"max_output_tokens": 200_000, "context_budget_mode": "custom",
        "agent_context_budgets": {role: {"soft": 80_000, "hard": 96_000} for role in ("writer", "editor")}})
    project = SimpleNamespace(db=SimpleNamespace(get_brief=lambda: SimpleNamespace(target_chapter_words=5000)))
    engine = InkFlowEngine(DeepSeekProvider(large_output), large_output)
    writer_context = engine._context_builder(project, "writer")
    editor_context = engine._context_builder(project, "editor")
    assert writer_context.output_reserve_tokens == 12_000
    assert editor_context.output_reserve_tokens == 8_000
    assert writer_context.configured_hard_token_limit - writer_context.output_reserve_tokens == 84_000
    assert large_output.max_output_tokens == 200_000

    async def provider_checks():
        seen = []

        async def emit(event):
            pass

        with patch.object(Settings, "from_env", return_value=custom), patch("inkflow.app_server.api_key_status", return_value={}):
            preview = await InkFlowAppService().dispatch("provider.status", {"base_url": "https://api.deepseek.com",
                "model": "deepseek-v4-flash"}, emit)
            assert preview["model_limits"]["max_output_tokens"] == 393_216
            assert preview["max_output_tokens"] == 500_000
            assert preview["effective_output_tokens"] == 393_216
            assert custom.model == "unknown-large" and custom.max_output_tokens == 500_000

        async def respond(request):
            body = json.loads(request.content)
            seen.append(body)
            if "system" in body:
                return httpx.Response(200, json={"content": [{"type": "text", "text": '{"answer":"ok"}'}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        client = httpx.AsyncClient
        with patch("inkflow.provider.httpx.AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw)), \
             patch.object(Settings, "require_api_key", return_value="offline"):
            for kind, provider in (("deepseek", DeepSeekProvider), ("anthropic", AnthropicProvider)):
                selected = Settings.from_mapping({"provider_kind": kind,
                    "base_url": "https://api.deepseek.com" if kind == "deepseek" else "https://api.anthropic.com",
                    "model": "deepseek-v4-flash" if kind == "deepseek" else "unknown-claude",
                    "max_output_tokens": 200_000, "context_budget_mode": "custom",
                    "agent_context_budgets": {"writer": {"soft": 80_000, "hard": 96_000}}})
                await provider(selected).generate_json(system_prompt="规则", user_prompt="本章任务",
                    output_model=Output, max_tokens=200_000, agent_role="writer")
                body = seen[-1]
                full_input = json.dumps({"system": body["system"], "messages": body["messages"]}, ensure_ascii=False) \
                    if "system" in body else json.dumps(body["messages"], ensure_ascii=False)
                assert body["max_tokens"] == 96_000 - estimate_tokens(full_input)
                assert body["max_tokens"] > 16_000
                overloaded = Settings.from_mapping({**selected.to_mapping(), "context_budget_mode": "unified",
                    "context_soft_tokens": 100, "context_hard_tokens": 100, "max_output_tokens": 50})
                before = len(seen)
                try:
                    await provider(overloaded).generate_json(system_prompt="规则", user_prompt="中" * 500,
                        output_model=Output, max_tokens=50, agent_role="writer")
                except ConfigurationError:
                    pass
                else:
                    raise AssertionError("Exhausted full input must stop before HTTP")
                assert len(seen) == before

    with TemporaryDirectory(prefix="dynamic-output-", dir="D:/墨流/cache") as directory, \
         patch("inkflow.model_usage.usage_ledger_path", return_value=Path(directory) / "usage.sqlite"):
        asyncio.run(provider_checks())
    print("Dynamic output checks passed: settings, verified limits, roles/override, full input, no over-budget HTTP.")


if __name__ == "__main__":
    check()
