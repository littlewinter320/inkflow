"""Recover only the Writer's existing H1 title; never regenerate its prose."""
import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.project import InkFlowProject
from inkflow.provider import DeepSeekProvider, _model_from_json_content
from inkflow.schemas import BookBrief, DraftOutput
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings


def test_missing_draft_title_reuses_own_heading_without_another_request(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline-test")
    prose = "\ufeff\n\n# 第51章 旧纸上的回执 ##\n\n" + "他把旧纸铺在桌上，按原先的顺序核对留下的笔迹。" * 12
    candidate = {"content": prose, "decision_summary": ["承接上一章的核对结果。"], "thread_changes": ["旧线索继续核实。"]}
    parse = lambda value: _model_from_json_content(json.dumps(value, ensure_ascii=False), DraftOutput)
    parsed = parse(candidate)
    assert parsed.title == "第51章 旧纸上的回执" and parsed.content == prose
    assert parsed.decision_summary == candidate["decision_summary"] and parsed.thread_changes == candidate["thread_changes"]
    assert "title" not in candidate
    assert parse({**candidate, "title": "原来单独返回的标题"}).title == "原来单独返回的标题"
    with pytest.raises(ValidationError):
        parse({**candidate, "title": None})
    for invalid in ("\ufeff\n\t ", "没有标题。" * 40, "## 二级标题\n" + "正文" * 70,
                    "开头是正文。\n# 后面的标题\n" + "正文" * 70, "    # 缩进代码\n" + "正文" * 70):
        with pytest.raises(ValidationError):
            parse({"content": invalid})

    project = InkFlowProject.create(tmp_path / "book", BookBrief(title="格式检查", genre="悬疑", protagonist="调查员",
        premise="调查员核对旧纸中保存的笔迹与凭据。"))
    sent, answer = [], [candidate]
    def response(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline-result", "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(answer[0], ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}}})
    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(response), **kwargs))
    for provider, model, endpoint in (("mimo", "mimo-v2.6-flash", "https://token-plan-cn.xiaomimimo.com/v1"),
                                      ("deepseek", "deepseek-v4-flash", "https://api.deepseek.com/v1")):
        settings = Settings(provider_kind=provider, model=model, base_url=endpoint, quality_preset="balanced",
                            writer_task_attempt_limit=1, workspace_root=project.root)
        scope = StudioService(project).db.prepare_task_settings("offline-" + provider, novel_id=project.project_id,
            settings=settings, workspace_root=project.root, collaboration_mode="everyday")
        before = len(sent)
        with use_task_settings(scope):
            result = asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="只返回章节草稿结构",
                user_prompt="按本次范围写作", output_model=DraftOutput, max_tokens=32000, agent_role="writer"))
        assert len(sent) == before + 1 and result.data.content == prose and result.data.title == parsed.title
        assert sent[-1]["thinking"]["type"] == "enabled"

    answer[0] = {"content": "没有标题的私稿。" * 40}
    scope = StudioService(project).db.prepare_task_settings("offline-no-heading", novel_id=project.project_id,
        settings=settings, workspace_root=project.root, collaboration_mode="everyday")
    before = len(sent)
    with use_task_settings(scope), pytest.raises(ProviderError) as failed:
        asyncio.run(DeepSeekProvider(settings).generate_json(system_prompt="只返回章节草稿结构", user_prompt="按本次范围写作",
            output_model=DraftOutput, agent_role="writer"))
    assert len(sent) == before + 1 and failed.value.recovery_exhausted
    assert "尝试 1 次" in str(failed.value) and "缺少标题" in str(failed.value)
    assert "私稿" not in str(failed.value) and "pydantic.dev" not in str(failed.value)
