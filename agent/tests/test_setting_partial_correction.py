import asyncio

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ProjectError
from inkflow.provider import ScriptedProvider
from inkflow.schemas import SettingRecordProposal, StorySettingsAudit
from inkflow.story_settings import StorySettingsService
from setting_fixture import project_fixture


def test_clear_one_setting_field_preserves_record_and_reports_actual_execution(tmp_path):
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    world = next(item for item in service.list_collections() if item["name"] == "世界设定")
    original_ref = {"source_kind": "canonical", "chapter_no": 10, "quote": "此前已核对的事实。"}
    original = service.save_record(world["collection_id"], title="世界基础边界",
        values={"premise": "修锁匠逐步寻找失踪的街坊。", "changes": "全书45章，每章2900字。"},
        evidence_refs=[original_ref], epistemic_status="user_constraint")
    candidate = SettingRecordProposal(collection_id=world["collection_id"], record_id=original["record_id"],
        expected_revision=original["revision"], title=original["title"], values={"changes": ""},
        evidence_refs=[{"source_kind": "planning", "path": "BOOK.md", "quote": project.db.get_brief().premise}],
        epistemic_status="user_constraint")
    model_summary = "已核对第1至10章全文并全部修正changes。"
    provider = ScriptedProvider([StorySettingsAudit(summary=model_summary,
        findings=["世界变化误放写作要求。"], setting_updates=[candidate])])
    engine = InkFlowEngine(provider, Settings(workspace_root=project.root))
    accepted_before = project.db.accepted_chapters()
    result = asyncio.run(engine.manage_story_settings(project.root,
        change={"operation": "audit", "collection_names": [world["name"]], "apply_corrections": True},
        instruction="核对世界设定，清空误放的写作要求，不改正文。"))

    assert result["status"] == "reviewed" and not result["pending_settings"]
    saved = service._get(original["record_id"], "record")
    assert saved["values"] == {"premise": original["values"]["premise"], "changes": ""}
    assert saved["status"] == "verified_reference" and saved["revision"] > original["revision"]
    assert len(saved["evidence_refs"]) == 2 and all(ref["located"] for ref in saved["evidence_refs"])
    assert result["model_summary"] == model_summary and model_summary not in result["summary"]
    assert "changes" not in result["summary"] and "1至10章全文" not in result["summary"]
    assert f"读取 {len(result['coverage']['canonical_chapters'])} 章正文" in result["summary"]
    assert "已保存并核对 1 条设定" in result["summary"] and len(provider.calls) == 1
    assert project.db.accepted_chapters() == accepted_before

    with pytest.raises(ProjectError, match="至少填写一项"):
        service.save_record(world["collection_id"], record_id=saved["record_id"],
            expected_revision=saved["revision"], values={"premise": "", "changes": ""},
            actor="memory_keeper", operation="supplement", evidence_refs=[original_ref])
    with pytest.raises(ProjectError, match="至少填写一项"):
        service.save_record(world["collection_id"], title="空设定", values={"changes": ""})
    with pytest.raises(ProjectError, match="未定位到来源原句"):
        service.save_record(world["collection_id"], record_id=saved["record_id"],
            expected_revision=saved["revision"], values={"changes": ""}, actor="memory_keeper", operation="supplement",
            evidence_refs=[{**original_ref, "quote": "正文没有这句话。"}])
    large = service.save_record(world["collection_id"], title="长度边界", values={"premise": "甲" * 15950})
    with pytest.raises(ProjectError, match="16000"):
        service.save_record(world["collection_id"], record_id=large["record_id"],
            expected_revision=large["revision"], values={"limits": "乙" * 100},
            actor="memory_keeper", operation="supplement", evidence_refs=[original_ref])
