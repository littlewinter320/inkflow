"""Offline checks of batch acceptance receipts; no remote model requests."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import inkflow.engine as engine_module
from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.provider import ScriptedProvider
from inkflow.review_rubric import SCORING_VERSION, EVIDENCE_POLICY_VERSION
from inkflow.schemas import ReviewReport


@pytest.mark.parametrize("change", ["none", "settings", "accepted_summary", "missing_receipt", "other_snapshot", "visible_during_tail", "planning"])
def test_batch_source_receipts_preserve_only_proven_local_acceptance(tmp_path, monkeypatch, change):
    from setting_fixture import project_fixture, arc
    from inkflow.errors import ValidationGateError
    from inkflow.schemas import MemoryPatch, SettingRecordProposal
    from inkflow.story_settings import StorySettingsService
    from inkflow.writer_notes import version_notes, notes_hash
    project = project_fixture(tmp_path)
    bundle = project.db.get_current_plan_bundle()
    project.db.save_plan_bundle(bundle.model_copy(update={"current_arc": arc(11, 15)}))
    collection = StorySettingsService(project).save_collection(name="地点", template_id="location")
    provider = ScriptedProvider([])
    engine = InkFlowEngine(provider, Settings(manual_edit_review_enabled=False))
    monkeypatch.setattr(engine_module, "_deterministic_audit", lambda *args, **kwargs: ({}, []))
    entries = []
    patches = {}
    for number in (11, 12, 13):
        content = f"# 第{number}章\n\n此前已核对的事实。\n"
        path = f"chapters/chapter_{number:05d}.draft.md"
        (project.root / path).write_text(content, encoding="utf-8", newline="")
        project.db.upsert_draft(number, "核对线索", path, content)
        chapter = project.db.get_chapter(number)
        patches[number] = MemoryPatch(chapter_no=number, chapter_summary="已核对事实", setting_updates=[
            SettingRecordProposal(collection_id=collection["collection_id"], title=f"地点{number}",
                values={"description": "此前已核对的事实。"}, epistemic_status="objective",
                evidence_refs=[{"source_kind": "draft", "chapter_no": number, "quote": "此前已核对的事实。"}])])
        project.db.save_provisional_memory_patch("batch-proof", number, chapter["version"], content, patches[number])
        entries.append({"chapter_no": number, "version": chapter["version"], "title": chapter["title"],
            "draft_path": path, "review_path": f"reviews/proof-{number}.md", "revision_rounds": 0, "verdict": "pass"})
    with engine._task_scope(project.root, "batch_accept") as scope:
        for entry in entries:
            number = entry["chapter_no"]
            chapter = project.db.get_chapter(number)
            report = ReviewReport(verdict="pass", confidence=1, summary="离线同源通过",
                source_hash=chapter["content_hash"], writer_notes_hash=notes_hash(version_notes(project, number)),
                scoring_version=SCORING_VERSION, evidence_policy_version=EVIDENCE_POLICY_VERSION,
                memory_patch=patches[number], source_comparisons=[dict(source_id=source, source_evidence="此前已核对的事实。",
                    chapter_evidence="此前已核对的事实。", relation="aligned", reason="离线通过来源")
                    for source in ("OUTLINE.md", "chapter:00010")])
            (project.root / entry["review_path"]).write_text("离线同源报告", encoding="utf-8")
            project.db.save_mode_review(number, chapter["version"], report, entry["review_path"], run_id="offline",
                primary_role="editor", bundle={"mode": "everyday", "snapshot_hash": scope.snapshot_hash,
                    "story_fingerprint": engine._writer_source_fingerprint(project, number),
                    "coverage": [{"check_id": check, "owner": owner, "status": "passed"}
                                 for check, owner in engine_module.check_owners_for_mode("everyday").items()]})
        manifest = {"batch_id": "batch-proof", "status": "accepting", "start_chapter_no": 11,
            "end_chapter_no": 13, "chapters": entries, "static_basis_hash": engine._batch_static_basis(project, 11, 13)}
        engine._save_batch_manifest(project, manifest)
        if change == "visible_during_tail":
            original_tail = engine._complete_acceptance_tail
            def edit_outside_app(*args, **kwargs):
                result = original_tail(*args, **kwargs)
                path = project.root / entries[1]["draft_path"]
                path.write_text(path.read_text(encoding="utf-8") + "外部编辑器的修改", encoding="utf-8", newline="")
                return result
            monkeypatch.setattr(engine, "_complete_acceptance_tail", edit_outside_app)
        first = asyncio.run(engine.accept_chapter(project.root, 11, _prepared_patch=patches[11], _provisional_batch_id="batch-proof"))
        assert first["status"] == "accepted"
        key = f"workflow_accept:{scope.task_id}:11"
        receipt = project.db.get_metadata(key)
        if change == "visible_during_tail":
            assert first["post_commit_status"] == "waiting_condition" and first["warnings"]
            assert "batch_source_transition" not in receipt
        else:
            assert not first["warnings"]
            assert set(receipt["batch_source_transition"]["sources"]) == {"12", "13"}
            assert all(proof["before"] != proof["after"] for proof in receipt["batch_source_transition"]["sources"].values())
        if change == "settings":
            stored = StorySettingsService(project).records(collection["collection_id"])[0]
            StorySettingsService(project).save_record(collection["collection_id"], record_id=stored["record_id"],
                expected_revision=stored["revision"], title=stored["title"], values={"description": "用户另行修改"}, actor="user")
        elif change == "accepted_summary":
            with project.db.connect() as connection:
                connection.execute("UPDATE chapters SET summary='另一任务修改了旧章摘要' WHERE chapter_no=1")
                connection.commit()
        elif change == "planning":
            (project.root / "OUTLINE.md").write_text("用户修改了当前全书方向。", encoding="utf-8")
        elif change in {"missing_receipt", "other_snapshot"}:
            if change == "missing_receipt":
                receipt.pop("batch_source_transition")
            else:
                receipt["batch_source_transition"]["snapshot_hash"] = "other-snapshot"
            project.db.set_metadata(key, receipt)
        if change == "none":
            result = asyncio.run(engine._accept_batch(project.root, "batch-proof"))
            assert result["status"] == "accepted"
            assert project.db.latest_accepted_chapter_no() == 13
            assert len(StorySettingsService(project).records(collection["collection_id"])) == 3
        else:
            with pytest.raises(ValidationGateError, match="设定或计划已变化" if change == "planning" else "可信批次推进记录"):
                asyncio.run(engine._accept_batch(project.root, "batch-proof"))
            current = engine._load_batch_manifest(project, "batch-proof")
            assert current["remaining_review_range"] == [12, 13]
            assert current["status"] == "needs_revision"
            assert project.db.latest_accepted_chapter_no() == 11
            assert project.db.get_chapter(12)["status"] == "draft"
            if change == "missing_receipt":
                async def refresh_review(root, number, **kwargs):
                    reviewed = project.db.latest_review_record(number)
                    project.db.save_mode_review(number, reviewed["chapter_version"], reviewed["report"], reviewed["path"],
                        run_id="offline-recheck", primary_role="editor", bundle={**reviewed["mode_bundle"],
                            "story_fingerprint": engine._writer_source_fingerprint(project, number)})
                    return {"verdict": "pass", "trace_id": "offline-recheck", "review_path": str(project.root / reviewed["path"])}
                engine._review_batch_chapter = AsyncMock(side_effect=refresh_review)
                engine.current_pass_review = lambda *args, **kwargs: None
                engine.revise_chapter = AsyncMock(side_effect=AssertionError("不能重写现有正文"))
                repaired = asyncio.run(engine._repair_batch(project.root, "batch-proof", review_only=True))
                assert repaired["status"] == "ready_for_acceptance"
                assert [call.args[1] for call in engine._review_batch_chapter.call_args_list] == [12, 13]
                resumed = asyncio.run(engine._accept_batch(project.root, "batch-proof"))
                assert resumed["status"] == "accepted" and project.db.latest_accepted_chapter_no() == 13
                engine.revise_chapter.assert_not_called()
        assert not provider.calls
