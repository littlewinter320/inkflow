"""Offline checks for actual reference recovery and frozen manual audit configuration."""
import asyncio
import json
from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.manual_edits import ManualEditsService, ManualEditReview, ManualEditIssue, RecoveredSettingEvidence
from inkflow.provider import ScriptedProvider
from inkflow.story_settings import StorySettingsService
from setting_fixture import project_fixture


def enqueue_record(project, record):
    return ManualEditsService(project).enqueue("story-setting:" + record["record_id"],
        json.dumps(record, ensure_ascii=False), kind="setting_record", record_revision=record["revision"])


def test_manual_review_recovers_original_reference_without_committing_memory(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    settings = Settings()
    monkeypatch.setattr(Settings, "from_env", lambda *_: settings)
    service = StorySettingsService(project)
    collection = service.save_collection(name="人物", template_id="character")
    record = service.save_record(collection["collection_id"], title="已核事实", values={"state": "此前已核对的事实。"}, epistemic_status="objective")
    provider = ScriptedProvider([ManualEditReview(verdict="pass", summary="原文支持该参考", planning_decision="compatible", planning_quotes=[project.db.get_current_plan_bundle().book.reader_promise], recovered_evidence=[
        RecoveredSettingEvidence(chapter_no=1, quote="此前已核对的事实。")])])
    manual = ManualEditsService(project)
    job = enqueue_record(project, record)
    result = asyncio.run(manual.review(job["job_id"], InkFlowEngine(provider, settings)))
    assert result["status"] == "passed" and result["attempts"] == 1
    stored = service.records(collection["collection_id"])[0]
    assert stored["status"] == "verified_reference" and stored["authority"] == "reference_only"
    assert stored["evidence_refs"][0]["source_hash"] == project.db.get_chapter(1)["content_hash"]
    assert project.db.current_facts() == [] and len(provider.calls) == 1


def test_manual_explanation_resume_retains_first_model_snapshot(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    original = Settings(model="original")
    current = [original]
    monkeypatch.setattr(Settings, "from_env", lambda *_: current[0])
    service = StorySettingsService(project)
    collection = service.save_collection(name="人物", template_id="character")
    record = service.save_record(collection["collection_id"], title="设想", values={"state": "未来回城"})
    provider = ScriptedProvider([
        ManualEditReview(verdict="needs_user", summary="确认是否仅未来设想", issues=[ManualEditIssue(reason="请说明范围", quote="未来回城")]),
        ManualEditReview(verdict="pass", summary="明确保留为设想", planning_decision="compatible", planning_quotes=[project.db.get_current_plan_bundle().book.reader_promise])])
    manual = ManualEditsService(project)
    job = enqueue_record(project, record)
    first = asyncio.run(manual.review(job["job_id"], InkFlowEngine(provider, original)))
    manual.decide(job["job_id"], "confirm_exception", "这只是未来设想，不改变过去", job["current_hash"])
    current[0] = Settings(model="changed")
    restored_models = []
    def frozen_provider(settings):
        restored_models.append(settings.model)
        return provider
    monkeypatch.setattr("inkflow.provider.create_provider", frozen_provider)
    second = asyncio.run(manual.review(job["job_id"], InkFlowEngine(provider, current[0])))
    assert second["status"] == "passed" and second["attempts"] == 2
    assert first["task_id"] == second["task_id"] and first["snapshot_hash"] == second["snapshot_hash"]
    assert restored_models == ["original"] and len(provider.calls) == 2


def test_setting_review_cannot_skip_existing_plan(tmp_path, monkeypatch):
    project = project_fixture(tmp_path)
    settings = Settings()
    monkeypatch.setattr(Settings, "from_env", lambda *_: settings)
    service = StorySettingsService(project)
    collection = service.save_collection(name="人物", template_id="character")
    record = service.save_record(collection["collection_id"], title="设想", values={"state": "未来回城"})
    provider = ScriptedProvider([ManualEditReview(verdict="pass", summary="只检查了正文")])
    manual = ManualEditsService(project)
    job = enqueue_record(project, record)
    result = asyncio.run(manual.review(job["job_id"], InkFlowEngine(provider, settings)))
    assert result["status"] == "needs_evidence" and result["planning_gap"]
    assert len(provider.calls) == 1
    assert "当前结构化规划" in provider.calls[0]["user_prompt"]
    assert service.records(collection["collection_id"])[0]["status"] != "verified_reference"
