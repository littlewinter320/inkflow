import asyncio
from types import SimpleNamespace

from inkflow.app_server import InkFlowAppService
from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.project_lock import project_write_lock
from inkflow.provider import ScriptedProvider
from inkflow.schemas import StorySettingsAudit, TerminalIntent
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from setting_fixture import project_fixture


def test_app_settings_audit_waits_for_short_write_after_project_already_opened(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    settings = Settings(collaboration_preset="adaptive", workspace_root=project.root)
    scope = StudioService(project).db.prepare_task_settings("offline-opened-project", novel_id=project.project_id,
        settings=settings, workspace_root=project.root)
    provider = ScriptedProvider([TerminalIntent(action="story_settings_manage", authorization="approved",
        specialist_role="memory_keeper", setting_change={"operation": "audit", "apply_corrections": True},
        visible_reason="核对设定，不改正文"), StorySettingsAudit(summary="已核对当前资料，没有额外改动")])
    engine = InkFlowEngine(provider, settings)
    service = object.__new__(InkFlowAppService)
    service.voice = SimpleNamespace(migrating=False)
    monkeypatch.setattr(InkFlowAppService, "_engine", staticmethod(lambda root: engine))
    monkeypatch.setattr(InkFlowAppService, "_schedule_manual_reviews", lambda *_: None)
    original_manage = InkFlowEngine.manage_story_settings
    ticks = []

    async def briefly_occupied(self, root, **kwargs):
        ready = asyncio.Event()

        async def other_write():
            async with project_write_lock(root):
                ready.set()
                await asyncio.sleep(.15)
                ticks.append("other write finished")

        owner = asyncio.create_task(other_write())
        await ready.wait()
        try:
            return await original_manage(self, root, **kwargs)
        finally:
            await owner

    monkeypatch.setattr(InkFlowEngine, "manage_story_settings", briefly_occupied)
    events = []

    async def emit(event):
        events.append(event)

    with use_task_settings(scope):
        result = asyncio.run(service.dispatch("conversation.send", {"project_root": str(project.root),
            "message": "先核对本书现有设定，不写正文。", "include_suggestions": False}, emit))
    assert result["status"] == "completed", result
    assert ticks == ["other write finished"]
    assert len(provider.calls) == 2 and provider.calls[1]["agent_role"] == "memory_keeper"
    assert project.db.get_metadata("pending_creation_task")["status"] == "completed"
    assert not (project.internal / "project.write.lock").exists()
    assert not any(event.get("stage") in {"session.intent", "session.lock_diagnostic"} for event in events)
