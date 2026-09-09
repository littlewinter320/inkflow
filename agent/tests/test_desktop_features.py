from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from inkflow import app_server
from inkflow.app_server import InkFlowAppService
from inkflow.config import Settings, load_user_settings, save_user_settings
from inkflow.errors import ConfigurationError
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.provider import ScriptedProvider
from inkflow.schemas import BookBrief
from inkflow.terminal_session import TerminalSession


async def _ignore_event(_event: dict) -> None:
    return None


def _brief() -> BookBrief:
    return BookBrief(
        title="纸月站台",
        genre="都市悬疑",
        premise="许衡在一座不存在的站台追查失踪者。",
        protagonist="许衡",
        target_audience="中文网文读者",
    )


def test_prompt_optimizer_returns_comparison_without_executing_prompt(monkeypatch) -> None:
    provider = ScriptedProvider(
        [
            {
                "optimized_prompt": "请审查第 3 章的因果链，只返回问题、证据和修改建议，不修改正文。",
                "change_summary": ["补全范围", "明确输出格式"],
                "preserved_constraints": ["只审查", "不修改正文"],
            }
        ]
    )
    monkeypatch.setattr(app_server, "DeepSeekProvider", lambda _settings: provider)

    result = asyncio.run(
        InkFlowAppService().dispatch(
            "prompt.optimize",
            {"prompt": "看看第三章，只审查别改"},
            _ignore_event,
        )
    )

    assert result["original_prompt"] == "看看第三章，只审查别改"
    assert result["optimized_prompt"].startswith("请审查第 3 章")
    assert result["source_method"] == "critique_then_synthesize"
    assert provider.calls[0]["agent_role"] is None
    assert "不执行提示词" in provider.calls[0]["system_prompt"]


def test_conversation_history_reads_old_and_timestamped_entries(tmp_path: Path) -> None:
    project = InkFlowProject.create(tmp_path / "story", _brief())
    (project.root / "DIALOGUE.md").write_text(
        "# 墨流对话记录\n\n"
        "## 用户\n\n先讨论主角动机\n\n## 墨流会话主控\n\n可以，暂不修改正文。\n\n> 继续讨论\n\n---\n\n"
        "## 用户\n\n审查第一章\n\n## 墨流会话主控\n\n发现一处时间冲突。\n\n"
        "> 记录时间：2026-09-08T10:00:00+00:00 · 已路由：review\n",
        encoding="utf-8",
    )

    entries = TerminalSession.history(project.root, limit=100)

    assert [entry["user"] for entry in entries] == ["先讨论主角动机", "审查第一章"]
    assert entries[0]["recorded_at"] == ""
    assert entries[0]["action_note"] == "继续讨论"
    assert entries[1]["recorded_at"] == "2026-09-08T10:00:00+00:00"
    assert entries[1]["action_note"] == "已路由：review"


def test_local_quick_answer_is_also_saved_to_history(tmp_path: Path) -> None:
    project = InkFlowProject.create(tmp_path / "story", _brief())
    session = TerminalSession(InkFlowEngine(ScriptedProvider([]), Settings()))

    result = asyncio.run(session.handle(project.root, "当前状态"))
    entries = TerminalSession.history(project.root)

    assert result["session"]["route"] == "status"
    assert entries[-1]["user"] == "当前状态"
    assert entries[-1]["assistant"] == "已返回当前项目状态（无需模型调用）。"
    assert entries[-1]["action_note"] == "本地快速回答"


def test_agent_generation_settings_persist_and_validate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    saved = save_user_settings(
        {
            "agent_generation": {
                "writer": {"temperature": 1.05, "top_p": 0.9, "top_k": 50},
                "reviewer": {"temperature": 0.15, "top_p": 0.7, "top_k": None},
                "memory_keeper": {"temperature": 0.05, "top_p": 0.6, "top_k": ""},
            }
        }
    )

    assert saved["agent_generation"]["writer"]["top_k"] == 50
    assert load_user_settings()["agent_generation"]["memory_keeper"]["top_k"] is None
    assert Settings.from_env().agent_generation["reviewer"]["temperature"] == 0.15

    with pytest.raises(ConfigurationError, match="temperature"):
        Settings.from_mapping({"agent_generation": {"writer": {"temperature": 3}}})
