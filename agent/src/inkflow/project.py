from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .database import ProjectDatabase
from .errors import ProjectError, ValidationGateError
from .project_lock import project_write_lock_sync
from .render import render_state
from .review_verifier import legacy_accepted_conflict
from .schemas import BookBrief, TerminalIntent
from .utils import atomic_write_json, atomic_write_text, content_hash, safe_filename, utc_now


class InkFlowProject:
    def __init__(self, root: str | Path, *, recover_on_open: bool = True):
        self.root = Path(root).resolve()
        self.internal = self.root / ".inkflow"
        self.config_path = self.internal / "project.json"
        if not self.config_path.exists():
            raise ProjectError(f"这里不是墨流小说项目：{self.root}")
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.db = ProjectDatabase(self.internal / "inkflow.db")
        self.recovery_warnings: list[str] = []
        if recover_on_open:
            with project_write_lock_sync(self.root):
                self.recover_open_state()

    def recover_open_state(self) -> None:
        """Run startup recovery only while the caller owns the project write lock."""
        if self.db.canonical_content_migration_required():
            self.recovery_warnings = [
                "此项目需要确认“正史正文数据库备份与升级”。升级前不会改动数据库，也不会尝试覆盖现有 Markdown。"
            ]
        else:
            self.recovery_warnings = [
                *self.recover_pending_commits(),
                *self.recover_accepted_chapter_projections(),
            ]
            from .planning_pipeline import recover_planning_publication
            warning = recover_planning_publication(self)
            if warning:
                self.recovery_warnings.append(warning)
        self.db.reconcile_collaboration_queue()
        self.reconcile_pending_work()

    def pending_work(self, *, chapter_no: int | None = None) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT value_json FROM metadata WHERE key LIKE 'pending_work:%' ORDER BY updated_at DESC"
            ).fetchall()
        items = [json.loads(row["value_json"]) for row in rows]
        native: list[dict[str, Any]] = []
        jobs = self.db.get_metadata("manual_edit_jobs", {})
        if isinstance(jobs, dict):
            for job in jobs.values():
                if job.get("status") in {"pending", "reviewing", "awaiting_confirmation", "needs_evidence", "failed", "revision_requested", "paused"}:
                    native.append({"id": "manual-" + str(job["job_id"]), "kind": "manual_edit",
                        "status": job["status"], "chapter_no": job.get("chapter_no"),
                        "reason": job.get("summary") or "手动修改候选仍需核对",
                        "next_action": "按候选原快照与来源续核；需要创作选择时先询问，不直接覆盖已接受正文。",
                        "source": {key: job.get(key) for key in ("job_id", "relative_path", "current_hash", "attempts")}})
        with self.db.connect() as connection:
            revisions = connection.execute("SELECT key,value_json FROM metadata WHERE key LIKE 'pending_plan_revision:%'").fetchall()
        for row in revisions:
            revision = json.loads(row["value_json"])
            if revision.get("status") in {"needs_writer", "awaiting_review"}:
                number = int(row["key"].split(":", 1)[1])
                native.append({"id": "plan-" + content_hash(row["value_json"])[:24], "kind": "plan_revision",
                    "status": revision["status"], "chapter_no": number,
                    "reason": revision.get("instruction") or "执行卡已改变，关联草稿尚未完成修订和复审",
                    "next_action": "按当前新卡核对草稿，再按已授权范围定向修订和复审。",
                    "intent": TerminalIntent(action="revise_review" if revision["status"] == "needs_writer" else "review",
                        chapter_no=number, authorization="proposed", visible_reason="仅处理这章执行卡修订交接",
                        operation_instruction=revision.get("instruction") or "按当前章节卡修订并复审；保留其他章节和已接受正文。",
                        forbidden_actions=["accept"]).model_dump(mode="json"),
                    "source": revision})
        native_ids = {item["id"] for item in native}
        items = [item for item in items if item["kind"] not in {"manual_edit", "plan_revision"}
                 or item["id"] in native_ids]
        saved = {item["id"]: item for item in items}
        items = [item for item in items if item["id"] not in native_ids]
        for item in native:
            decision = saved.get(item["id"], {})
            items.append({**decision, **item, **({"status": "deferred"} if decision.get("status") == "deferred" else {})})
        workflows = {item["source"]["task_id"]: item for item in items if item["kind"] == "workflow" and item.get("source", {}).get("task_id")}
        for item in items:
            parent = workflows.get(item.get("source", {}).get("task_id"))
            if parent and parent["id"] != item["id"]:
                item["parent_id"] = parent["id"]
            handling = "resume" if item.get("intent") else "blocked"
            if item["kind"] in {"accepted_projection", "planning_publication"}:
                handling = "local"
            elif item["kind"] == "manual_edit":
                handling = "manual" if item["status"] in {"pending", "failed", "deferred"} and int(item["source"].get("attempts") or 0) < 2 else "decision"
            elif item["kind"] in {"review_gate", "review_evidence", "chapter_gate"} and item.get("chapter_no"):
                chapter = self.db.get_chapter(int(item["chapter_no"]))
                handling = "review" if chapter and chapter["status"] == "draft" else "blocked"
            if item["status"] == "running" or (item["kind"] == "manual_edit" and item["status"] == "reviewing"):
                handling = "running"
            if int(item.get("processing_attempts", 0)) >= 2:
                handling = "blocked"
            if item["kind"] in {"workflow", "accept_finalization"} and not (item.get("run_id") and item.get("source", {}).get("task_id")):
                handling = "blocked"
            task_id = item.get("source", {}).get("task_id")
            if task_id and handling == "resume":
                with self.db.connect() as connection:
                    records = connection.execute("SELECT value_json FROM metadata WHERE key LIKE ?", (f"coordinator_recovery:{task_id}:%",)).fetchall()
                if any(len(record.get("attempts", [])) >= 2 or record.get("status") == "waiting_user"
                       for row in records if isinstance(record := json.loads(row["value_json"]), dict)):
                    handling = "blocked"
            item["handling"] = "deferred" if item["status"] == "deferred" else handling
            item["can_process"] = handling in {"resume", "local", "manual", "review"}
        return [item for item in items if item["status"] not in {"completed", "superseded"}
                and (chapter_no is None or item.get("chapter_no") in {None, chapter_no})]

    def record_pending_work(self, *, kind: str, reason: str, next_action: str,
                            source: dict[str, Any], intent: dict[str, Any] | None = None,
                            run_id: str = "", status: str = "pending",
                            chapter_no: int | None = None) -> dict[str, Any]:
        identity = content_hash(json.dumps([kind, source, reason if kind == "optimization" else ""],
                                          ensure_ascii=False, sort_keys=True))[:24]
        key = f"pending_work:{identity}"
        with project_write_lock_sync(self.root):
            previous = self.db.get_metadata(key, {})
            item = {**previous, "id": identity, "kind": kind, "reason": reason,
                    "next_action": next_action, "source": source, "chapter_no": chapter_no,
                    "status": previous.get("status", status) if previous.get("status") not in {"completed", "superseded"} else status,
                    "created_at": previous.get("created_at", utc_now()),
                    "updated_at": utc_now()}
            if intent is not None:
                item["intent"] = intent
            if run_id:
                item["run_id"] = run_id
            self.db.set_metadata(key, item)
        return item

    def update_pending_work(self, identity: str, **changes: Any) -> dict[str, Any]:
        with project_write_lock_sync(self.root):
            key = f"pending_work:{identity}"
            item = self.db.get_metadata(key)
            if not isinstance(item, dict) or item.get("id") != identity:
                item = next((value for value in self.pending_work() if value["id"] == identity), None)
                if item is None:
                    raise ProjectError("待处理事项不存在，请刷新后选择具体事项。")
            item = {**item, **changes, "updated_at": utc_now()}
            self.db.set_metadata(key, item)
            return item

    def reconcile_pending_work(self, *, current_pass: Callable[[int], dict[str, Any] | None] | None = None,
                               current_source: dict[str, Any] | None = None) -> None:
        """Close only issues whose concrete source/output now proves resolution."""
        validated: dict[int, dict[str, Any] | None] = {}
        with self.db.connect() as connection:
            rows = connection.execute("SELECT value_json FROM metadata WHERE key LIKE 'pending_work:%'").fetchall()
        finished_tasks = {item["source"].get("task_id"): item for row in rows
                          if (item := json.loads(row["value_json"])).get("kind") == "workflow" and item.get("status") == "completed" and item.get("source", {}).get("task_id")}
        for item in self.pending_work():
            if item["kind"] == "plan_revision":
                number = int(item["chapter_no"])
                chapter = self.db.get_chapter(number)
                marker = self.db.get_metadata(f"pending_plan_revision:{number}", {})
                if chapter and chapter["status"] == "accepted" and marker == item["source"]:
                    self.update_pending_work(item["id"], status="superseded", resolution="目标章节已接受，旧执行卡修订不再用于改写正史。")
                    self.db.set_metadata(f"pending_plan_revision:{number}", {**marker, "status": "superseded"})
                    continue
            if item["kind"] == "plan_revision" and item["source"].get("status") == "awaiting_review" and current_pass is not None:
                number = int(item["chapter_no"])
                reviewed = current_pass(number)
                marker = self.db.get_metadata(f"pending_plan_revision:{number}", {})
                if reviewed is not None and marker == item["source"]:
                    self.update_pending_work(item["id"], status="completed", resolution={"review_id": reviewed["id"]})
                    self.db.set_metadata(f"pending_plan_revision:{number}", {**marker, "status": "completed", "review_id": reviewed["id"]})
                continue
            parent = finished_tasks.get(item.get("source", {}).get("task_id"))
            if parent and item["kind"] in {"coordinator_recovery", "development_repair", "recovery_decision"}:
                self.update_pending_work(item["id"], status="completed",
                    resolution={"workflow_id": parent["id"], "progress": parent.get("progress", {})})
                continue
            if item["kind"] == "optimization" and current_source is not None and item["source"] != current_source:
                self.update_pending_work(item["id"], status="superseded", resolution="提出建议时的来源已变化；旧建议保留历史，不再反复安排。")
                continue
            if item["kind"] == "context_source":
                number = item.get("source", {}).get("chapter_no")
                chapter = self.db.get_chapter(int(number)) if number else None
                if chapter and chapter["status"] == "accepted":
                    text_path = self.resolve_user_path(chapter["path"])
                    if text_path.is_file() and content_hash(text_path.read_text(encoding="utf-8")) == chapter["content_hash"]:
                        self.update_pending_work(item["id"], status="completed", resolution={"version": chapter["version"], "content_hash": chapter["content_hash"]})
                continue
            if item["kind"] == "planning_dependency" and self.db.get_current_plan_bundle() is not None:
                self.update_pending_work(item["id"], status="completed", resolution="当前规划记录已恢复，原缺失依赖已解除。")
                continue
            if item["kind"] in {"chapter_gate", "review_gate", "review_evidence"} and item.get("chapter_no"):
                chapter = self.db.get_chapter(item["chapter_no"])
                review = self.db.latest_review_record(item["chapter_no"])
                if not chapter or not review:
                    continue
                text_path = self.resolve_user_path(chapter["path"])
                if item["source"].get("content_hash") != chapter["content_hash"]:
                    self.update_pending_work(item["id"], status="superseded", resolution="正文已有新版本，旧问题来源仅保留审计。")
                    continue
                if (chapter["status"] == "accepted" and text_path.is_file()
                        and content_hash(text_path.read_text(encoding="utf-8")) == chapter["content_hash"]):
                    self.update_pending_work(item["id"], status="completed",
                        resolution={"version": chapter["version"], "content_hash": chapter["content_hash"], "review_id": review["id"]})
                elif item["kind"] in {"review_gate", "review_evidence"}:
                    if item["source"].get("content_hash") != chapter["content_hash"]:
                        self.update_pending_work(item["id"], status="superseded", resolution="正文已有新版本，旧报告仅保留审计。")
                    else:
                        number = item["chapter_no"]
                        if current_pass is not None and number not in validated:
                            try:
                                validated[number] = current_pass(number)
                            except (ProjectError, ValidationGateError, ValueError, OSError):
                                validated[number] = None
                        if validated.get(number) is not None:
                            self.update_pending_work(item["id"], status="completed", resolution={"review_id": review["id"], "version": chapter["version"]})
                        elif item["kind"] == "review_gate" and item["source"].get("review_id") != review["id"]:
                            self.update_pending_work(item["id"], status="superseded", resolution="由当前版本的新审查接管问题，保留原报告。")
            elif item["kind"] == "accepted_projection" and item.get("chapter_no"):
                chapter = self.db.get_chapter(int(item["chapter_no"]))
                if not chapter or chapter["status"] != "accepted":
                    continue
                source = {key: chapter[key] for key in ("chapter_no", "version", "content_hash", "path")}
                try:
                    if item["source"] != source:
                        self.update_pending_work(item["id"], status="superseded", resolution={
                            "source": source, "reason": "已接受正史来源已变化；旧问题和恢复日志保留审计，不套用旧恢复动作。"})
                    else:
                        self._finish_projection_pending_work(chapter)
                except (OSError, UnicodeError, ValueError, KeyError, TypeError, sqlite3.Error, ProjectError) as exc:
                    self.recovery_warnings.append(f"第 {chapter['chapter_no']} 章投影待办核验未完成：{exc}")
            elif item["kind"] == "planning_source":
                from .planning_pipeline import load_active_planning
                try:
                    active = load_active_planning(self)
                except (ProjectError, ValidationGateError, ValueError, OSError):
                    continue
                if active:
                    self.update_pending_work(item["id"], status="completed",
                        resolution={"run_id": active[0]["trace_id"], "revision_no": active[0].get("revision_no", 1)})

    @classmethod
    def create(cls, root: str | Path, brief: BookBrief) -> "InkFlowProject":
        project_root = Path(root).resolve()
        project_root.mkdir(parents=True, exist_ok=True)
        internal = project_root / ".inkflow"
        if (internal / "project.json").exists():
            raise ProjectError(f"项目已经存在：{project_root}")
        for path in (
            project_root / "chapters",
            project_root / "reviews",
            internal / "runs",
            internal / "cache",
            internal / "index",
            internal / "trash",
            internal / "references" / "raw",
            internal / "references" / "features",
            internal / "checkpoints",
        ):
            path.mkdir(parents=True, exist_ok=True)
        project_id = f"inkflow-{content_hash(str(project_root))[:10]}"
        atomic_write_json(
            internal / "project.json",
            {
                "project_id": project_id,
                "product": "墨流 / InkFlow",
                "schema_version": 1,
                "created_at": utc_now(),
                "permission_profile": "trusted_workspace",
                "trace_level": "full",
                "show_provider_reasoning": False,
            },
        )
        database = ProjectDatabase(internal / "inkflow.db")
        database.set_brief(brief)
        database.set_metadata("project_id", project_id)
        atomic_write_text(project_root / "BOOK.md", render_book_brief(brief))
        atomic_write_text(
            project_root / "PLAN.md",
            "# 小说规划\n\n> 尚未生成四级规划。请在编辑器中让墨流执行 `novel_plan_generate`。\n",
        )
        atomic_write_text(
            project_root / "STATE.md",
            "# 当前正史状态\n\n> 尚无已接受章节。草稿不会进入正史。\n",
        )
        return cls(project_root)

    @property
    def project_id(self) -> str:
        return str(self.config["project_id"])

    def latest_accepted_quality_hold(self) -> dict[str, Any] | None:
        """Expose a narrow legacy review defect before later chapters depend on it."""

        if self.db.canonical_content_migration_required():
            return None
        chapter_no = self.db.latest_accepted_chapter_no()
        if chapter_no < 1:
            return None
        chapter = self.db.get_chapter(chapter_no)
        review = self.db.latest_review_record(chapter_no)
        if not chapter or not review:
            return None
        content = self.db.canonical_chapter_content(chapter_no)
        if content is None or content_hash(content) != chapter["content_hash"]:
            return None
        resolutions = self.db.list_agent_artifacts(
            chapter_no=chapter_no, artifact_type="accepted_continuity_resolution", limit=50,
        )
        current_resolution = None
        for artifact in resolutions:
            data = artifact["data"]
            if (artifact["chapter_version"] == int(chapter["version"])
                    and data.get("source_review_id") == review["id"]
                    and data.get("new_hash") == chapter["content_hash"]):
                if artifact["status"] == "user_approved":
                    return None
                if artifact["status"] == "user_rejected":
                    return {"chapter_no": chapter_no, "source_hash": chapter["content_hash"],
                        "stage": "user_rejected", "first_evidence": "", "second_evidence": "",
                        "reason": "你已拒绝当前修订版，等待按理由续修和复核。",
                        "detail": data["reason"], "decision_id": artifact["artifact_id"]}
                if artifact["status"] == "verified":
                    current_resolution = artifact
        if review["chapter_version"] == int(chapter["version"]):
            if current_resolution:
                return None
            conflict = legacy_accepted_conflict(review["report"], content)
        elif current_resolution and current_resolution["data"].get("old_hash") != chapter["content_hash"]:
            conflict = legacy_accepted_conflict(review["report"], content, require_source_hash=False)
            original_evidence_found = conflict is not None
            if not conflict:
                anchor = str(current_resolution["data"].get("verification", {}).get("anchor_excerpt") or "")
                conflict = {
                    "first_evidence": anchor if anchor in content else "",
                    "second_evidence": "",
                    "detail": "原审查引文在当前修订版中已变化，请打开正文核对本次修改和前后因果。",
                }
            if conflict:
                conflict["reason"] = f"第 {chapter_no} 章的自动修订已保存，但仍需你核对当前版本；通过或写下拒绝原因。"
                conflict["detail"] = (
                    ("原审核指出：" if original_evidence_found else "") + str(conflict.get("detail") or "")
                    + " 自动复核自述："
                    + str(current_resolution["data"].get("verification", {}).get("reason") or "未提供说明")
                    + "。请重点核对前后动作及物品去向。"
                )
        else:
            return None
        return {
            "chapter_no": chapter_no,
            "source_hash": chapter["content_hash"],
            "stage": "repair_confirmation" if review["chapter_version"] != int(chapter["version"]) else "legacy_review",
            **conflict,
        } if conflict else None

    def approve_accepted_quality_hold(self, chapter_no: int, expected_hash: str) -> dict[str, Any]:
        """Record a user's explicit choice to keep the existing wording, not an AI pass."""
        with project_write_lock_sync(self.root):
            hold = self.latest_accepted_quality_hold()
            chapter = self.db.get_chapter(chapter_no)
            review = self.db.latest_review_record(chapter_no)
            if (not hold or int(hold["chapter_no"]) != chapter_no or not chapter or not review
                    or chapter["status"] != "accepted" or chapter["content_hash"] != expected_hash
                    or self.db.latest_accepted_chapter_no() != chapter_no):
                raise ProjectError("待核问题或正史版本已变化；请刷新后重新决定，未解除复核提醒。")
            content = self.db.canonical_chapter_content(chapter_no)
            path = self.root / str(chapter["path"])
            if (content is None or content_hash(content) != expected_hash or not path.is_file()
                    or content_hash(path.read_text(encoding="utf-8")) != expected_hash):
                raise ProjectError("数据库与正文文件不一致；不能按旧版本记录用户决定。")
            artifact = self.db.save_agent_artifact(
                artifact_type="accepted_continuity_resolution",
                run_id=f"user-hold-{chapter_no}-{uuid.uuid4().hex[:8]}",
                role="coordinator",
                chapter_no=chapter_no, chapter_version=int(chapter["version"]),
                dimension="continuity", status="user_approved",
                data={"source_review_id": review["id"], "old_hash": expected_hash,
                      "new_hash": expected_hash, "decision_by": "user",
                      "decision": "keep_current_text", "hold": hold},
            )
            resolutions = self.db.list_agent_artifacts(chapter_no=chapter_no,
                artifact_type="accepted_continuity_resolution", limit=200)
            for item in self.pending_work(chapter_no=chapter_no):
                if item["kind"] != "accepted_repair" or item.get("chapter_no") != chapter_no:
                    continue
                if hold.get("decision_id") and item["source"].get("decision_id") != hold["decision_id"]:
                    continue
                origin = next((value for value in resolutions
                    if value["artifact_id"] == item["source"].get("decision_id")
                    and value["status"] == "user_rejected"), None)
                if origin is None or origin["data"].get("source_review_id") != review["id"]:
                    continue
                hashes = {origin["data"]["new_hash"]}
                # ponytail: bounded artifact history; explicit audit lookup if more than 200 repairs are needed.
                for repair in reversed(resolutions):
                    data = repair["data"]
                    if (repair["status"] == "verified" and data.get("source_review_id") == review["id"]
                            and repair["chapter_version"] >= origin["chapter_version"]
                            and data.get("old_hash") in hashes):
                        hashes.add(data.get("new_hash"))
                if expected_hash in hashes:
                    self.update_pending_work(item["id"], status="completed",
                        resolution={"decision_by": "user", "decision": "keep_current_text",
                            "decision_id": artifact["artifact_id"], "source_decision_id": origin["artifact_id"],
                            "version": chapter["version"], "content_hash": expected_hash})
            return {"chapter_no": chapter_no, "version": int(chapter["version"]),
                    "content_hash": expected_hash, "decision": "user_approved",
                    "artifact_id": artifact["artifact_id"],
                    "summary": "已按你的选择保留当前正文并解除这处提醒；这不是模型复核通过。"}

    def reject_accepted_quality_hold(self, chapter_no: int, expected_hash: str, reason: str) -> dict[str, Any]:
        reason = reason.strip()
        if not reason or len(reason) > 4000:
            raise ProjectError("请填写 1～4000 字的明确拒绝理由。")
        with project_write_lock_sync(self.root):
            hold = self.latest_accepted_quality_hold()
            chapter = self.db.get_chapter(chapter_no)
            review = self.db.latest_review_record(chapter_no)
            if (not hold or hold["chapter_no"] != chapter_no or not chapter or not review
                    or chapter["status"] != "accepted" or chapter["content_hash"] != expected_hash):
                raise ProjectError("待核问题或当前正文已变化，请刷新后重新决定。")
            content = self.db.canonical_chapter_content(chapter_no)
            path = self.resolve_user_path(chapter["path"])
            if (content is None or content_hash(content) != expected_hash or not path.is_file()
                    or content_hash(path.read_text(encoding="utf-8")) != expected_hash):
                raise ProjectError("正文来源不一致，未按旧版本记录拒绝。")
            artifact = self.db.save_agent_artifact(artifact_type="accepted_continuity_resolution",
                run_id=f"user-reject-{uuid.uuid4().hex}", role="coordinator", chapter_no=chapter_no,
                chapter_version=chapter["version"], dimension="continuity", status="user_rejected",
                data={"decision_by": "user", "decision": "reject_current_repair", "reason": reason,
                      "source_review_id": review["id"], "old_hash": expected_hash, "new_hash": expected_hash,
                      "hold": hold})
            self.record_pending_work(kind="accepted_repair", reason=reason,
                next_action="按用户拒绝理由定向续修并复核，保留旧版；必要创作选择先询问。",
                source={"decision_id": artifact["artifact_id"], "version": chapter["version"], "content_hash": expected_hash,
                        "source_review_id": review["id"]},
                intent={"action": "repair_accepted", "chapter_no": chapter_no, "authorization": "proposed",
                        "requested_outcome": "按用户拒绝理由定向修复已接受章节并复核",
                        "visible_reason": "已有独立用户拒绝决定，等待明确续修授权。",
                        "operation_instruction": f"用户决定 ID：{artifact['artifact_id']}。请按以下拒绝理由局部修复并重新审核，保留旧版：{reason}"[:4000]},
                chapter_no=chapter_no, status="waiting_condition")
            return {"chapter_no": chapter_no, "version": chapter["version"], "content_hash": expected_hash,
                    "decision_id": artifact["artifact_id"], "summary": "已单独记录你的拒绝理由，当前正文保留，继续等待续修和复核。"}

    def accepted_revision_impact(self, chapter_no: int) -> dict[str, Any]:
        chapter = self.db.get_chapter(chapter_no)
        later = [{key: row[key] for key in ("chapter_no", "version", "content_hash", "path")}
                 for row in self.db.accepted_chapters() if row["chapter_no"] > chapter_no]
        with self.db.connect() as connection:
            own_facts = [row["fact_id"] for row in connection.execute("SELECT fact_id FROM facts WHERE source_chapter=?", (chapter_no,))]
            evidence_rows = connection.execute("SELECT key,value_json FROM metadata WHERE key LIKE 'memory.evidence:%'").fetchall()
        references = [row["key"].split(":", 1)[1] for row in evidence_rows
                      if any(ref.get("source_chapter") == chapter_no for ref in json.loads(row["value_json"]))]
        return {"source": {key: chapter[key] for key in ("chapter_no", "version", "content_hash", "path")} if chapter else None,
                "downstream_chapters": later, "source_fact_ids": own_facts, "referencing_fact_ids": references,
                "coverage": "direct_evidence_and_conservative_later_chapters",
                "explanation": "直接证据引用可定位；后续章是待核范围，不表示已证实每章都依赖这处改动。"}

    def _canonical_content_migration_preflight(self) -> tuple[dict[str, Any], dict[str, Any], dict[int, str]]:
        snapshot = self.db.canonical_content_sources()
        checks: list[dict[str, Any]] = []
        verified: dict[int, str] = {}
        for source in snapshot["sources"]:
            if not source["canonical_missing"]:
                continue
            check = {**source, "observed_hash": None, "reason": ""}
            try:
                path = self.resolve_user_path(str(source["path"]))
                if not path.is_file():
                    raise ProjectError("正文文件不存在或不是普通文件")
                text = path.read_text(encoding="utf-8")
                check["observed_hash"] = content_hash(text)
                if check["observed_hash"] == source["content_hash"]:
                    verified[int(source["chapter_no"])] = text
                else:
                    check["reason"] = "当前 Markdown 与已接受版本哈希不同，保留改稿，不自动回填"
            except (OSError, UnicodeError, ProjectError) as exc:
                check["reason"] = str(exc)
            checks.append(check)
        required = bool(snapshot["schema_required"] or checks)
        token = "canon-migration-" + content_hash(json.dumps(
            {"project_id": self.project_id, "snapshot": snapshot, "checks": checks},
            ensure_ascii=False, sort_keys=True,
        ))[:24]
        unresolved = [check for check in checks if check["reason"]]
        impact = (
            "会先在 .inkflow/backups 创建并核验 SQLite 备份，再在同一事务内增补正文列及回填缺失正文；"
            f"仅回填与原已接受版本哈希一致的 {len(verified)} 章，不修改章节版本、审查、事实或用户文件。"
            + ("不能自动回填：" + "；".join(f"第 {row['chapter_no']} 章：{row['reason']}" for row in unresolved) + "。可修复原文件后重新确认。" if unresolved else "")
            if required else "当前项目已具备可恢复的 SQLite 正史正文，无需升级。"
        )
        status = {
            "required": required,
            "confirmation_token": token if required else "",
            "accepted_chapter_count": len(snapshot["sources"]),
            "schema_required": snapshot["schema_required"],
            "missing_content_count": len(checks),
            "verified_chapter_count": len(verified),
            "source_checks": checks,
            "impact": impact,
        }
        return status, snapshot, verified

    def canonical_content_migration_status(self) -> dict[str, Any]:
        """Describe sources, hash mismatches and backup impact without applying changes."""
        return self._canonical_content_migration_preflight()[0]

    def apply_canonical_content_migration(self, confirmation_token: str) -> dict[str, Any]:
        """Back up and upgrade legacy projects after the user has explicitly confirmed."""

        with project_write_lock_sync(self.root):
            status, snapshot, verified = self._canonical_content_migration_preflight()
            if not status["required"]:
                return {**status, "applied": False, "message": "当前项目无需升级。"}
            if confirmation_token != status["confirmation_token"]:
                raise ProjectError("正史正文升级确认缺失或来源已变化；请重新查看逐章影响并确认。")
            backups = self.resolve_user_path(".inkflow/backups", allow_internal=True)
            backups.mkdir(parents=True, exist_ok=True)
            backup_path = backups / f"inkflow-before-canon-content-{utc_now().replace(':', '').replace('+00:00', 'Z')}-{uuid.uuid4().hex[:8]}.db"
            # A raw file copy can miss uncheckpointed WAL pages.  SQLite's
            # backup API creates a transactionally consistent local snapshot.
            with self.db.connect() as source:
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                    integrity = destination.execute("PRAGMA quick_check").fetchall()
                    if integrity != [("ok",)]:
                        raise ProjectError(f"正史数据库备份未通过完整性核验：{integrity}；未执行迁移。")
                finally:
                    destination.close()
            refreshed, refreshed_snapshot, refreshed_verified = self._canonical_content_migration_preflight()
            if refreshed["confirmation_token"] != status["confirmation_token"]:
                raise ProjectError("备份期间正史来源或文件变化，备份已保留，尚未迁移；请重新确认。")
            backfilled = self.db.migrate_canonical_content(
                expected_sources=refreshed_snapshot, verified_contents=refreshed_verified,
                audit={"backup_path": str(backup_path), "confirmed_token": confirmation_token,
                       "sources": snapshot, "source_checks": status["source_checks"], "applied_at": utc_now()},
            )
            unresolved = [int(row["chapter_no"]) for row in status["source_checks"] if row["reason"]]
            self.recovery_warnings = self.recover_accepted_chapter_projections()
            return {
                **self.canonical_content_migration_status(),
                "applied": True,
                "backup_path": str(backup_path),
                "backfilled_chapters": len(backfilled),
                "unresolved_chapters": unresolved,
                "message": "已核验本地备份并完成可恢复正史正文升级。" if not unresolved else "已完成可验证正文回填；未解决章节保留断点与原因，修复原文件后可再次确认回填。",
            }

    def resolve_user_path(self, relative_path: str | Path, *, allow_internal: bool = False) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ProjectError("路径越出小说项目根目录。")
        if not allow_internal and candidate.is_relative_to(self.internal):
            raise ProjectError("通用文件工具不能直接修改 .inkflow；请调用对应的结构化工具。")
        return candidate

    def read_file(self, relative_path: str) -> str:
        path = self.resolve_user_path(relative_path)
        if not path.is_file():
            raise ProjectError(f"文件不存在：{relative_path}")
        return path.read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str, *, overwrite: bool = True) -> Path:
        with project_write_lock_sync(self.root):
            path = self.resolve_user_path(relative_path)
            if path.exists() and not overwrite:
                raise ProjectError(f"文件已经存在：{relative_path}")
            return atomic_write_text(path, content)

    def delete_file(self, relative_path: str, *, permanent: bool = False) -> dict[str, Any]:
        with project_write_lock_sync(self.root):
            return self._delete_file_locked(relative_path, permanent=permanent)

    def _delete_file_locked(self, relative_path: str, *, permanent: bool = False) -> dict[str, Any]:
        path = self.resolve_user_path(relative_path)
        if not path.exists():
            raise ProjectError(f"目标不存在：{relative_path}")
        if permanent:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return {"deleted": str(path), "recoverable": False}
        stamp = utc_now().replace(":", "").replace("+00:00", "Z")
        trash_id = f"{stamp}-file-{uuid.uuid4().hex[:10]}"
        trash_dir = self.internal / "trash" / trash_id
        trash_dir.mkdir(parents=True, exist_ok=False)
        destination = trash_dir / safe_filename(path.name)
        digest = content_hash(path.read_bytes()) if path.is_file() else ""
        shutil.move(str(path), str(destination))
        atomic_write_json(
            trash_dir / "manifest.json",
            {
                "operation": "file_delete",
                "trash_id": trash_id,
                "source": str(path),
                "source_relative_path": path.relative_to(self.root).as_posix(),
                "destination": str(destination),
                "content_hash": digest,
                "deleted_at": utc_now(),
            },
        )
        return {
            "deleted": str(path), "recoverable": True, "trash_id": trash_id,
            "trash_path": str(destination), "content_hash": digest,
        }

    def list_document_trash(self) -> list[dict[str, Any]]:
        """List recoverable user-file deletions, excluding internal rollback archives."""
        trash_root = (self.internal / "trash").resolve()
        if not trash_root.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for manifest_path in trash_root.glob("*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("operation") != "file_delete":
                    continue
                trash_dir = manifest_path.parent.resolve()
                if not trash_dir.is_relative_to(trash_root):
                    continue
                destination = Path(str(manifest.get("destination") or "")).resolve()
                if not destination.is_relative_to(trash_dir) or not destination.is_file():
                    continue
                relative = str(manifest.get("source_relative_path") or "").replace("\\", "/")
                if not relative:
                    continue
                target = self.resolve_user_path(relative)
                items.append({
                    "trash_id": str(manifest.get("trash_id") or trash_dir.name),
                    "relative_path": relative,
                    "deleted_at": str(manifest.get("deleted_at") or ""),
                    "content_hash": str(manifest.get("content_hash") or content_hash(destination.read_bytes())),
                    "restore_blocked": target.exists(),
                })
            except (OSError, ValueError, TypeError, json.JSONDecodeError, ProjectError):
                continue
        return sorted(items, key=lambda item: item["deleted_at"], reverse=True)

    def restore_document(self, trash_id: str, *, expected_hash: str = "") -> dict[str, Any]:
        with project_write_lock_sync(self.root):
            trash_root = (self.internal / "trash").resolve()
            trash_dir = (trash_root / str(trash_id)).resolve()
            if not trash_dir.is_relative_to(trash_root) or not trash_dir.is_dir():
                raise ProjectError("回收站项目不存在或已被移除。")
            manifest_path = trash_dir / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProjectError("回收站记录损坏，未恢复文件。") from exc
            if manifest.get("operation") != "file_delete":
                raise ProjectError("该回收项不是普通文件删除记录，不能从此入口恢复。")
            relative = str(manifest.get("source_relative_path") or "").replace("\\", "/")
            target = self.resolve_user_path(relative)
            if target.exists():
                raise ProjectError(f"原位置已有同名文件，未覆盖：{relative}。请先移动现有文件后再恢复。")
            destination = Path(str(manifest.get("destination") or "")).resolve()
            if not destination.is_relative_to(trash_dir) or not destination.is_file():
                raise ProjectError("回收站里的文件不存在，无法恢复。")
            digest = content_hash(destination.read_bytes())
            if expected_hash and digest != expected_hash:
                raise ProjectError("回收内容已变化，请刷新回收站后再恢复。")
            chapter_match = re.search(r"chapter_(\d+)\.draft\.md$", relative)
            existing = self.db.get_chapter(int(chapter_match.group(1))) if chapter_match else None
            if existing and existing.get("status") == "accepted":
                raise ProjectError("该章节已有正史，不能把草稿直接恢复为现行章节；请先另存为修订提案。")
            if existing and existing.get("status") == "draft" and str(existing.get("path") or "").replace("\\", "/") != relative:
                raise ProjectError("该章节已有另一份草稿记录，未覆盖。请先核对章节工作区。")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(destination), str(target))
            if chapter_match:
                chapter_no = int(chapter_match.group(1))
                title = str((existing or {}).get("title") or target.stem)
                try:
                    self.db.upsert_draft(chapter_no, title, relative, target.read_text(encoding="utf-8"))
                except Exception:
                    shutil.move(str(target), str(destination))
                    raise
            return {"restored": relative, "trash_id": str(trash_id), "content_hash": digest}

    def prepare_file_commit(self, final_relative: str | Path, content: str) -> dict[str, Any]:
        """Stage an accepted chapter before its SQLite transaction commits."""
        transactions = self.internal / "transactions"
        transactions.mkdir(parents=True, exist_ok=True)
        transaction_id = f"chapter-{uuid.uuid4().hex}"
        staged_path = transactions / f"{transaction_id}.md"
        journal_path = transactions / f"{transaction_id}.json"
        final_path = self.resolve_user_path(final_relative)
        state_path = self.resolve_user_path("STATE.md")
        previous_file_hash = self._projection_hash(final_path)
        previous_state_hash = self._projection_hash(state_path)
        chapter_no = int(final_path.stem.split("_")[-1])
        source_chapter = self.db.get_chapter(chapter_no)
        if not source_chapter:
            raise ProjectError("正史投影缺少已登记章节，未创建提交日志。")
        # Ordinary acceptance keeps the draft version; the sole accepted-repair
        # caller creates the next version in its guarded SQLite transaction.
        chapter_version = int(source_chapter["version"]) + (source_chapter["status"] == "accepted")
        atomic_write_text(staged_path, content)
        journal = {
            "transaction_id": transaction_id,
            "status": "prepared",
            "final_path": final_path.relative_to(self.root).as_posix(),
            "staged_path": staged_path.relative_to(self.root).as_posix(),
            "content_hash": content_hash(content),
            "chapter_no": chapter_no,
            "chapter_version": chapter_version,
            "source_version": int(source_chapter["version"]),
            "source_hash": str(source_chapter["content_hash"]),
            "previous_file_hash": previous_file_hash,
            "previous_state_hash": previous_state_hash,
            "created_at": utc_now(),
        }
        atomic_write_json(journal_path, journal)
        return {"journal_path": journal_path, "staged_path": staged_path, "final_path": final_path, **journal}

    def mark_file_commit_database(self, transaction: dict[str, Any]) -> None:
        journal_path = Path(transaction["journal_path"])
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "database_committed"
        journal["database_committed_at"] = utc_now()
        atomic_write_json(journal_path, journal)

    def finalize_file_commit(self, transaction: dict[str, Any]) -> None:
        """Finish only the projections whose pre-commit versions still match."""
        journal_path = Path(transaction["journal_path"])
        if not journal_path.exists():
            final_path = self.resolve_user_path(str(transaction.get("final_path", "")))
            expected_hash = str(transaction.get("content_hash", ""))
            chapter_no = int(final_path.stem.split("_")[-1])
            chapter = self.db.get_chapter(chapter_no)
            canonical = self.db.canonical_chapter_content(chapter_no)
            if (chapter and chapter["status"] == "accepted" and chapter["content_hash"] == expected_hash
                    and ("chapter_version" not in transaction or int(chapter["version"]) == int(transaction["chapter_version"]))
                    and self.resolve_user_path(str(chapter["path"])) == final_path
                    and canonical is not None and content_hash(canonical) == expected_hash
                    and final_path.is_file() and expected_hash and self._projection_hash(final_path) == expected_hash):
                desired_state = render_state(self.db.current_facts(), self.db.open_threads(), self.db.project_status())
                if self._projection_hash(self.resolve_user_path("STATE.md")) == content_hash(desired_state):
                    return
            raise ProjectError("正史提交日志不存在，且正文或状态投影尚未验证完成。")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        final_path = self.resolve_user_path(str(journal["final_path"]))
        staged_path = (self.root / str(journal["staged_path"])).resolve()
        transactions = (self.internal / "transactions").resolve()
        if not staged_path.is_relative_to(transactions):
            raise ProjectError("正史事务暂存路径越界，已停止同步。")
        chapter_no = int(final_path.stem.split("_")[-1])
        chapter = self.db.get_chapter(chapter_no)
        expected_hash = str(journal["content_hash"])
        canonical_content = self.db.canonical_chapter_content(chapter_no)
        if (
            not chapter or chapter["status"] != "accepted"
            or ("chapter_no" in journal and int(journal["chapter_no"]) != chapter_no)
            or ("chapter_version" in journal and int(chapter["version"]) != int(journal["chapter_version"]))
            or str(chapter["path"]).replace("\\", "/") != str(journal["final_path"]).replace("\\", "/")
            or chapter["content_hash"] != expected_hash
            or canonical_content is None or content_hash(canonical_content) != expected_hash
        ):
            raise ProjectError("正史事务与数据库版本不一致，已保留日志等待核对。")
        final_hash = self._projection_hash(final_path)
        state_path = self.resolve_user_path("STATE.md")
        desired_state = render_state(self.db.current_facts(), self.db.open_threads(), self.db.project_status())
        desired_state_hash = content_hash(desired_state)
        state_hash = self._projection_hash(state_path)
        if final_hash not in {expected_hash, journal.get("previous_file_hash")}:
            raise ProjectError(f"第 {chapter_no} 章文件在提交后发生变化；正史已入库，原文件未被覆盖，等待用户核对。")
        if state_hash not in {desired_state_hash, journal.get("previous_state_hash")}:
            raise ProjectError("STATE.md 在提交后发生变化；正史已入库，用户文件未被覆盖，等待核对。")
        if final_hash != expected_hash:
            if staged_path.is_file() and self._projection_hash(staged_path) == expected_hash:
                if final_path.exists():
                    self._archive_projection(final_path)
                if self._projection_hash(final_path) != final_hash:
                    raise ProjectError(f"第 {chapter_no} 章文件在同步期间变化，已停止替换并保留事务日志。")
                final_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_path, final_path)
            else:
                if final_path.exists():
                    self._archive_projection(final_path)
                if self._projection_hash(final_path) != final_hash:
                    raise ProjectError(f"第 {chapter_no} 章文件在同步期间变化，已停止替换并保留事务日志。")
                atomic_write_text(final_path, canonical_content)
        journal["status"] = "file_committed"
        journal["file_committed_at"] = utc_now()
        atomic_write_json(journal_path, journal)
        if self._projection_hash(state_path) not in {desired_state_hash, journal.get("previous_state_hash")}:
            raise ProjectError("STATE.md 在正文同步期间发生变化；已保留事务日志，未覆盖用户文件。")
        if self._projection_hash(state_path) != desired_state_hash:
            prior_state_hash = self._projection_hash(state_path)
            if state_path.exists():
                self._archive_projection(state_path)
            if self._projection_hash(state_path) != prior_state_hash:
                raise ProjectError("STATE.md 在同步期间变化，已停止替换并保留事务日志。")
            atomic_write_text(state_path, desired_state)
        staged_path.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)

    @staticmethod
    def _projection_hash(path: Path) -> str | None:
        if not path.exists():
            return None
        if not path.is_file():
            raise ProjectError(f"投影路径不是普通文件：{path}")
        return content_hash(path.read_text(encoding="utf-8"))

    def _archive_projection(self, path: Path) -> None:
        """Keep the previous projection recoverable before atomic replacement."""
        stamp = utc_now().replace(":", "").replace("+00:00", "Z")
        trash_dir = self.internal / "trash" / f"projection-{stamp}-{uuid.uuid4().hex[:8]}"
        trash_dir.mkdir(parents=True, exist_ok=False)
        destination = trash_dir / safe_filename(path.name)
        shutil.copy2(path, destination)
        atomic_write_json(trash_dir / "manifest.json", {
            "source": str(path), "destination": str(destination),
            "archived_at": utc_now(), "reason": "accepted_projection_replaced",
        })

    def recover_pending_commits(self) -> list[str]:
        """Finish or discard accepted-chapter journals after a process crash."""
        transactions = self.internal / "transactions"
        if not transactions.exists():
            return []
        warnings: list[str] = []
        for journal_path in sorted(transactions.glob("chapter-*.json")):
            chapter = None
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                staged_path = (self.root / journal["staged_path"]).resolve()
                final_path = (self.root / journal["final_path"]).resolve()
                if not staged_path.is_relative_to(transactions.resolve()) or not final_path.is_relative_to(self.root):
                    warnings.append(f"已忽略越界的正史事务日志：{journal_path.name}")
                    continue
                chapter_no = int(Path(journal["final_path"]).stem.split("_")[-1])
                chapter = self.db.get_chapter(chapter_no)
                accepted = bool(chapter and chapter["status"] == "accepted")
                if not accepted:
                    if journal.get("status") != "prepared":
                        raise ProjectError("日志记录已入库，但当前数据库无对应正史；保留日志及暂存等待核对数据库来源。")
                    if (not chapter or chapter["status"] != "draft"
                            or chapter["content_hash"] != journal.get("source_hash", journal.get("content_hash"))
                            or ("source_version" in journal and int(chapter["version"]) != int(journal["source_version"]))):
                        raise ProjectError("未提交日志的原草稿来源已变化或缺失，保留日志及暂存等待核对。")
                    if staged_path.exists() and self._projection_hash(staged_path) != journal.get("content_hash"):
                        raise ProjectError("未提交暂存正文已变化，保留文件及日志等待核对。")
                    staged_path.unlink(missing_ok=True)
                    journal_path.unlink(missing_ok=True)
                    continue
                self.finalize_file_commit({"journal_path": journal_path})
                self._finish_projection_pending_work(chapter)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error, ProjectError) as exc:
                warning = f"正史事务恢复失败（{journal_path.name}）：{exc}"
                warnings.append(warning)
                self._record_projection_pending_work(chapter, warning, warnings)
        return warnings

    def _record_projection_pending_work(self, chapter: dict[str, Any] | None, reason: str, warnings: list[str]) -> None:
        if not chapter or chapter["status"] != "accepted":
            return
        try:
            self.record_pending_work(
                kind="accepted_projection", chapter_no=int(chapter["chapter_no"]), status="waiting_condition",
                source={key: chapter[key] for key in ("chapter_no", "version", "content_hash", "path")},
                reason=reason,
                next_action="核对原事务日志与原接受任务凭据，仅恢复本地投影；保留用户改稿，不重复接受或提交记忆。缺原数据库正文时先查看备份迁移影响。",
            )
        except (OSError, sqlite3.Error, ProjectError) as exc:
            warnings.append(f"投影待处理事项暂未保存：{exc}；请保留原事务日志。")

    def _finish_projection_pending_work(self, chapter: dict[str, Any]) -> None:
        source = {key: chapter[key] for key in ("chapter_no", "version", "content_hash", "path")}
        pending = [item for item in self.pending_work(chapter_no=int(chapter["chapter_no"]))
                   if item["kind"] == "accepted_projection" and item["source"] == source]
        if not pending:
            return
        current = self.db.get_chapter(int(chapter["chapter_no"]))
        if not current or current["status"] != "accepted" or any(current[key] != value for key, value in source.items()):
            return
        # An unresolved journal is still a recovery dependency, even when a
        # different journal has already made this chapter's projection match.
        for path in (self.internal / "transactions").glob("chapter-*.json"):
            journal = json.loads(path.read_text(encoding="utf-8"))
            if int(Path(journal["final_path"]).stem.split("_")[-1]) == int(chapter["chapter_no"]):
                return
        if self._projection_hash(self.resolve_user_path(str(chapter["path"]))) != chapter["content_hash"]:
            return
        canonical = self.db.canonical_chapter_content(int(chapter["chapter_no"]))
        if canonical is None or content_hash(canonical) != chapter["content_hash"]:
            return
        state = render_state(self.db.current_facts(), self.db.open_threads(), self.db.project_status())
        if self._projection_hash(self.resolve_user_path("STATE.md")) != content_hash(state):
            return
        for item in pending:
            self.update_pending_work(item["id"], status="completed", resolution={"source": source, "projection_status": "synced"})

    def recover_accepted_chapter_projections(self) -> list[str]:
        """Report projection drift; only a verified pending journal may auto-repair it."""

        warnings: list[str] = []
        for chapter in self.db.accepted_chapters():
            chapter_no = int(chapter["chapter_no"])
            try:
                final_path = self.resolve_user_path(str(chapter["path"]))
            except ProjectError as exc:
                warning = f"第 {chapter_no} 章投影路径无效：{exc}；未改动文件。"
                warnings.append(warning)
                self._record_projection_pending_work(chapter, warning, warnings)
                continue
            canonical_content = self.db.canonical_chapter_content(chapter_no)
            if canonical_content is None:
                warning = f"第 {chapter_no} 章数据库中没有可恢复正文；请保留当前 Markdown 并走备份迁移。"
                warnings.append(warning)
                self._record_projection_pending_work(chapter, warning, warnings)
                continue
            expected_hash = content_hash(canonical_content)
            if expected_hash != chapter["content_hash"]:
                warning = f"第 {chapter_no} 章数据库正文与版本哈希不一致，已停止自动同步。"
                warnings.append(warning)
                self._record_projection_pending_work(chapter, warning, warnings)
                continue
            try:
                if self._projection_hash(final_path) == expected_hash:
                    self._finish_projection_pending_work(chapter)
                    continue
            except (OSError, UnicodeError, ValueError, KeyError, TypeError, sqlite3.Error, ProjectError) as exc:
                warning = f"第 {chapter_no} 章投影恢复核验未完成：{exc}；未改动该文件。"
                warnings.append(warning)
                self._record_projection_pending_work(chapter, warning, warnings)
                continue
            warning = f"第 {chapter_no} 章 Markdown 与正史数据库不一致或已被删除；未覆盖用户文件，请核对后恢复。"
            warnings.append(warning)
            self._record_projection_pending_work(chapter, warning, warnings)
        return warnings

    def run_powershell(self, command: str, timeout_seconds: int = 60) -> dict[str, Any]:
        from .config import Settings

        if not Settings.from_env(self.root).powershell_enabled:
            raise ProjectError("PowerShell 能力默认关闭。请先在设置中阅读影响并明确开启，再重试该命令。")
        if not command.strip():
            raise ProjectError("PowerShell 命令不能为空。")

        command_hash = content_hash(command)
        request = self.db.create_computer_action_request(command, command_hash, ttl_seconds=300)
        request_id = str(request["request_id"])
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = self.db.get_computer_action_request(request_id)
            if current is None:
                raise ProjectError("电脑操作确认请求丢失；没有执行任何命令。")
            if current["status"] in {"rejected", "expired"}:
                return {
                    "status": str(current["status"]),
                    "request_id": request_id,
                    "command_hash": command_hash,
                    "message": "用户未批准此命令；没有执行任何操作。",
                }
            if current["status"] == "approved":
                break
            time.sleep(0.25)
        else:
            # Listing pending requests also atomically marks expired tickets.
            self.db.list_pending_computer_action_requests()
            return {
                "status": "expired",
                "request_id": request_id,
                "command_hash": command_hash,
                "message": "等待确认超时；命令没有执行。请重新提出操作并在墨流弹窗中确认。",
            }

        with project_write_lock_sync(self.root):
            if not Settings.from_env(self.root).powershell_enabled:
                self.db.cancel_approved_computer_action(request_id, command_hash)
                raise ProjectError("PowerShell 权限已关闭；命令没有执行。")
            claimed = self.db.claim_approved_computer_action(request_id, command_hash)
            if claimed is None:
                raise ProjectError("电脑操作确认已失效或已被使用；命令没有执行。")
            try:
                result = self._run_powershell_locked(command, timeout_seconds)
            except Exception as exc:
                self.db.finish_computer_action_request(request_id, exit_code=None, error=str(exc))
                raise
            self.db.finish_computer_action_request(request_id, exit_code=int(result["exit_code"]))
            return {**result, "status": "completed" if int(result["exit_code"]) == 0 else "failed", "request_id": request_id}

    def _run_powershell_locked(self, command: str, timeout_seconds: int = 60) -> dict[str, Any]:
        from .config import Settings

        if not Settings.from_env(self.root).powershell_enabled:
            raise ProjectError("PowerShell 能力默认关闭。请先在设置中阅读影响并明确开启，再重试该命令。")
        if not command.strip():
            raise ProjectError("PowerShell 命令不能为空。")
        environment = os.environ.copy()
        environment["INKFLOW_PROJECT_ROOT"] = str(self.root)
        powershell = (
            Path(environment.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout_seconds, 600)),
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return {
            "cwd": str(self.root),
            "command_hash": content_hash(command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-20_000:],
            "stderr": completed.stderr[-20_000:],
        }


def render_book_brief(brief: BookBrief) -> str:
    rules = "\n".join(f"- {item}" for item in brief.user_rules) or "- 暂无额外规则"
    return f"""# {brief.title}

## 项目契约

- 题材：{brief.genre}
- 目标读者：{brief.target_audience}
- 主角：{brief.protagonist}
- 核心卖点：{brief.core_selling_point or '待规划时细化'}
- 单章目标：{brief.target_chapter_words} 字
- 预计规模：{brief.estimated_volumes} 卷 / {brief.estimated_chapters} 章

## 故事前提

{brief.premise}

## 用户规则

{rules}

> 本文件是数据库的人类可读投影。修改后应通过墨流的计划补丁工具导入，不要让宿主 Agent 直接改数据库。
"""
