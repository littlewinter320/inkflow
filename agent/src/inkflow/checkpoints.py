from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from .errors import ProjectError, ValidationGateError
from .project import InkFlowProject
from .utils import atomic_write_bytes, atomic_write_json, content_hash, json_dumps, safe_filename, utc_now


_CHECKPOINT_ID = re.compile(r"^cp-[0-9A-Za-zT]+-[0-9a-f]{8}$")
_TOP_LEVEL_FILES = ("BOOK.md", "PLAN.md", "STATE.md")
_MANAGED_DIRS = ("chapters", "reviews")


class CheckpointService:
    """Consistent SQLite + managed Markdown snapshots with branch-preserving restore."""

    def __init__(self, project: InkFlowProject):
        self.project = project
        self.root = (project.internal / "checkpoints").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"
        self.history_path = self.root / "history.jsonl"
        self.lock_path = self.root / "restore.lock"
        self.journal_path = self.root / "restore-in-progress.json"

    def create(
        self,
        *,
        label: str,
        reason: str,
        advance: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        checkpoint_id = self._new_checkpoint_id(now)
        current = self._read_json(self.current_path, {})
        parent_id = current.get("checkpoint_id")
        branch_id = str(current.get("branch_id") or "main")
        temp_dir = (self.root / f".tmp-{uuid.uuid4().hex}").resolve()
        final_dir = (self.root / checkpoint_id).resolve()
        if not temp_dir.is_relative_to(self.root) or not final_dir.is_relative_to(self.root):
            raise ProjectError("检查点路径越界。")
        temp_dir.mkdir(parents=True, exist_ok=False)
        try:
            database_path = temp_dir / "inkflow.db"
            self._backup_database(database_path)
            file_records: list[dict[str, Any]] = []
            for source, relative in self._managed_files():
                destination = temp_dir / "files" / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                raw = destination.read_bytes()
                file_records.append(
                    {"path": relative, "sha256": content_hash(raw), "size": len(raw)}
                )
            database_raw = database_path.read_bytes()
            status = self.project.db.project_status()
            manifest = {
                "schema_version": 1,
                "checkpoint_id": checkpoint_id,
                "parent_id": parent_id,
                "branch_id": branch_id,
                "created_at": now,
                "label": label.strip()[:200] or "未命名检查点",
                "reason": reason.strip()[:100] or "manual",
                "boundary_chapter": self.project.db.latest_accepted_chapter_no(),
                "status": status,
                "database": {
                    "path": "inkflow.db",
                    "sha256": content_hash(database_raw),
                    "size": len(database_raw),
                },
                "files": sorted(file_records, key=lambda item: item["path"]),
            }
            manifest["state_hash"] = content_hash(json_dumps(manifest, indent=None))
            atomic_write_json(temp_dir / "manifest.json", manifest)
            os.replace(temp_dir, final_dir)
            if advance:
                self._write_current(checkpoint_id, branch_id, restored_from=None)
            self._append_history(
                {
                    "event": "checkpoint_created",
                    "checkpoint_id": checkpoint_id,
                    "parent_id": parent_id,
                    "branch_id": branch_id,
                    "created_at": now,
                    "reason": manifest["reason"],
                }
            )
            return self._summary(manifest)
        except Exception:
            if temp_dir.exists() and temp_dir.is_relative_to(self.root):
                shutil.rmtree(temp_dir)
            raise

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or not _CHECKPOINT_ID.fullmatch(directory.name):
                continue
            path = directory / "manifest.json"
            if not path.is_file():
                continue
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        manifests.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
        return [self._summary(item) for item in manifests[: max(1, min(limit, 500))]]

    def resolve(self, checkpoint_id: str | None = None, boundary_chapter: int | None = None) -> str:
        if checkpoint_id:
            self._load_manifest(checkpoint_id)
            return checkpoint_id
        if boundary_chapter is None:
            raise ValidationGateError("请给出 checkpoint_id，或说明要回到哪一章已接受之后。")
        matches = [item for item in self.list(500) if item["boundary_chapter"] == boundary_chapter]
        if not matches:
            raise ValidationGateError(f"没有找到第 {boundary_chapter} 章已接受后的检查点。")
        return str(matches[0]["checkpoint_id"])

    def preview_restore(self, checkpoint_id: str) -> dict[str, Any]:
        manifest = self._load_manifest(checkpoint_id, validate=True)
        current_records = {
            relative: {"sha256": content_hash(path.read_bytes()), "size": path.stat().st_size}
            for path, relative in self._managed_files()
        }
        target_records = {item["path"]: item for item in manifest["files"]}
        current_paths = set(current_records)
        target_paths = set(target_records)
        create_paths = sorted(target_paths - current_paths)
        remove_paths = sorted(current_paths - target_paths)
        overwrite_paths = sorted(
            path
            for path in current_paths & target_paths
            if current_records[path]["sha256"] != target_records[path]["sha256"]
        )
        unchanged = len(current_paths & target_paths) - len(overwrite_paths)
        fingerprint_payload = {
            "status": self.project.db.project_status(),
            "files": current_records,
            "current": self._read_json(self.current_path, {}),
        }
        fingerprint = content_hash(json_dumps(fingerprint_payload, indent=None))
        confirmation_token = content_hash(f"restore:{checkpoint_id}:{fingerprint}")[:24]
        return {
            "checkpoint": self._summary(manifest),
            "current_status": self.project.db.project_status(),
            "target_status": manifest["status"],
            "impact": {
                "create": create_paths,
                "overwrite": overwrite_paths,
                "remove_to_recoverable_trash": remove_paths,
                "unchanged": unchanged,
            },
            "confirmation_token": confirmation_token,
            "instruction": (
                "确认后会先为当前状态创建安全检查点，再恢复目标快照并开启新分支；"
                "请把 checkpoint_id 与 confirmation_token 一并提交。"
            ),
        }

    def restore(self, checkpoint_id: str, confirmation_token: str) -> dict[str, Any]:
        return self._restore(checkpoint_id, confirmation_token)

    def _restore(
        self,
        checkpoint_id: str,
        confirmation_token: str,
        *,
        allow_pending_journal: bool = False,
    ) -> dict[str, Any]:
        preview = self.preview_restore(checkpoint_id)
        if confirmation_token != preview["confirmation_token"]:
            raise ValidationGateError("确认码无效或当前项目在预览后已变化；请重新预览回退影响。")
        if not allow_pending_journal and self.journal_path.exists():
            raise ValidationGateError(
                f"发现未完成恢复日志：{self.journal_path}。"
                "上一次回退没有完成，之后的回退已被阻止；"
                "请在“检查点与分支式回退”点击“恢复到中断前的状态”，再重新预览回退。"
            )
        self._acquire_lock(checkpoint_id)
        safety: dict[str, Any] | None = None
        trash_dir: Path | None = None
        try:
            manifest = self._load_manifest(checkpoint_id, validate=True)
            safety = self.create(
                label=f"回退前安全点 · {manifest['label']}",
                reason=f"pre_restore:{checkpoint_id}",
                advance=True,
            )
            stamp = utc_now().replace("+00:00", "Z").replace(":", "").replace("-", "")
            trash_dir = (self.project.internal / "trash" / f"{stamp}-rollback-{checkpoint_id}").resolve()
            if not trash_dir.is_relative_to(self.project.internal / "trash"):
                raise ProjectError("回退回收目录越界。")
            trash_dir.mkdir(parents=True, exist_ok=False)
            atomic_write_json(
                self.journal_path,
                {
                    "checkpoint_id": checkpoint_id,
                    "safety_checkpoint_id": safety["checkpoint_id"],
                    "trash_dir": str(trash_dir),
                    "started_at": utc_now(),
                },
            )

            for source, relative in self._managed_files():
                destination = trash_dir / "files" / Path(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))

            self._restore_database(self.root / checkpoint_id / "inkflow.db", trash_dir)
            # 旧版检查点可能来自更早的数据库结构（缺少后来新增的表）。
            # 先按当前结构补齐，再校验状态，否则旧清单会因为“no such table”而无法恢复。
            self.project.db.initialize()
            files_root = (self.root / checkpoint_id / "files").resolve()
            for record in manifest["files"]:
                relative = self._validate_managed_relative(str(record["path"]))
                source = (files_root / Path(relative)).resolve()
                if not source.is_relative_to(files_root):
                    raise ProjectError("检查点投影路径越界。")
                target = (self.project.root / Path(relative)).resolve()
                if not target.is_relative_to(self.project.root) or target.is_relative_to(self.project.internal):
                    raise ProjectError("恢复目标路径越界。")
                atomic_write_bytes(target, source.read_bytes())

            restored_status = self.project.db.project_status()
            # 只比较清单里记录过的字段：旧检查点由更早的引擎写入，
            # 缺少后来新增的状态字段不应被判定为“恢复失败”。
            mismatched = {
                key: {"expected": expected, "actual": restored_status.get(key)}
                for key, expected in manifest["status"].items()
                if restored_status.get(key) != expected
            }
            if mismatched:
                raise ProjectError(
                    f"恢复后数据库状态不一致：{mismatched}"
                    f"（清单记录 {manifest['status']}，实际 {restored_status}）"
                )
            branch_id = f"branch-{stamp}-{uuid.uuid4().hex[:6]}"
            self._write_current(checkpoint_id, branch_id, restored_from=safety["checkpoint_id"])
            self._append_history(
                {
                    "event": "checkpoint_restored",
                    "checkpoint_id": checkpoint_id,
                    "safety_checkpoint_id": safety["checkpoint_id"],
                    "branch_id": branch_id,
                    "created_at": utc_now(),
                    "trash_dir": str(trash_dir),
                }
            )
            self.journal_path.unlink(missing_ok=True)
            return {
                "status": "restored",
                "checkpoint": self._summary(manifest),
                "new_branch_id": branch_id,
                "safety_checkpoint_id": safety["checkpoint_id"],
                "recoverable_trash": str(trash_dir),
                "project_status": restored_status,
                "next_action": f"从第 {int(manifest['boundary_chapter']) + 1} 章重新生成",
            }
        except Exception as exc:
            safety_id = safety["checkpoint_id"] if safety else "未创建"
            raise ProjectError(
                f"回退未完整完成；安全检查点={safety_id}，恢复日志={self.journal_path}，错误={exc}"
            ) from exc
        finally:
            self.lock_path.unlink(missing_ok=True)

    def pending_recovery(self) -> dict[str, Any] | None:
        """报告上一次未完成的回退；没有则返回 None。"""

        journal = self._read_json(self.journal_path, None)
        if not isinstance(journal, dict) or not journal:
            return None
        safety_id = str(journal.get("safety_checkpoint_id") or "")
        return {
            "failed_checkpoint_id": str(journal.get("checkpoint_id") or ""),
            "safety_checkpoint_id": safety_id,
            "trash_dir": str(journal.get("trash_dir") or ""),
            "started_at": str(journal.get("started_at") or ""),
            "safety_checkpoint_available": bool(safety_id)
            and (self.root / safety_id / "manifest.json").is_file(),
        }

    def recover_interrupted(self) -> dict[str, Any]:
        """收拾中断的回退：恢复到回退前创建的安全检查点并清理残留日志与锁。"""

        journal = self._read_json(self.journal_path, None)
        if not isinstance(journal, dict) or not journal:
            raise ValidationGateError("当前没有未完成的回退需要恢复。")
        safety_id = str(journal.get("safety_checkpoint_id") or "")
        if not safety_id or not _CHECKPOINT_ID.fullmatch(safety_id):
            raise ProjectError("恢复日志缺少可用的安全检查点，无法自动回到中断前的状态。")
        self._load_manifest(safety_id)
        self._append_history(
            {
                "event": "restore_recovery_started",
                "failed_checkpoint_id": journal.get("checkpoint_id"),
                "safety_checkpoint_id": safety_id,
                "created_at": utc_now(),
            }
        )
        self.lock_path.unlink(missing_ok=True)
        preview = self.preview_restore(safety_id)
        result = self._restore(
            safety_id,
            preview["confirmation_token"],
            allow_pending_journal=True,
        )
        self._append_history(
            {
                "event": "restore_recovered",
                "safety_checkpoint_id": safety_id,
                "new_branch_id": result.get("new_branch_id"),
                "created_at": utc_now(),
            }
        )
        return {
            **result,
            "status": "recovered",
            "recovered_from": str(journal.get("checkpoint_id") or ""),
            "message": "已回到上一次回退中断前的状态；残留日志和锁已清理。",
        }

    def _managed_files(self) -> list[tuple[Path, str]]:
        result: list[tuple[Path, str]] = []
        for name in _TOP_LEVEL_FILES:
            path = self.project.root / name
            if path.is_file():
                result.append((path, name))
        for directory_name in _MANAGED_DIRS:
            directory = self.project.root / directory_name
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ProjectError(f"检查点不接受符号链接：{path}")
                if path.is_file():
                    relative = path.relative_to(self.project.root).as_posix()
                    result.append((path, self._validate_managed_relative(relative)))
        return result

    @staticmethod
    def _validate_managed_relative(relative: str) -> str:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise ProjectError(f"检查点包含不安全路径：{relative}")
        normalized = path.as_posix()
        if normalized in _TOP_LEVEL_FILES:
            return normalized
        if not any(normalized == name or normalized.startswith(f"{name}/") for name in _MANAGED_DIRS):
            raise ProjectError(f"检查点包含非托管路径：{relative}")
        return normalized

    def _backup_database(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.project.db.path)) as source, closing(
            sqlite3.connect(destination)
        ) as target:
            source.backup(target)
            target.commit()
            check = target.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise ProjectError(f"SQLite 检查点完整性校验失败：{check}")

    def _restore_database(self, source_path: Path, trash_dir: Path) -> None:
        live = self.project.db.path.resolve()
        if not live.is_relative_to(self.project.internal):
            raise ProjectError("SQLite 恢复目标越界。")
        temp = (self.project.internal / f".restore-{uuid.uuid4().hex}.db").resolve()
        if not temp.is_relative_to(self.project.internal):
            raise ProjectError("SQLite 临时恢复路径越界。")
        shutil.copy2(source_path, temp)
        try:
            with closing(sqlite3.connect(temp)) as connection:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                if not check or check[0] != "ok":
                    raise ProjectError(f"目标快照完整性校验失败：{check}")
            if live.exists():
                with closing(sqlite3.connect(live)) as connection:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                database_trash = trash_dir / "database"
                database_trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(live), str(database_trash / "inkflow.db"))
                for suffix in ("-wal", "-shm"):
                    companion = Path(f"{live}{suffix}")
                    if companion.exists():
                        shutil.move(str(companion), str(database_trash / companion.name))
            os.replace(temp, live)
        finally:
            temp.unlink(missing_ok=True)

    def _load_manifest(self, checkpoint_id: str, *, validate: bool = False) -> dict[str, Any]:
        if not _CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValidationGateError("checkpoint_id 格式不合法。")
        directory = (self.root / checkpoint_id).resolve()
        if not directory.is_relative_to(self.root):
            raise ValidationGateError("checkpoint_id 路径越界。")
        path = directory / "manifest.json"
        if not path.is_file():
            raise ValidationGateError(f"检查点不存在：{checkpoint_id}")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("checkpoint_id") != checkpoint_id:
            raise ProjectError("检查点清单 ID 不一致。")
        if validate:
            database = directory / "inkflow.db"
            raw = database.read_bytes()
            if content_hash(raw) != manifest["database"]["sha256"]:
                raise ProjectError("检查点 SQLite 哈希不一致。")
            files_root = (directory / "files").resolve()
            for record in manifest["files"]:
                relative = self._validate_managed_relative(str(record["path"]))
                file_path = (files_root / Path(relative)).resolve()
                if not file_path.is_relative_to(files_root) or not file_path.is_file():
                    raise ProjectError(f"检查点文件缺失：{relative}")
                if content_hash(file_path.read_bytes()) != record["sha256"]:
                    raise ProjectError(f"检查点文件哈希不一致：{relative}")
        return manifest

    def _acquire_lock(self, checkpoint_id: str) -> None:
        try:
            with self.lock_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(json_dumps({"checkpoint_id": checkpoint_id, "created_at": utc_now()}) + "\n")
        except FileExistsError as exc:
            raise ValidationGateError(f"另一个回退正在进行：{self.lock_path}") from exc

    def _write_current(self, checkpoint_id: str, branch_id: str, restored_from: str | None) -> None:
        atomic_write_json(
            self.current_path,
            {
                "checkpoint_id": checkpoint_id,
                "branch_id": branch_id,
                "restored_from_safety_checkpoint": restored_from,
                "updated_at": utc_now(),
            },
        )

    def _append_history(self, value: dict[str, Any]) -> None:
        with self.history_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json_dumps(value, indent=None) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _new_checkpoint_id(timestamp: str) -> str:
        stamp = timestamp.replace("+00:00", "Z").replace(":", "").replace("-", "")
        return f"cp-{stamp}-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _summary(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "checkpoint_id": manifest["checkpoint_id"],
            "parent_id": manifest.get("parent_id"),
            "branch_id": manifest.get("branch_id", "main"),
            "created_at": manifest["created_at"],
            "label": manifest["label"],
            "reason": manifest["reason"],
            "boundary_chapter": int(manifest.get("boundary_chapter", 0)),
            "status": manifest["status"],
            "state_hash": manifest["state_hash"],
        }
