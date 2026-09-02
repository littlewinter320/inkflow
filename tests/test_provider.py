from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import inkflow.provider as provider_module
import pytest
from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.provider import DeepSeekProvider, _model_from_json_content
from inkflow.schemas import ReviewReport


def _review_payload() -> dict[str, Any]:
    return {
        "verdict": "pass",
        "confidence": 0.9,
        "summary": "因果与章节卡一致。",
        "strengths": ["主角主动选择"],
        "findings": [],
    }


def test_json_parser_recovers_object_after_short_explanation() -> None:
    content = "审查完成，结果如下：\n" + json.dumps(_review_payload(), ensure_ascii=False) + "\n以上。"
    result = _model_from_json_content(content, ReviewReport)
    assert result.verdict == "pass"


def test_json_parser_tolerates_raw_newlines_inside_model_string_fields() -> None:
    payload = _review_payload()
    payload["summary"] = "第一行\n第二行"
    malformed_but_recoverable = json.dumps(payload, ensure_ascii=False).replace("\\n", "\n")

    result = _model_from_json_content(malformed_but_recoverable, ReviewReport)

    assert result.summary == "第一行\n第二行"


def test_empty_content_retries_with_smaller_reasoning_budget(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, body: dict[str, Any]):
            self.body = body

        def json(self) -> dict[str, Any]:
            return self.body

    class FakeAsyncClient:
        bodies = [
            {
                "id": "first",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "", "reasoning_content": "省略的推理"},
                    }
                ],
                "usage": {
                    "completion_tokens": 12_000,
                    "completion_tokens_details": {"reasoning_tokens": 12_000},
                },
            },
            {
                "id": "second",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(_review_payload(), ensure_ascii=False),
                            "reasoning_content": "较短的推理",
                        },
                    }
                ],
                "usage": {"completion_tokens": 240},
            },
        ]
        payloads: list[dict[str, Any]] = []

        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, _url: str, *, headers: dict[str, str], json: dict[str, Any]):
            assert headers["Authorization"] == "Bearer test-key"
            self.__class__.payloads.append(copy.deepcopy(json))
            return FakeResponse(self.__class__.bodies.pop(0))

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", FakeAsyncClient)
    provider = DeepSeekProvider(Settings(reasoning_effort="high"))

    result = asyncio.run(
        provider.generate_json(
            system_prompt="审查",
            user_prompt="待审正文",
            output_model=ReviewReport,
            effort="high",
            max_tokens=12_000,
        )
    )

    assert result.data.verdict == "pass"
    assert FakeAsyncClient.payloads[0]["reasoning_effort"] == "high"
    assert FakeAsyncClient.payloads[1]["reasoning_effort"] == "low"
    assert FakeAsyncClient.payloads[1]["max_tokens"] == 16_000


def test_total_request_timeout_stops_stalled_provider_without_retry(monkeypatch) -> None:
    class StalledAsyncClient:
        calls = 0

        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any):
            self.__class__.calls += 1
            await asyncio.sleep(1)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", StalledAsyncClient)
    provider = DeepSeekProvider(Settings(request_timeout_seconds=0.01))

    with pytest.raises(ProviderError, match="不会自动重试"):
        asyncio.run(
            provider.generate_json(
                system_prompt="审查",
                user_prompt="待审正文",
                output_model=ReviewReport,
                timeout_seconds=0.01,
            )
        )

    assert StalledAsyncClient.calls == 1


def test_custom_openai_compatible_endpoint_omits_deepseek_only_fields(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {
                "id": "custom-response",
                "model": "custom-model",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(_review_payload(), ensure_ascii=False)}}],
                "usage": {"completion_tokens": 120},
            }

    class FakeAsyncClient:
        payload: dict[str, Any] = {}

        def __init__(self, **_: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, _url: str, *, headers: dict[str, str], json: dict[str, Any]):
            self.__class__.payload = copy.deepcopy(json)
            return FakeResponse()

    monkeypatch.setenv("INKFLOW_API_KEY", "test-key")
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", FakeAsyncClient)
    provider = DeepSeekProvider(Settings(base_url="https://gateway.example.test/v1", model="custom-model"))

    result = asyncio.run(
        provider.generate_json(
            system_prompt="审查",
            user_prompt="待审正文",
            output_model=ReviewReport,
            max_tokens=32_000,
        )
    )

    assert result.data.verdict == "pass"
    assert "thinking" not in FakeAsyncClient.payload
    assert "reasoning_effort" not in FakeAsyncClient.payload
    assert FakeAsyncClient.payload["max_tokens"] == 16_000
