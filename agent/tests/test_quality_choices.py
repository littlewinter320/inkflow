"""Source-bound author choices use the saved review, with no live model calls."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ValidationGateError
from inkflow.provider import ScriptedProvider
from inkflow.schemas import ReviewAssessment, ReviewFinding, ReviewReport, TerminalIntent
from inkflow.studio import StudioService
from inkflow.terminal_session import TerminalSession
from inkflow.conversation_scope import conversation_key
from inkflow.utils import content_hash


def fixture(tmp_path, preset="balanced", criterion="motivation", grade=2, evidence="他把纸条收好，却没说为什么要去。"):
    from setting_fixture import project_fixture, arc
    project = project_fixture(tmp_path)
    bundle = project.db.get_current_plan_bundle()
    project.db.save_plan_bundle(bundle.model_copy(update={"current_arc": arc(11, 15)}))
    text = "# 第11章\n\n他把纸条收好，却没说为什么要去。\n"
    path = "chapters/chapter_00011.draft.md"
    (project.root / path).write_text(text, encoding="utf-8")
    version = project.db.upsert_draft(11, "核对", path, text)
    report = ReviewReport(verdict="pass", confidence=.9, summary="核心结果成立，动机有明显薄弱处。",
        source_hash=content_hash(text), assessments=[ReviewAssessment(criterion=criterion, status="partial",
            grade=grade, chapter_evidence=evidence, reason="这个关键决定缺少人物自己的取舍，影响后续行动的理解。",
            alternative="也可理解为克制，但当前没有保留这种表达所需的动作依据。", evidence_relation="direct")])
    (project.root / "reviews/choice.md").write_text("同版审查", encoding="utf-8")
    project.db.save_review(11, version, report, "reviews/choice.md")
    engine = InkFlowEngine(ScriptedProvider([]), Settings(quality_preset=preset, manual_edit_review_enabled=False))
    return project, engine


@pytest.mark.parametrize("preset,criterion,grade,evidence,expected", [
    ("economy", "motivation", 2, "他把纸条收好，却没说为什么要去。", True),
    ("balanced", "progression", 2, "他把纸条收好，却没说为什么要去。", True),
    ("balanced", "readability", 2, "他把纸条收好，却没说为什么要去。", False),
    ("quality", "readability", 2, "他把纸条收好，却没说为什么要去。", True),
    ("quality", "readability", 3, "他把纸条收好，却没说为什么要去。", False),
    ("quality", "motivation", 2, "未出现的原文", False),
])
def test_only_substantial_current_evidence_requests_choice(tmp_path, preset, criterion, grade, evidence, expected):
    project, engine = fixture(tmp_path, preset, criterion, grade, evidence)
    with engine._task_scope(project.root, "write_review"):
        result = asyncio.run(engine._chapter_improvement_choice(project, 11, {"verdict": "pass"}, can_revise=True))
    assert bool(result) is expected
    if expected:
        assert result["status"] == "waiting_user" and len(result["questions"]) == 1
    assert not engine.provider.responses


def test_choice_keeps_original_scope_and_cannot_apply_after_source_change(tmp_path, monkeypatch):
    project, engine = fixture(tmp_path)
    with engine._task_scope(project.root, "write_review") as scope:
        choice = asyncio.run(engine._chapter_improvement_choice(project, 11, {"verdict": "pass"}, can_revise=True))
        card = choice["questions"][0]
        original = TerminalIntent(action="batch_draft", chapter_no=11, end_chapter_no=15,
            batch_id="batch-original", visible_reason="原批次", authorization="approved", forbidden_actions=["accept"])
        project.db.set_metadata(conversation_key("pending_terminal_question"), {"id": "question-original",
            "intent": original.model_dump(mode="json"), "questions": [card], "snapshot_task_id": scope.task_id,
            "run_id": "original-run", "task_id": "ticket-original"})
        answer = "按这些要点局部改一次"
        resumed = TerminalSession.pending_resume(project, answer)
        assert resumed["task_id"] == scope.task_id and resumed["run_id"] == "original-run"
        assert resumed["intent"]["end_chapter_no"] == 15 and resumed["intent"]["batch_id"] == "batch-original"
        assert resumed["intent"]["forbidden_actions"] == ["accept"]
        assert TerminalSession._improvement_answer(project, answer + "，顺便重写所有正文") is None
        key = card["quality_choice_key"]
        stored = project.db.get_metadata(key)
        project.db.set_metadata(key, {**stored, "decision": "approve"})
        approved = asyncio.run(engine._chapter_improvement_choice(project, 11, {"verdict": "pass"}, can_revise=True))
        assert approved["status"] == "approved"
        (project.root / "OUTLINE.md").write_text("后来修改的大纲", encoding="utf-8")
        with pytest.raises(ValidationGateError, match="失效"):
            asyncio.run(engine._chapter_improvement_choice(project, 11, {"verdict": "pass"}, can_revise=True))


def test_keep_valid_pass_adds_no_writer_or_reviewer_call(tmp_path):
    project, engine = fixture(tmp_path)
    reviewed = {"verdict": "pass", "trace_id": "original-review"}
    engine.current_pass_review = lambda *args, **kwargs: reviewed
    engine.review_chapter = AsyncMock(side_effect=AssertionError("不能重复审查"))
    engine.revise_chapter = AsyncMock(side_effect=AssertionError("不能额外改稿"))
    with engine._task_scope(project.root, "write_review") as scope:
        choice = asyncio.run(engine.review_and_repair(project.root, 11))
        key = choice["questions"][0]["quality_choice_key"]
        stored = project.db.get_metadata(key)
        project.db.set_metadata(key, {**stored, "decision": "keep"})
        result = asyncio.run(engine.review_and_repair(project.root, 11))
    assert result["verdict"] == "pass" and result["revision_rounds"] == 0
    engine.review_chapter.assert_not_called()
    engine.revise_chapter.assert_not_called()


def test_approved_edit_only_runs_once_and_interruption_cannot_repeat(tmp_path):
    project, engine = fixture(tmp_path)
    reviewed = {"verdict": "pass", "trace_id": "original-review"}
    engine.current_pass_review = lambda *args, **kwargs: reviewed
    engine.revise_chapter = AsyncMock(side_effect=RuntimeError("offline failed edit"))
    with engine._task_scope(project.root, "write_review"):
        choice = asyncio.run(engine.review_and_repair(project.root, 11))
        key = choice["questions"][0]["quality_choice_key"]
        project.db.set_metadata(key, {**project.db.get_metadata(key), "decision": "approve"})
        with pytest.raises(RuntimeError, match="offline failed edit") as failure:
            asyncio.run(engine.review_and_repair(project.root, 11))
        assert failure.value.recovery_exhausted is True and failure.value.recovery_attempts == 1
        result = asyncio.run(engine.review_and_repair(project.root, 11))
        recovery = asyncio.run(engine._native_recovery_attempts(project, "chapter.revision", 11))
    assert result["status"] == "needs_input" and recovery["attempts"] == 1
    assert engine.revise_chapter.await_count == 1


def test_pending_choice_prevents_accepting_a_pass_report(tmp_path):
    project, engine = fixture(tmp_path)
    session = TerminalSession(engine)
    engine.accept_chapter = AsyncMock(side_effect=AssertionError("待答不能验收"))
    report = {"verdict": "pass", "status": "waiting_user", "questions": [{"id": "chapter-improvement"}]}
    result = asyncio.run(session._conditionally_accept(project.root, 11, [], {"result": report}))
    assert result["status"] == "waiting_user"
    engine.accept_chapter.assert_not_called()


def test_approved_success_reuses_review_and_does_not_offer_another_polish(tmp_path):
    project, engine = fixture(tmp_path)
    original = {"verdict": "pass", "trace_id": "original-review"}
    engine.current_pass_review = lambda *args, **kwargs: original
    async def revise(*args, **kwargs):
        chapter = project.db.get_chapter(11)
        text = (project.root / chapter["path"]).read_text(encoding="utf-8").replace("却没说为什么要去", "想起昨日的承诺，决定走一趟")
        (project.root / chapter["path"]).write_text(text, encoding="utf-8")
        version = project.db.upsert_draft(11, "核对", chapter["path"], text)
        return {"version": version, "trace_id": "approved-local-edit"}
    async def review(*args, **kwargs):
        chapter = project.db.get_chapter(11)
        previous = project.db.latest_review_record(11)["report"]
        project.db.save_review(11, chapter["version"], previous.model_copy(update={"source_hash": chapter["content_hash"]}), "reviews/choice.md")
        return {"verdict": "pass", "trace_id": "review-after-edit"}
    engine.revise_chapter = AsyncMock(side_effect=revise)
    engine.review_chapter = AsyncMock(side_effect=review)
    with engine._task_scope(project.root, "write_review"):
        choice = asyncio.run(engine.review_and_repair(project.root, 11))
        key = choice["questions"][0]["quality_choice_key"]
        project.db.set_metadata(key, {**project.db.get_metadata(key), "decision": "approve"})
        result = asyncio.run(engine.review_and_repair(project.root, 11))
        later = asyncio.run(engine.review_and_repair(project.root, 11))
    assert result["verdict"] == later["verdict"] == "pass"
    assert result["revision_rounds"] == later["revision_rounds"] == 1
    assert engine.revise_chapter.await_count == engine.review_chapter.await_count == 1


def test_batch_choice_stops_before_memory_and_keeps_requested_range(tmp_path):
    project, engine = fixture(tmp_path)
    engine.ensure_chapter_plan = AsyncMock()
    engine.review_and_repair = AsyncMock(return_value={"verdict": "pass", "status": "waiting_user",
        "revision_rounds": 0, "trace_id": "original-review", "reason": "先商量本章动机",
        "questions": [{"id": "chapter-improvement"}]})
    engine._extract_memory_patch = AsyncMock(side_effect=AssertionError("待答不能暂存并继续后章"))
    engine.write_chapter = AsyncMock(side_effect=AssertionError("已有11章；不应越过待答写12章"))
    result = asyncio.run(engine.draft_batch(project.root, 11, 12))
    assert result["status"] == "waiting_user" and result["questions"]
    assert result["chapter_range"] == [11, 12]
    assert result["stopped_at_chapter"] == 11
    engine._extract_memory_patch.assert_not_called()
    engine.write_chapter.assert_not_called()


def test_complex_preparation_is_one_source_bound_choice_before_any_production(tmp_path):
    from inkflow.provider import ProviderResult
    from inkflow.task_settings import use_task_settings
    project, engine = fixture(tmp_path, "quality")
    session = TerminalSession(engine)
    database = StudioService(project).db
    database.start_task("original-preparation", owner_id="offline", method="terminal.handle", params={})
    scope = database.prepare_task_settings("original-preparation", novel_id=project.project_id,
        settings=engine.settings, workspace_root=project.root, registered_now=True)
    routed = TerminalIntent(action="write_review", chapter_no=11, authorization="approved",
        visible_reason="推进本章转折", planning_discussion="本章要让人物改变立场，先商量他愿意付出的代价吗？")
    session._route_packet = AsyncMock(return_value=ProviderResult(data=routed, model="offline", response_id=None,
        reasoning_content=None, usage={}))
    session._dispatch = AsyncMock(return_value={"reply": "只验证入口，本例未生成正文"})
    with use_task_settings(scope):
        result = asyncio.run(session.handle(project.root, "照这个转折推进第11章", opened_project=project))
        assert result["status"] == "waiting_user"
        pending = session._pending_question(project)
        assert pending["questions"][0]["id"] == "complex-preparation"
        session._dispatch.assert_not_called()
        resume = session.pending_resume(project, "按现有规划直接写作")
        assert resume["task_id"] == scope.task_id and resume["run_id"] == "original-preparation"
        continued = asyncio.run(session.handle(project.root, "按现有规划直接写作", opened_project=project))
    assert continued["status"] == "completed"
    assert session._dispatch.await_count == session._route_packet.await_count == 1


def test_mimo_credentials_are_rejected_before_dialogue_or_trace(tmp_path):
    from inkflow.terminal_session import _SENSITIVE_PATTERN
    for prefix in ("sk", "tp", "ttp"):
        assert _SENSITIVE_PATTERN.search(prefix + "-" + "offline_example_1234567890")
