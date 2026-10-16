import asyncio

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ValidationGateError
from inkflow.provider import ScriptedProvider
from inkflow.runtime import ensure_run_runtime
from inkflow.schemas import SettingRecordProposal, StorySettingsAudit, TerminalIntent
from inkflow.studio import StudioService
from inkflow.story_settings import StorySettingsService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession
from setting_fixture import project_fixture


def test_multi_collection_maintenance_resumes_same_scope_with_memory_owner(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    additions = [service.save_collection(name=name, fields=[{"key": "state", "label": "状态"}])
        for name in ("线索与物证", "时间线与事件", "关系与立场")]
    base = {item["name"]: item for item in service.list_collections()}
    character, world = base["人物设定"], base["世界设定"]
    names = [*[item["name"] for item in additions], "人物设定", "世界设定"]
    proposals = []
    source = [{"source_kind": "canonical", "chapter_no": 10, "quote": "此前已核对的事实。"}]
    for collection, title, field in ((world, "背景一", "premise"), (world, "背景二", "limits"), (character, "人物现状", "state")):
        record = service.save_record(collection["collection_id"], title=title, values={field: "尚待补核"})
        proposals.append(SettingRecordProposal(collection_id=collection["collection_id"], title=title,
            record_id=record["record_id"], expected_revision=record["revision"],
            values={field: "此前已核对的事实。"}, evidence_refs=source, epistemic_status="objective"))
    proposals += [SettingRecordProposal(collection_id=item["collection_id"], title=item["name"] + "记录",
        values={"state": "此前已核对的事实。"}, evidence_refs=source, epistemic_status="objective") for item in additions]
    settings = Settings(collaboration_preset="adaptive", workspace_root=project.root)
    scope = StudioService(project).db.prepare_task_settings("offline-settings-maintenance", novel_id=project.project_id,
        settings=settings, workspace_root=project.root)
    change = {"operation": "audit", "collection_names": names, "new_record_limit": 3, "apply_corrections": True}
    intent = TerminalIntent(action="story_settings_manage", authorization="approved", visible_reason="维护现有设定",
        specialist_role="memory_keeper", specialist_reason="由记忆整理角色维护证据与记录", setting_change=change,
        requested_outcome="核对五份设定，只补齐已有正文记录，不写正文或改正史")
    provider = ScriptedProvider([intent, StorySettingsAudit(summary="已核对原文并维护设定参考", setting_updates=proposals)])
    engine = InkFlowEngine(provider, settings)
    session = TerminalSession(engine)
    original_manage = InkFlowEngine.manage_story_settings
    failed = False

    async def fail_once(self, root, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise ValidationGateError("设定范围适配需要修复，未执行设定审查。")
        return await original_manage(self, root, **kwargs)

    monkeypatch.setattr(InkFlowEngine, "manage_story_settings", fail_once)
    accepted_before = project.db.accepted_chapters()
    with use_task_settings(scope), ensure_run_runtime() as runtime:
        runtime.task_id, runtime.run_id = scope.task_id, "offline-settings-maintenance"
        stopped = asyncio.run(session.handle(project.root, "先让记忆整理角色维护五份现有设定，不写正文或验收。", opened_project=project))
        assert stopped["status"] == "waiting_condition" and "设定检查" in stopped["next_action"]
        assert "正文和报告" not in stopped["next_action"]
        resume = TerminalSession.pending_resume(project, "继续刚才的任务")
        assert resume["intent"]["setting_change"] == change and resume["task_id"] == scope.task_id
        assert resume["run_id"] == runtime.run_id
        result = asyncio.run(session.handle(project.root, "继续刚才的任务", opened_project=project))
    assert result["status"] == "completed" and result["result"]["status"] == "reviewed"
    assert not result["result"]["pending_settings"]
    assert len(provider.calls) == 2 and provider.calls[1]["agent_role"] == "memory_keeper"
    assert len(result["result"]["adopted_settings"]) == 6
    assert len(service.records(world["collection_id"])) == 2
    assert all(record["status"] == "verified_reference" for collection in [character, world, *additions]
        for record in service.records(collection["collection_id"]))
    assert project.db.accepted_chapters() == accepted_before
    assert project.db.get_metadata("pending_creation_task")["status"] == "completed"
    with pytest.raises(ValidationGateError, match="一至三份"):
        asyncio.run(original_manage(engine, project.root, change={"operation": "collection_create_many", "collections": [{"name": str(n)} for n in range(4)], "populate_records": True}))
    with pytest.raises(ValidationGateError, match="一至三条"):
        asyncio.run(original_manage(engine, project.root, change={**change, "new_record_limit": 4}))
    assert len(provider.calls) == 2
