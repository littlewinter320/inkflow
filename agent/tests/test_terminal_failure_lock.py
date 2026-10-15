"""One offline failure-save scenario; no provider or user project access."""
import asyncio
from types import SimpleNamespace

from inkflow.config import Settings
from inkflow.conversation_scope import conversation_key
from inkflow.project import InkFlowProject
from inkflow.project_lock import project_write_lock
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import BookBrief, TerminalIntent
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession


def test_failure_save_waits_without_blocking_or_overwriting_another_task(tmp_path):
    project = InkFlowProject.create(tmp_path, BookBrief(title="断点检查", genre="悬疑", protagonist="调查员", premise="调查员根据保存的证据调查一桩旧案。"))
    scope = StudioService(project).db.prepare_task_settings("offline-failure", novel_id=project.project_id,
        settings=Settings(), workspace_root=project.root, collaboration_mode="everyday")
    session = TerminalSession(SimpleNamespace(settings=scope.settings))
    runtime = RunRuntime(publish=lambda event: None, run_id="current-run", task_id=scope.task_id)
    intent = TerminalIntent(action="write_review_accept", chapter_no=51, authorization="approved", visible_reason="完成第51章")

    async def scenario():
        with use_task_settings(scope):
            token = active_runtime.set(runtime)
            try:
                async with project_write_lock(project.root):
                    session._save_pending_task(project, intent, "running")
                    # Even a matching run_id must not authorize a foreign task.
                    foreign = project.record_pending_work(kind="workflow", reason="别的任务", next_action="保留",
                        source={"task_id": "other-task"}, run_id=runtime.run_id,
                        intent=intent.model_dump(mode="json"), status="running")
                own = next(item for item in project.pending_work() if item.get("source", {}).get("task_id") == scope.task_id)
                held, release = asyncio.Event(), asyncio.Event()

                async def competing_write():
                    async with project_write_lock(project.root):
                        held.set()
                        await release.wait()

                owner = asyncio.create_task(competing_write())
                await held.wait()
                saving = asyncio.create_task(session._save_failed_task_state(project,
                    intent.model_copy(update={"pending_work_id": foreign["id"]}), "本地失败"))
                try:
                    await asyncio.sleep(.02)
                    assert not saving.done()
                    assert project.db.get_metadata(conversation_key("pending_creation_task"))["status"] == "running"
                finally:
                    release.set()
                await asyncio.wait_for(asyncio.gather(owner, saving), timeout=3)
                assert project.db.get_metadata(conversation_key("pending_creation_task"))["status"] == "failed"
                assert project.db.get_metadata(f"pending_work:{own['id']}")["status"] == "failed"
                assert project.db.get_metadata(f"pending_work:{foreign['id']}") == foreign
                assert runtime.calls == 0

                newer = {"run_id": "other-run", "task_id": "other-task", "status": "running"}
                async with project_write_lock(project.root):
                    project.db.set_metadata(conversation_key("pending_creation_task"), newer)
                    session._save_pending_task(project, intent, "completed")
                    assert session._mark_current_pending_task_failed(project) is False
                assert project.db.get_metadata(conversation_key("pending_creation_task")) == newer
                assert project.db.get_metadata(f"pending_work:{foreign['id']}") == foreign
            finally:
                active_runtime.reset(token)

    asyncio.run(scenario())
