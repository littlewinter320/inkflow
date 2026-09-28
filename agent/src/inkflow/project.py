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
from typing import Any

from .database import ProjectDatabase
from .errors import ProjectError
from .project_lock import project_write_lock_sync
from .render import render_state
from .review_verifier import legacy_accepted_conflict
from .schemas import BookBrief
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
                role="coordinator", role_protocol_version=2,
                chapter_no=chapter_no, chapter_version=int(chapter["version"]),
                dimension="continuity", status="user_approved",
                data={"source_review_id": review["id"], "old_hash": expected_hash,
                      "new_hash": expected_hash, "decision_by": "user",
                      "decision": "keep_current_text", "hold": hold},
            )
            return {"chapter_no": chapter_no, "version": int(chapter["version"]),
                    "content_hash": expected_hash, "decision": "user_approved",
                    "artifact_id": artifact["artifact_id"],
                    "summary": "已按你的选择保留当前正文并解除这处提醒；这不是模型复核通过。"}

    def canonical_content_migration_status(self) -> dict[str, Any]:
        """Describe the additive accepted-text migration without applying it."""

        required = self.db.canonical_content_migration_required()
        accepted = self.db.accepted_chapters()
        token = f"canon-migration-{content_hash(self.project_id + str(len(accepted)))[:16]}"
        return {
            "required": required,
            "confirmation_token": token if required else "",
            "accepted_chapter_count": len(accepted),
            "impact": (
                "会先在 .inkflow/backups 创建 SQLite 备份，再为 chapters 表增加 content_text 字段；"
                "随后仅在正文哈希一致时把已接受 Markdown 回填到数据库。"
                if required
                else "当前项目已经具备 SQLite 正史正文列，无需升级。"
            ),
        }

    def apply_canonical_content_migration(self, confirmation_token: str) -> dict[str, Any]:
        """Back up and upgrade legacy projects after the user has explicitly confirmed."""

        with project_write_lock_sync(self.root):
            status = self.canonical_content_migration_status()
            if not status["required"]:
                return {**status, "applied": False, "message": "当前项目无需升级。"}
            if confirmation_token != status["confirmation_token"]:
                raise ProjectError("正史正文数据库升级尚未确认；请先阅读影响并在界面中确认。")
            backups = self.internal / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            backup_path = backups / f"inkflow-before-canon-content-{utc_now().replace(':', '').replace('+00:00', 'Z')}.db"
            # A raw file copy can miss uncheckpointed WAL pages.  SQLite's
            # backup API creates a transactionally consistent local snapshot.
            with self.db.connect() as source:
                destination = sqlite3.connect(backup_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            self.db.migrate_canonical_content()
            backfilled = 0
            unresolved: list[int] = []
            for chapter in self.db.accepted_chapters():
                chapter_no = int(chapter["chapter_no"])
                projection = self.root / str(chapter["path"])
                if projection.is_file() and self.db.backfill_canonical_chapter_content(
                    chapter_no, projection.read_text(encoding="utf-8")
                ):
                    backfilled += 1
                elif self.db.canonical_chapter_content(chapter_no) is None:
                    unresolved.append(chapter_no)
            self.recovery_warnings = self.recover_accepted_chapter_projections()
            return {
                **self.canonical_content_migration_status(),
                "applied": True,
                "backup_path": str(backup_path),
                "backfilled_chapters": backfilled,
                "unresolved_chapters": unresolved,
                "message": "已创建本地备份并完成可恢复正史正文升级。" if not unresolved else "升级已完成，但部分旧章节无法从现有 Markdown 验证回填。",
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
        atomic_write_text(staged_path, content)
        journal = {
            "transaction_id": transaction_id,
            "status": "prepared",
            "final_path": final_path.relative_to(self.root).as_posix(),
            "staged_path": staged_path.relative_to(self.root).as_posix(),
            "content_hash": content_hash(content),
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
            final_path = Path(transaction.get("final_path", ""))
            expected_hash = str(transaction.get("content_hash", ""))
            if final_path.is_file() and expected_hash and self._projection_hash(final_path) == expected_hash:
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
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                staged_path = (self.root / journal["staged_path"]).resolve()
                final_path = (self.root / journal["final_path"]).resolve()
                if not staged_path.is_relative_to(transactions.resolve()) or not final_path.is_relative_to(self.root):
                    warnings.append(f"已忽略越界的正史事务日志：{journal_path.name}")
                    continue
                chapter_no = int(Path(journal["final_path"]).stem.split("_")[-1])
                chapter = self.db.get_chapter(chapter_no)
                accepted = bool(chapter and chapter["status"] == "accepted" and chapter["path"] == journal["final_path"])
                if not accepted:
                    staged_path.unlink(missing_ok=True)
                    journal_path.unlink(missing_ok=True)
                    continue
                self.finalize_file_commit({"journal_path": journal_path})
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, ProjectError) as exc:
                warnings.append(f"正史事务恢复失败（{journal_path.name}）：{exc}")
        return warnings

    def recover_accepted_chapter_projections(self) -> list[str]:
        """Report projection drift; only a verified pending journal may auto-repair it."""

        warnings: list[str] = []
        for chapter in self.db.accepted_chapters():
            chapter_no = int(chapter["chapter_no"])
            try:
                final_path = self.resolve_user_path(str(chapter["path"]))
            except ProjectError as exc:
                warnings.append(f"第 {chapter_no} 章投影路径无效：{exc}；未改动文件。")
                continue
            canonical_content = self.db.canonical_chapter_content(chapter_no)
            if canonical_content is None:
                warnings.append(f"第 {chapter_no} 章数据库中没有可恢复正文；请保留当前 Markdown 并走备份迁移。")
                continue
            expected_hash = content_hash(canonical_content)
            if expected_hash != chapter["content_hash"]:
                warnings.append(f"第 {chapter_no} 章数据库正文与版本哈希不一致，已停止自动同步。")
                continue
            try:
                if self._projection_hash(final_path) == expected_hash:
                    continue
            except (OSError, ProjectError) as exc:
                warnings.append(f"第 {chapter_no} 章 Markdown 无法读取：{exc}；未改动该文件。")
                continue
            warnings.append(f"第 {chapter_no} 章 Markdown 与正史数据库不一致或已被删除；未覆盖用户文件，请核对后恢复。")
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
