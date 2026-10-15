import asyncio

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ProjectError, ValidationGateError
from inkflow.provider import ScriptedProvider
from inkflow.schemas import SettingRecordProposal, SettingRecordSet, TerminalIntent
from inkflow.studio import StudioService
from inkflow.story_settings import StorySettingsService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession
from setting_fixture import project_fixture


def merge_fixture(tmp_path):
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    collection = service.save_collection(name="重要地点", fields=[{"key":"place","label":"位置"},{"key":"note","label":"备注"}])
    ref={"source_kind":"canonical","chapter_no":10,"quote":"此前已核对的事实。"}
    keep=service.save_record(collection["collection_id"],title="同一地点",values={"place":"已核对的位置。"},evidence_refs=[ref])
    other=service.save_record(collection["collection_id"],title="同一地点",values={"note":"另一条独有的信息。"},evidence_refs=[ref])
    change={"operation":"record_merge","collection_id":collection["collection_id"],"keep_record_id":keep["record_id"],
        "keep_expected_revision":keep["revision"],"archive_record_id":other["record_id"],"archive_expected_revision":other["revision"]}
    candidate=SettingRecordProposal(collection_id=collection["collection_id"],record_id=keep["record_id"],
        expected_revision=keep["revision"],title=keep["title"],values={**keep["values"],**other["values"]},
        evidence_refs=[ref],epistemic_status="objective")
    return project,service,collection,keep,other,change,candidate


def test_merge_answer_reuses_snapshot_and_only_memory_merges_before_archiving(tmp_path):
    project,service,collection,keep,other,change,candidate=merge_fixture(tmp_path)
    settings=Settings(workspace_root=project.root,collaboration_preset="full_specialist")
    db=StudioService(project).db
    db.start_task("original-settings",owner_id="offline",method="conversation.send",params={"message":"检查设定"})
    scope=db.prepare_task_settings("original-settings",novel_id=project.project_id,settings=settings,workspace_root=project.root,registered_now=True)
    db.finish_task("original-settings",status="waiting_user")
    original=TerminalIntent(action="story_settings_manage",setting_change={"operation":"audit","apply_corrections":True},
        visible_reason="检查设定",forbidden_actions=["write","accept"])
    project.db.set_metadata("pending_terminal_question",{"id":"original-choice","task_id":"original-ticket",
        "snapshot_task_id":scope.task_id,"run_id":"", "intent":original.model_dump(mode="json"),
        "questions":[{"id":"question-1","selection":"single","question":"两条重复记录怎样处理？",
            "options":[{"id":"q1-o1","label":"合并并留档","kind":"choice"},{"id":"q1-o2","label":"保留两条","kind":"choice"}]}]})
    answer="我来回答刚才的问题：合并到较完整的记录，另一条留档；保留双方独有内容。"
    binding=TerminalSession.pending_resume(project,answer)
    assert binding["route_answer"] and binding["task_id"]==scope.task_id and binding["run_id"]=="original-settings"
    assert TerminalSession.pending_resume(project,"今天天气不错，聊聊吧。") is None
    restored=db.prepare_task_settings("continued-settings",novel_id=project.project_id,
        settings=Settings(workspace_root=project.root,quality_preset="economy"),workspace_root=project.root,resume_run_id=binding["run_id"])
    assert restored.snapshot_hash==scope.snapshot_hash and restored.settings.quality_preset==settings.quality_preset
    intent=TerminalIntent(action="story_settings_manage",setting_change=change,visible_reason="按原选择合并并留档",
        authorization="approved",operation_instruction="保留双方独有信息，合并核对后留档。")
    provider=ScriptedProvider([intent,SettingRecordSet(records=[candidate])])
    engine=InkFlowEngine(provider,settings)
    accepted=project.db.accepted_chapters()
    with use_task_settings(restored):
        result=asyncio.run(TerminalSession(engine).handle(project.root,answer,opened_project=project))
    merged=result["result"]
    assert merged["status"]=="merged" and len(provider.calls)==2
    assert provider.calls[0]["agent_role"]=="coordinator" and provider.calls[1]["agent_role"]=="memory_keeper"
    assert merged["record"]["values"]==candidate.values and merged["record"]["status"]=="verified_reference"
    archived=service._get(other["record_id"],"record")
    assert archived["status"]=="archived" and archived["values"]==other["values"] and archived["history"]
    assert len(service.records(collection["collection_id"]))==1 and len(service.records(collection["collection_id"],include_archived=True))==2
    assert project.db.get_metadata("pending_terminal_question") is None
    assert project.db.get_metadata("pending_creation_task")["intent"]["forbidden_actions"]==["write","accept"]
    assert project.db.accepted_chapters()==accepted


def test_merge_missing_unique_field_stale_revision_and_archive_failure_keep_sources(tmp_path,monkeypatch):
    project,service,collection,keep,other,change,candidate=merge_fixture(tmp_path)
    settings=Settings(workspace_root=project.root)
    missing=candidate.model_copy(update={"values":keep["values"]})
    provider=ScriptedProvider([SettingRecordSet(records=[missing]),SettingRecordSet(records=[candidate])])
    engine=InkFlowEngine(provider,settings)
    with pytest.raises(ValidationGateError,match="两条记录编号"):
        asyncio.run(engine.manage_story_settings(project.root,change={"operation":"record_merge"}))
    with pytest.raises(ValidationGateError,match="修订号"):
        asyncio.run(engine.manage_story_settings(project.root,change={**change,"archive_expected_revision":99}))
    assert not provider.calls
    with pytest.raises(ValidationGateError,match="遗漏"):
        asyncio.run(engine.manage_story_settings(project.root,change=change))
    assert service._get(keep["record_id"],"record")["revision"]==keep["revision"]
    assert service._get(other["record_id"],"record")["status"]!="archived"
    def fail_archive(*args,**kwargs):
        raise ProjectError("模拟留档写入失败")
    monkeypatch.setattr(StorySettingsService,"archive_record",fail_archive)
    partial=asyncio.run(engine.manage_story_settings(project.root,change=change))
    assert partial["status"]=="needs_evidence" and partial["record"]["status"]=="verified_reference"
    assert partial["record"]["values"]==candidate.values and len(service.records(collection["collection_id"]))==2
    assert len(provider.calls)==2 and "两条资料均保留" in partial["summary"]
