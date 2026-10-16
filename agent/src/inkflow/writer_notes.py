"""Version-bound public Writer handoff. These records never supply canon evidence."""

import re
from typing import Any

from .schemas import HookNote
from .utils import content_hash, json_dumps


def align_annotation_quotes(note: dict[str, Any], content: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    annotations = [dict(item) for item in note.get("annotations", [])]
    changes = []
    for index, item in enumerate(annotations):
        quote = str(item.get("quote") or "")
        aligned = quote.replace("、", "，")
        if quote and quote not in content and aligned != quote and aligned in content:
            item["quote"] = aligned
            changes.append({"annotation_index": index, "original_quote": quote, "quote": aligned,
                "reason": "正文格式处理已替换顿号；同一连续原句同步定位，不改说明含义"})
    return ({**note, "annotations": annotations}, changes) if changes else (note, [])


def version_notes(project: Any, chapter_no: int) -> dict[str, Any]:
    chapter = project.db.get_chapter(chapter_no)
    if not chapter:
        return {}
    notes: dict[str, Any] = {}
    for key, artifact_type in (("hook_note", "writer_hook_note"), ("scene_blueprint", "writer_scene_blueprint")):
        item = next((item for item in project.db.list_agent_artifacts(
            chapter_no=chapter_no, artifact_type=artifact_type, limit=200)
            if item["status"] == "current" and item["chapter_version"] == chapter["version"]
            and item["data"].get("content_hash", chapter["content_hash"]) == chapter["content_hash"]), None)
        if item:
            notes[key] = item["data"]
            notes.setdefault("artifact_ids", []).append(item["artifact_id"])
    if notes:
        notes.update(chapter_no=chapter_no, chapter_version=chapter["version"], content_hash=chapter["content_hash"])
        content = chapter.get("content_text")
        if not isinstance(content, str) or content_hash(content) != chapter["content_hash"]:
            path = project.resolve_user_path(chapter["path"])
            content = path.read_text(encoding="utf-8") if path.is_file() else ""
        if content_hash(content) == chapter["content_hash"] and "hook_note" in notes:
            notes["hook_note"], alignments = align_annotation_quotes(notes["hook_note"], content)
            if alignments:
                notes["quote_alignments"] = alignments
        original_notes_hash = notes_hash(notes)
        for artifact in project.db.list_agent_artifacts(chapter_no=chapter_no, artifact_type="writer_note_clarification", limit=200):
            data = artifact["data"]
            if (content_hash(content) != chapter["content_hash"] or artifact.get("status") == "superseded" or data.get("source_hash") != chapter["content_hash"]
                    or data.get("writer_notes_hash") != original_notes_hash or not isinstance(data.get("reply"), dict)):
                continue
            reply = clarify_annotation_quotes(notes.get("hook_note", {}), data.get("questions", []), data["reply"], content)
            if reply.get("corrected_hook_note") and not annotation_gaps(reply["corrected_hook_note"], content):
                notes["hook_note"] = {**notes.get("hook_note", {}), **reply["corrected_hook_note"]}
                notes["clarification_source"] = {"artifact_id": artifact["artifact_id"],
                    "meaning": "复用同版Writer短答中的原文更正候选；仍由当前审核角色判断语义，原始说明保留。"}
            break
    return notes


def notes_hash(notes: dict[str, Any]) -> str:
    return content_hash(json_dumps(notes)) if notes else ""


def annotation_gaps(note: dict[str, Any], content: str) -> list[str]:
    gaps = []
    for index, item in enumerate(note.get("annotations", [])):
        quote = str(item.get("quote") or "").strip()
        if (item.get("kind") in {"new_fact", "interpretation"} and not quote) or (quote and quote not in content):
            gaps.append(f"说明第{index + 1}项原句未定位，请纠正或撤回该项说明，不改正文：{str(item.get('statement') or '')[:160]}")
    return gaps


def clarify_annotation_quotes(note: dict[str, Any], questions: list[str], reply: dict[str, Any], content: str) -> dict[str, Any]:
    """Project exact quotes already supplied by Writer; the review role still checks meaning."""
    if reply.get("corrected_hook_note") is not None or len(reply.get("answers", [])) != len(questions):
        return reply
    corrected = {key: value for key, value in note.items() if key in HookNote.model_fields}
    corrected["annotations"] = [dict(item) for item in note.get("annotations", [])]
    changed = False
    gaps = set(annotation_gaps(note, content))
    for question, answer in zip(questions, reply.get("answers", []), strict=True):
        if question not in gaps:
            continue
        match = re.match(r"说明第(\d+)项原句未定位", question)
        if not match or not isinstance(answer, str):
            continue
        candidates = []
        for left, right in (("‘", "’"), ("“", "”"), ('"', '"'), ("「", "」")):
            candidates.extend(re.findall(re.escape(left) + "(.+?)" + re.escape(right), answer, re.S))
        candidates = [quote for quote in candidates if 8 <= len(quote) <= 200 and content.count(quote) == 1]
        if not candidates:
            continue
        corrected["annotations"][int(match.group(1)) - 1]["quote"] = max(candidates, key=len)
        changed = True
    return {**reply, "corrected_hook_note": HookNote.model_validate(corrected).model_dump(mode="json")} if changed else reply


def notes_prompt(notes: dict[str, Any]) -> str:
    if not notes:
        return "\n\n# Writer版本说明\n当前版本无独立说明，直接审正文，不为补说明生成新稿。"
    return ("\n\n# Writer版本说明（待核协作资料，不是正文、正史或引用来源）\n"
            + json_dumps(notes))


def approved_notes(project: Any, chapter_no: int, *, provisional_content: str | None = None) -> dict[str, Any]:
    """Only an accepted chapter's matching pass report can expose a future-intent reference."""
    chapter = project.db.get_chapter(chapter_no)
    record = project.db.latest_review_record(chapter_no)
    notes = version_notes(project, chapter_no)
    provisional = bool(chapter and chapter["status"] == "draft" and provisional_content
                       and content_hash(provisional_content) == chapter["content_hash"])
    if (not chapter or (chapter["status"] != "accepted" and not provisional) or not record or not notes
            or record["chapter_version"] != chapter["version"]
            or record["report"].verdict != "pass" or record["report"].source_hash != chapter["content_hash"]
            or record["report"].writer_notes_hash != notes_hash(notes)):
        return {}
    recovery = record["report"].evidence_recovery
    for data in [recovery, *recovery.get("roles", {}).values()]:
        reply = data.get("writer_note_clarification", {})
        corrected = reply.get("reply", {}).get("corrected_hook_note")
        if corrected:
            # Preserve the original handoff alongside the reviewed correction.
            notes = {**notes, "hook_note": {**notes.get("hook_note", {}), **HookNote.model_validate(corrected).model_dump(mode="json")}}
            notes["clarification_artifact_id"] = reply.get("artifact_id")
            break
    return {**notes, "authority": "本批次已核草稿的交接参考，尚非正史" if provisional else
            "已接受章的创作交接参考；事实读正史，主线读当前规划，旧设想可调整"}


def recent_approved_notes(project: Any, chapter_no: int, provisional_chapters: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    provisional = {int(row["chapter_no"]): row for row in provisional_chapters or []
                   if int(row["chapter_no"]) < chapter_no and isinstance(row.get("memory_patch"), dict)}
    recent = sorted({row["chapter_no"] for row in project.db.accepted_chapters() if row["chapter_no"] < chapter_no}
                    | set(provisional), reverse=True)[:3]
    result = []
    for number in reversed(recent):
        notes = approved_notes(project, number, provisional_content=provisional.get(number, {}).get("content"))
        if not notes:
            continue
        hook = notes.get("hook_note", {})
        result.append({"chapter_no": number, "content_hash": notes["content_hash"],
            "authority": notes["authority"],
            "important_decisions": [str(item)[:240] for item in hook.get("decision_summary", [])[:4]],
            "fact_candidates_to_check": [str(item)[:240] for item in hook.get("new_fact_candidates", [])[:4]],
            "thread_changes_to_check": [str(item)[:240] for item in hook.get("thread_changes", [])[:4]],
            "reader_expectation": str(hook.get("reader_expectation") or "")[:160],
            "intentionally_withheld": str(hook.get("intentionally_withheld") or "")[:160],
            "planned_followup": str(hook.get("planned_followup") or "")[:160],
            "future_intents": [item for item in hook.get("annotations", []) if item.get("kind") == "future"][:2],
            "reference_only": "简要意图参考可能省略细节；未结承诺以正史伏笔状态为准，不能从省略推定不存在"})
    return result
