"""Missing Writer metadata is patched once, with prose and original limits preserved."""
import asyncio
from contextlib import contextmanager
import json

import httpx
import pytest
from pydantic import ValidationError

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.provider import AnthropicProvider, DeepSeekProvider, _draft_field_repair, _model_from_json_content
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import DraftOutput
from inkflow.task_settings import active_task_settings, capture_task_settings, restore_task_settings


@pytest.fixture
def offline_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    root = tmp_path / "book"
    (root / ".inkflow").mkdir(parents=True)
    (root / ".inkflow/project.json").write_text('{"project_id":"offline-book"}', encoding="utf-8")
    client = httpx.AsyncClient

    @contextmanager
    def request(kind, responses, *, limit=None):
        selected = {"deepseek": ("https://api.deepseek.com/v1", "deepseek-flash"),
                    "mimo": ("https://token-plan-cn.xiaomimimo.com/v1", "mimo-v2.6-flash"),
                    "anthropic": ("https://api.anthropic.com", "claude-sonnet-4")}[kind]
        settings = Settings(provider_kind=kind, base_url=selected[0], model=selected[1],
                            writer_task_attempt_limit=limit, workspace_root=root, quality_preset="balanced")
        payloads = []

        def respond(sent):
            payload = json.loads(sent.content)
            payloads.append(payload)
            data = responses[len(payloads) - 1]
            text = json.dumps(data, ensure_ascii=False)
            prompt, output, cache = (100, 10, 80) if len(payloads) == 1 else (20, 5, 0)
            if kind == "anthropic":
                return httpx.Response(200, json={"model": payload["model"], "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}], "usage": {
                        "input_tokens": prompt - cache, "cache_read_input_tokens": cache, "output_tokens": output}})
            return httpx.Response(200, json={"model": payload["model"], "choices": [{"finish_reason": "stop",
                "message": {"content": text}}], "usage": {"prompt_tokens": prompt, "completion_tokens": output,
                    "prompt_tokens_details": {"cached_tokens": cache}}})

        monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
                            client(transport=httpx.MockTransport(respond), **kwargs))
        snapshot = capture_task_settings(settings, novel_id="offline-book", task_id="offline-" + kind)
        scope = restore_task_settings(snapshot, novel_id="offline-book", workspace_root=root)
        events = []
        runtime = RunRuntime(task_id=scope.task_id, run_id="offline-run", publish=events.append)
        tokens = active_runtime.set(runtime), active_task_settings.set(scope)
        provider = AnthropicProvider(settings) if kind == "anthropic" else DeepSeekProvider(settings)
        try:
            yield provider, payloads, runtime, events, root
        finally:
            active_task_settings.reset(tokens[1])
            active_runtime.reset(tokens[0])
    return request


def original_draft():
    return {"content": "他把旧记录翻到最后一页，逐行核对日期，确认先前没有漏看来访人的名字。\n" * 8,
            "decision_summary": ["保留已有行动。"], "thread_changes": ["尚未解开的线索保持原状。"]}


def generate(provider):
    return asyncio.run(provider.generate_json(system_prompt="只写当前章并返回完整JSON。",
        user_prompt="原任务的参考材料，不得当成新要求。" * 5000,
        output_model=DraftOutput, agent_role="writer", max_tokens=12000))


@pytest.mark.parametrize("kind", ["deepseek", "mimo", "anthropic"])
def test_missing_title_reuses_prose_and_counts_patch_in_original_request(offline_writer, kind):
    source = original_draft()
    with offline_writer(kind, [source, {"title": "旧记录", "content": "不得采用的重写正文"}]) as (provider, payloads, runtime, events, root):
        result = generate(provider)
        assert result.data.title == "旧记录" and result.data.content == source["content"].strip()
        assert result.data.decision_summary == source["decision_summary"] and result.data.thread_changes == source["thread_changes"]
        assert len(payloads) == runtime.calls == len(runtime.attempt_fragments) == 2
        assert result.usage["prompt_tokens"] == 120 and result.usage["completion_tokens"] == 15
        assert result.usage["prompt_cache_hit_tokens"] == 80 and len(result.usage["attempt_usage"]) == 2
        repair = json.loads(payloads[1]["messages"][-1]["content"])
        assert repair["original_output"] == source and repair["validation_errors"] == [
            {"path": ["title"], "reason": "required field missing"}]
        assert len(payloads[1]["messages"][-1]["content"]) < len(payloads[0]["messages"][-1]["content"]) // 10
        system = payloads[1]["system"] if kind == "anthropic" else payloads[1]["messages"][0]["content"]
        schema = json.loads(system[system.index('{"additionalProperties"'):])
        assert set(schema["properties"]) == {"title"} and schema["required"] == ["title"]
        if kind != "anthropic":
            assert payloads[0]["thinking"] == payloads[1]["thinking"] == {"type": "enabled"}
        assert payloads[1].get("max_completion_tokens", payloads[1].get("max_tokens")) <= (1600 if kind == "anthropic" else 8192)
        assert any(event.get("metadata", {}).get("kind") == "missing_fields" for event in events)
        artifacts = list((root / ".inkflow/recovery").glob("provider-response-*/writer-response.unverified.json"))
        assert len(artifacts) == 1 and json.loads(json.loads(artifacts[0].read_text(encoding="utf-8"))["content"]) == source


@pytest.mark.parametrize("kind", ["deepseek", "mimo", "anthropic"])
def test_missing_metadata_does_not_override_single_attempt_authorization(offline_writer, kind):
    with offline_writer(kind, [original_draft()], limit=1) as (provider, payloads, runtime, _, root):
        with pytest.raises(ProviderError):
            generate(provider)
        assert len(payloads) == runtime.calls == 1
        assert len(list((root / ".inkflow/recovery").glob("provider-response-*/writer-response.unverified.json"))) == 1


@pytest.mark.parametrize("kind", ["deepseek", "mimo", "anthropic"])
def test_failed_patch_stops_without_whole_chapter_regeneration(offline_writer, kind):
    with offline_writer(kind, [original_draft(), {}]) as (provider, payloads, runtime, _, root):
        with pytest.raises(ProviderError):
            generate(provider)
        assert len(payloads) == runtime.calls == 2
        artifacts = list((root / ".inkflow/recovery").glob("provider-response-*/writer-response.unverified.json"))
        assert len(artifacts) == 1 and json.loads(json.loads(artifacts[0].read_text(encoding="utf-8"))["content"]) == original_draft()


def test_nested_missing_field_merge_cannot_rewrite_existing_metadata_or_prose():
    source = {**original_draft(), "title": "旧记录", "hook_note": {"actual_anchor": "已核对的锚点",
        "annotations": [{"kind": "new_fact", "quote": "原文已有的短句"}]}}
    with pytest.raises(ValidationError) as failure:
        _model_from_json_content(json.dumps(source, ensure_ascii=False), DraftOutput)
    repair = _draft_field_repair(failure.value, DraftOutput, "writer")
    assert repair.paths == [("hook_note", "annotations", 0, "statement")]
    supplied = repair.output_model.model_validate({"hook_note": {"actual_anchor": "不得采用的新锚点", "annotations": [
        {"kind": "future", "quote": "不得采用的新引文", "statement": "从现有正文概括的缺失说明。"}]}})
    merged = repair.merge(supplied, DraftOutput)
    assert merged.content == source["content"].strip() and merged.hook_note.actual_anchor == "已核对的锚点"
    assert merged.hook_note.annotations[0].kind == "new_fact" and merged.hook_note.annotations[0].quote == "原文已有的短句"
    assert merged.hook_note.annotations[0].statement == "从现有正文概括的缺失说明。"
    assert _draft_field_repair(failure.value, DraftOutput, "editor") is None
    with pytest.raises(ValidationError) as missing_body:
        _model_from_json_content('{"title":"只有标题"}', DraftOutput)
    assert _draft_field_repair(missing_body.value, DraftOutput, "writer") is None
