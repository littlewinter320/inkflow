"""Merge two explicitly selected reference records before archiving the duplicate."""
from __future__ import annotations

from .errors import ValidationGateError
from .project_lock import project_write_lock
from .role_protocol import check_owners_for_mode
from .schemas import SettingRecordSet
from .story_settings import StorySettingsService, has_setting_content
from .task_settings import active_task_settings
from .trace import TraceRecorder
from .utils import estimate_tokens, json_dumps


async def merge_story_setting_records(engine, project, *, change, instruction):
    service = StorySettingsService(project)
    scope = active_task_settings.get()
    role = check_owners_for_mode(scope.effective_collaboration_mode if scope else "everyday")["memory"]
    if any(not isinstance(change.get(key), str) or not change[key].strip()
            for key in ("keep_record_id", "archive_record_id")):
        raise ValidationGateError("合并须明确保留与留档的两条记录编号，未改动已有资料。")
    keep = service._get(change.get("keep_record_id"), "record")
    other = service._get(change.get("archive_record_id"), "record")
    if not keep or not other or keep["record_id"] == other["record_id"]:
        raise ValidationGateError("合并须明确两条不同的当前设定记录，以及保留和留档的方向。")
    collection = service._get(keep["collection_id"], "collection")
    if (not collection or collection["status"] != "active"
            or other["collection_id"] != keep["collection_id"]
            or change.get("collection_id", keep["collection_id"]) != keep["collection_id"]
            or keep["status"] == "archived" or other["status"] == "archived"):
        raise ValidationGateError("只合并同一有效合集中的两条在用记录，未改动已有资料。")
    for record, key in ((keep, "keep_expected_revision"), (other, "archive_expected_revision")):
        if type(change.get(key)) is not int or change[key] != record["revision"]:
            raise ValidationGateError("待合并记录的修订号已经变化，请读取当前两条记录后继续，未凭旧选择留档。")
    if (role not in collection["read_roles"] or role not in collection["write_roles"]
            or not collection.get("auto_record", True)):
        raise ValidationGateError("当前记忆责任角色没有这份设定的读写许可，或自动记录已关闭，未执行合并。")
    before = service.source_fingerprint()
    story_before = engine._planning_source_fingerprint(project, 1, max(1, project.db.latest_accepted_chapter_no() + 1))
    original_refs, windows = [], []
    for record in (keep, other):
        for ref in service._evidence(record["evidence_refs"]):
            if ref in original_refs:
                continue
            original_refs.append(ref)
            content, _ = service._source(ref)
            start = content.index(ref["quote"])
            windows.append({"source": ref, "text": content[max(0, start - 600):start + len(ref["quote"]) + 600]})
    prompt = json_dumps({"用户要求": instruction,
        "合集": {key: collection[key] for key in ("collection_id", "revision", "name", "instructions", "fields")},
        "保留记录": {key: keep[key] for key in ("record_id", "revision", "title", "values", "epistemic_status", "evidence_refs")},
        "待留档记录": {key: other[key] for key in ("record_id", "revision", "title", "values", "epistemic_status", "evidence_refs")},
        "实际来源窗口": windows})
    output_budget = engine.settings.effective_output_tokens(5000, role)
    if estimate_tokens(prompt) + output_budget > engine.settings.context_budget_for(role)[1]:
        raise ValidationGateError("两条设定及来源超过当前记忆角色的容量，尚未合并或留档。")
    trace = TraceRecorder(project.root, "setting-merge", engine.settings.trace_level)
    saved = None
    verified = False
    try:
        trace.record("settings.merge", "started", "当前记忆角色核对两条设定的共同内容、独有信息与原文依据",
            metadata={"agent_role": role, "keep_record_id": keep["record_id"], "archive_record_id": other["record_id"]})
        trace.record("settings.merge.sources", "completed", "固定合并来源与原任务配置",
            metadata={"task_settings": scope.public_summary() if scope else None, "source_fingerprint": before,
                "story_source_fingerprint": story_before, "keep_revision": keep["revision"], "archive_revision": other["revision"]}, public=False)
        result = await engine.provider.generate_json(system_prompt=(
            "你是本任务的记忆责任角色，合并用户明确选择的两条设定参考，不写正文、不改正史。"
            "阅读双方字段和实际来源窗口，保留双方独有的有用信息，去除真正重复的表述；不因修订号较高就丢弃另一条内容。"
            "返回恰好一条已核对的合并候选，collection_id、record_id、title和expected_revision沿保留记录。"
            "values须包含双方所有有内容的字段，内容有冲突则按原文说明时间或认知差异，不暗中选边。"
            "事实、信念、传闻和设想保持原有边界，不把未核设想改成事实。evidence_refs仅摘取实际来源窗口的连续原句。"
            "同次返回代表你已核对合并内容及语义支持，仅认证参考，不代表正史接受。"
            "没有证据支持或无法保留重要差异时records返回空数组，保留两条等待作者处理；不得自行留档。"),
            user_prompt=prompt, output_model=SettingRecordSet, max_tokens=output_budget, thinking=False, agent_role=role)
        trace.record_model("settings.merge", result, "记忆责任角色返回合并候选，准备核源并保存")
        if len(result.data.records) != 1:
            raise ValidationGateError("当前记忆角色未给出唯一有依据的合并结果，两条记录均保留。")
        candidate = result.data.records[0]
        if (candidate.collection_id != keep["collection_id"] or candidate.record_id != keep["record_id"]
                or candidate.expected_revision != keep["revision"] or candidate.title != keep["title"]):
            raise ValidationGateError("合并候选与本次指定的保留记录或版本不一致，两条记录均保留。")
        required_fields = {key for record in (keep, other) for key, value in record["values"].items() if has_setting_content(value)}
        if any(not has_setting_content(candidate.values.get(key)) for key in required_fields):
            raise ValidationGateError("合并候选遗漏了原记录有内容的字段，两条记录均保留，请先核对独有信息。")
        refs = list(original_refs)
        for ref in service._evidence(candidate.evidence_refs):
            if not any(ref["source_kind"] == window["source"]["source_kind"]
                    and ref.get("chapter_no") == window["source"].get("chapter_no")
                    and ref.get("path") == window["source"].get("path")
                    and ref.get("artifact_id") == window["source"].get("artifact_id")
                    and ref["quote"] in window["text"] for window in windows):
                raise ValidationGateError("合并引用超出本次实际读取的来源窗口，两条记录均保留。")
            if ref not in refs:
                refs.append(ref)
        async with project_write_lock(project.root):
            if (service.source_fingerprint() != before or story_before != engine._planning_source_fingerprint(
                    project, 1, max(1, project.db.latest_accepted_chapter_no() + 1))):
                raise ValidationGateError("合并期间设定或故事来源已经变化，未覆盖当前记录，也未留档另一条。")
            saved = service.save_record(**{**candidate.model_dump(), "evidence_refs": refs}, actor=role, operation="supplement")
            saved = service.record_review(saved["record_id"], saved["revision"], actor=role,
                decision="verified_reference", source_fingerprint=service.source_fingerprint())
            verified = True
            archived = service.archive_record(other["record_id"], other["revision"], actor="user")
        from .manual_edits import ManualEditsService
        cleanup_warning = ""
        try:
            ManualEditsService(project).supersede(["story-setting:" + item["record_id"] for item in (saved, archived)])
        except Exception as exc:
            cleanup_warning = "旧的后台设定核对状态尚未同步。"
            trace.record("settings.merge.cleanup", "failed", cleanup_warning, str(exc), public=False)
        summary = "已合并并核对两条设定，另一条已移出使用并留档。"
        trace.finish(summary=summary)
        return {"status": "merged", "summary": summary, "record": saved, "archived_record": archived,
                "canon_changed": False, "warnings": [cleanup_warning] if cleanup_warning else [], "trace_id": trace.run_id}
    except Exception as exc:
        trace.finish(status="failed", summary=str(exc))
        if saved is not None:
            return {"status": "needs_evidence", "record": saved, "archive_pending": other["record_id"],
                "summary": "合并内容已保存" + ("并核对" if verified else "，核对尚未完成") + "，另一条尚未留档；两条资料均保留。",
                "reason": str(exc), "canon_changed": False, "trace_id": trace.run_id}
        raise
