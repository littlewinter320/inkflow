"""Preview and explicitly delete superseded planning candidates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ValidationGateError
from .project import InkFlowProject
from .utils import content_hash


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
                           "blocked": blocked, "action": "永久删除文件"})
    token_data = {"active": content_hash(_read(active_path)),
                  "suggestions": content_hash(_read(suggestion_path)),
                  "candidates": [(item["path"], item["content_hash"], item["blocked"]) for item in candidates]}
    return {"candidates": candidates, "confirmation_token": content_hash(json.dumps(token_data, ensure_ascii=False)),
            "impact": "只永久删除勾选的旧大纲候选文件，不移入项目回收区；删除后无法在墨流中恢复。生效大纲、卷细纲、近期规划、正史和数据库不变。"}


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
            if (candidate_path.is_symlink()
                    or not source.is_relative_to((project.root / "planning" / "outlines").resolve())):
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
    return {"status": "deleted", "deleted": deleted,
            "next_action": f"已永久删除 {len(deleted)} 个旧大纲候选文件；生效规划和正文未改。"}
