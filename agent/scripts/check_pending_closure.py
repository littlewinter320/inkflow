"""Focused offline checks for startup-era stale work; no provider or real book writes."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from inkflow.app_server import InkFlowAppService
from inkflow.config import Settings
from inkflow.errors import ProviderError
from inkflow.engine import InkFlowEngine
from inkflow.coordinator import Coordinator
from inkflow.provider import ScriptedProvider
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief, TerminalIntent
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from inkflow.runtime import active_runtime
from inkflow.terminal_session import TerminalSession


def check():
    with TemporaryDirectory(prefix="pending-closure-", dir="D:/墨流/cache") as directory:
        project = InkFlowProject.create(Path(directory) / "book", BookBrief(
            title="离线待办", genre="现实", protagonist="林安", premise="仅核对来源、队列与收尾。"))
        db = project.db
        empty_intent = TerminalSession._confirmed_pending_work_intent(project, "处理所有待办")
        assert empty_intent.action == "pending_work" and empty_intent.pending_work_decision == "process_all"
        assert empty_intent.visible_reason
        local_session = TerminalSession(InkFlowEngine(ScriptedProvider([]), Settings()))
        empty_result = asyncio.run(local_session.handle(project.root, "查看待办"))
        assert empty_result["status"] == "completed" and empty_result["pending_work"] == []
        packet = local_session._build_packet(project, "仅查看当前信息")
        policy = next(section.content for section in packet.sections if section.key == "D")
        assert '"Editor"' in policy and '"Memory Keeper"' in policy and "只有 Reviewer" not in policy
        thread = db.open_collaboration_thread(run_id="old", topic="已完成交流")
        db.append_collaboration_message(thread_id=thread["thread_id"], run_id="old", sender_role="writer",
            recipient_role="coordinator", message_type="answer", claim="交流结束", status="resolved")
        assert db.reconcile_collaboration_queue()["closed_threads"] == 1
        assert db.get_collaboration_thread(thread["thread_id"])["status"] == "resolved"
        orphan = db.append_collaboration_message(thread_id=thread["thread_id"], run_id="late", sender_role="editor",
            recipient_role="user", message_type="risk", claim="已关闭线程的迟到显示", status="escalated")
        assert orphan["message_id"] not in {item["message_id"] for item in db.list_collaboration_messages(active_only=True)}
        db.reconcile_collaboration_queue()
        assert next(item for item in db.list_collaboration_messages() if item["message_id"] == orphan["message_id"])["status"] == "resolved"

        old = db.append_collaboration_message(thread_id="same", run_id="old", sender_role="coordinator",
            recipient_role="writer", message_type="task_assignment", claim="旧指派")
        new = db.append_collaboration_message(thread_id="same", run_id="new", sender_role="coordinator",
            recipient_role="writer", message_type="task_assignment", claim="新指派")
        db.reconcile_collaboration_queue()
        active_ids = {item["message_id"] for item in db.list_collaboration_messages(active_only=True)}
        assert new["message_id"] in active_ids and old["message_id"] not in active_ids

        for claim in ("正文问题", "独立记忆问题"):
            db.append_collaboration_message(thread_id="issues", run_id="same", sender_role="editor",
                recipient_role="user", message_type="risk", claim=claim, status="escalated")
        db.reconcile_collaboration_queue()
        assert len([item for item in db.list_collaboration_messages(active_only=True) if item["thread_id"] == "issues"]) == 2
        db.resolve_collaboration_thread("issues")
        assert not [item for item in db.list_collaboration_messages(active_only=True) if item["thread_id"] == "issues"]

        intent = TerminalIntent(action="review", chapter_no=1, authorization="approved", visible_reason="原审查")
        work = project.record_pending_work(kind="workflow", reason="原任务", next_action="原节点续接",
            source={"task_id": "original-task", "conversation": "pending_creation_task"},
            intent=intent.model_dump(mode="json"), run_id="original-run")
        resumed = TerminalSession.pending_resume(project, "处理待办 " + work["id"])
        assert resumed["task_id"] == "original-task" and resumed["run_id"] == "original-run"
        db.set_metadata("coordinator_recovery:original-task:review", {"attempts": [{"number": 1}, {"number": 2}]})
        blocked = next(item for item in project.pending_work() if item["id"] == work["id"])
        assert not blocked["can_process"]
        response = {"pending_work": [blocked]}
        TerminalSession._pending_work_question(project, response)
        assert "questions" not in response

        diagnostic = project.record_pending_work(kind="coordinator_recovery", reason="原缺口", next_action="原节点",
            source={"task_id": "original-task", "node": "review"})
        unrelated = project.record_pending_work(kind="optimization", reason="其他任务", next_action="待确认", source={"other": True})
        project.update_pending_work(work["id"], status="completed", progress={"status": "completed"})
        project.reconcile_pending_work()
        remaining_ids = {item["id"] for item in project.pending_work()}
        assert diagnostic["id"] not in remaining_ids and unrelated["id"] in remaining_ids

        db.set_metadata("pending_plan_revision:1", {"status": "awaiting_review", "revision_id": "revision-1", "instruction": "按新卡衔接", "revised_version": 2})
        plan = next(item for item in project.pending_work() if item["kind"] == "plan_revision")
        assert plan["intent"]["action"] == "review" and "accept" in plan["intent"]["forbidden_actions"]
        explicit = TerminalSession._confirmed_pending_work_intent(project, "处理待办 " + plan["id"])
        normalized = Coordinator.normalize_intent(explicit, "处理待办 " + plan["id"])
        assert normalized.action == "review" and normalized.authorization == "approved"
        assert normalized.pending_question_id is None and "accept" in normalized.forbidden_actions
        project.reconcile_pending_work(current_pass=lambda number: None)
        assert any(item["kind"] == "plan_revision" for item in project.pending_work())
        project.reconcile_pending_work(current_pass=lambda number: {"id": "verified-review"})
        assert not any(item["kind"] == "plan_revision" for item in project.pending_work())

        db.set_metadata("manual_edit_jobs", {"job": {"job_id": "job", "status": "pending", "attempts": 0,
            "relative_path": "BOOK.md", "current_hash": "first", "summary": "当前候选"}})
        project.update_pending_work("manual-job", status="deferred")
        db.set_metadata("manual_edit_jobs", {"job": {"job_id": "job", "status": "failed", "attempts": 1,
            "relative_path": "BOOK.md", "current_hash": "second", "summary": "当前断点"}})
        native = next(item for item in project.pending_work() if item["id"] == "manual-job")
        assert native["status"] == "deferred" and native["source"]["current_hash"] == "second"
        assert native["reason"] == "当前断点"

        local = project.record_pending_work(kind="planning_publication", reason="恢复记录缺失", next_action="核对原日志", source={"run_id": "missing"})
        session = TerminalSession(SimpleNamespace(settings=Settings(), current_pass_review=lambda *args: None))
        action = TerminalIntent(action="pending_work", authorization="approved", visible_reason="处理原节点",
            pending_work_id=local["id"], pending_work_decision="process")
        for _ in range(3):
            result = asyncio.run(session._dispatch(project.root, action))
            assert result["status"] == "waiting_condition"
        current = next(item for item in project.pending_work() if item["id"] == local["id"])
        assert current["processing_attempts"] == 2 and not current["can_process"]
        defer = TerminalIntent(action="pending_work", authorization="approved", visible_reason="留待下次",
            pending_work_id=local["id"], pending_work_decision="defer")
        result = asyncio.run(session._dispatch(project.root, defer, None, None))
        assert result["status"] == "completed"

        reply_thread = db.open_collaboration_thread(run_id="reply", topic="两次失败后必须停止")
        db.append_collaboration_message(thread_id=reply_thread["thread_id"], run_id="reply", sender_role="coordinator",
            recipient_role="writer", message_type="fact_query", claim="仅回复既有问题")
        scope = StudioService(project).db.prepare_task_settings("offline-reply", novel_id=project.project_id,
            settings=Settings(), workspace_root=project.root)
        service = InkFlowAppService()
        provider = SimpleNamespace(generate_json=AsyncMock(side_effect=ProviderError("离线模拟失败")))

        async def no_outer_replay():
            fake_runtime = SimpleNamespace(task_id=scope.task_id, snapshot=lambda: {})
            token = active_runtime.set(fake_runtime)
            report = SimpleNamespace(source_hash="body-hash", evidence_recovery={"attempted": True,
                "roles": {"editor": {"error": "本轮补读结构化输出仍不完整"}}})
            try:
                with use_task_settings(scope), patch.object(project.db, "latest_review_record", return_value={"chapter_version": 1, "report": report}), patch.object(project.db, "get_chapter", return_value={"version": 1, "content_hash": "body-hash"}):
                    response = await local_session._recover_failed_workflow(project,
                        TerminalIntent(action="review", chapter_no=1, authorization="approved", visible_reason="原节点"),
                        SimpleNamespace(model_dump=lambda **kwargs: {}),
                        SimpleNamespace(workflow="review", task_snapshot_hash=scope.snapshot_hash, collaboration_mode=scope.collaboration_mode),
                        {"steps": [{"step": "engine.review_mode", "result": {"verdict": "unknown"}}]}, None)
                assert response["status"] == "waiting_condition" and response["coordinator_recovery"]["status"] == "not_run"
                assert "本轮补读结构化输出仍不完整" in response["reply"]
            finally:
                active_runtime.reset(token)
        asyncio.run(no_outer_replay())

        async def replies():
            async def emit(event):
                pass
            with use_task_settings(scope), patch("inkflow.app_server.create_provider", return_value=provider):
                for _ in range(2):
                    try:
                        await service.dispatch("collaboration.reply", {"project_root": str(project.root), "thread_id": reply_thread["thread_id"]}, emit)
                    except ProviderError:
                        pass
                    else:
                        raise AssertionError("必须记录本次失败")
                stopped = await service.dispatch("collaboration.reply", {"project_root": str(project.root), "thread_id": reply_thread["thread_id"]}, emit)
                assert stopped["status"] == "waiting_user" and stopped["thread"]["status"] == "escalated"
            assert provider.generate_json.await_count == 2

        asyncio.run(replies())
        print("待办收尾边界检查通过：旧议题、同秒去重、独立问题、原任务身份、耗尽停止、关联收尾、规划复核、原生来源、延期结束。")


if __name__ == "__main__":
    check()
