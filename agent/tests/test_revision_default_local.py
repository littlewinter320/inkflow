"""Ordinary review fixes preserve every character outside exact replacements."""
import asyncio
import json

import pytest

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.errors import ValidationGateError
from inkflow.project import InkFlowProject
from inkflow.provider import ScriptedProvider
from inkflow.schemas import (
    AcceptedContinuityPatch, BookBrief, DraftOutput, HookNote, PlanBundle,
    ReviewFinding, ReviewReport, SceneBlueprintItem, SettingRecordProposal,
)
from inkflow.utils import content_hash, json_dumps
from inkflow.writer_notes import annotation_gaps, version_notes
from setting_fixture import arc


def test_review_revision_defaults_to_exact_patch_and_keeps_public_notes(tmp_path, monkeypatch):
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    target = "他把钥匙交给了王叔。"
    replacement = "他把钥匙交给了李叔。"
    untouched = "他把旧收据压在玻璃下面，等王叔看清上面的日期。"
    source = ("# 第 1 章 旧钥匙\n\n雨还没有停，他收起柜台上的工具，给门外的人让出位置。"
              "那人没有急着开口，只指了指挂在墙上的旧钟。\n\n" + target + "\n\n" + untouched
              + "\n\n街口的灯亮了，雨水沿着门框往下淌。他没有再问，走回柜台里，把剩下的材料一张张摆好。"
                "这件事还没有结论，他只能先保存手里已经取得的东西，等对方说完再决定下一步。\n")

    def prepare(name, response):
        root = tmp_path / name
        brief = BookBrief(title="局部修订", genre="现实悬疑", protagonist="修锁匠",
                          premise="修锁匠核对街坊留下的旧收据与钥匙。")
        project = InkFlowProject.create(root, brief)
        current_arc = arc(1, 1)
        project.db.save_plan_bundle(PlanBundle.model_validate({
            "book": dict(title=brief.title, premise=brief.premise, reader_promise="事实有据",
                narrative_engine="核对旧物", main_conflict="线索不足", protagonist_start="独处",
                protagonist_end="查明缘由", theme_question="为何保存", ending_direction="厘清疑问",
                estimated_chapters=10, estimated_volumes=1,
                volume_compass=[dict(volume_no=1, title="旧物", promise="核对线索",
                    start_state="线索不足", end_state="疑问厘清", estimated_chapters=10)]),
            "current_volume": dict(volume_no=1, title="旧物", chapter_start=1, chapter_end=10,
                promise="核对线索", start_state="线索不足", end_state="疑问厘清",
                antagonist_pressure="信息不全", midpoint_turn="取得信任", climax="核实结果",
                cost_and_result="付出时间", next_volume_bridge="保留余韵",
                arcs=[dict(arc_id=current_arc.arc_id, title=current_arc.title, chapter_start=1,
                    chapter_end=1, promise=current_arc.promise, end_state=current_arc.end_state)]),
            "current_arc": current_arc.model_dump(mode="json"),
        }))
        relative = "chapters/chapter_00001.draft.md"
        (root / relative).write_text(source, encoding="utf-8")
        version = project.db.upsert_draft(1, "旧钥匙", relative, source)
        report = ReviewReport(verdict="patch", confidence=.9, source_hash=content_hash(source),
            summary="钥匙接收者写错，其他内容保留。", findings=[ReviewFinding(
                category="character", severity="major", evidence=target,
                explanation="这次交接对象应为李叔。", repair_instruction="仅纠正接收者姓名。",
                verification_status="anchored", semantic_status="supported")])
        project.db.save_review(1, version, report, "reviews/baseline.md")
        provider = ScriptedProvider([response])
        engine = InkFlowEngine(provider, Settings(workspace_root=root))
        old = DraftOutput(title="旧钥匙", content=source,
            hook_note=HookNote(actual_anchor=untouched, annotations=[dict(kind="new_fact",
                statement="收据已供对方查看，后续解释仍待核对。", quote=untouched)]),
            new_fact_candidates=["对方已经看过收据。"], thread_changes=["收据线索继续推进。"],
            scene_blueprint=[SceneBlueprintItem(scene="柜台交接", entry_state="等待核对",
                character_goal="确认日期", new_pressure="对方迟疑", key_choice="出示收据",
                exit_change="双方看过记录")],
            setting_updates=[SettingRecordProposal(collection_id="old-proposal", title="收据设想",
                values={"描述": "收据交接"}, evidence_refs=[dict(source_kind="draft", chapter_no=1,
                    quote=untouched, source_version=version, source_hash=content_hash(source))])])
        hook = engine._save_hook_note(project, chapter_no=1, chapter_version=version,
            run_id="original-writer", draft=old, card=project.db.get_chapter_card(1))
        engine._save_scene_blueprint(project, chapter_no=1, chapter_version=version,
            run_id="original-writer", draft=old)
        return project, engine, provider, hook

    patch = AcceptedContinuityPatch(edits=[dict(target_excerpt=target, replacement=replacement)],
                                    reason="仅纠正钥匙接收者，收据及其他段落保留。")
    project, engine, provider, old_hook = prepare("ordinary", patch)
    result = asyncio.run(engine.revise_chapter(project.root, 1, "按审查意见修改"))
    actual = (project.root / "chapters/chapter_00001.draft.md").read_text(encoding="utf-8")
    assert actual == source.replace(target, replacement)
    assert provider.calls[0]["output_model"] == "AcceptedContinuityPatch" and len(provider.calls) == 1
    prompt = provider.calls[0]["user_prompt"]
    assert target in prompt and "这次交接对象应为李叔。" in prompt and "仅纠正接收者姓名。" in prompt
    assert project.db.get_chapter(1)["status"] == "draft"
    assert project.db.latest_review_record(1)["chapter_version"] == 1
    assert result["next_action"] == "重新审查当前版本" and result["version"] == 2
    notes = version_notes(project, 1)
    assert notes["hook_note"]["annotations"] == old_hook["data"]["annotations"]
    assert notes["hook_note"]["new_fact_candidates"] == old_hook["data"]["new_fact_candidates"]
    assert notes["hook_note"]["thread_changes"] == old_hook["data"]["thread_changes"]
    assert notes["scene_blueprint"]["scenes"][0]["scene"] == "柜台交接"
    assert notes["hook_note"]["setting_updates"] == []  # The old proposal keeps its source version.
    assert not annotation_gaps(notes["hook_note"], actual)
    assert project.db.list_agent_artifacts(chapter_no=1, artifact_type="writer_hook_note")[-1]["data"] == old_hook["data"]
    events = [json.loads(line) for line in
              (project.internal / "runs" / result["trace_id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    inherited = next(item["metadata"] for item in events if item["stage"] == "writer.notes.inherited")
    assert inherited["source_content_hash"] == content_hash(source) and inherited["inherited_from_version"] == 1
    assert inherited["content_hash"] == content_hash(actual) and inherited["setting_proposals_not_rebound"] == 1

    for label, edits, error in (
        ("missing", [dict(target_excerpt="原稿没有这句话。", replacement="不能凭空修改。")], "唯一定位"),
        ("overlap", [dict(target_excerpt=target, replacement=replacement),
                     dict(target_excerpt="钥匙交给了王叔", replacement="钥匙交给了李叔")], "互相重叠"),
    ):
        invalid, invalid_engine, invalid_provider, _ = prepare(label,
            AcceptedContinuityPatch(edits=edits, reason="尝试修正指定交接句。"))
        with pytest.raises(ValidationGateError, match=error):
            asyncio.run(invalid_engine.revise_chapter(invalid.root, 1, "按审查意见修改"))
        assert (invalid.root / "chapters/chapter_00001.draft.md").read_text(encoding="utf-8") == source
        assert invalid.db.get_chapter(1)["version"] == 1
        assert invalid_provider.calls[0]["output_model"] == "AcceptedContinuityPatch"

    full_draft = DraftOutput(title="新计划", content="核对收据后，他沿着原先留下的线索继续向前。" * 12)
    for label, explicit in (("plan", None), ("explicit-full", False)):
        planned, plan_engine, plan_provider, _ = prepare(label, full_draft)
        if explicit is None:
            planned.db.set_metadata("pending_plan_revision:1", dict(status="needs_writer", revision_id="authorized-plan",
                draft_version=1, draft_hash=content_hash(source),
                card_hash=content_hash(json_dumps(planned.db.get_chapter_card(1))), instruction="按已采用新计划承接。"))
        async def no_length_call(draft, content, **kwargs):
            return draft, content
        monkeypatch.setattr(plan_engine, "_repair_short_draft", no_length_call)
        revised = asyncio.run(plan_engine.revise_chapter(planned.root, 1, "按审查意见修改", local_only=explicit))
        assert plan_provider.calls[0]["output_model"] == "DraftOutput" and revised["version"] == 2
        assert planned.db.get_chapter(1)["status"] == "draft"
        if explicit is None:
            assert planned.db.get_metadata("pending_plan_revision:1")["status"] == "awaiting_review"
