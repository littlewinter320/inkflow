"""Frozen quality/range waiting applies without adding requests or leaking cancelled work."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json

import httpx
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.context import active_batch_context
from inkflow.model_usage import UsageLedger
from inkflow.provider import AnthropicProvider, DeepSeekProvider, _request_wait_policy
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.task_settings import active_task_settings, capture_task_settings, restore_task_settings


class BatchWaitProbe(BaseModel):
    answer: str


def test_quality_waits_use_frozen_range_and_cancel_unlimited_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    settings = Settings.from_mapping({"request_timeout_seconds": 180, "quality_preset": "economy"}, workspace_root=tmp_path)

    @contextmanager
    def bound(current=settings, count=4, task_id="offline-batch-wait", events=None):
        scope = restore_task_settings(capture_task_settings(current, novel_id="offline-book", task_id=task_id),
                                      novel_id="offline-book", workspace_root=tmp_path)
        runtime = RunRuntime(publish=(events.append if events is not None else lambda _: None), task_id=scope.task_id)
        manifest = {"batch_id": "batch-offline-wait", "start_chapter_no": 52, "end_chapter_no": 51 + count,
                    "task_settings": scope.public_summary()}
        binding = {"root": tmp_path.resolve(), "manifest": manifest}
        tokens = (active_runtime.set(runtime), active_task_settings.set(scope), active_batch_context.set(binding))
        try:
            yield runtime, binding
        finally:
            active_batch_context.reset(tokens[2])
            active_task_settings.reset(tokens[1])
            active_runtime.reset(tokens[0])

    for preset in ("economy", "balanced", "quality"):
        selected = replace(settings, quality_preset=preset, quality_custom_context=True)
        for count in (1, 3, 4):
            with bound(selected, count) as (_, binding):
                for role in ("coordinator", "writer", "editor", "reviewer", "memory_keeper"):
                    policy = _request_wait_policy(selected, role, 120)
                    scale = 2 if preset == "balanced" else 1
                    assert policy["batch_chapter_count"] == count
                    assert policy["request_timeout_seconds"] == (None if preset == "quality" else (360 + 120 * (count - 1)) * scale)
                    assert policy["soft_wait_seconds"] == (None if preset == "quality" else (180 + 60 * (count - 1)) * scale)
                binding["manifest"]["stopped_at_chapter"] = binding["manifest"]["end_chapter_no"]
                assert _request_wait_policy(selected, "writer", 120) == policy
                if preset != "quality":
                    assert _request_wait_policy(selected, "writer", 2000)["request_timeout_seconds"] == 2000
    with bound() as (runtime, binding):
        for field, value in (("task_id", "unrelated-task"), ("snapshot_hash", "wrong-hash"), ("novel_id", "another-book")):
            original = binding["manifest"]["task_settings"][field]
            binding["manifest"]["task_settings"][field] = value
            assert _request_wait_policy(settings, "writer", 120)["batch_chapter_count"] == 1
            binding["manifest"]["task_settings"][field] = original
        binding["root"] = tmp_path / "unrelated"
        assert _request_wait_policy(settings, "writer", 120)["batch_chapter_count"] == 1
        binding["root"] = tmp_path.resolve()
        runtime.task_id = "unrelated-runtime-task"
        assert _request_wait_policy(settings, "writer", 120)["batch_chapter_count"] == 1
        runtime.task_id = binding["manifest"]["task_settings"]["task_id"]
        assert _request_wait_policy(replace(settings, request_timeout_seconds=500.0), "writer", 120)["batch_chapter_count"] == 1
        binding["manifest"]["stopped_at_chapter"] = 99
        assert _request_wait_policy(settings, "writer", 120)["batch_chapter_count"] == 1
    independent = _request_wait_policy(settings, "writer", None)
    assert (independent["soft_wait_seconds"], independent["request_timeout_seconds"]) == (180, 360)
    legacy_custom = _request_wait_policy(replace(settings, quality_preset="custom"), "coordinator", 120)
    assert (legacy_custom["soft_wait_seconds"], legacy_custom["request_timeout_seconds"]) == (360, 720)
    assert _request_wait_policy(replace(settings, quality_preset="quality"), None, 15)["request_timeout_seconds"] == 15

    import inkflow.provider as provider_module
    original_client, original_wait, original_mimo = httpx.AsyncClient, asyncio.wait_for, provider_module._mimo_response_with_reminder
    client_limits, outer_limits, mimo_limits = [], [], []

    async def wait(awaitable, timeout):
        outer_limits.append(timeout)
        return await original_wait(awaitable, timeout=timeout)

    async def mimo(client, url, **kwargs):
        mimo_limits.append((kwargs["soft_wait_seconds"], kwargs["timeout"]))
        return await original_mimo(client, url, **kwargs)

    monkeypatch.setattr(provider_module.asyncio, "wait_for", wait)
    monkeypatch.setattr(provider_module, "_mimo_response_with_reminder", mimo)

    async def exercise(kind, preset, count, cancel=False):
        requests, events = [], []
        started, release = asyncio.Event(), asyncio.Event()
        closed = False
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 0}}

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await release.wait()
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                nonlocal closed
                closed = True

        def respond(request):
            requests.append(json.loads(request.content))
            if cancel:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=WaitingStream())
            if kind == "anthropic":
                return httpx.Response(200, json={"content": [{"type": "text", "text": '{"answer":"ok"}'}],
                    "usage": {"input_tokens": 20, "output_tokens": 10}})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}, "finish_reason": "stop"}], "usage": usage})

        def client(**kwargs):
            client_limits.append(kwargs["timeout"])
            return original_client(transport=httpx.MockTransport(respond), **kwargs)

        monkeypatch.setattr(provider_module.httpx, "AsyncClient", client)
        selected = replace(settings, provider_kind=kind, quality_preset=preset,
                           base_url="https://token-plan-cn.xiaomimimo.com/v1" if kind == "mimo" else settings.base_url,
                           model="mimo-v2.6-flash" if kind == "mimo" else "claude-sonnet-4" if kind == "anthropic" else settings.model)
        with bound(selected, count, f"offline-wait-{kind}-{preset}-{cancel}", events) as (runtime, _):
            provider = AnthropicProvider(selected) if kind == "anthropic" else DeepSeekProvider(selected)
            before_outer = len(outer_limits)
            baseline = set(asyncio.all_tasks())
            task = asyncio.create_task(provider.generate_json(system_prompt="离线档位等待", user_prompt="沿原范围执行",
                output_model=BatchWaitProbe, max_tokens=1000, timeout_seconds=120, agent_role="editor", thinking=False))
            if cancel:
                async with asyncio.timeout(.5):
                    await started.wait()
                task.cancel()
                try:
                    await task
                    raise AssertionError("取消必须向上传播")
                except asyncio.CancelledError:
                    pass
                assert closed and not (set(asyncio.all_tasks()) - baseline)
            else:
                assert (await task).data.answer == "ok"
            assert len(requests) == runtime.calls == len(runtime.attempt_fragments) == 1
            starts = [event for event in events if event.get("type") == "model.request.started"]
            assert len(starts) == 1 and starts[0]["role"] == "editor"
            expected = _request_wait_policy(selected, "editor", 120)
            assert starts[0]["metadata"]["request_timeout_seconds"] == expected["request_timeout_seconds"]
            if preset == "quality":
                assert len(outer_limits) == before_outer
                assert client_limits[-1].read is None
                assert all(getattr(client_limits[-1], field) == 120 for field in ("connect", "write", "pool"))
                assert runtime.attempt_fragments[0]["diagnostic"]["request_timeout_seconds"] is None
                assert not any(event.get("type") == "model.waiting" for event in events)
            else:
                assert client_limits[-1] == outer_limits[-1] == expected["request_timeout_seconds"]
            if kind == "mimo":
                assert mimo_limits[-1] == (expected["soft_wait_seconds"], expected["request_timeout_seconds"])
            with UsageLedger()._connect() as db:
                rows = db.execute("SELECT diagnostic_json FROM model_attempts WHERE task_id=?", (runtime.task_id,)).fetchall()
            assert len(rows) == 1
            stored = json.loads(rows[0]["diagnostic_json"])
            if preset == "quality":
                assert stored["request_timeout_seconds"] is None and stored["soft_wait_seconds"] is None
            else:
                assert float(stored["request_timeout_seconds"]) == expected["request_timeout_seconds"]

    async def run():
        for kind, preset, count, cancel in (("mimo", "economy", 4, False), ("deepseek", "balanced", 3, False),
                ("anthropic", "quality", 1, False), ("mimo", "quality", 4, True), ("deepseek", "quality", 3, True)):
            await exercise(kind, preset, count, cancel)

    asyncio.run(run())
