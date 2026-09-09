from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .database import ProjectDatabase
from .errors import ProjectError
from .project_lock import project_write_lock_sync
from .schemas import BookBrief
from .utils import atomic_write_json, atomic_write_text, content_hash, safe_filename, utc_now


class InkFlowProject:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.internal = self.root / ".inkflow"
        self.config_path = self.internal / "project.json"
        if not self.config_path.exists():
            raise ProjectError(f"这里不是墨流小说项目：{self.root}")
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.db = ProjectDatabase(self.internal / "inkflow.db")
        with project_write_lock_sync(self.root):
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
        trash_dir = self.internal / "trash" / stamp
        trash_dir.mkdir(parents=True, exist_ok=True)
        destination = trash_dir / safe_filename(path.name)
        shutil.move(str(path), str(destination))
        atomic_write_json(
            trash_dir / "manifest.json",
            {"source": str(path), "destination": str(destination), "deleted_at": utc_now()},
        )
        return {"deleted": str(path), "recoverable": True, "trash_path": str(destination)}

    def prepare_file_commit(self, final_relative: str | Path, content: str) -> dict[str, Any]:
        """Stage an accepted chapter before its SQLite transaction commits."""
        transactions = self.internal / "transactions"
        transactions.mkdir(parents=True, exist_ok=True)
        transaction_id = f"chapter-{uuid.uuid4().hex}"
        staged_path = transactions / f"{transaction_id}.md"
        journal_path = transactions / f"{transaction_id}.json"
        final_path = self.resolve_user_path(final_relative)
        atomic_write_text(staged_path, content)
        journal = {
            "transaction_id": transaction_id,
            "status": "prepared",
            "final_path": str(final_path.relative_to(self.root)),
            "staged_path": str(staged_path.relative_to(self.root)),
            "content_hash": content_hash(content),
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
        """Atomically switch the staged file into place and close its journal."""
        journal_path = Path(transaction["journal_path"])
        staged_path = Path(transaction["staged_path"])
        final_path = Path(transaction["final_path"])
        if not staged_path.exists():
            raise ProjectError("正史事务暂存文件不存在，已停止提交以避免覆盖正文。")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists() and content_hash(final_path.read_text(encoding="utf-8")) == transaction["content_hash"]:
            staged_path.unlink()
        else:
            if final_path.exists():
                self.delete_file(final_path.relative_to(self.root).as_posix(), permanent=False)
            os.replace(staged_path, final_path)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "file_committed"
        journal["file_committed_at"] = utc_now()
        atomic_write_json(journal_path, journal)
        journal_path.unlink(missing_ok=True)

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
                expected_hash = str(journal.get("content_hash", ""))
                canonical_content = self.db.canonical_chapter_content(chapter_no)
                if staged_path.exists():
                    staged_content = staged_path.read_text(encoding="utf-8")
                    if content_hash(staged_content) != expected_hash:
                        if canonical_content is None or content_hash(canonical_content) != expected_hash:
                            warnings.append(f"第 {chapter_no} 章暂存文件与数据库正文都无法通过哈希：{journal_path.name}")
                            continue
                        staged_path.unlink()
                        staged_content = canonical_content
                    if final_path.exists() and content_hash(final_path.read_text(encoding="utf-8")) != expected_hash:
                        self.delete_file(final_path.relative_to(self.root).as_posix(), permanent=False)
                    if staged_path.exists():
                        os.replace(staged_path, final_path)
                    else:
                        atomic_write_text(final_path, staged_content)
                elif not final_path.exists() or content_hash(final_path.read_text(encoding="utf-8")) != expected_hash:
                    if canonical_content is None or content_hash(canonical_content) != expected_hash:
                        warnings.append(f"第 {chapter_no} 章正史事务缺少可验证的数据库正文：{journal_path.name}")
                        continue
                    if final_path.exists():
                        self.delete_file(final_path.relative_to(self.root).as_posix(), permanent=False)
                    atomic_write_text(final_path, canonical_content)
                journal_path.unlink(missing_ok=True)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                warnings.append(f"正史事务恢复失败（{journal_path.name}）：{exc}")
        return warnings

    def recover_accepted_chapter_projections(self) -> list[str]:
        """Rebuild accepted Markdown from SQLite, which is the canonical content store."""

        warnings: list[str] = []
        for chapter in self.db.accepted_chapters():
            chapter_no = int(chapter["chapter_no"])
            final_path = self.resolve_user_path(str(chapter["path"]))
            canonical_content = self.db.canonical_chapter_content(chapter_no)
            if canonical_content is None:
                if final_path.is_file():
                    projected = final_path.read_text(encoding="utf-8")
                    if self.db.backfill_canonical_chapter_content(chapter_no, projected):
                        continue
                warnings.append(f"第 {chapter_no} 章是旧版正史，但数据库中没有可恢复正文；请保留当前 Markdown。")
                continue
            expected_hash = content_hash(canonical_content)
            if final_path.is_file() and content_hash(final_path.read_text(encoding="utf-8")) == expected_hash:
                continue
            if final_path.exists():
                self.delete_file(final_path.relative_to(self.root).as_posix(), permanent=False)
            atomic_write_text(final_path, canonical_content)
            warnings.append(f"已从 SQLite 正史恢复第 {chapter_no} 章 Markdown 投影。")
        return warnings

    def run_powershell(self, command: str, timeout_seconds: int = 60) -> dict[str, Any]:
        with project_write_lock_sync(self.root):
            return self._run_powershell_locked(command, timeout_seconds)

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
