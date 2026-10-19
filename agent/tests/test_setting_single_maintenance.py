import asyncio

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ValidationGateError
from inkflow.provider import ScriptedProvider
from inkflow.schemas import SettingRecordProposal, SettingRecordSet, TerminalIntent
from inkflow.studio import StudioService
from inkflow.story_settings import StorySettingsService, user_supplied_setting_values
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession, _resumable_intent
from setting_fixture import project_fixture


def test_missing_or_coordinator_values_route_to_same_owner_once(tmp_path):
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    world = next(item for item in service.list_collections() if item["name"] == "世界设定")
    original = service.save_record(world["collection_id"], title="基础边界",
        values={"premise": "修锁匠寻找街坊。", "changes": "全书45章，每章2900字。"})
    candidate = SettingRecordProposal(collection_id=world["collection_id"], record_id=original["record_id"],
        expected_revision=original["revision"], title=original["title"], values={"changes": ""},
        evidence_refs=[{"source_kind": "planning", "path": "BOOK.md", "quote": project.db.get_brief().premise}],
        epistemic_status="user_constraint")
    provider = ScriptedProvider([SettingRecordSet(records=[candidate])])
    settings = Settings(workspace_root=project.root, collaboration_preset="full_specialist")
    scope = StudioService(project).db.prepare_task_settings("single-maintenance",novel_id=project.project_id,
        settings=settings,workspace_root=project.root)
    engine = InkFlowEngine(provider, settings)
    change={"operation": "record_save", "collection_id": world["collection_id"], "record_id": original["record_id"],
        "expected_revision": original["revision"]}
    intent = TerminalIntent(action="story_settings_manage",setting_change=change,visible_reason="单条核对")
    assert _resumable_intent(intent)
    accepted_before=project.db.accepted_chapters()
    with use_task_settings(scope):
        result=asyncio.run(engine.manage_story_settings(project.root,change=change,instruction="核对并纠正这一条。",
            coordinator_proposal=True,user_message="核对并纠正这一条，保留其余内容。"))
    assert result["status"]=="saved" and len(result["records"])==1 and not result["failures"]
    assert len(provider.calls)==1 and provider.calls[0]["agent_role"]=="memory_keeper"
    saved=service._get(original["record_id"],"record")
    assert saved["values"]=={"premise":original["values"]["premise"],"changes":""}
    assert saved["status"]=="verified_reference" and project.db.accepted_chapters()==accepted_before
    assert not project.db.get_metadata("manual_edit_jobs",{})
    assert not user_supplied_setting_values("请帮我核对",{"changes":"Coordinator编造内容"})
    assert user_supplied_setting_values('请保存 {"changes":"明确原话"}',{"changes":"明确原话"})
    with pytest.raises(ValidationGateError,match="字段与内容"):
        asyncio.run(engine.manage_story_settings(project.root,change={**change,"values":[]}))
    with pytest.raises(ValidationGateError,match="先指定"):
        asyncio.run(engine.manage_story_settings(project.root,change={"operation":"record_save","collection_id":world["collection_id"]}))

    extra=service.save_record(world["collection_id"],title="另一条",values={"premise":"已有内容。"})
    empty_provider=ScriptedProvider([SettingRecordSet(records=[])])
    empty_engine=InkFlowEngine(empty_provider,settings)
    bad={"operation":"record_save","collection_id":world["collection_id"],"record_id":extra["record_id"],
        "expected_revision":extra["revision"],"values":{"premise":"Coordinator编造内容"}}
    with use_task_settings(scope):
        empty_result=asyncio.run(empty_engine.manage_story_settings(project.root,change=bad,
            instruction="核对这一条。",coordinator_proposal=True,user_message="核对这一条。"))
    assert empty_result["status"]=="needs_evidence" and len(empty_provider.calls)==1
    assert "Coordinator编造内容" not in empty_provider.calls[0]["user_prompt"]
    assert service._get(extra["record_id"],"record")["values"]==extra["values"]
