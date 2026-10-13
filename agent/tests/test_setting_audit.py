import asyncio
import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ValidationGateError
from inkflow.manual_edits import ManualEditReview
from inkflow.provider import ScriptedProvider
from inkflow.schemas import StorySettingsAudit, SettingRuleCorrection, SettingRecordProposal
from inkflow.setting_audit import corrected_rules
from setting_fixture import project_fixture


def test_setting_missing_target_explains_actual_choice(tmp_path, monkeypatch):
    from inkflow.terminal_session import TerminalSession
    from inkflow.schemas import TerminalIntent
    project = project_fixture(tmp_path)
    engine = InkFlowEngine(ScriptedProvider([]), Settings())
    async def missing_target(*args, **kwargs):
        return {"status": "needs_input", "reason": "请选择需要补充的设定。",
                "collections": [{"name": "人物设定"}, {"name": "世界设定"}]}
    monkeypatch.setattr(engine, "manage_story_settings", missing_target)
    result = asyncio.run(TerminalSession(engine)._dispatch(project.root,
        TerminalIntent(action="story_settings_manage", visible_reason="补充指定设定", authorization="approved",
            setting_change={"operation": "record_supplement"})))
    assert result["reply"] == "请选择需要补充的设定。"
    assert result["needs_clarification"]
    assert [item["label"] for item in result["questions"][0]["options"]] == ["人物设定", "世界设定", "其他"]


def test_setting_evidence_uses_shared_typed_identity():
    from pydantic import ValidationError
    from inkflow.utils import workflow_result_status
    def proposal(ref):
        return SettingRecordProposal(collection_id="setting-base-world", title="边界", values={"limits": "约定"}, evidence_refs=[ref])
    assert proposal({"source_chapter": "BOOK.md", "quote": "当前书籍约定"}).evidence_refs[0]["source_kind"] == "planning"
    assert proposal({"source_chapter": "chapter:00044", "quote": "明确的正文原句"}).evidence_refs[0]["chapter_no"] == 44
    with pytest.raises(ValidationError):
        proposal({"source_kind": "canonical", "quote": "没有章号"})
    with pytest.raises(ValidationError):
        proposal({"chapter_no": 43, "source_chapter": "chapter:00044", "quote": "串用了章号"})
    assert workflow_result_status({"result": {"status": "needs_evidence"}}) == "waiting_condition"


def test_setting_review_decodes_only_its_own_shared_sources():
    from inkflow.schemas import ModeCheckOutput, ContextUseAudit, SettingUseCheck
    from inkflow.review_verifier import resolve_review_quote_refs
    output = ModeCheckOutput(verdict="pass", confidence=1, summary="对照同一条记录",
        context_use_audit=ContextUseAudit(setting_checks=[SettingUseCheck(record_id="setting-a",
            record_revision=2, disposition="compatible", setting_quote="@quote:0",
            chapter_quote="@quote:1", reason="对应本条人物")]))
    quotes = {"@quote:0": {"source_id": "setting-a", "quote": "人物怕水。"},
              "@quote:1": {"source_id": "当前正文", "quote": "他停在水边。"}}
    decoded = resolve_review_quote_refs(output, quotes).context_use_audit.setting_checks[0]
    assert decoded.setting_quote == "人物怕水。" and decoded.chapter_quote == "他停在水边。"
    quotes["@quote:0"]["source_id"] = "setting-b"
    assert resolve_review_quote_refs(output, quotes).context_use_audit.setting_checks[0].setting_quote == "@quote:0"


def test_writer_note_budget_fits_current_handoff_and_does_not_reset_attempt(tmp_path):
    from inkflow.schemas import ContextPacket, WriterNoteClarification, HookNote
    from inkflow.trace import TraceRecorder
    project = project_fixture(tmp_path)
    chapter = project.db.get_chapter(10)
    notes = {"chapter_version": chapter["version"], "hook_note": {"why_keep": "原有说明需要完整保留。" * 200}}
    provider = ScriptedProvider([WriterNoteClarification(answers=["已核对原句"], corrected_hook_note=HookNote())])
    settings = Settings()
    engine = InkFlowEngine(provider, settings)
    packet = ContextPacket(project_id=project.project_id, chapter_no=10, task="仅更正说明", sections=[], estimated_tokens=0)
    trace = TraceRecorder(project.root, "note-budget", "full")
    async def run():
        first = await engine._clarify_writer_notes(project, notes, ["核对说明原句"], project.db.canonical_chapter_content(10), packet, trace)
        second = await engine._clarify_writer_notes(project, notes, ["核对说明原句"], project.db.canonical_chapter_content(10), packet, trace)
        return first, second
    first, second = asyncio.run(run())
    assert first["status"] == "answered" and second["reused"]
    assert len(provider.calls) == 1
    assert 2000 < provider.calls[0]["max_tokens"] <= settings.effective_output_tokens(8000, "writer")


def test_rule_correction_requires_unique_old_rule_and_real_source():
    correction = SettingRuleCorrection(old_rule="旧要求", new_rule="新要求", reason="用户已改规模",
        source_id="OUTLINE.md", source_quote="完整的新要求")
    assert corrected_rules(["旧要求", "保留文风"], [correction], {"OUTLINE.md": "完整的新要求"}) == ["新要求", "保留文风"]
    assert corrected_rules(["新要求", "保留文风"], [correction], {"OUTLINE.md": "完整的新要求"}) == ["新要求", "保留文风"]
    with pytest.raises(ValidationGateError):
        corrected_rules(["旧要求", "旧要求"], [correction], {"OUTLINE.md": "完整的新要求"})
    with pytest.raises(ValidationGateError):
        corrected_rules(["旧要求"], [correction], {"OUTLINE.md": "无对应引文"})


@pytest.mark.parametrize("invalid_rule", [False, True])
def test_setting_audit_reads_real_material_then_uses_existing_edit_review(tmp_path, monkeypatch, invalid_rule):
    project = project_fixture(tmp_path)
    brief = project.db.get_brief().model_copy(update={"user_rules": ["旧规模要求", "保持日常对白"]})
    from inkflow.project import render_book_brief
    project.db.set_brief(brief)
    (project.root / "BOOK.md").write_text(render_book_brief(brief), encoding="utf-8")
    original = (project.root / "BOOK.md").read_text(encoding="utf-8")
    settings = Settings()
    monkeypatch.setattr(Settings, "from_env", lambda *_: settings)
    from inkflow.story_settings import StorySettingsService
    service = StorySettingsService(project)
    collection = service.save_collection(name="地点", template_id="location")
    audit = StorySettingsAudit(summary="发现旧要求需同步", rule_corrections=[SettingRuleCorrection(
        old_rule="缺失的旧规则" if invalid_rule else "旧规模要求", new_rule="沿已有证据逐步对清疑问。", reason="按现行依据同步",
        source_id="OUTLINE.md", source_quote="沿已有证据逐步对清疑问。")],
        setting_updates=[SettingRecordProposal(collection_id=collection["collection_id"], title="已核对记录",
            values={"description": "此前已核对的事实。"}, epistemic_status="objective",
            evidence_refs=[{"source_chapter": 10, "quote": "此前已核对的事实。", "relation": "support"}])])
    review = ManualEditReview(verdict="pass", summary="与当前规划相容", planning_decision="compatible",
        planning_quotes=[project.db.get_current_plan_bundle().book.reader_promise])
    provider = ScriptedProvider([audit, review])
    engine = InkFlowEngine(provider, settings)
    canon = project.db.accepted_chapters()
    result = asyncio.run(engine.manage_story_settings(project.root, change={"operation": "audit", "apply_corrections": True},
        instruction="检查并处理本书设定与规划的不一致"))
    assert result["status"] == ("needs_evidence" if invalid_rule else "corrected")
    assert len(provider.calls) == (1 if invalid_rule else 2) and all(call["agent_role"] == "editor" for call in provider.calls)
    assert 6000 < provider.calls[0]["max_tokens"] <= settings.max_output_tokens
    assert "旧规模要求" in provider.calls[0]["user_prompt"] and "OUTLINE.md" in provider.calls[0]["user_prompt"]
    assert project.db.get_brief().user_rules[-1] == "保持日常对白"
    assert project.db.accepted_chapters() == canon
    if invalid_rule:
        assert result["changes"] is None and (project.root / "BOOK.md").read_text(encoding="utf-8") == original
    else:
        assert result["changes"]["before"] == original
    assert len(result["adopted_settings"]) == 1
    stored = service.records(collection["collection_id"])[0]
    assert stored["status"] == "verified_reference"
    assert stored["evidence_refs"][0]["source_hash"] == project.db.get_chapter(10)["content_hash"]
    assert stored["record_id"] in service.context(chapter_no=11, actor="writer")
    assert project.db.current_facts() == []


def test_accepted_chapter_adopts_editor_reference_once(tmp_path):
    from inkflow.story_settings import StorySettingsService
    from inkflow.writer_notes import version_notes, notes_hash
    from inkflow.schemas import ReviewReport, MemoryPatch
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    collection = service.save_collection(name="地点", template_id="location")
    chapter = project.db.get_chapter(10)
    proposal = SettingRecordProposal(collection_id=collection["collection_id"], title="已核条目",
        values={"description": "此前已核对的事实。"}, epistemic_status="objective",
        evidence_refs=[{"source_kind": "draft", "chapter_no": 10, "quote": "此前已核对的事实。"}])
    artifact = project.db.save_agent_artifact(artifact_type="mode_check_candidate", run_id="audit-test",
        role="editor", chapter_no=10, chapter_version=chapter["version"], dimension="everyday", status="verified",
        data={"source_hash": chapter["content_hash"], "story_fingerprint": "story", "snapshot_hash": "snapshot",
              "output": {"verdict": "pass", "setting_updates": [proposal.model_dump(mode="json")]}})
    report = ReviewReport(verdict="pass", confidence=1, summary="正文及参考候选通过",
        source_hash=chapter["content_hash"], writer_notes_hash=notes_hash(version_notes(project, 10)))
    review = {"chapter_version": chapter["version"], "report": report,
        "mode_bundle": {"mode": "everyday", "story_fingerprint": "story", "snapshot_hash": "snapshot",
                        "candidate_artifact_ids": {"editor": artifact["artifact_id"]}}}
    engine = InkFlowEngine(ScriptedProvider([]), Settings())
    patch = MemoryPatch(chapter_no=10, chapter_summary="已核事实")
    first = engine._adopt_setting_proposals(project, 10, chapter["version"], patch, review)
    assert len(first["record_ids"]) == 1 and not first["warnings"]
    record = service.records(collection["collection_id"])[0]
    assert record["evidence_refs"][0]["source_kind"] == "canonical"
    assert record["status"] == "verified_reference"
    second = engine._adopt_setting_proposals(project, 10, chapter["version"], patch, review)
    assert first == second
    assert len(service.records(collection["collection_id"])) == 1
    assert service.records(collection["collection_id"])[0]["revision"] == record["revision"]


def test_setting_page_reads_the_same_versioned_review(tmp_path):
    from inkflow.story_settings import StorySettingsService
    from inkflow.schemas import ReviewReport, ContextUseAudit, SettingUseCheck
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    collection = service.save_collection(name="地点", template_id="location")
    record = service.save_record(collection["collection_id"], title="线索", values={"description": "此前已核对的事实。"})
    chapter = project.db.get_chapter(10)
    report = ReviewReport(verdict="pass", confidence=1, summary="逐条核对", source_hash=chapter["content_hash"],
        context_use_audit=ContextUseAudit(setting_checks=[SettingUseCheck(record_id=record["record_id"],
            record_revision=record["revision"], disposition="compatible", setting_quote="此前已核对的事实。",
            chapter_quote="此前已核对的事实。", reason="原句支持")]))
    project.db.save_review(10, chapter["version"], report, "reviews/test.md")
    def links():
        return next(item for item in service.list_collections(include_review_links=True)
                    if item["collection_id"] == collection["collection_id"])["records"][0]["review_links"]
    assert links()[0]["current"] and links()[0]["chapter_no"] == 10
    service.save_record(collection["collection_id"], title="线索", record_id=record["record_id"],
        expected_revision=record["revision"], values={"description": "未来另有线索。"})
    assert links()[0]["current"] is False


@pytest.mark.parametrize("allow_repair", [False, True])
def test_setting_evidence_repairs_only_failed_candidates(tmp_path, allow_repair):
    from inkflow.schemas import SettingRecordSet
    from inkflow.story_settings import StorySettingsService
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    collection = service.save_collection(name="地点", template_id="location")
    valid = SettingRecordProposal(collection_id=collection["collection_id"], title="有效条目",
        values={"description": "此前已核对的事实。"}, epistemic_status="objective",
        evidence_refs=[{"source_kind": "canonical", "chapter_no": 10, "quote": "此前已核对的事实。"}])
    bad = valid.model_copy(update={"title": "待修引文", "evidence_refs": [{"source_kind": "canonical", "chapter_no": 10, "quote": "不存在的原句"}]})
    fixed = valid.model_copy(update={"title": "待修引文"})
    provider = ScriptedProvider([StorySettingsAudit(summary="检查设定依据", setting_updates=[valid, bad]), SettingRecordSet(records=[fixed])])
    result = asyncio.run(InkFlowEngine(provider, Settings()).manage_story_settings(project.root,
        change={"operation": "audit", "apply_corrections": True}, instruction="维护设定", allow_evidence_repair=allow_repair))
    assert len(provider.calls) == (2 if allow_repair else 1)
    assert len(service.records(collection["collection_id"])) == (2 if allow_repair else 1)
    assert result["status"] == ("reviewed" if allow_repair else "needs_evidence")
    if allow_repair:
        assert "有效条目" not in provider.calls[1]["user_prompt"]


def test_unchanged_book_does_not_block_world_record(tmp_path):
    from inkflow.project import render_book_brief
    from inkflow.story_settings import StorySettingsService
    project = project_fixture(tmp_path)
    brief = project.db.get_brief().model_copy(update={"user_rules": ["主角不能拥有超自然能力。"]})
    project.db.set_brief(brief)
    (project.root / "BOOK.md").write_text(render_book_brief(brief), encoding="utf-8")
    provider = ScriptedProvider([StorySettingsAudit(summary="整理世界边界", rule_corrections=[SettingRuleCorrection(
        old_rule=brief.user_rules[0], new_rule=brief.user_rules[0], reason="已一致", source_id="BOOK.md", source_quote=brief.user_rules[0])],
        setting_updates=[SettingRecordProposal(collection_id="setting-base-world", title="能力边界",
            values={"limits": brief.user_rules[0]}, epistemic_status="user_constraint",
            evidence_refs=[{"source_kind": "planning", "path": "BOOK.md", "quote": brief.user_rules[0]}])])])
    result = asyncio.run(InkFlowEngine(provider, Settings()).manage_story_settings(project.root,
        change={"operation": "audit", "apply_corrections": True}, instruction="维护设定"))
    assert result["status"] == "reviewed" and result["changes"] is None
    assert len(provider.calls) == 1 and not result["pending_settings"]
    assert StorySettingsService(project).records("setting-base-world")[0]["status"] == "verified_reference"
