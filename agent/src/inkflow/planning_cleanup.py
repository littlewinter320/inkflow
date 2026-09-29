"""Preview, retain, or explicitly delete superseded planning copies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ValidationGateError
from .project import InkFlowProject
from .utils import atomic_write_text, content_hash, utc_now


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def cleanup_preview(project: InkFlowProject) -> dict[str, Any]:
    suggestion_path = project.root / "planning" / "cleanup-suggestions.json"
    active_path = project.root / "planning" / "active-v2.json"
    try:
        suggestions = json.loads(_read(suggestion_path))
        active = json.loads(_read(active_path))
    except (ValueError, TypeError) as exc:
        raise ValidationGateError("尚无可核对的新版规划与旧候选清单，不会猜测删除目标。") from exc
    if active.get("status") != "active":
        raise ValidationGateError("三层规划尚未生效，旧候选不可清理。")
    references = "\n".join(_read(project.root / name) for name in
                           ("BOOK.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md", "STATE.md"))
    candidates: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    history_root = (project.root / "planning" / "history").resolve()
    for record_path in sorted(history_root.glob("revision-*.json")) if history_root.is_dir() else []:
        try:
            record = json.loads(_read(record_path))
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("status") not in {"pending", "kept"} or not isinstance(record.get("files"), list):
            continue
        revision_id = str(record.get("run_id") or "")
        if record_path.name != f"revision-{revision_id}.json":
            continue
        revisions.append({"run_id": revision_id, "revision_no": record.get("revision_no", 0),
                          "status": record["status"],
                          "created_at": record.get("created_at", ""),
                          "paths": [item.get("path") for item in record["files"] if isinstance(item, dict)]})
        for item in record["files"]:
            if not isinstance(item, dict):
                continue
            relative = str(item.get("path") or "").replace("\\", "/")
            candidate_path = project.root / relative
            path = candidate_path.resolve()
            allowed = (relative.startswith("planning/history/") and path.is_relative_to(history_root)
                       and path.suffix in {".md", ".json"} and not candidate_path.is_symlink())
            if allowed and not path.is_file():
                continue
            actual = content_hash(_read(path)) if allowed and path.is_file() else ""
            blocked = ""
            if not allowed:
                blocked = "历史文件路径不合法"
            elif actual != item.get("content_hash"):
                blocked = "历史内容已变化，需要重新核对"
            elif relative in references or path.name in references:
                blocked = "当前资料仍引用此旧版"
            elif relative.endswith(".json"):
                try:
                    archived = json.loads(_read(path)).get("plans", [])
                    archived_cards = {row["plan_key"]: (row["parent_key"], row["data_json"])
                                      for row in archived if row.get("kind") == "chapter"}
                except (ValueError, TypeError, KeyError):
                    archived_cards = {}
                with project.db.connect() as connection:
                    dependent = any(
                        archived_cards.get(row["plan_key"]) == (row["parent_key"], row["data_json"])
                        for row in connection.execute(
                            "SELECT p.plan_key,p.parent_key,p.data_json FROM plans p "
                            "JOIN chapters c ON p.plan_key=printf('chapter:%05d',c.chapter_no) "
                            "WHERE p.kind='chapter' AND c.status='accepted'")
                    )
                if dependent:
                    blocked = "此快照含已接受章节引用的旧版执行卡，恢复依赖必须保留"
            candidates.append({"path": relative, "content_hash": actual, "reason": "已被新版替换的规划历史",
                               "blocked": blocked, "action": "永久删除历史副本", "revision_id": revision_id})
    for item in suggestions.get("candidates", []):
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path") or "").replace("\\", "/")
        candidate_path = project.root / relative
        path = candidate_path.resolve()
        allowed = (relative.startswith("planning/outlines/outline_")
                   and relative.endswith(".md")
                   and not candidate_path.is_symlink()
                   and path.is_relative_to((project.root / "planning" / "outlines").resolve()))
        if allowed and not path.is_file():
            # A completed delete should disappear from the action list even if
            # an older suggestion snapshot still names the former path.
            continue
        actual = content_hash(_read(path)) if allowed and path.is_file() else ""
        blocked = ""
        if not allowed:
            blocked = "文件不在可清理的旧大纲候选范围"
        elif not actual:
            blocked = "文件已不存在"
        elif actual != item.get("content_hash"):
            blocked = "文件内容已变化，需要重新核对"
        elif actual == active.get("outline_hash"):
            blocked = "仍是当前生效大纲"
        elif path.name in references or relative in references:
            blocked = "当前资料仍引用该候选"
        candidates.append({"path": relative, "content_hash": actual,
                           "reason": "旧大纲候选，与当前生效大纲不同",
                           "blocked": blocked, "action": "永久删除文件", "revision_id": ""})
    token_data = {"active": content_hash(_read(active_path)),
                  "suggestions": content_hash(_read(suggestion_path)),
                  "candidates": [(item["path"], item["content_hash"], item["blocked"]) for item in candidates],
                  "revisions": [(item["run_id"], item["status"]) for item in revisions]}
    return {"candidates": candidates, "revisions": revisions,
            "confirmation_token": content_hash(json.dumps(token_data, ensure_ascii=False)),
            "impact": "仅删除选中的旧版历史副本或候选；新版生效资料与已接受正文不变。删除历史副本后，不能再用它恢复、参考或融合；有正史恢复依赖的备份会阻止删除。"}


def cleanup_keep(project: InkFlowProject, *, confirmation_token: str, revision_id: str) -> dict[str, Any]:
    preview = cleanup_preview(project)
    if confirmation_token != preview["confirmation_token"]:
        raise ValidationGateError("规划历史已变化，请重新预览后选择。")
    if not any(item["run_id"] == revision_id and item["status"] == "pending" for item in preview["revisions"]):
        raise ValidationGateError("此旧版不在待处理清单中。")
    record_path = project.root / "planning" / "history" / f"revision-{revision_id}.json"
    record = json.loads(_read(record_path))
    record.update(status="kept", decided_at=utc_now())
    atomic_write_text(record_path, json.dumps(record, ensure_ascii=False, indent=2))
    return {"status": "kept", "next_action": "旧版已保留在历史记录；正文、规划与模型上下文只使用当前生效版。"}


def cleanup_apply(project: InkFlowProject, *, confirmation_token: str,
                  selected_paths: list[str]) -> dict[str, Any]:
    preview = cleanup_preview(project)
    if confirmation_token != preview["confirmation_token"]:
        raise ValidationGateError("旧候选清单或文件内容已变化，请重新预览后确认。")
    eligible = {item["path"]: item for item in preview["candidates"] if not item["blocked"]}
    selected = list(dict.fromkeys(str(path).replace("\\", "/") for path in selected_paths))
    if not selected or any(path not in eligible for path in selected):
        raise ValidationGateError("未选择可删除的准确旧候选文件，未删除任何内容。")
    for relative in selected:
        if content_hash(_read((project.root / relative).resolve())) != eligible[relative]["content_hash"]:
            raise ValidationGateError("候选内容在确认后变化，本次未删除任何文件；请重新预览。")
    deleted: list[str] = []
    for relative in selected:
        candidate_path = project.root / relative
        source = candidate_path.resolve()
        try:
            allowed_roots = ((project.root / "planning" / "outlines").resolve(),
                             (project.root / "planning" / "history").resolve())
            if candidate_path.is_symlink() or not any(source.is_relative_to(root) for root in allowed_roots):
                return {"status": "partial" if deleted else "not_deleted", "deleted": deleted,
                        "next_action": f"{relative} 的路径刚刚变化；已删除 {len(deleted)} 项，其余未删除。请重新预览。"}
            if content_hash(_read(source)) != eligible[relative]["content_hash"]:
                return {"status": "partial" if deleted else "not_deleted", "deleted": deleted,
                        "next_action": f"{relative} 的内容刚刚变化；已删除 {len(deleted)} 项，其余未删除。请重新预览。"}
            source.unlink()
        except OSError as exc:
            return {"status": "partial" if deleted else "not_deleted", "deleted": deleted,
                    "next_action": f"删除 {relative} 失败：{exc}。已删除 {len(deleted)} 项；请重新预览剩余候选。"}
        deleted.append(relative)
    affected = {eligible[path].get("revision_id") for path in deleted} - {"", None}
    remaining_preview = cleanup_preview(project)
    protected = 0
    for revision_id in affected:
        record_path = project.root / "planning" / "history" / f"revision-{revision_id}.json"
        record = json.loads(_read(record_path))
        remaining = [str(item["path"]) for item in record["files"]
                     if (project.root / str(item["path"])).exists()]
        if not remaining:
            record.update(status="deleted", decided_at=utc_now())
            atomic_write_text(record_path, json.dumps(record, ensure_ascii=False, indent=2))
        elif all(any(candidate["path"] == path and candidate["blocked"]
                     for candidate in remaining_preview["candidates"]) for path in remaining):
            record.update(status="protected", decided_at=utc_now())
            protected += len(remaining)
            atomic_write_text(record_path, json.dumps(record, ensure_ascii=False, indent=2))
    return {"status": "deleted", "deleted": deleted,
            "next_action": f"已永久删除 {len(deleted)} 个选中的旧规划文件；"
                           + (f"另有 {protected} 个正史恢复依赖已保护且不再作为历史参考。" if protected else "")
                           + "生效规划和正文未改。"}
