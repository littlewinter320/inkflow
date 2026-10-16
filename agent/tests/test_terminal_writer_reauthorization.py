"""Explicit human grants stay separate from exhausted recovery; no network."""
import asyncio
import json
from dataclasses import replace

from inkflow.config import Settings
from inkflow.conversation_scope import conversation_key
from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.project_lock import project_write_lock
from inkflow.provider import ProviderResult, ScriptedProvider
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import BookBrief, ContextPacket, TerminalIntent
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession
from inkflow.utils import content_hash


def test_new_single_writer_grant_preserves_old_cap_and_checkpoints_before_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    project = InkFlowProject.create(tmp_path, BookBrief(title="授权交接", genre="悬疑", protagonist="调查员",
        premise="调查员核对旧案中保存的记录与线索。"))
    settings = Settings(writer_task_attempt_limit=1, quality_preset="balanced", workspace_root=tmp_path)
    scope = StudioService(project).db.prepare_task_settings("new-authorization", novel_id=project.project_id,
        settings=settings, workspace_root=project.root, collaboration_mode="everyday")
    runtime = RunRuntime(publish=lambda event: None, run_id="new-run", task_id=scope.task_id)
    session = TerminalSession(InkFlowEngine(ScriptedProvider([]), scope.settings))
    old_intent = TerminalIntent(action="write_review_accept", chapter_no=51, authorization="approved",
        authorization_source="current_request", visible_reason="完成第51章")
    old = project.record_pending_work(kind="workflow", reason="旧请求失败", next_action="等待用户授权",
        source={"task_id": "old-task"}, run_id="old-run", status="interrupted",
        intent=old_intent.model_dump(mode="json"), chapter_no=51)
    recovery_key = "coordinator_recovery:old-task:writer.write"
    exhausted = {"status": "waiting_user", "attempts": [{"run_id": "a"}, {"run_id": "b"}]}
    project.db.set_metadata(recovery_key, exhausted)
    work = next(item for item in project.pending_work() if item["id"] == old["id"])
    assert work["can_process"] is False
    routed = old_intent.model_copy(update={"pending_work_id": old["id"], "related_task_id": "old-task",
        "response_kind": "task_revision", "confidence": "high"})
    proposal = [routed]
    message = ("我这次明确新增一次第51章写作尝试的授权：旧失败和已用次数保留。"
        "请仅完成第51章，按必要审查和接受流程执行；第1～50章保持原文，不覆盖。")

    async def scenario():
        with use_task_settings(scope):
            token = active_runtime.set(runtime)
            try:
                fresh = session._fresh_writer_authorization(project, message, routed, work)
                assert fresh.pending_work_id is None and fresh.related_task_id == "old-task"
                assert fresh.response_kind == "new_task" and fresh.authorization_source == "current_request"
                for rejected in ("继续", "代码修好了，请再试一次第51章。", "换个提示词继续第51章。",
                                 "如果" + message, "“" + message + "”", "不要" + message,
                                 message.replace("第51章写作", "第52章写作")):
                    assert session._fresh_writer_authorization(project, rejected, routed, work) is None
                for invalid in (routed.model_copy(update={"chapter_no": 52}),
                                routed.model_copy(update={"authorization": "proposed"}),
                                routed.model_copy(update={"action": "batch_draft_accept", "end_chapter_no": 55})):
                    assert session._fresh_writer_authorization(project, message, invalid, work) is None
                assert session._fresh_writer_authorization(project, message, routed,
                    {**work, "source": {"task_id": scope.task_id}}) is None
                with use_task_settings(replace(scope, settings=replace(settings, writer_task_attempt_limit=None))):
                    assert session._fresh_writer_authorization(project, message, routed, work) is None
                async with project_write_lock(project.root):
                    session._save_pending_task(project, fresh, "running")
                resumed = session.pending_resume(project, "继续")
                assert resumed["task_id"] == scope.task_id and resumed["intent"] == fresh.model_dump(mode="json")
                own = next(item for item in project.pending_work() if item.get("source", {}).get("task_id") == scope.task_id)
                assert own["source"]["related_task_id"] == "old-task"

                # A plain request must still stop at the exhausted old-work gate.
                # Its successfully parsed output must survive that local failure.
                project.db.set_metadata(conversation_key("pending_creation_task"), None)
                monkeypatch.setattr(session, "_deterministic_workflow_intent", lambda *a, **k: None)
                monkeypatch.setattr(session, "_deterministic_single_chapter_write", lambda *a, **k: None)
                monkeypatch.setattr(session, "_build_packet", lambda *a: ContextPacket(project_id=project.project_id,
                    chapter_no=51, task="授权交接检查", estimated_tokens=0, sections=[]))
                async def local_route(packet):
                    return ProviderResult(data=proposal[0], model="offline-parsed", response_id="parsed-result",
                                          reasoning_content=None, usage={})
                monkeypatch.setattr(session, "_route_packet", local_route)
                ordinary = "请仅完成第51章，审查通过后接受。"
                result = await session.handle(project.root, ordinary, opened_project=project)
                assert "此待办状态" in result["gate"]
                saved = project.db.get_metadata(conversation_key("pending_creation_task"))
                assert saved["task_id"] == scope.task_id and saved["status"] == "failed"
                assert saved["intent"]["pending_work_id"] == old["id"]
                trace = list((project.internal / "runs").glob("*/events.jsonl"))[-1]
                events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
                checkpoint = next(item for item in events if item["stage"] == "session.intent")["metadata"]
                assert checkpoint["raw_intent"] == routed.model_dump(mode="json")
                assert checkpoint["task_settings"]["snapshot_hash"] == scope.snapshot_hash
                assert checkpoint["response_id"] == "parsed-result" and checkpoint["message_hash"]
                assert project.db.get_metadata(recovery_key) == exhausted
                assert project.db.get_metadata(f"pending_work:{old['id']}") == old
                assert runtime.calls == 0 and not session.engine.provider.calls

                proposal[0] = routed.model_copy(update={"pending_work_id": "missing-old-work"})
                invalid = await session.handle(project.root, message, opened_project=project)
                assert "没有可执行的原任务" in invalid["gate"]
                assert project.db.get_metadata(conversation_key("writer_authorization:" + content_hash(message))) is None
                proposal[0] = routed
                async def no_production(*args, **kwargs):
                    return {"status": "completed", "reply": "离线只检查授权交接，不生成正文。"}
                monkeypatch.setattr(session, "_dispatch", no_production)
                completed = await session.handle(project.root, message, opened_project=project)
                assert completed["status"] == "completed"
                key = conversation_key("writer_authorization:" + content_hash(message))
                receipt = project.db.get_metadata(key)
                assert receipt["task_id"] == scope.task_id and receipt["snapshot_hash"] == scope.snapshot_hash
                assert receipt["intent"]["pending_work_id"] is None and receipt["trace_id"]
                resumed = session.pending_resume(project, message)
                assert resumed["writer_authorization"] and resumed["task_id"] == scope.task_id

                # Even a second model proposal without a pending ID cannot claim
                # this same human grant through a different frozen task.
                newer = StudioService(project).db.prepare_task_settings("another-new-run", novel_id=project.project_id,
                    settings=settings, workspace_root=project.root, collaboration_mode="everyday")
                async def forbidden_reroute(packet):
                    raise AssertionError("an already bound grant must not be routed again")
                monkeypatch.setattr(session, "_route_packet", forbidden_reroute)
                with use_task_settings(newer):
                    second_runtime = active_runtime.set(RunRuntime(publish=lambda event: None,
                        run_id="another-new-run", task_id=newer.task_id))
                    try:
                        blocked = await session.handle(project.root, message, opened_project=project)
                        assert "已绑定原任务" in blocked["gate"]
                    finally:
                        active_runtime.reset(second_runtime)
                assert project.db.get_metadata(key) == receipt
                assert project.db.get_metadata(recovery_key) == exhausted
                assert project.db.get_metadata(f"pending_work:{old['id']}") == old
                assert runtime.calls == 0 and not session.engine.provider.calls
            finally:
                active_runtime.reset(token)

    asyncio.run(scenario())


def test_natural_writer_grant_and_saved_original_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    project = InkFlowProject.create(tmp_path, BookBrief(title="自然授权", genre="悬疑", protagonist="调查员",
        premise="调查员沿现行规划核对街坊留下的旧线索。"))
    settings = Settings(writer_task_attempt_limit=1, quality_preset="balanced", workspace_root=tmp_path)
    scope = StudioService(project).db.prepare_task_settings("saved-natural-authorization", novel_id=project.project_id,
        settings=settings, workspace_root=project.root, collaboration_mode="everyday")
    runtime = RunRuntime(publish=lambda event: None, run_id="saved-natural-run", task_id=scope.task_id)
    session = TerminalSession(InkFlowEngine(ScriptedProvider([]), scope.settings))
    original = TerminalIntent(action="write_review_accept", chapter_no=51, authorization="approved",
        authorization_source="current_request", visible_reason="完成第51章")
    old = project.record_pending_work(kind="workflow", reason="旧Writer请求失败", next_action="等待明确新增授权",
        source={"task_id": "old-natural-task"}, run_id="old-natural-run", status="interrupted",
        intent=original.model_dump(mode="json"), chapter_no=51)
    recovery_key = "coordinator_recovery:old-natural-task:writer.write"
    recovery = {"status": "waiting_user", "attempts": [{"run_id": "old-a"}, {"run_id": "old-b"}]}
    project.db.set_metadata(recovery_key, recovery)
    work = next(item for item in project.pending_work() if item["id"] == old["id"])
    ui_message = (
        "刚才第51章断流了，这次我明确追加一次Writer请求，旧的失败记录和次数都保留。"
        "请先用平衡档、四人连续性审查把第51章完成，审查通过后接收，前50章不要改，也先不写后面的章节。"
        "本次Writer最多请求一次，格式修复也算在内。沿用我保存的MiMo设置和当前生效规划，不重新规划，"
        "不为小幅润色额外调用，也不用生成下一步推荐。"
        "沿用已保存的完整短稿，只定点补写，原段落保留。"
    )
    routed = original.model_copy(update={"pending_work_id": old["id"], "related_task_id": "old-natural-task",
        "response_kind": "task_revision", "confidence": "high", "user_message": ui_message,
        "operation_instruction": ui_message})
    recognized = (
        ui_message,
        "我这次明确追加一次Writer请求，请先把第51章完成。",
        "这次我明确追加一次Writer请求，请先把第51章完成。",
        "刚才连接断了。这次我明确追加一次Writer请求，请先把第51章完成。",
        "刚才提示“连接断开”，这次我明确追加一次Writer请求，请先把第51章完成。",
        "请先把第51章完成，我这次明确追加一次Writer请求。",
        "墨宝，这次我明确新增一次第51章Writer定点补写授权，沿用已保存的完整短稿，只定点补写。",
    )
    rejected = (
        "继续", "代码修好了，请再试一次第51章。", "换个提示词继续第51章。",
        "不要追加一次Writer请求，只查看第51章。",
        "如果可以，这次我明确追加一次Writer请求，请先把第51章完成。",
        "假如这次我明确追加一次Writer请求，请先把第51章完成。",
        "“这次我明确追加一次Writer请求”，这是例句，第51章先不写。",
        "“刚才连接断了。这次我明确追加一次Writer请求，请先把第51章完成。”",
        "这次我明确追加一次Writer请求吗？请先查看第51章。",
        "这次我明确追加一次Writer请求，可以吗？请先查看第51章。",
        "这次我明确追加一次Writer请求，请先把第52章完成。",
    )

    async def scenario():
        with use_task_settings(scope):
            token = active_runtime.set(runtime)
            try:
                for message in recognized:
                    fresh = session._fresh_writer_authorization(project, message, routed, work)
                    assert fresh is not None, message
                    assert fresh.pending_work_id is None and fresh.related_task_id == "old-natural-task"
                    assert fresh.user_message == message
                for message in rejected:
                    assert session._fresh_writer_authorization(project, message, routed, work) is None, message

                revised_route = routed.model_copy(update={"action": "revise_review_accept"})
                assert session._fresh_writer_authorization(project, ui_message, revised_route, work).action == "write_review_accept"
                assert session._fresh_writer_authorization(project, recognized[-1], revised_route, work).action == "write_review_accept"
                assert session._fresh_writer_authorization(project, recognized[1], revised_route, work) is None
                original_lookup = project.db.get_chapter
                monkeypatch.setattr(project.db, "get_chapter", lambda chapter: {"status": "accepted"})
                assert session._fresh_writer_authorization(project, ui_message, revised_route, work) is None
                monkeypatch.setattr(project.db, "get_chapter", original_lookup)

                # Recreate the saved successful route from the local old-work
                # gate failure. No new Coordinator decision is needed to resume.
                async with project_write_lock(project.root):
                    session._save_pending_task(project, revised_route, "failed")
                pending = session.pending_resume(project, "继续")
                assert pending["task_id"] == scope.task_id and pending["intent"]["user_message"] == ui_message
                monkeypatch.setattr(session, "_build_packet", lambda *a: ContextPacket(project_id=project.project_id,
                    chapter_no=51, task="自然授权续接检查", estimated_tokens=0, sections=[]))
                async def no_coordinator(packet):
                    raise AssertionError("the saved natural grant must resume without a Coordinator call")
                monkeypatch.setattr(session, "_route_packet", no_coordinator)
                dispatched = []
                async def no_production(root, intent, ticket, plan, *args, **kwargs):
                    from inkflow.task_settings import active_task_settings
                    active = active_task_settings.get()
                    dispatched.append((intent, active.task_id, active.snapshot_hash))
                    return {"status": "completed", "reply": "离线仅核授权交接，不生成或接受正文。"}
                monkeypatch.setattr(session, "_dispatch", no_production)
                completed = await session.handle(project.root, "继续", opened_project=project, include_suggestions=False)
                assert completed["status"] == "completed" and len(dispatched) == 1
                saved_intent, task_id, snapshot_hash = dispatched[0]
                assert task_id == scope.task_id and snapshot_hash == scope.snapshot_hash
                assert saved_intent.user_message == ui_message and saved_intent.pending_work_id is None
                assert saved_intent.action == "write_review_accept"
                assert saved_intent.related_task_id == "old-natural-task" and saved_intent.chapter_no == 51
                key = conversation_key("writer_authorization:" + content_hash(ui_message))
                receipt = project.db.get_metadata(key)
                assert receipt["task_id"] == scope.task_id and receipt["snapshot_hash"] == scope.snapshot_hash
                assert receipt["message_hash"] == content_hash(ui_message)
                assert receipt["intent"]["user_message"] == ui_message
                assert project.db.get_metadata(conversation_key("writer_authorization:" + content_hash("继续"))) is None
                repeated = session.pending_resume(project, ui_message)
                assert repeated["task_id"] == scope.task_id and repeated["writer_authorization"]
                assert project.db.get_metadata(recovery_key) == recovery
                assert project.db.get_metadata(f"pending_work:{old['id']}") == old
                assert not session.engine.provider.calls and runtime.calls == 0
            finally:
                active_runtime.reset(token)

    asyncio.run(scenario())
