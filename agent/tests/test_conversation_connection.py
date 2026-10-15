import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from inkflow.config import Settings, api_key_status, mimo_endpoint_for_key
from inkflow.engine import InkFlowEngine
from inkflow.errors import ProviderError
from inkflow.provider import ScriptedProvider
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession
from setting_fixture import project_fixture


def session_scope(project, provider):
    settings = Settings(provider_kind="mimo", model="mimo-v2.6-flash", base_url="https://token-plan-cn.xiaomimimo.com/v1", workspace_root=project.root)
    scope = StudioService(project).db.prepare_task_settings("conversation-check", novel_id=project.project_id,
        settings=settings, workspace_root=project.root)
    return TerminalSession(InkFlowEngine(provider, scope.settings)), scope


def test_model_question_is_local_and_keeps_unrelated_pending_work(tmp_path):
    project = project_fixture(tmp_path)
    provider = ScriptedProvider([])
    session, scope = session_scope(project, provider)
    pending = {"status": "waiting_condition", "run_id": "older-novel-task", "intent": {"action": "write_review", "chapter_no": 11}}
    project.db.set_metadata("pending_creation_task", pending)
    with use_task_settings(scope):
        result = asyncio.run(session.handle(project.root, "你现在是什么模型？", opened_project=project))
    assert result["session"]["route"] == "model_info" and "mimo-v2.6-flash" in result["reply"]
    assert not provider.calls
    assert project.db.get_metadata("pending_creation_task") == pending
    assert TerminalSession.history(project.root)[-1]["assistant"] == result["reply"]


def test_casual_conversation_uses_only_reply_contract_and_one_call(tmp_path):
    project = project_fixture(tmp_path)
    provider = ScriptedProvider([{"reply": "听起来今天挺累的。愿意说说发生了什么吗？"}])
    session, scope = session_scope(project, provider)
    session._suggest_after_result = AsyncMock(side_effect=AssertionError("闲聊不追加写作建议调用"))
    with use_task_settings(scope):
        result = asyncio.run(session.handle(project.root, "今天心情有点低落", opened_project=project, include_suggestions=True))
    assert len(provider.calls) == 1 and provider.calls[0]["output_model"] == "ConversationReply"
    assert "全书大纲" not in provider.calls[0]["user_prompt"]
    assert "受限工作流" not in provider.calls[0]["system_prompt"]
    assert result["session"]["route"] == "chat" and result["suggested_prompts"] == []
    assert project.db.get_metadata("pending_creation_task") is None


@pytest.mark.parametrize("message", ["继续上次的任务", "请修改第11章", "把这里删除", "帮我写一段对白"])
def test_task_requests_are_not_forced_into_casual_conversation(tmp_path, message):
    project = project_fixture(tmp_path)
    assert not TerminalSession._casual_message(project, message)


def test_fixed_question_answer_keeps_original_routing(tmp_path):
    project = project_fixture(tmp_path)
    project.db.set_metadata("pending_terminal_question", {"id": "question-current", "questions": []})
    assert not TerminalSession._casual_message(project, "就用冷淡一点的语气")


def test_implicit_changes_keep_semantic_routing_instead_of_losing_task_scope(tmp_path):
    project = project_fixture(tmp_path)
    for message in ("请把这个弄好", "就用这个吧", "再帮我安排一下", "我们换一个方向"):
        assert not TerminalSession._casual_message(project, message)
    assert TerminalSession._casual_message(project, "今天心情有点低落")
    assert TerminalSession._casual_message(project, "为什么天空是蓝色的？")


@pytest.mark.parametrize("status", [401, 402])
def test_connection_failure_has_relevant_guidance_and_no_outer_retry(tmp_path, status):
    project = project_fixture(tmp_path)
    provider = SimpleNamespace(generate_json=AsyncMock(side_effect=ProviderError(f"小米 MiMo API 返回 HTTP {status}：拒绝请求")))
    session, scope = session_scope(project, provider)
    with use_task_settings(scope):
        result = asyncio.run(session.handle(project.root, "你好呀，今天有点累", opened_project=project))
    assert result["failure_kind"] == "model_connection"
    assert str(status) in result["reply"] and "正文" not in result["next_action"] and "报告" not in result["next_action"]
    assert provider.generate_json.await_count == 1


def test_saved_key_status_masks_secret_and_pairs_mimo_endpoint(monkeypatch):
    monkeypatch.delenv("INKFLOW_API_KEY", raising=False)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    secret = "tp-unit-test-never-return-full-1234"
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_password=lambda *_: secret))
    status = api_key_status("mimo")
    assert status["api_key_configured"] and status["api_key_preview"].endswith("1234")
    assert secret not in str(status) and "unit-test" not in str(status)
    assert mimo_endpoint_for_key("https://api.xiaomimimo.com/v1", secret) == "https://token-plan-cn.xiaomimimo.com/v1"
    assert mimo_endpoint_for_key("https://token-plan-cn.xiaomimimo.com/v1", "sk-example") == "https://api.xiaomimimo.com/v1"
    assert mimo_endpoint_for_key("https://custom-proxy.example/v1", secret) == "https://custom-proxy.example/v1"


def test_settings_rpc_pairs_endpoint_and_reveals_only_on_explicit_action(tmp_path, monkeypatch):
    from inkflow.app_server import InkFlowAppService, _should_track_task
    from inkflow.config import load_user_settings
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("INKFLOW_API_KEY", raising=False)
    monkeypatch.delenv("MIMO_API_KEY", raising=False)
    saved_keys = {}
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(
        get_password=lambda service, account: saved_keys.get((service, account)),
        set_password=lambda service, account, key: saved_keys.update({(service, account): key})))
    service = object.__new__(InkFlowAppService)
    async def emit(_):
        raise AssertionError("凭据查看不生成任务事件")
    secret = "tp-unit-test-keep-local-9876"
    saved = asyncio.run(service.dispatch("provider.configure", dict(provider_kind="mimo",
        model="mimo-v2.6-flash", base_url="https://api.xiaomimimo.com/v1", api_key=secret), emit))
    assert saved["base_url"] == "https://token-plan-cn.xiaomimimo.com/v1"
    assert secret not in str(saved) and secret not in str(load_user_settings())
    masked = asyncio.run(service.dispatch("provider.key", {"provider_kind": "mimo"}, emit))
    assert "api_key" not in masked and masked["api_key_preview"].endswith("9876")
    revealed = asyncio.run(service.dispatch("provider.key", {"provider_kind": "mimo", "reveal": True}, emit))
    assert revealed == {"api_key": secret}
    assert not _should_track_task("provider.key", {"reveal": True})
