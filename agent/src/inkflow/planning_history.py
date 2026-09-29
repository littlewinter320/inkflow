"""Explicit, version-scoped access to superseded planning proposals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ValidationGateError
from .project import InkFlowProject
from .schemas import BookOutlineV2, RollingPlanV2, VolumeDetailV2


_PARTS = {"outline": ("OUTLINE.md", "outline-candidate.json"),
          "detail": ("STORY_DETAIL.md", "volume-detail-candidate.json"),
          "recent": ("RECENT_PLAN.md", "chapter-window-candidate.json")}


def kept_revisions(project: InkFlowProject) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted((project.root / "planning" / "history").glob("revision-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if (isinstance(record, dict) and record.get("status") == "kept"
                and path.name == f"revision-{record.get('run_id')}.json"):
            archived_parts = {item.get("source") for item in record.get("files", [])
                              if isinstance(item, dict) and (project.root / str(item.get("path") or "")).is_file()}
            deleted_parts = {item.get("source") for item in record.get("files", [])
                             if isinstance(item, dict) and not (project.root / str(item.get("path") or "")).is_file()}
            result.append({"revision_no": int(record.get("revision_no") or 0),
                           "record_id": record["run_id"], "created_at": record.get("created_at", ""),
                           "parts": [key for key in _PARTS if _PARTS[key][0] not in deleted_parts and
                                     (record.get("previous_run_id") or _PARTS[key][0] in archived_parts)]})
    return result


def kept_record(project: InkFlowProject, revision_no: int) -> dict[str, Any]:
    matches = [item for item in kept_revisions(project) if item["revision_no"] == revision_no]
    if len(matches) != 1:
        raise ValidationGateError("未找到唯一且已选择保留的规划版本；请先查看历史版本列表。")
    path = project.root / "planning" / "history" / f"revision-{matches[0]['record_id']}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def read_kept_part(project: InkFlowProject, revision_no: int, part: str,
                   *, chapter_no: int | None = None, volume_no: int | None = None) -> dict[str, Any]:
    if part not in _PARTS:
        raise ValidationGateError("请选择旧版大纲、卷细纲或近期规划中的一层。")
    record = kept_record(project, revision_no)
    filename, candidate_name = _PARTS[part]
    archived_entry = next((item for item in record.get("files", [])
                           if isinstance(item, dict) and item.get("source") == filename), None)
    if archived_entry and not (project.root / str(archived_entry.get("path") or "")).is_file():
        raise ValidationGateError("这一部分的历史副本已按用户选择删除，不再提供引用。")
    old_run = record.get("previous_run_id")
    if old_run and Path(str(old_run)).name != str(old_run):
        raise ValidationGateError("历史来源标识不合法，未读取旧版内容。")
    candidate_path = project.internal / "runs" / str(old_run) / candidate_name if old_run else None
    if candidate_path is not None and candidate_path.is_file():
        raw = candidate_path.read_text(encoding="utf-8")
        if part == "outline":
            item = BookOutlineV2.model_validate_json(raw)
            content = (next((volume.model_dump_json(indent=2) for volume in item.volumes
                             if volume.volume_no == volume_no), "") if volume_no else item.model_dump_json(indent=2))
        elif part == "detail":
            item = VolumeDetailV2.model_validate_json(raw)
            content = item.model_dump_json(indent=2) if volume_no in {None, item.volume_no} else ""
        else:
            item = RollingPlanV2.model_validate_json(raw)
            content = (next((chapter.model_dump_json(indent=2) for chapter in item.chapters
                             if chapter.chapter_no == chapter_no), "") if chapter_no else item.model_dump_json(indent=2))
    else:
        if chapter_no is not None or volume_no is not None:
            raise ValidationGateError("该旧版没有结构化章节或卷索引，不能猜测选段；请先查看整层并指定原文。")
        entry = archived_entry
        if not entry:
            content = ""
        else:
            relative = str(entry["path"]).replace("\\", "/")
            path = (project.root / relative).resolve()
            history_root = (project.root / "planning" / "history").resolve()
            if not path.is_relative_to(history_root) or not path.is_file():
                raise ValidationGateError("所选历史文件缺失或路径不合法。")
            content = path.read_text(encoding="utf-8")
    if not content:
        raise ValidationGateError("该版本没有所选部分；不会从其他版本猜测补齐。")
    return {"revision_no": revision_no, "part": part, "chapter_no": chapter_no,
            "volume_no": volume_no, "content": content, "status": "historical_reference_only"}
