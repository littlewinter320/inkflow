from __future__ import annotations

import asyncio

from inkflow import app_server
from inkflow.app_server import InkFlowAppService
from inkflow.provider import ScriptedProvider


def test_zero_idea_flow_returns_fast_writer_candidate_without_project(monkeypatch) -> None:
    candidates = []
    for index, genre in enumerate(("都市异能", "古代悬疑", "科幻经营"), start=1):
        candidates.append(
            {
                "concept_id": f"idea-{index}",
                "title": f"测试书名{index}",
                "genre": genre,
                "premise": f"主角{index}在意外事件后必须完成一个长期目标，同时面对持续升级的阻力。",
                "protagonist": f"主角{index}",
                "target_audience": "中文网文读者",
                "core_selling_point": "",
                "target_chapter_words": 3000,
                "estimated_chapters": 200,
                "estimated_volumes": 6,
                "user_rules": [],
                "opening_hook": "第一章立刻发生不可逆事件",
                "long_term_engine": "每次选择都会扩大代价和世界范围",
                "choice_note": "适合喜欢连续追读的读者",
            }
        )
    provider = ScriptedProvider(
        [{"candidates": [candidates[0]], "public_reasoning_summary": ["快速方案保留核心矛盾和长线引擎。", "题材与用户偏好一致。"]}]
    )
    monkeypatch.setattr(app_server, "DeepSeekProvider", lambda _settings: provider)
    emitted: list[dict] = []

    async def emit(event: dict) -> None:
        emitted.append(event)

    result = asyncio.run(
        InkFlowAppService().dispatch(
            "project.ideate",
            {"preferences": "女频爽文，从落榜那天开始", "fast": True},
            emit,
        )
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["genre"] == "都市异能"
    assert result["candidates"][0]["user_rules"] == ["用户原始偏好（不可擅自改写）：女频爽文，从落榜那天开始"]
    assert result["candidates"][0]["choice_note"].endswith("这是本次创意提案，不代表用户已经确认。")
    assert result["candidates"][0]["core_selling_point"] == "都市异能题材下的低起点成长、持续升级矛盾与长线悬念"
    assert result["public_reasoning_summary"][0] == "已按原文记录用户偏好：女频爽文，从落榜那天开始"
    assert result["mode"] == "quick"
    assert [event["type"] for event in emitted] == ["writer.started", "writer.completed"]
    assert provider.calls[0]["output_model"] == "NovelIdeaBundle"
    assert provider.calls[0]["thinking"] is False
    assert provider.calls[0]["max_tokens"] == 1200
    assert "女性主角" in provider.calls[0]["system_prompt"]
    assert "从落榜那天开始" in provider.calls[0]["user_prompt"]
