import asyncio
from dataclasses import replace

from inkflow.config import Settings
from inkflow.conversation_scope import conversation_key
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.provider import ProviderResult, ScriptedProvider
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import BookBrief, ContextPacket, TerminalIntent
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession
from inkflow.utils import content_hash


def test_explicit_review_grant_detaches_stale_writer_work_without_resetting_it(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    project = InkFlowProject.create(tmp_path, BookBrief(title="审查续接", genre="悬疑", protagonist="调查员",
        premise="调查员核对已有记录中的线索。"))
    project.db.upsert_draft(51, "旧记录", "chapter51.md", "已保存正文保持不变。")
    settings = Settings(workspace_root=tmp_path, quality_preset="balanced", automatic_output_tokens=True)
    scope = StudioService(project).db.prepare_task_settings("new-review", novel_id=project.project_id,
        settings=settings, workspace_root=project.root, collaboration_mode="review_boost")
    session = TerminalSession(InkFlowEngine(ScriptedProvider([]), scope.settings))
    original = TerminalIntent(action="write_review_accept", chapter_no=51, authorization="approved",
        authorization_source="current_request", visible_reason="旧写作任务")
    old = project.record_pending_work(kind="workflow", reason="旧尝试耗尽", next_action="等待新增授权",
        source={"task_id": "old-writer"}, intent=original.model_dump(mode="json"), chapter_no=51,
        run_id="old-run", status="interrupted")
    exhausted = {"status": "waiting_user", "attempts": [{"run_id": "first"}, {"run_id": "second"}]}
    project.db.set_metadata("coordinator_recovery:old-writer:writer.write", exhausted)
    message = "墨宝，这次我明确授权新增一次第51章补审任务。保留当前正文及旧失败次数，不调用Writer。"
    routed = original.model_copy(update={"action": "review_accept", "user_message": message,
        "response_kind": "task_revision", "related_task_id": "old-writer", "pending_work_id": old["id"]})
    runtime = RunRuntime(publish=lambda _: None, task_id=scope.task_id, run_id="new-review-run")
    monkeypatch.setattr(session, "_build_packet", lambda *a: ContextPacket(project_id=project.project_id,
        chapter_no=51, task="补审", estimated_tokens=0, sections=[]))
    async def route(*_):
        return ProviderResult(data=routed, model="offline", response_id="route", reasoning_content=None, usage={})
    monkeypatch.setattr(session, "_route_packet", route)
    dispatched = []
    async def dispatch(root, intent, *_, **kwargs):
        dispatched.append(intent)
        return {"status": "completed", "reply": "离线仅核路由，不执行审查或接受。"}
    monkeypatch.setattr(session, "_dispatch", dispatch)

    async def scenario():
        with use_task_settings(scope):
            token = active_runtime.set(runtime)
            try:
                for text in ("继续。", "修好了，再补审一次第51章。", "如果" + message, "“" + message + "”",
                             "不要" + message, message.replace("任务。", "任务？")):
                    assert session._fresh_review_authorization(project, text, routed) is None
                for invalid in (routed.model_copy(update={"chapter_no": 52}),
                                routed.model_copy(update={"authorization": "proposed"}),
                                routed.model_copy(update={"action": "revise_review_accept"}),
                                routed.model_copy(update={"end_chapter_no": 55})):
                    assert session._fresh_review_authorization(project, message, invalid) is None
                session._save_pending_task(project, routed, "failed")
                result = await session.handle(project.root, "继续。", opened_project=project, include_suggestions=False)
                assert result["status"] == "completed" and len(dispatched) == 1
                assert dispatched[0].action == "review_accept" and dispatched[0].chapter_no == 51
                assert dispatched[0].pending_work_id is None and dispatched[0].related_task_id is None
                assert dispatched[0].user_message == message
                grant_key = conversation_key("review_authorization:" + content_hash(message))
                receipt = project.db.get_metadata(grant_key)
                assert receipt["task_id"] == scope.task_id
                newer = StudioService(project).db.prepare_task_settings("another-review", novel_id=project.project_id,
                    settings=settings, workspace_root=project.root, collaboration_mode="review_boost")
                with use_task_settings(newer):
                    other = active_runtime.set(replace(runtime, task_id=newer.task_id, run_id="another-run"))
                    try:
                        duplicate = await session.handle(project.root, message, opened_project=project, include_suggestions=False)
                        assert "已绑定原补审任务" in duplicate["gate"] and len(dispatched) == 1
                    finally:
                        active_runtime.reset(other)
                assert project.db.get_metadata(grant_key) == receipt
                assert project.db.get_metadata("coordinator_recovery:old-writer:writer.write") == exhausted
                assert project.db.get_metadata(f"pending_work:{old['id']}") == old
                assert project.db.get_chapter(51)["version"] == 1
                assert not session.engine.provider.calls and runtime.calls == 0
            finally:
                active_runtime.reset(token)
    asyncio.run(scenario())
