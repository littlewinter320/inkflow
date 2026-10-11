"""New review prompts require attribution; legacy reports retain unknown attribution. Offline only."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from inkflow.config import Settings
from inkflow.provider import _generation_schema, create_provider
from inkflow.schemas import ModeCheckOutput, ReviewAssessment, ReviewSourceComparison


def check():
    legacy = ReviewAssessment.model_validate({"criterion": "readability", "status": "met", "grade": 4,
        "reason": "旧报告缺少归因", "alternative": "保留待核"})
    assert legacy.evidence_relation == "unchecked"
    source = ReviewSourceComparison.model_validate({"source_id": "OUTLINE.md", "source_evidence": "原文引用",
        "chapter_evidence": "正文引用", "relation": "aligned", "reason": "旧报告缺少归因"})
    assert source.evidence_relation == "unchecked"
    expected = _generation_schema(ModeCheckOutput)
    for name in ("ReviewAssessment", "ReviewSourceComparison"):
        definition = expected["$defs"][name]
        assert definition["required"].count("evidence_relation") == 1
        assert "default" not in definition["properties"]["evidence_relation"]
        original = ModeCheckOutput.model_json_schema()["$defs"][name]
        assert original["properties"]["evidence_relation"]["default"] == "unchecked"
        assert "evidence_relation" not in original["required"]
    assert _generation_schema(ModeCheckOutput) == expected
    assert ReviewAssessment.model_validate(legacy.model_dump()).evidence_relation == "unchecked"

    async def check_http():
        calls = []
        async def respond(request):
            body = json.loads(request.content)
            system = body.get("system") or body["messages"][0]["content"]
            sent = json.loads(system[system.index('{'):])
            assert sent == expected
            calls.append(body)
            content = json.dumps({"verdict": "unknown", "confidence": 0, "summary": "离线检查，没有真实证据"}, ensure_ascii=False)
            if "system" in body:
                return httpx.Response(200, json={"content": [{"type": "text", "text": content}],
                    "usage": {"input_tokens": 1, "output_tokens": 1}})
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

        client = httpx.AsyncClient
        with patch("inkflow.provider.httpx.AsyncClient", lambda **kw: client(transport=httpx.MockTransport(respond), **kw)), \
             patch.object(Settings, "get_api_key", return_value="offline-placeholder"):
            for kind in ("openai", "ollama", "mimo", "anthropic"):
                settings = Settings.from_mapping({"provider_kind": kind,
                    "base_url": "https://offline.example/v1", "model": "offline-model"})
                result = await create_provider(settings).generate_json(system_prompt="只核对生成Schema", user_prompt="离线请求",
                    output_model=ModeCheckOutput, thinking=False)
                assert result.data.verdict == "unknown"
        assert len(calls) == 4

    Path("D:/墨流/cache").mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="review-schema-", dir="D:/墨流/cache") as directory, \
         patch("inkflow.model_usage.usage_ledger_path", return_value=Path(directory) / "usage.sqlite"), \
         patch.dict(os.environ, {"INKFLOW_SETTINGS_PATH": str(Path(directory) / "settings.json")}):
        asyncio.run(check_http())
    print("Review generation schema checks passed: 4 mocked providers require attribution; legacy defaults stay unchecked.")


if __name__ == "__main__":
    check()
