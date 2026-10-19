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


@pytest.mark.parametrize("actor,include_writer", [("editor", False), ("editor", True), ("reviewer", False)])
def test_accepted_chapter_adopts_editor_reference_once(tmp_path, actor, include_writer):
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
    if include_writer:
        project.db.save_agent_artifact(artifact_type="writer_hook_note", run_id="writer-test",
            role="writer", chapter_no=10, chapter_version=chapter["version"], status="current",
            data={"content_hash": chapter["content_hash"], "setting_updates": [proposal.model_copy(
                update={"epistemic_status": "hypothesis"}).model_dump(mode="json")]})
    artifact = project.db.save_agent_artifact(artifact_type="mode_check_candidate", run_id="audit-test",
        role=actor, chapter_no=10, chapter_version=chapter["version"], dimension="everyday", status="verified",
        data={"source_hash": chapter["content_hash"], "story_fingerprint": "story", "snapshot_hash": "snapshot",
              "output": {"verdict": "pass", "confidence": 1, "summary": "按当前正文核对附件", "setting_updates": [proposal.model_dump(mode="json")]}})
    report = ReviewReport(verdict="pass", confidence=1, summary="正文及参考候选通过",
        source_hash=chapter["content_hash"], writer_notes_hash=notes_hash(version_notes(project, 10)))
    review = {"chapter_version": chapter["version"], "report": report,
        "mode_bundle": {"mode": "everyday", "story_fingerprint": "story", "snapshot_hash": "snapshot",
                        "candidate_artifact_ids": {actor: artifact["artifact_id"]}}}
    engine = InkFlowEngine(ScriptedProvider([]), Settings())
    patch = MemoryPatch(chapter_no=10, chapter_summary="已核事实")
    first = engine._adopt_setting_proposals(project, 10, chapter["version"], patch, review)
    if actor != "editor":
        assert not first["record_ids"] and not first["warnings"] and first["deferred_candidates"]
        assert service.records(collection["collection_id"]) == []
        return
    assert len(first["record_ids"]) == 1 and not first["warnings"]
    record = service.records(collection["collection_id"])[0]
    assert record["evidence_refs"][0]["source_kind"] == "canonical"
    assert record["status"] == "verified_reference"
    second = engine._adopt_setting_proposals(project, 10, chapter["version"], patch, review)
    assert first == second
    assert len(service.records(collection["collection_id"])) == 1
    assert service.records(collection["collection_id"])[0]["revision"] == record["revision"]


def test_required_rules_readback_and_world_classification(tmp_path):
    import json
    from inkflow.story_settings import book_requirement_collection, setting_classification_issues
    from inkflow.schemas import ContextPacket, ContextSection, ModeCheckOutput, ContextUseAudit, SettingUseCheck
    from inkflow.review_verifier import mode_output_model, setting_review_gaps
    from pydantic import ValidationError
    project = project_fixture(tmp_path)
    brief = project.db.get_brief().model_copy(update={"user_rules": ["只写普通人", "保留日常对白"]})
    collection = book_requirement_collection(brief, "只写普通人\n保留日常对白")
    records = collection["records"]
    packet = ContextPacket(project_id=project.project_id, chapter_no=11, task="核对本书要求", estimated_tokens=0,
        sections=[ContextSection(key="CSET", title="必读要求", content=json.dumps({"collections": [collection]}),
            hard=True, source_ids=[r["record_id"] for r in records])])
    checks = [SettingUseCheck(record_id=r["record_id"], record_revision=1, disposition="not_applicable",
        setting_quote=r["values"]["requirement"], reason="本次仅核对未涉及该要求的材料") for r in records]
    output = ModeCheckOutput(verdict="pass", confidence=1, summary="逐项读取", context_use_audit=ContextUseAudit(setting_checks=checks))
    assert not setting_review_gaps(output, "本次材料", packet)
    model = mode_output_model(packet, primary=True)
    model.model_validate(output.model_dump())
    with pytest.raises(ValidationError):
        model.model_validate(output.model_copy(update={"context_use_audit": ContextUseAudit(setting_checks=checks[:1])}).model_dump())
    assert setting_classification_issues({"template_id": "world"}, {"changes": "第一卷15章，单章约2900字。"})
    assert not setting_classification_issues({"template_id": "world"}, {"changes": "城里开始拆迁，登记制度发生变化。"})
    assert not setting_classification_issues({"template_id": "constraint"}, {"changes": "第一卷15章，单章约2900字。"})


def test_role_context_counts_use_project_and_request_limit(tmp_path):
    from inkflow.context_status import role_context_status
    from inkflow.model_usage import UsageLedger
    from inkflow.utils import utc_now
    path = tmp_path / "usage.sqlite"
    ledger = UsageLedger(path)
    settings = Settings(model="local-test", context_soft_tokens=10000, context_hard_tokens=20000)
    attempt = ledger.begin(settings, request_id="context-test", model="local-test", role="writer",
        input_estimate=800, max_output_tokens=1000, input_bound=2000)
    ledger.settle(attempt, {"prompt_tokens": 1000, "completion_tokens": 100})
    with ledger._connect() as db:
        db.execute("UPDATE model_attempts SET novel_id=?,outcome='completed',started_at=? WHERE attempt_id=?", ("this-book", utc_now(), attempt))
    roles, error = role_context_status(settings, "this-book", path)
    writer = next(r for r in roles if r["role"] == "writer")
    assert not error and writer["input_kind"] == "reported" and writer["input_tokens"] == 1000
    assert writer["used_percent"] == 5 and writer["remaining_tokens"] == 19000
    assert writer["limit_source"] == "request_snapshot"
    with ledger._connect() as db:
        db.execute("UPDATE model_attempts SET diagnostic_json='{}' WHERE attempt_id=?", (attempt,))
    historical, _ = role_context_status(settings, "this-book", path)
    old = next(r for r in historical if r["role"] == "writer")
    assert old["input_tokens"] == 1000 and old["used_percent"] is None and old["remaining_tokens"] is None
    other, _ = role_context_status(settings, "other-book", path)
    assert all(r["input_tokens"] is None and r["request_state"] == "not_called" for r in other)
    path.write_bytes(b"broken local record")
    broken, error = role_context_status(settings, "this-book", path)
    assert error and all(r["input_tokens"] is None and r["request_state"] == "unknown" for r in broken)


def test_context_material_snapshot_keeps_role_and_rejects_changed_source(tmp_path):
    from inkflow.context import ContextBuilder
    from inkflow.context_status import read_context_material
    from inkflow.schemas import ContextPacket, ContextSection
    from inkflow.errors import ProjectError
    import json
    project = project_fixture(tmp_path)
    builder = ContextBuilder(project)
    packet = ContextPacket(project_id=project.project_id, chapter_no=11, task="写作", estimated_tokens=10,
        sections=[ContextSection(key="B", title="正史", content="可信的原文", source_ids=["chapter:00010"])])
    builder._publish_context_status(packet, 10, role="writer")
    first = json.loads((project.internal / "context-status-writer.json").read_text(encoding="utf-8"))
    summary = json.loads((project.internal / "context-status-writer-summary.json").read_text(encoding="utf-8"))
    assert summary["packet_hash"] == first["packet_hash"] and "material_contents" not in summary
    args = dict(role="writer", section_key="B", updated_at=first["updated_at"], packet_hash=first["packet_hash"])
    assert read_context_material(project, **args)["content"] == "可信的原文"
    packet.sections[0].content = "更新后的原文"
    builder._publish_context_status(packet, 10, role="writer")
    with pytest.raises(ProjectError):
        read_context_material(project, **args)

    (project.internal / "context-status-writer.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ProjectError):
        read_context_material(project, **args)


def test_writer_handoff_is_available_to_next_batch_chapter_only_after_review(tmp_path):
    from inkflow.writer_notes import version_notes, notes_hash, recent_approved_notes
    from inkflow.schemas import ReviewReport
    project = project_fixture(tmp_path)
    chapter = project.db.get_chapter(10)
    project.db.save_agent_artifact(artifact_type="writer_hook_note", run_id="handoff", role="writer", chapter_no=10,
        chapter_version=chapter["version"], status="current", data={"content_hash": chapter["content_hash"],
            "decision_summary": ["选择先找见证人"], "thread_changes": ["旧线索得到回应"], "new_fact_candidates": ["取得新回执"]})
    assert recent_approved_notes(project, 11) == []
    project.db.save_review(10, chapter["version"], ReviewReport(verdict="pass", confidence=1, summary="核对通过",
        source_hash=chapter["content_hash"], writer_notes_hash=notes_hash(version_notes(project, 10))), "reviews/handoff.md")
    assert recent_approved_notes(project, 11)[0]["important_decisions"] == ["选择先找见证人"]
    with project.db.connect() as db:
        db.execute("UPDATE chapters SET status='draft' WHERE chapter_no=10")
        db.commit()
    assert recent_approved_notes(project, 11) == []
    draft = {"chapter_no": 10, "content": (project.root / chapter["path"]).read_text(encoding="utf-8"), "memory_patch": {"chapter_summary": "已核摘要"}}
    assert recent_approved_notes(project, 11, [draft])[0]["thread_changes_to_check"] == ["旧线索得到回应"]
    assert recent_approved_notes(project, 11, [{**draft, "content": "变化后的正文"}]) == []


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


def test_new_collections_populate_only_when_requested_with_same_audit_owner(tmp_path, monkeypatch):
    import inkflow.setting_audit as audit
    from inkflow.story_settings import StorySettingsService
    project = project_fixture(tmp_path)
    engine = InkFlowEngine(ScriptedProvider([]), Settings())
    calls = []
    async def review(engine_arg, project_arg, **kwargs):
        calls.append(kwargs)
        return {"summary": "已核对具体记录", "adopted_settings": []}
    monkeypatch.setattr(audit, "audit_story_settings", review)
    definitions = [{"name": "证据物件", "category": "高级 / 物件", "instructions": "记录原件状态", "fields": [{"key": "state", "label": "状态"}]}]
    asyncio.run(engine.manage_story_settings(project.root, change={"operation": "collection_create_many", "collections": definitions}))
    assert calls == []
    result = asyncio.run(engine.manage_story_settings(project.root, change={"operation": "collection_create_many", "collections": definitions, "populate_records": True}, instruction="新增具体设定"))
    assert len(calls) == 1 and calls[0]["collection_names"] == ["证据物件"]
    assert calls[0]["new_record_limit"] == 1 and calls[0]["apply_rule_corrections"] is False
    assert len([item for item in StorySettingsService(project).list_collections() if item["name"] == "证据物件"]) == 1
    assert result["summary"] == "已核对具体记录"


def test_requested_specific_setting_is_not_complete_when_model_returns_only_template(tmp_path):
    from inkflow.story_settings import StorySettingsService
    project = project_fixture(tmp_path)
    service = StorySettingsService(project)
    service.save_collection(name="新证据", category="证据 / 物件", instructions="只记已有正文证据", fields=[{"key": "state", "label": "状态"}])
    provider = ScriptedProvider([StorySettingsAudit(summary="分类可以使用", findings=[], setting_updates=[])])
    engine = InkFlowEngine(provider, Settings())
    result = asyncio.run(engine.manage_story_settings(project.root, change={"operation": "audit", "collection_names": ["新证据"], "new_record_limit": 1, "apply_corrections": True}))
    assert result["status"] == "needs_evidence"
    assert any("新证据" in line for line in result["pending_settings"])
    assert result["adopted_settings"] == []
