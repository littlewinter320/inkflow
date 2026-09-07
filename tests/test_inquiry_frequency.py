from __future__ import annotations

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.provider import ScriptedProvider
from inkflow.schemas import BookBrief, TerminalIntent
from inkflow.terminal_session import TerminalSession


def _project(tmp_path) -> InkFlowProject:
    engine = InkFlowEngine(ScriptedProvider([]), Settings())
    engine.create_project(
        tmp_path / "novel",
        BookBrief(
            title="询问频率测试",
            genre="悬疑",
            premise="主角必须在城市封锁前找出失踪案背后的真相。",
            protagonist="林舟",
        ),
    )
    return InkFlowProject(tmp_path / "novel")


def _intent(confidence: str) -> TerminalIntent:
    return TerminalIntent(
        action="plan",
        requested_outcome="规划第一篇章",
        confidence=confidence,
        authorization="approved",
        clarification_question="你更看重悬疑解谜还是人物关系？",
        visible_reason="用户要求开始规划，但仍有一个会影响主线重心的选择。",
    )


def test_inquiry_frequency_controls_optional_questions(tmp_path) -> None:
    project = _project(tmp_path)

    medium = TerminalSession(InkFlowEngine(ScriptedProvider([]), Settings(inquiry_frequency="medium")))
    _, medium_response = medium._resolve_intent(project, "直接规划第一篇章", _intent("medium"))
    assert medium_response is None

    high = TerminalSession(InkFlowEngine(ScriptedProvider([]), Settings(inquiry_frequency="high")))
    _, high_response = high._resolve_intent(project, "规划第一篇章", _intent("medium"))
    assert high_response and high_response["needs_clarification"] is True

    ultra = TerminalSession(InkFlowEngine(ScriptedProvider([]), Settings(inquiry_frequency="ultra")))
    _, ultra_response = ultra._resolve_intent(project, "规划第一篇章", _intent("high"))
    assert ultra_response and "悬疑解谜" in ultra_response["reply"]
    _, direct_response = ultra._resolve_intent(project, "不用再问，直接开始规划", _intent("high"))
    assert direct_response is None

    low = TerminalSession(InkFlowEngine(ScriptedProvider([]), Settings(inquiry_frequency="low")))
    _, low_response = low._resolve_intent(project, "直接规划", _intent("low"))
    assert low_response is None
