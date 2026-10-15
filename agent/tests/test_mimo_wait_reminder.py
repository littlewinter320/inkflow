"""The local reminder observes one sent request without creating another attempt."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.provider import DeepSeekProvider
from inkflow.runtime import RunRuntime, active_runtime


class ReminderProbe(BaseModel):
    answer: str


def test_mimo_soft_reminder_single_attempt_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    # Scale transport timing only; creation-profile formulas have their own check.
    monkeypatch.setattr("inkflow.provider._request_wait_policy", lambda *_: {
        "request_timeout_seconds": .1, "soft_wait_seconds": .01,
        "timeout_policy": "offline_scaled", "batch_chapter_count": 1})
    original_client = httpx.AsyncClient

    async def scenario(mode):
        events, requests = [], []
        slot_release, started, release, reminded = (asyncio.Event() for _ in range(4))
        if mode != "wait_for_slot":
            slot_release.set()
        if mode == "early":
            release.set()
        closed = False

        class WaitingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await release.wait()
                chunk = {"choices": [{"delta": {"content": '{"answer":"完整结果"}'}, "finish_reason": "stop"}]}
                yield ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()

            async def aclose(self):
                nonlocal closed
                closed = True

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=WaitingStream())

        @asynccontextmanager
        async def slot(_):
            await slot_release.wait()
            yield

        def publish(event):
            events.append(event)
            if event.get("type") == "model.waiting":
                reminded.set()

        monkeypatch.setattr("inkflow.provider._model_request_slot", slot)
        monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
                            original_client(transport=httpx.MockTransport(respond), **kwargs))
        runtime = RunRuntime(publish=publish, task_id=f"offline-reminder-{mode}")
        token = active_runtime.set(runtime)
        baseline = set(asyncio.all_tasks())
        try:
            settings = Settings(provider_kind="mimo", base_url="https://token-plan-cn.xiaomimimo.com/v1",
                                model="mimo-v2.6-flash", request_timeout_seconds=.1,
                                writer_task_attempt_limit=1, workspace_root=tmp_path)
            task = asyncio.create_task(DeepSeekProvider(settings).generate_json(
                system_prompt="离线提醒检查", user_prompt="保留原请求", output_model=ReminderProbe,
                agent_role="writer", thinking=True))
            if mode == "wait_for_slot":
                await asyncio.sleep(.025)
                assert not requests and not reminded.is_set()
                slot_release.set()
            await asyncio.wait_for(started.wait(), .2)
            if mode == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif mode == "timeout":
                with pytest.raises(ProviderError, match="1 次限次.*超时"):
                    await task
            elif mode == "early":
                assert (await task).data.answer == "完整结果"
            else:
                await asyncio.wait_for(reminded.wait(), .08)
                await asyncio.sleep(.02)
                release.set()
                assert (await task).data.answer == "完整结果"
            await asyncio.sleep(.025)
            notices = [event for event in events if event.get("type") == "model.waiting"]
            assert len(notices) == (0 if mode in {"early", "cancel"} else 1)
            if notices:
                assert notices[0]["role"] == "writer" and notices[0]["metadata"]["attempt"] == 1
                assert "写作请求" in notices[0]["summary"]
            assert len(requests) == 1 and requests[0]["thinking"] == {"type": "enabled"}
            assert runtime.calls == 1 and closed
            assert not (set(asyncio.all_tasks()) - baseline)
        finally:
            active_runtime.reset(token)

    async def run():
        for mode in ("wait_for_slot", "timeout", "early", "cancel"):
            await scenario(mode)

    asyncio.run(run())
