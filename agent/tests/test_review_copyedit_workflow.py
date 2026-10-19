"""Review suggestions replace one Writer call, then require fresh review and memory."""
import asyncio

import pytest

from inkflow.engine import InkFlowEngine, _review_copyedit_patch
from inkflow.errors import ValidationGateError
from inkflow.schemas import DraftOutput, HookNote, ReviewCopyEdit, ReviewFinding, ReviewReport
from inkflow.role_protocol import check_owners_for_mode
from inkflow.task_settings import use_task_settings
from inkflow.utils import content_hash
from test_mode_review import PassingProvider, make_project, mode_scope, review_packet


TARGET = "他把信封放会抽屉。"
REPLACEMENT = "他把信封放回抽屉。"


def copyedit():
    return ReviewCopyEdit(target_excerpt=TARGET, replacement=REPLACEMENT,
        kind="typo", reason="误用同音字，动作和意思保持。", meaning_preserved=True, author_role="editor")


def finding():
    return ReviewFinding(category="style", severity="minor", evidence=TARGET,
        explanation="放会为误字。", repair_instruction="只改会为回。", verification_status="anchored")


def test_copyedit_requires_small_located_agreed_change_and_matching_source():
    source = "未改的前文。" * 8 + TARGET + "未改的后文。" * 8
    report = ReviewReport(verdict="pass", confidence=.9, summary="仅误字。", source_hash=content_hash(source),
                          findings=[finding()], copyedits=[copyedit()])
    assert _review_copyedit_patch(report, source).edits[0].replacement == REPLACEMENT
    assert _review_copyedit_patch(report, source + "后来改文。") is None
    for invalid in (copyedit().model_copy(update={"meaning_preserved": False}),
                    copyedit().model_copy(update={"author_role": None}),
                    copyedit().model_copy(update={"target_excerpt": "不存在的原句。"}),
                    copyedit().model_copy(update={"replacement": "他把2封信放回抽屉。"})):
        assert _review_copyedit_patch(report.model_copy(update={"copyedits": [invalid]}), source) is None
    assert _review_copyedit_patch(report.model_copy(update={"verdict": "unknown"}), source) is None
    assert _review_copyedit_patch(report.model_copy(update={"missing_source_ids": ["chapter:00001"]}), source) is None
    assert _review_copyedit_patch(report.model_copy(update={"findings": [finding().model_copy(update={"severity": "major"})]}), source) is None
    conflict = copyedit().model_copy(update={"replacement": "他把信封搁回抽屉。", "author_role": "reviewer"})
    assert _review_copyedit_patch(report.model_copy(update={"copyedits": [copyedit(), conflict]}), source) is None
    overlap = copyedit().model_copy(update={"target_excerpt": "信封放会抽屉", "replacement": "信封放回抽屉"})
    assert _review_copyedit_patch(report.model_copy(update={"copyedits": [copyedit(), overlap]}), source) is None
    for original, revised in (("他没有拿走那封信。", "他拿走了那封信。"), ("他拿走了那封信。", "他没有拿走那封信。")):
        negative_source = source.replace(TARGET, original)
        negative_report = report.model_copy(update={"source_hash": content_hash(negative_source),
            "findings": [finding().model_copy(update={"evidence": original})],
            "copyedits": [copyedit().model_copy(update={"target_excerpt": original, "replacement": revised})]})
        assert _review_copyedit_patch(negative_report, negative_source) is None


@pytest.mark.parametrize("mode", ["everyday", "review_boost", "memory_boost"])
def test_review_only_proposes_then_authorized_repair_rechecks_new_prose_and_memory(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    source = ("# 第 1 章 旧信\n\n他沿着老街找到一封信，反复辨认信封上的字迹。想到失踪的朋友，他决定沿线继续追查。\n\n"
              + "他先核对已经抄好的地址，再把门口的工具放到柜台下，没有另作打算。" * 4 + "\n\n" + TARGET + "\n")
    project = make_project(tmp_path, source, 260)
    _, scope = mode_scope(project, mode)
    memory_role = check_owners_for_mode(mode)["memory"]
    proposer = "reviewer" if mode == "review_boost" else "editor"
    monkeypatch.setattr("inkflow.engine._deterministic_audit", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(InkFlowEngine, "_context_builder", lambda self, project, role="writer":
        SimpleNamespace(build=lambda chapter_no, task, **kwargs: review_packet(project, chapter_no, task)))

    class CopyeditingProvider(PassingProvider):
        def __init__(self):
            super().__init__(memory_role=memory_role)
            self.inputs = []

        async def generate_json(self, **kwargs):
            assert kwargs["agent_role"] in {"editor", "reviewer", memory_role}, "A micro edit must not invoke Writer."
            self.inputs.append(kwargs["user_prompt"])
            result = await super().generate_json(**kwargs)
            if kwargs["agent_role"] == proposer and TARGET in kwargs["user_prompt"].split("# 当前正文", 1)[-1].split("# 唯一分配", 1)[0]:
                result.data.copyedits = [copyedit().model_copy(update={"author_role": "editor" if proposer == "reviewer" else "reviewer"})]
                result.data.findings = [finding()]
            return result

    provider = CopyeditingProvider()
    engine = InkFlowEngine(provider, scope.settings)
    if mode == "everyday":
        engine._save_hook_note(project, chapter_no=1, chapter_version=1, run_id="original-writer",
            draft=DraftOutput(title="旧信", content=source, hook_note=HookNote(annotations=[dict(
                kind="new_fact", statement="信封已经收进抽屉。", quote=TARGET)])), card=project.db.get_chapter_card(1))
    with use_task_settings(scope):
        first = asyncio.run(engine.review_chapter_mode(project.root, 1, mode=mode))
        assert first["verdict"] == "pass" and first["copyedits"][0]["author_role"] == proposer
        assert (project.root / "chapters/chapter_00001.draft.md").read_text(encoding="utf-8") == source
        expected_roles = {"editor"} if mode == "everyday" else {"editor", "reviewer"} if mode == "review_boost" else {"editor", "memory_keeper"}
        assert project.db.get_chapter(1)["version"] == 1 and len(provider.calls) == len(expected_roles)
        original_review = project.db.latest_review_record(1)
        if mode == "everyday":
            with monkeypatch.context() as changed:
                changed.setattr("inkflow.engine._review_settings_fingerprint", lambda _: "another-configuration")
                with pytest.raises(ValidationGateError, match="原审查配置或分工已变化"):
                    asyncio.run(engine.revise_chapter(project.root, 1, "采用同次审查微改", review_copyedits=True))
            with monkeypatch.context() as changed:
                changed.setattr(InkFlowEngine, "_writer_source_fingerprint", staticmethod(lambda *_: "another-story-version"))
                with pytest.raises(ValidationGateError, match="原审查配置或分工已变化"):
                    asyncio.run(engine.revise_chapter(project.root, 1, "采用同次审查微改", review_copyedits=True))
            assert project.db.get_chapter(1)["version"] == 1 and len(provider.calls) == 1
        repaired = asyncio.run(engine.review_and_repair(project.root, 1))
    actual = (project.root / "chapters/chapter_00001.draft.md").read_text(encoding="utf-8")
    assert actual == source.replace(TARGET, REPLACEMENT)
    assert project.db.get_chapter(1)["status"] == "draft" and project.db.get_chapter(1)["version"] == 2
    assert all(provider.calls.count(role) == 2 for role in expected_roles) and len(provider.calls) == 2 * len(expected_roles)
    assert REPLACEMENT in provider.inputs[-1]
    assert repaired["verdict"] == "pass" and repaired["revision_rounds"] == 1
    latest = project.db.latest_review_record(1)
    assert latest["id"] != original_review["id"] and latest["chapter_version"] == 2
    assert latest["report"].source_hash == content_hash(actual) and latest["report"].memory_patch is not None
    assert original_review["report"].source_hash == content_hash(source)
    notes = project.db.list_agent_artifacts(chapter_no=1, artifact_type="writer_hook_note")
    assert notes[0]["role"] == proposer
    if mode == "everyday":
        assert notes[0]["data"]["annotations"][0]["quote"] == REPLACEMENT
        assert notes[-1]["data"]["annotations"][0]["quote"] == TARGET
    recoveries = [item for item in project.db.list_agent_artifacts(chapter_no=1) if item["artifact_type"] == "writer_context_manifest"]
    assert recoveries == [], "A code-applied review edit is not a new Writer model input."


def test_conflicting_fifth_suggestion_is_detected_before_four_item_limit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    targets = [TARGET, "他低低声应了一下。", "柜台上摆摆着纸盒。", "她又再次点了点头。"]
    replacements = [REPLACEMENT, "他低声应了一下。", "柜台上摆着纸盒。", "她再次点了点头。"]
    source = ("他沿着老街找到一封信，反复辨认信封上的字迹。想到失踪的朋友，他决定沿线继续追查。\n\n"
              + "\n".join(targets) + "\n")
    project = make_project(tmp_path, source, 120)
    _, scope = mode_scope(project, "review_boost")
    monkeypatch.setattr("inkflow.engine._deterministic_audit", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(InkFlowEngine, "_context_builder", lambda self, project, role="writer":
        SimpleNamespace(build=lambda chapter_no, task, **kwargs: review_packet(project, chapter_no, task)))

    class ConflictingProvider(PassingProvider):
        async def generate_json(self, **kwargs):
            result = await super().generate_json(**kwargs)
            if kwargs["agent_role"] == "editor":
                result.data.copyedits = [ReviewCopyEdit(target_excerpt=target, replacement=replacement,
                    kind="typo", reason="同次指出的微小文字问题。", meaning_preserved=True)
                    for target, replacement in zip(targets, replacements)]
                result.data.findings = [finding().model_copy(update={"evidence": target}) for target in targets]
            else:
                result.data.copyedits = [copyedit().model_copy(update={"replacement": "他将信封放回抽屉。"})]
                result.data.findings = [finding()]
            return result

    provider = ConflictingProvider(memory_role="editor")
    with use_task_settings(scope):
        result = asyncio.run(InkFlowEngine(provider, scope.settings).review_chapter_mode(project.root, 1, mode="review_boost"))
    assert result["verdict"] == "pass" and result["copyedits"] == [] and "分歧或重叠" in result["summary"]
    assert len(result["findings"]) >= 4 and set(provider.calls) == {"editor", "reviewer"}
    assert project.db.get_chapter(1)["version"] == 1
    candidates = project.db.list_agent_artifacts(chapter_no=1, artifact_type="mode_check_candidate")
    assert sorted(len(item["data"]["output"]["copyedits"]) for item in candidates) == [1, 4]
