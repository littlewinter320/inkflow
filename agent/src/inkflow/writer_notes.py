"""Version-bound public Writer handoff. These records never supply canon evidence."""

from typing import Any

from .schemas import HookNote
from .utils import content_hash, json_dumps


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


def notes_prompt(notes: dict[str, Any]) -> str:
    if not notes:
        return "\n\n# Writer版本说明\n当前版本无独立说明，直接审正文，不为补说明生成新稿。"
    return ("\n\n# Writer版本说明（待核协作资料，不是正文、正史或引用来源）\n"
            + json_dumps(notes))


def approved_notes(project: Any, chapter_no: int) -> dict[str, Any]:
    """Only an accepted chapter's matching pass report can expose a future-intent reference."""
    chapter = project.db.get_chapter(chapter_no)
    record = project.db.latest_review_record(chapter_no)
    notes = version_notes(project, chapter_no)
    if (not chapter or chapter["status"] != "accepted" or not record or not notes
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
    return {**notes, "authority": "仅作未来意图参考；事实读正史，主线读当前规划，旧设想可调整"}


def recent_approved_notes(project: Any, chapter_no: int) -> list[dict[str, Any]]:
    recent = sorted((row for row in project.db.accepted_chapters() if row["chapter_no"] < chapter_no),
                    key=lambda row: row["chapter_no"], reverse=True)[:3]
    result = []
    for row in reversed(recent):
        notes = approved_notes(project, row["chapter_no"])
        if not notes:
            continue
        hook = notes.get("hook_note", {})
        result.append({"chapter_no": row["chapter_no"], "content_hash": notes["content_hash"],
            "authority": notes["authority"],
            "reader_expectation": str(hook.get("reader_expectation") or "")[:160],
            "intentionally_withheld": str(hook.get("intentionally_withheld") or "")[:160],
            "planned_followup": str(hook.get("planned_followup") or "")[:160],
            "future_intents": [item for item in hook.get("annotations", []) if item.get("kind") == "future"][:2],
            "reference_only": "简要意图参考可能省略细节；未结承诺以正史伏笔状态为准，不能从省略推定不存在"})
    return result
