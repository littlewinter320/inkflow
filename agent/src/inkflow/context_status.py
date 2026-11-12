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
    """Read the latest durable attempt, keeping completion and usage reporting separate."""
    path = path or usage_ledger_path()
    rows = {}
    error = ""
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                for role in ROLES:
                    row = db.execute("SELECT role,model,input_tokens,input_estimate,max_output_tokens,task_id,attempt_id,"
                        "started_at,finished_at,outcome,diagnostic_json FROM model_attempts "
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
        diagnostic = {}
        if row:
            try:
                diagnostic = json.loads(row["diagnostic_json"] or "{}")
                recorded_limit = diagnostic.get("context_limit_tokens")
                if recorded_limit is not None:
                    recorded_limit = int(recorded_limit)
            except (ValueError, TypeError, AttributeError):
                recorded_limit = None
                diagnostic = {}
        if recorded_limit and recorded_limit > 0:
            limit = recorded_limit
        used = row["input_tokens"] if row and row["input_tokens"] is not None else row["input_estimate"] if row else None
        result.append({"role": role, "model": model, "input_tokens": used,
            "input_estimate_tokens": row["input_estimate"] if row else None,
            "input_over_budget_tokens": max(0, row["input_tokens"] - limit)
                if row and row["input_tokens"] is not None and recorded_limit else None,
            "input_underestimate_tokens": max(0, row["input_tokens"] - row["input_estimate"])
                if row and row["input_tokens"] is not None and row["input_estimate"] is not None else None,
            "input_kind": "reported" if row and row["input_tokens"] is not None else "estimated" if row else "unknown",
            "context_limit_tokens": limit, "limit_source": "request_snapshot" if recorded_limit else "current_configuration",
            "remaining_tokens": max(0, limit - used) if used is not None and recorded_limit else None,
            "used_percent": round(100 * used / limit, 1) if used is not None and recorded_limit else None,
            "output_policy": diagnostic.get("output_policy", "legacy_explicit"),
            "budget_scope": "input" if diagnostic.get("output_policy") in {"provider_default", "provider_required_model_limit", "model_capacity"} else "input_and_output",
            "request_output_limit": diagnostic.get("request_output_limit") if "output_policy" in diagnostic else row["max_output_tokens"] if row else None,
            "output_reservation_upper": diagnostic.get("output_reservation_upper"),
            "output_reserve_tokens": row["max_output_tokens"] if row and row["max_output_tokens"] >= 0 and diagnostic.get("output_policy") not in {"provider_default", "model_capacity"} else None,
            "updated_at": row["started_at"] if row else None,
            "finished_at": row["finished_at"] if row else None,
            "task_id": row["task_id"] if row else "",
            "attempt_id": row["attempt_id"] if row else "",
            "run_id": diagnostic.get("run_id", ""),
            "request_stage": diagnostic.get("stage", ""),
            "request_state": row["outcome"] if row else "unknown" if error else "not_called"})
    return result, error


def read_context_status(project, settings) -> dict:
    """Join saved material and request identities without relabelling older work as current."""
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
        try:
            snapshot = None
            if summary_path.is_file():
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    if (isinstance(summary, dict) and summary.get("packet_hash") and summary.get("updated_at")
                            and summary.get("project_id") == project.project_id and summary.get("actor") == role["role"]):
                        snapshot = summary
                        if snapshot_path.is_file():
                            # A delayed summary write must never describe a different full record.
                            # Check only its small header; avoid rereading private material bodies on every poll.
                            with snapshot_path.open(encoding="utf-8") as source:
                                header = source.read(4096)
                            if not all(f'"{key}": {json.dumps(summary[key], ensure_ascii=False)},' in header
                                       for key in ("packet_hash", "updated_at", "chapter_no", "task_id", "run_id") if key in summary):
                                snapshot = None
                except (OSError, ValueError, TypeError):
                    snapshot = None
            if snapshot is None:
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.is_file() else value if value.get("actor") == role["role"] else None
            if snapshot is not None and not isinstance(snapshot, dict):
                raise ValueError("Invalid context snapshot")
            if snapshot and snapshot.get("project_id") == project.project_id and snapshot.get("actor") == role["role"]:
                role["materials"] = {key: snapshot.get(key) for key in (
                    "updated_at", "packet_hash", "chapter_no", "material_scope", "task", "task_id", "run_id", "estimated_tokens", "change_since_previous_tokens", "budget_allocation",
                    "record_kind", "configured_hard_limit_tokens", "hard_limit_tokens", "output_reserve_tokens")}
                if role["role"] == "coordinator":
                    # Older routing packets used chapter 1 as a required internal placeholder.
                    role["materials"].update(material_scope="project", chapter_no=None)
                same_task = bool(snapshot.get("task_id") and snapshot["task_id"] == role["task_id"])
                same_run = bool(snapshot.get("run_id") and snapshot["run_id"] == role["run_id"])
                role["materials_relation"] = "same_run" if same_task and same_run else "same_task" if same_task else "unlinked"
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
