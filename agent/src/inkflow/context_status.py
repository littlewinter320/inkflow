"""Read local compilation and per-role request counts without starting model work."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .model_usage import usage_ledger_path
from .utils import project_source_revision

ROLES = ("coordinator", "writer", "editor", "reviewer", "memory_keeper")


def role_context_status(settings, project_id: str, path: Path | None = None) -> tuple[list[dict], str]:
    path = path or usage_ledger_path()
    rows = {}
    error = ""
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                for role in ROLES:
                    row = db.execute("SELECT role,model,input_tokens,input_estimate,max_output_tokens,"
                        "started_at,outcome,diagnostic_json FROM model_attempts "
                        "WHERE novel_id=? AND purpose='production' AND role=? ORDER BY started_at DESC,rowid DESC LIMIT 1",
                        (project_id, role)).fetchone()
                    if row is not None:
                        rows[role] = dict(row)
        except sqlite3.Error:
            error = "角色用量记录暂时无法读取，稍后会再更新；这不代表用量为零。"
    result = []
    for role in ROLES:
        row = rows.get(role)
        model = row["model"] if row else settings.model_for(role)
        limit = settings.context_budget_for(role, model_override=model)[1]
        recorded_limit = None
        if row:
            try:
                recorded_limit = json.loads(row["diagnostic_json"] or "{}").get("context_limit_tokens")
                if recorded_limit is not None:
                    recorded_limit = int(recorded_limit)
            except (ValueError, TypeError, AttributeError):
                recorded_limit = None
        if recorded_limit and recorded_limit > 0:
            limit = recorded_limit
        used = row["input_tokens"] if row and row["input_tokens"] is not None else row["input_estimate"] if row else None
        result.append({"role": role, "model": model, "input_tokens": used,
            "input_kind": "reported" if row and row["input_tokens"] is not None else "estimated" if row else "unknown",
            "context_limit_tokens": limit, "limit_source": "request_snapshot" if recorded_limit else "current_configuration",
            "remaining_tokens": max(0, limit - used) if used is not None and recorded_limit else None,
            "used_percent": round(100 * used / limit, 1) if used is not None and recorded_limit else None,
            "output_reserve_tokens": row["max_output_tokens"] if row else None,
            "updated_at": row["started_at"] if row else None,
            "request_state": row["outcome"] if row else "unknown" if error else "not_called"})
    return result, error


def read_context_status(project, settings) -> dict:
    value = {"project_root": str(project.root), "status": "idle", "estimated_tokens": 0, "before_compression_tokens": 0,
        "previous_estimated_tokens": None, "change_since_previous_tokens": None,
        "soft_limit_tokens": settings.context_budget_for("writer")[0],
        "hard_limit_tokens": settings.context_budget_for("writer")[1], "hard_usage_percent": 0,
        "compression_applied": False, "hard_sections": [], "compressible_sections": [], "warnings": []}
    path = project.internal / "context-status.json"
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("project_id") not in {None, "", project.project_id}:
                raise ValueError("Invalid context identity")
            value.update(saved)
            current = project_source_revision(project.root, project.internal)
            if saved.get("source_revision") and saved["source_revision"] != current:
                value.update(current_source_revision=current,
                    stale_reason="项目后来有新活动；这里保留的是上次编译时的资料，不表示当前任务出错。执行前会按来源版本重新核对。")
        except (OSError, ValueError, TypeError):
            value.update(status="unavailable", read_error="上次上下文记录无法读取，下次任务会重新生成；当前角色用量单独读取。")
    value["roles"], error = role_context_status(settings, project.project_id)
    for role in value["roles"]:
        snapshot_path = project.internal / f"context-status-{role['role']}.json"
        summary_path = project.internal / f"context-status-{role['role']}-summary.json"
        if summary_path.is_file():
            snapshot_path = summary_path
        try:
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.is_file() else value if value.get("actor") == role["role"] else None
            if snapshot is not None and not isinstance(snapshot, dict):
                raise ValueError("Invalid context snapshot")
            if snapshot and snapshot.get("project_id") == project.project_id and snapshot.get("actor") == role["role"]:
                role["materials"] = {key: snapshot.get(key) for key in (
                    "updated_at", "packet_hash", "chapter_no", "estimated_tokens", "change_since_previous_tokens", "budget_allocation",
                    "record_kind", "configured_hard_limit_tokens", "hard_limit_tokens", "output_reserve_tokens")}
        except (OSError, ValueError, TypeError):
            role["materials_error"] = "这份资料明细暂时无法读取。"
    value.pop("material_contents", None)
    if error:
        value["read_error"] = error
    return value


def read_context_material(project, *, role: str, section_key: str, updated_at: str, packet_hash: str) -> dict:
    from .errors import ProjectError
    if role not in ROLES:
        raise ProjectError("请选择需要查看的角色。")
    path = project.internal / f"context-status-{role}.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectError("这份旧记录没有保存资料明细；下次执行后会显示。") from exc
    if (not isinstance(snapshot, dict) or not packet_hash or snapshot.get("project_id") != project.project_id or snapshot.get("actor") != role
            or snapshot.get("updated_at") != updated_at or snapshot.get("packet_hash") != packet_hash):
        raise ProjectError("这个角色已经读取了新资料，请收起后重新打开详情。")
    section = next((item for item in snapshot.get("material_contents", []) if item.get("key") == section_key), None)
    if section is None:
        raise ProjectError("这份资料没有保存在本次输入中。")
    return {"title": section["title"], "content": section["content"], "updated_at": updated_at}
