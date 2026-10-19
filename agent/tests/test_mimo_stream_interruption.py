"""Offline MiMo SSE completion boundaries using actual asynchronous byte streams."""
import asyncio
import json

import httpx
import pytest

from inkflow.errors import ProviderError
from inkflow.provider import _mimo_stream_response
from inkflow.runtime import RunRuntime, active_runtime


CONTENT = '{"answer":"完整中文结果"}'
PRIVATE_REASONING = "不得进入恢复状态或进度诊断的思考片段"


def event(value):
    return "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


def choice(*, content=None, reasoning=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


START = event(choice(reasoning=PRIVATE_REASONING)) + event(choice(content=CONTENT))
STOP = event(choice(finish="stop"))


class InterruptedBytes(httpx.AsyncByteStream):
    def __init__(self, text, waiting=None):
        self.data = text.encode("utf-8")
        self.waiting = waiting
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.data), 3):
            yield self.data[offset:offset + 3]
        if self.waiting is not None:
            self.waiting.set()
            await asyncio.Event().wait()
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body (incomplete chunked read)")

    async def aclose(self):
        self.closed = True


async def receive(stream, state):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await _mimo_stream_response(client, "https://offline.invalid/chat/completions",
                headers={}, payload={"stream": True}, state=state)
    finally:
        assert len(requests) == 1, "The transport collector must never add a request."
        assert stream.closed


def test_completed_stop_survives_transport_tail_and_keeps_usage_without_cot_diagnostics():
    async def scenario():
        for usage_chunks in (
            [{"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}},
             {"completion_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 5}},
             {"total_tokens": 112}],
            [],
        ):
            state, progress = {}, []
            token = active_runtime.set(RunRuntime(publish=progress.append))
            try:
                response = await receive(InterruptedBytes(START + STOP + "".join(
                    event({"choices": [], "usage": usage}) for usage in usage_chunks)), state)
            finally:
                active_runtime.reset(token)
            body = response.json()
            assert body["choices"][0]["message"]["content"] == CONTENT
            assert body["choices"][0]["finish_reason"] == "stop"
            if usage_chunks:
                assert body["usage"] == {key: value for usage in usage_chunks for key, value in usage.items()}
            else:
                assert "usage" not in body
            assert state["completed"] is True and state["content"] == CONTENT
            assert state["diagnostic"]["tail_interrupted"] is True
            assert state["diagnostic"]["reasoning_chars"] == len(PRIVATE_REASONING)
            assert PRIVATE_REASONING not in json.dumps(state, ensure_ascii=False)
            assert PRIVATE_REASONING not in json.dumps(progress, ensure_ascii=False)

    asyncio.run(scenario())


def test_interruption_before_stop_or_inside_an_unclosed_event_is_not_completed():
    async def scenario():
        for tail in ("", STOP.rstrip("\n") + "\n",
                     STOP + event({"choices": [], "usage": {"prompt_tokens": 100}}).rstrip("\n") + "\n"):
            state = {}
            with pytest.raises(httpx.RemoteProtocolError):
                await receive(InterruptedBytes(START + tail), state)
            assert state["completed"] is False and state["content"] == CONTENT
            assert state["diagnostic"]["tail_interrupted"] is False
            assert PRIVATE_REASONING not in json.dumps(state, ensure_ascii=False)

    asyncio.run(scenario())


def test_stop_cannot_hide_later_service_parser_or_text_errors():
    async def scenario():
        for tail, error in (
            (event({"error": {"code": "invalid_api_key", "message": "Invalid API Key"}}), ProviderError),
            (event({"error": {"code": "unknown_service_error", "message": "Try later"}}), httpx.RemoteProtocolError),
            ("data: {invalid-json}\n\n", httpx.RemoteProtocolError),
            (event({"choices": "invalid-structure"}), httpx.RemoteProtocolError),
            (event(choice(content="不应追加的正文")), httpx.RemoteProtocolError),
            (event(choice(reasoning="不应追加的思考")), httpx.RemoteProtocolError),
        ):
            state = {}
            with pytest.raises(error):
                await receive(InterruptedBytes(START + STOP + tail), state)
            assert state["completed"] is False
            assert state["diagnostic"]["tail_interrupted"] is False
            assert state["content"] == CONTENT

    asyncio.run(scenario())


def test_service_error_preserves_safe_reason_without_reasoning_or_credentials():
    async def scenario():
        state = {}
        tail = event({"error": {"code": "server_busy", "message":
            "Service temporarily busy tp-privatecredential123 ttp-teamcredential456"}})
        with pytest.raises(httpx.RemoteProtocolError):
            await receive(InterruptedBytes(event(choice(reasoning=PRIVATE_REASONING)) + tail), state)
        diagnostic = state["diagnostic"]
        assert "Service temporarily busy" in diagnostic["service_error"]
        assert "privatecredential" not in json.dumps(diagnostic) and "teamcredential" not in json.dumps(diagnostic)
        assert PRIVATE_REASONING not in json.dumps(state, ensure_ascii=False)
        assert state["completed"] is False and state["content"] == ""
    asyncio.run(scenario())


def test_cancellation_after_stop_still_cancels_and_closes_the_stream():
    async def scenario():
        waiting, state = asyncio.Event(), {}
        task = asyncio.create_task(receive(InterruptedBytes(START + STOP, waiting), state))
        await asyncio.wait_for(waiting.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["completed"] is False and state["content"] == CONTENT
        assert state["diagnostic"]["error_type"] == "CancelledError"
        assert state["diagnostic"]["tail_interrupted"] is False
        assert PRIVATE_REASONING not in json.dumps(state, ensure_ascii=False)

    asyncio.run(scenario())


def test_provider_cap1_preserves_incomplete_writer_identity_without_retry_or_canon(tmp_path, monkeypatch):
    from inkflow.config import Settings
    from inkflow.provider import DeepSeekProvider
    from inkflow.schemas import DraftOutput
    from inkflow.task_settings import capture_task_settings, restore_task_settings, use_task_settings
    from inkflow.utils import content_hash

    credential = "offline-credential-must-never-be-saved"
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: credential)
    root = tmp_path / "book"
    (root / ".inkflow").mkdir(parents=True)
    project_config = '{"project_id":"offline-interrupted-book"}'
    (root / ".inkflow/project.json").write_text(project_config, encoding="utf-8")
    settings = Settings(provider_kind="mimo", base_url="https://token-plan-cn.xiaomimimo.com/v1",
        model="mimo-v2.6-flash", workspace_root=root, writer_task_attempt_limit=1,
        quality_preset="balanced")
    snapshot = capture_task_settings(settings, novel_id="offline-interrupted-book", task_id="original-writer-task")
    scope = restore_task_settings(snapshot, novel_id="offline-interrupted-book", workspace_root=root)
    partial = '{"title":"旧信","content":"他回到桌前，继续核对尚未读完的记录'
    stream = InterruptedBytes(event({"id": "response-offline", "model": settings.model,
        "choices": [{"index": 0, "delta": {"reasoning_content": PRIVATE_REASONING}, "finish_reason": None}]})
        + event(choice(content=partial)))
    requests, states, events = [], [], []
    real_client, real_collector = httpx.AsyncClient, _mimo_stream_response

    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async def collector(*args, **kwargs):
        states.append(kwargs["state"])
        return await real_collector(*args, **kwargs)

    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
        real_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("inkflow.provider._mimo_stream_response", collector)
    runtime = RunRuntime(task_id=scope.task_id, run_id="original-writer-run", publish=events.append)
    with use_task_settings(scope):
        token = active_runtime.set(runtime)
        try:
            with pytest.raises(ProviderError):
                asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="离线写稿合同。",
                    user_prompt="仅写本次授权章节。", output_model=DraftOutput, agent_role="writer", max_tokens=1000))
        finally:
            active_runtime.reset(token)

    assert len(requests) == runtime.calls == len(runtime.attempt_fragments) == 1
    assert runtime.unknown_calls == 1
    fragment = runtime.attempt_fragments[0]
    assert fragment["request_sent"] is True and fragment["task_id"] == scope.task_id
    assert fragment["usage_state"] == "unknown_or_partial"
    assert fragment["input_tokens"] is fragment["output_tokens"] is fragment["cache_hit_tokens"] is None
    assert len(states) == 1 and states[0]["completed"] is False and states[0]["content"] == partial
    paths = list((root / ".inkflow/recovery").glob("provider-response-*/writer-response.unverified.json"))
    assert len(paths) == 1
    serialized = paths[0].read_text(encoding="utf-8")
    artifact = json.loads(serialized)
    assert artifact["status"] == "incomplete" and artifact["authority"] == "task_temporary_not_canon"
    assert artifact["task_settings"] == scope.public_summary()
    assert artifact["run_id"] == runtime.run_id and artifact["attempt"] == 1
    assert artifact["request_id"] == fragment["request_id"] and artifact["attempt_id"] == fragment["attempt_id"]
    assert artifact["role"] == "writer" and artifact["schema"] == "DraftOutput"
    assert artifact["response_id"] == "response-offline" and artifact["model"] == settings.model
    assert artifact["content"] == partial and artifact["content_hash"] == content_hash(partial)
    assert artifact["input_hash"] and artifact["schema_hash"]
    assert artifact["finish_reason"] is None and artifact["stream_diagnostic"]["tail_interrupted"] is False
    for saved in (serialized, json.dumps(states, ensure_ascii=False), json.dumps(events, ensure_ascii=False)):
        assert PRIVATE_REASONING not in saved and credential not in saved
    assert not (root / "chapters").exists() and not list(root.rglob("*.md"))
    assert (root / ".inkflow/project.json").read_text(encoding="utf-8") == project_config
    assert not (root / ".inkflow/inkflow.db").exists()
    assert stream.closed
