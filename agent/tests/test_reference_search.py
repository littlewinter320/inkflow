from __future__ import annotations

import asyncio

import httpx

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.provider import ScriptedProvider
from inkflow.references import ReferenceService
from inkflow.schemas import BookBrief


class _SearchClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    async def get(self, url: str, *, params: dict[str, str]):
        html = """
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Faclanthology.org%2F2024.eacl-long.147%2F">悬念故事研究</a>
          <div class="result__snippet">用迭代规划建立可追踪的悬念。</div>
        </div>
        <div class="result">
          <a class="result__a" href="https://example.org/writing">写作资料</a>
          <div class="result__snippet">另一个公开来源。</div>
        </div>
        """
        return httpx.Response(200, text=html, request=httpx.Request("GET", url, params=params))


def test_public_search_previews_results_without_importing(monkeypatch, tmp_path) -> None:
    engine = InkFlowEngine(ScriptedProvider([]), Settings())
    engine.create_project(
        tmp_path / "novel",
        BookBrief(title="资料搜索测试", genre="悬疑", premise="主角追查一封不该存在的信。", protagonist="林舟"),
    )
    project = InkFlowProject(tmp_path / "novel")
    monkeypatch.setattr("inkflow.references.httpx.AsyncClient", _SearchClient)

    result = asyncio.run(ReferenceService(project).search_public("悬疑小说 信息差", limit=1))

    assert result["adapter"] == "duckduckgo_html_v1"
    assert result["results"] == [
        {
            "title": "悬念故事研究",
            "url": "https://aclanthology.org/2024.eacl-long.147/",
            "source": "aclanthology.org",
            "snippet": "用迭代规划建立可追踪的悬念。",
        }
    ]
    assert ReferenceService(project).list_references() == []

