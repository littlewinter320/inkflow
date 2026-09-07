from __future__ import annotations

import asyncio

from inkflow import app_server
from inkflow.app_server import InkFlowAppService
from inkflow.provider import ScriptedProvider


def test_zero_idea_flow_returns_three_writer_candidates_without_project(monkeypatch) -> None:
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
                "core_selling_point": "强开篇与可持续升级",
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
        [{"candidates": candidates, "public_reasoning_summary": ["三个方向题材和矛盾引擎不同。"]}]
    )
    monkeypatch.setattr(app_server, "DeepSeekProvider", lambda _settings: provider)
    emitted: list[dict] = []

    async def emit(event: dict) -> None:
        emitted.append(event)

    result = asyncio.run(InkFlowAppService().dispatch("project.ideate", {"preferences": ""}, emit))

    assert len(result["candidates"]) == 3
    assert {item["genre"] for item in result["candidates"]} == {"都市异能", "古代悬疑", "科幻经营"}
    assert [event["type"] for event in emitted] == ["writer.started", "writer.completed"]
    assert provider.calls[0]["output_model"] == "NovelIdeaBundle"
    assert provider.calls[0]["thinking"] is True
