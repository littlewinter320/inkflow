"""A complete Writer response survives local schema failure without another HTTP request."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.provider import AnthropicProvider, DeepSeekProvider, _generation_schema, _preserve_model_response
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import DraftOutput
from inkflow.task_settings import active_task_settings, capture_task_settings, restore_task_settings
from inkflow.utils import content_hash, json_dumps


def test_writer_response_is_bound_unverified_and_not_retried_after_local_save_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-credential-sentinel")
    root = tmp_path / "book"
    (root / ".inkflow").mkdir(parents=True)
    manifest = root / ".inkflow/project.json"
    manifest.write_text(json.dumps({"project_id": "offline-response-book"}), encoding="utf-8")
    settings = Settings.from_mapping({"provider_kind": "mimo", "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "model": "mimo-v2.6-flash", "quality_preset": "economy", "writer_task_attempt_limit": 1}, workspace_root=root)
    raw = json.dumps({"content": "他拿出已经核对过的材料，逐行确认先前的记录，并没有把这一步当成新发现。\n" * 8,
                      "decision_summary": ["沿现有记录推进，不重复先前行动。"]}, ensure_ascii=False, indent=2)
    original_client = httpx.AsyncClient
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        usage = {"prompt_tokens": 200, "completion_tokens": 150, "prompt_tokens_details": {"cached_tokens": 0}}
        if "/messages" in str(request.url):
            return httpx.Response(200, json={"id": "offline-anthropic-response", "model": payload["model"], "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": "private-reasoning-sentinel"}, {"type": "text", "text": raw}],
                "usage": {"input_tokens": 200, "output_tokens": 150}})
        return httpx.Response(200, json={"id": "../unsafe-response-id", "model": payload["model"],
            "choices": [{"finish_reason": "stop", "message": {"content": raw, "reasoning_content": "private-reasoning-sentinel"}}],
            "usage": usage})

    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(respond), **kwargs))

    @contextmanager
    def bound(current, name):
        snapshot = capture_task_settings(current, novel_id="offline-response-book", task_id=name)
        scope = restore_task_settings(snapshot, novel_id="offline-response-book", workspace_root=root)
        events = []
        runtime = RunRuntime(publish=events.append, task_id=scope.task_id, run_id=f"run-{name}")
        tokens = active_runtime.set(runtime), active_task_settings.set(scope)
        try:
            yield scope, runtime, events
        finally:
            active_task_settings.reset(tokens[1])
            active_runtime.reset(tokens[0])

    def artifacts():
        return list((root / ".inkflow/recovery").glob("provider-response-*/writer-response.unverified.json"))

    for kind in ("mimo", "anthropic"):
        current = settings if kind == "mimo" else replace(settings, provider_kind="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4")
        with bound(current, f"offline-response-{kind}") as (scope, runtime, events):
            before = set(artifacts())
            provider = AnthropicProvider(current) if kind == "anthropic" else DeepSeekProvider(current)
            with pytest.raises(ProviderError):
                asyncio.run(provider.generate_json(system_prompt="返回完整正文JSON。", user_prompt="只写本章。",
                    output_model=DraftOutput, agent_role="writer", thinking=False))
            added = set(artifacts()) - before
            assert len(added) == runtime.calls == len(runtime.attempt_fragments) == 1
            path = added.pop()
            text = path.read_text(encoding="utf-8")
            saved = json.loads(text)
            assert saved["content"] == raw and saved["content_hash"] == content_hash(raw)
            assert saved["status"] == "unverified" and saved["authority"] == "task_temporary_not_canon"
            assert saved["task_settings"] == scope.public_summary() and saved["run_id"] == runtime.run_id
            assert saved["attempt_id"] == runtime.attempt_fragments[0]["attempt_id"] and saved["attempt"] == 1
            assert saved["schema"] == "DraftOutput" and saved["schema_hash"] == content_hash(json_dumps(_generation_schema(DraftOutput)))
            payload = requests[-1]
            actual_input = {"system": payload["system"], "messages": payload["messages"]} if kind == "anthropic" else payload["messages"]
            assert saved["input_hash"] == content_hash(json.dumps(actual_input, ensure_ascii=False))
            assert "private-reasoning-sentinel" not in text and "offline-credential-sentinel" not in text
            assert "reasoning_content" not in saved and "headers" not in saved
            assert any(event["type"] == "model.response_preserved" for event in events)
            assert path.resolve().is_relative_to(root.resolve()) and "unsafe-response-id" not in str(path)
    assert len(requests) == 2

    accounting = SimpleNamespace(fields={"task_id": "offline-no-artifact", "request_id": "offline-request"}, attempt_id="offline-attempt")
    kwargs = dict(role="writer", output_model=DraftOutput, content=raw, model=settings.model, response_id=None,
                  finish_reason="stop", serialized_input="[]", attempt=1)
    before = set(artifacts())
    assert _preserve_model_response(settings, accounting, **kwargs) is None
    with bound(settings, "offline-no-artifact") as (scope, _, _):
        assert _preserve_model_response(replace(settings, workspace_root=tmp_path / "wrong-project"), accounting, **kwargs) is None
        manifest.write_text(json.dumps({"project_id": "another-project"}), encoding="utf-8")
        assert _preserve_model_response(settings, accounting, **kwargs) is None
        manifest.write_text("{}", encoding="utf-8")
        assert _preserve_model_response(settings, accounting, **kwargs) is None
        manifest.write_text(json.dumps({"project_id": scope.novel_id}), encoding="utf-8")
        assert _preserve_model_response(settings, accounting, **{**kwargs, "finish_reason": "length"}) is None
    assert set(artifacts()) == before

    def failed_save(*_args, **_kwargs):
        raise OSError("offline storage unavailable")

    monkeypatch.setattr("inkflow.provider.atomic_write_text", failed_save)
    # Without a task cap the economy loop still has two attempts; local save failure must stop it after one.
    uncapped = replace(settings, writer_task_attempt_limit=None)
    before_requests = len(requests)
    with bound(uncapped, "offline-save-failure") as (_, runtime, _):
        with pytest.raises(ProviderError, match="本地未验证产物保存失败") as failed:
            asyncio.run(DeepSeekProvider(uncapped).generate_json(system_prompt="返回完整正文JSON。", user_prompt="只写本章。",
                output_model=DraftOutput, agent_role="writer", thinking=False))
        assert failed.value.recovery_exhausted and failed.value.recovery_node == "provider.response_preservation"
        assert failed.value.unsaved_response["content"] == raw
        assert len(requests) == before_requests + 1 and runtime.calls == 1
    assert set(artifacts()) == before
