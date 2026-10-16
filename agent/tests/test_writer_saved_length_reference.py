"""A fresh one-call grant expands the saved return, without regenerating a chapter."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from inkflow.config import Settings
from inkflow.conversation_scope import conversation_key
from inkflow.database import ProjectDatabase
from inkflow.engine import InkFlowEngine, _saved_length_reference_requested
from inkflow.errors import ValidationGateError
from inkflow.project import InkFlowProject
from inkflow.provider import ProviderResult, ScriptedProvider, _generation_schema
from inkflow.runtime import RunRuntime, active_runtime
from inkflow.schemas import BookBrief, ContextPacket, ContextSection, DraftOutput
from inkflow.studio import StudioService
from inkflow.task_settings import use_task_settings
from inkflow.trace import TraceRecorder
from inkflow.utils import content_hash, json_dumps


def test_saved_reference_requires_new_grant_and_only_inserts_into_original(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    project = InkFlowProject.create(tmp_path / "book", BookBrief(title="留稿续写", genre="悬疑",
        protagonist="调查员", premise="核对原有记录，继续未完成的调查。"))
    settings = Settings(writer_task_attempt_limit=1, workspace_root=project.root)
    studio = StudioService(project)
    old_scope = studio.db.prepare_task_settings("old-run", novel_id=project.project_id,
        settings=settings, workspace_root=project.root, collaboration_mode="everyday")
    scope = studio.db.prepare_task_settings("new-run", novel_id=project.project_id,
        settings=settings, workspace_root=project.root, collaboration_mode="everyday")
    old_work = project.record_pending_work(kind="workflow", reason="篇幅不足", next_action="等待新授权",
        source={"task_id": old_scope.task_id}, intent={"chapter_no": 51}, chapter_no=51)
    original = "他逐行核对旧记录。" * 20 + "\n\n" + "她把末页压在桌角。" * 10
    draft = DraftOutput(title="旧记录", content=original, new_fact_candidates=["未审候选，不是正史"])
    content = json_dumps(draft.model_dump(mode="json"))
    artifact = {"status": "unverified", "authority": "task_temporary_not_canon", "role": "writer",
        "schema": "DraftOutput", "schema_hash": content_hash(json_dumps(_generation_schema(DraftOutput))),
        "task_settings": old_scope.public_summary(), "run_id": "old-run", "attempt_id": "attempt-one",
        "request_id": "request-one", "response_id": "response-one", "finish_reason": "stop", "model": "offline-model",
        "content": content, "content_hash": content_hash(content), "input_hash": "old-input"}
    path = project.internal / "recovery/provider-response-offline/writer-response.unverified.json"
    path.parent.mkdir(parents=True)
    path.write_text(json_dumps(artifact), encoding="utf-8")
    TraceRecorder(project.root, "write-00051", "full").record_model("writer.model",
        ProviderResult(data=draft, model="offline-model", response_id="response-one", reasoning_content=None, usage={}),
        "旧任务的完整交稿")
    ledger = tmp_path / "ledger.sqlite"
    with sqlite3.connect(ledger) as db:
        db.execute("""CREATE TABLE model_attempts(attempt_id,request_id,task_id,novel_id,role,request_sent,
            outcome,finished_at,diagnostic_json,model)""")
        db.execute("INSERT INTO model_attempts VALUES(?,?,?,?,?,?,?,?,?,?)", (
            "attempt-one", "request-one", old_scope.task_id, project.project_id, "writer", 1, "completed", "finished",
            json.dumps({"stage": "DraftOutput", "materials_hash": "old-input"}), "offline-model"))
    monkeypatch.setattr("inkflow.model_usage.usage_ledger_path", lambda: ledger)
    message = "这次我明确追加一次Writer请求，第51章沿用已保存原稿，只定点补写；不重写整章。"
    assert _saved_length_reference_requested(message)
    for instruction in ("不要沿用原稿，只补写。", "“沿用原稿，只补写”是引用。", "保留原稿，重新生成整章。"):
        assert not _saved_length_reference_requested(instruction)
    reference = {"task_id": old_scope.task_id, "message_hash": content_hash(message)}
    grant = {"task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash, "novel_id": project.project_id,
        "message_hash": content_hash(message), "intent": {"user_message": message,
            "related_task_id": old_scope.task_id, "chapter_no": 51, "authorization": "approved",
            "authorization_source": "current_request", "action": "write_review_accept"}}
    grant_key = conversation_key("writer_authorization:" + content_hash(message))
    provider = ScriptedProvider([{"insertions": [{"before_paragraph": 1, "content": "他继续检查纸页背面的折痕。" * 11}],
        "decision_summary": ["仅补足原场景动作，原段落保留。"]}])
    engine = InkFlowEngine(provider, scope.settings)
    runtime = RunRuntime(publish=lambda event: None, run_id="new-run", task_id=scope.task_id)
    runtime_token = active_runtime.set(runtime)
    with use_task_settings(scope):
        try:
            with pytest.raises(ValidationGateError, match="授权身份"):
                engine._writer_length_reference(project, 51, reference)
            project.db.set_metadata(grant_key, grant)
            loaded, _, _ = engine._writer_length_reference(project, 51, reference)
            assert loaded.content == original and not loaded.setting_updates
            with pytest.raises(ValidationGateError, match="授权身份"):
                engine._writer_length_reference(project, 52, reference)
            path.write_text(json_dumps({**artifact, "content": content + "篡改"}), encoding="utf-8")
            with pytest.raises(ValidationGateError, match="不完整"):
                engine._writer_length_reference(project, 51, reference)
            path.write_text(json_dumps(artifact), encoding="utf-8")

            async def ready(*args, **kwargs):
                return None
            changed = [False]
            async def concurrent_draft(*args, **kwargs):
                changed[0] = True
            with monkeypatch.context() as race:
                race.setattr(engine, "ensure_chapter_plan", concurrent_draft)
                race.setattr(ProjectDatabase, "get_chapter", lambda *args: {"status": "draft"} if changed[0] else None)
                with pytest.raises(ValidationGateError, match="准备补写期间"):
                    asyncio.run(engine.write_chapter(project.root, 51, draft_reference=reference))
                assert not provider.calls
            packet = ContextPacket(project_id=project.project_id, chapter_no=51, task="当前资料核对",
                estimated_tokens=10, sections=[ContextSection(key="CSET", title="当前设定", content="使用当前有效来源")])
            monkeypatch.setattr(engine, "ensure_chapter_plan", ready)
            monkeypatch.setattr(ProjectDatabase, "get_chapter_card", lambda self, chapter: {"target_words": 400})
            monkeypatch.setattr(engine, "_writer_source_fingerprint", lambda *args: "current-sources")
            monkeypatch.setattr(engine, "_context_builder", lambda *args: SimpleNamespace(build=lambda *a, **k: packet))
            monkeypatch.setattr(engine, "_continuity_anchor", lambda *args: "")
            result = asyncio.run(engine.write_chapter(project.root, 51, "仅补足篇幅", draft_reference=reference))
            assert result["version"] == 1 and project.db.get_chapter(51)["status"] == "draft"
            assert len(provider.calls) == 1 and provider.calls[0]["output_model"] == "ParagraphExpansionPlan"
            assert "当前有效来源" in provider.calls[0]["user_prompt"]
            text = (project.root / "chapters/chapter_00051.draft.md").read_text(encoding="utf-8")
            assert all(paragraph in text for paragraph in original.split("\n\n"))
            assert project.db.get_metadata(f"pending_work:{old_work['id']}") == old_work
            assert project.db.get_metadata(f"workflow_writer:{scope.task_id}:51:write")["version"] == 1
            assert project.db.get_metadata(f"native_recovery:" + content_hash(json_dumps([scope.task_id, "writer.length", 51])))["limit"] == 1
            with pytest.raises(ValidationGateError, match="已有当前版本"):
                asyncio.run(engine.write_chapter(project.root, 51, draft_reference=reference))
            assert len(provider.calls) == 1
            failures = [json.loads(line) for path in (project.internal / "runs").glob("*-write-00051-*/events.jsonl")
                        for line in path.read_text(encoding="utf-8").splitlines()]
            assert any(event.get("stage") == "write" and event.get("metadata", {}).get("error_type") == "ValidationGateError"
                and any(site["file"] == "engine.py" and site["function"] == "write_chapter"
                        for site in event["metadata"]["error_site"]) for event in failures)
        finally:
            active_runtime.reset(runtime_token)
