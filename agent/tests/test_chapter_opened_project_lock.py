import asyncio
from types import SimpleNamespace

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.project_lock import project_write_lock
from inkflow.provider import ScriptedProvider
from setting_fixture import project_fixture


def test_writer_waits_for_short_project_write_without_blocking_its_owner(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    provider = ScriptedProvider([])
    engine = InkFlowEngine(provider, Settings(workspace_root=project.root))

    class OpenedProject(Exception):
        pass

    def stop_before_generation(*args, **kwargs):
        raise OpenedProject

    monkeypatch.setattr("inkflow.engine.TraceRecorder", stop_before_generation)
    finished = []

    async def exercise():
        ready = asyncio.Event()

        async def other_write():
            async with project_write_lock(project.root):
                ready.set()
                await asyncio.sleep(.1)
                finished.append(True)

        owner = asyncio.create_task(other_write())
        await ready.wait()
        try:
            with pytest.raises(OpenedProject):
                await engine.write_chapter(project.root, 11)
        finally:
            await owner

    asyncio.run(exercise())
    assert finished == [True] and not provider.calls
    assert not (project.internal / "project.write.lock").exists()


def test_writer_scan_does_not_take_a_synchronous_lock_on_the_event_loop(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    provider = ScriptedProvider([])
    engine = InkFlowEngine(provider, Settings(workspace_root=project.root))

    class ReachedScan(Exception):
        pass

    def check_scan(_):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        raise ReachedScan

    monkeypatch.setattr("inkflow.manual_edits.ManualEditsService.scan", check_scan)
    with pytest.raises(ReachedScan):
        asyncio.run(engine.write_chapter(project.root, 11))
    assert not provider.calls


def test_short_write_contention_does_not_request_coordinator_diagnosis(tmp_path):
    from inkflow.runtime import RunRuntime, active_runtime
    from inkflow.schemas import TerminalIntent
    from inkflow.task_settings import capture_task_settings, restore_task_settings, use_task_settings
    from inkflow.terminal_session import TerminalSession

    project = project_fixture(tmp_path)
    settings = Settings(workspace_root=project.root)
    provider = ScriptedProvider([])
    scope = restore_task_settings(capture_task_settings(settings, novel_id=project.project_id),
                                 novel_id=project.project_id, workspace_root=project.root)
    response = {"steps": [{"step": "writer.write", "status": "waiting_condition",
                           "gate": "短暂写锁占用", "error_type": "ProjectBusyError"}]}
    runtime = RunRuntime(publish=lambda event: None, task_id=scope.task_id)
    token = active_runtime.set(runtime)
    try:
        with use_task_settings(scope):
            result = asyncio.run(TerminalSession(InkFlowEngine(provider, settings))._recover_failed_workflow(
                project, TerminalIntent(action="write_review_accept", authorization="approved", chapter_no=11,
                                        visible_reason="续写当前章"), None,
                SimpleNamespace(task_snapshot_hash=scope.snapshot_hash), response, SimpleNamespace()))
    finally:
        active_runtime.reset(token)
    assert result["coordinator_recovery"]["status"] == "not_run"
    assert result["steps"] == response["steps"] and not provider.calls and runtime.calls == 0
