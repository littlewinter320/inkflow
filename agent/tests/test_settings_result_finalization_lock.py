import asyncio
import json
import threading

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ProjectError
from inkflow.project import InkFlowProject
from inkflow.project_lock import project_write_lock_sync
from inkflow.provider import ScriptedProvider
from inkflow.runtime import ensure_run_runtime
from inkflow.schemas import SettingRecordProposal, SettingRecordSet, TerminalIntent
from inkflow.studio import StudioService
from inkflow.story_settings import StorySettingsService
from inkflow.task_settings import use_task_settings
from inkflow.terminal_session import TerminalSession
from setting_fixture import project_fixture


def test_settings_result_survives_final_pending_sync_contention_and_reuses_checkpoint(tmp_path,monkeypatch):
    project=project_fixture(tmp_path)
    story=StorySettingsService(project)
    world=next(item for item in story.list_collections() if item["name"]=="世界设定")
    original=story.save_record(world["collection_id"],title="基础边界",values={"premise":"修锁匠寻找街坊。","changes":"全书45章，每章2900字。"})
    change={"operation":"record_supplement","collection_id":world["collection_id"],"record_id":original["record_id"],"expected_revision":original["revision"]}
    candidate=SettingRecordProposal(collection_id=world["collection_id"],record_id=original["record_id"],expected_revision=original["revision"],
        title=original["title"],values={"changes":""},epistemic_status="user_constraint",
        evidence_refs=[{"source_kind":"planning","path":"BOOK.md","quote":project.db.get_brief().premise}])
    intent=TerminalIntent(action="story_settings_manage",authorization="approved",visible_reason="只核对这一条设定",setting_change=change)
    provider=ScriptedProvider([intent,SettingRecordSet(records=[candidate])])
    settings=Settings(workspace_root=project.root,collaboration_preset="full_specialist")
    scope=StudioService(project).db.prepare_task_settings("offline-finalization-lock",novel_id=project.project_id,settings=settings,workspace_root=project.root)
    session=TerminalSession(InkFlowEngine(provider,settings))
    appended=TerminalSession._append_dialogue
    threads=[]
    ticks=[]

    def append_and_compete(self,*args,**kwargs):
        appended(self,*args,**kwargs)
        if threads:
            project.record_pending_work(kind="workflow",reason="后台进度需要同步",next_action="同步实际结果",
                source={"task_id":scope.task_id,"conversation":"pending_creation_task"},run_id="offline-finalization-lock",status="waiting_condition")
            return
        occupied=threading.Event()
        def other_writer():
            with project_write_lock_sync(project.root):
                occupied.set()
                threading.Event().wait(.15)
                ticks.append("released")
        worker=threading.Thread(target=other_writer)
        threads.append(worker)
        worker.start()
        assert occupied.wait(1)
        async def heartbeat():
            await asyncio.sleep(.03)
            ticks.append("event loop free")
        asyncio.create_task(heartbeat())
    monkeypatch.setattr(TerminalSession,"_append_dialogue",append_and_compete)

    with use_task_settings(scope),ensure_run_runtime() as runtime:
        runtime.run_id="offline-finalization-lock"
        runtime.task_id=scope.task_id
        result=asyncio.run(session.handle(project.root,"只核对并纠正这一条设定，保留其余内容。",opened_project=project))
        assert result["status"]=="completed" and result["result"]["records"][0]["status"]=="verified_reference"
        assert ticks==["event loop free","released"]
        assert len(provider.calls)==2
        saved=story._get(original["record_id"],"record")
        assert saved["values"]["changes"]=="" and saved["revision"]>original["revision"]
        pending=project.db.get_metadata("pending_creation_task")
        assert pending["response_checkpoint"]["response"]["result"]==result["result"]
        assert pending["response_checkpoint"]["task_id"]==scope.task_id
        assert pending["response_checkpoint"]["snapshot_hash"]==scope.snapshot_hash
        record_trace=project.internal/"runs"/result["result"]["trace_id"]
        output=json.loads((record_trace/"settings.record.1.output.json").read_text(encoding="utf-8"))
        assert output["records"][0]["values"]=={"changes":""}
        internal=[json.loads(line) for line in (record_trace/"events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(event["stage"]=="settings.record.sources" and event["metadata"]["task_settings"]["task_id"]==scope.task_id for event in internal)
        assert any(event["stage"]=="settings.record.result" and event["metadata"]["result"]["records"] for event in internal)

        # Recovering secondary bookkeeping must reuse the completed, source-bound result.
        project.db.set_metadata("pending_creation_task",{**pending,"status":"interrupted"})
        original_update=InkFlowProject.update_pending_work
        def fail_only_progress(self,identity,**updates):
            if "resolution" in updates and updates.get("progress",{}).get("action")=="story_settings_manage":
                raise ProjectError("模拟待办同步存储失败")
            return original_update(self,identity,**updates)
        monkeypatch.setattr(InkFlowProject,"update_pending_work",fail_only_progress)
        resumed=asyncio.run(session.handle(project.root,"继续刚才的任务",opened_project=project))
    assert resumed["status"]=="completed" and resumed["result"]==result["result"]
    assert resumed["warnings"] and "后台待办" in resumed["warnings"][0]
    assert len(provider.calls)==2 and story._get(original["record_id"],"record")["revision"]==saved["revision"]
    assert not (project.internal/"project.write.lock").exists()
    for worker in threads:
        worker.join(1)
