from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterator

from .errors import ProjectError, ValidationGateError
from .project import InkFlowProject
from .project_lock import project_write_lock_sync
from .config import Settings
from .task_settings import (
    TaskSettingsError, TaskSettingsScope, active_task_settings, capture_task_settings,
    restore_task_settings, validate_task_settings_snapshot,
)
from .utils import atomic_write_text, content_hash, effective_character_count, json_dumps, utc_now


STUDIO_SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    version_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    parent_hash TEXT,
    content_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_versions_path
ON document_versions(relative_path, created_at DESC);

CREATE TABLE IF NOT EXISTS annotations (
    annotation_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    comment TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_annotations_document
ON annotations(relative_path, status, created_at);

CREATE TABLE IF NOT EXISTS bible_entries (
    entry_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    data_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_notes (
    chapter_no INTEGER NOT NULL,
    scene_no INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(chapter_no, scene_no)
);

CREATE TABLE IF NOT EXISTS context_pins (
    pin_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    chapter_no INTEGER,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, chapter_no)
);

CREATE INDEX IF NOT EXISTS idx_context_pins_active
ON context_pins(status, chapter_no, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    method TEXT NOT NULL,
    action TEXT,
    status TEXT NOT NULL,
    params_json TEXT NOT NULL,
    summary TEXT,
    error_message TEXT,
    error_code TEXT,
    retry_allowed INTEGER,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_runs_status_updated
ON task_runs(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS task_presentation (
    run_id TEXT PRIMARY KEY,
    title TEXT,
    progress_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_settings_snapshots (
    task_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_run_settings (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES task_settings_snapshots(task_id)
);

CREATE TABLE IF NOT EXISTS batch_resume_claims (
    batch_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
"""


def _task_display_title(method: str, action: str | None, params: dict[str, Any]) -> str:
    """Name a task from its requested work without another model request."""
    action_names = {
        "plan": "安排章节计划", "write": "写章节草稿", "review": "审查章节",
        "revise": "修订章节", "batch_draft": "批量写章节", "batch_resume": "续接批量写作", "arc_audit": "复核篇章",
        "accept": "验收章节", "batch_accept": "验收批次", "checkpoint_create": "创建检查点",
        "rollback_restore": "恢复项目版本", "rollback_recover": "恢复中断的回退",
    }
    if method == "conversation.send":
        message = str(params.get("message") or "").split("\n# 用户的运行中引导", 1)[0]
        first = re.split(r"[。！？\n]", message, maxsplit=1)[0]
        first = re.sub(r"\s+", " ", first).strip(" ，。！？：:；; \t")
        if not first or first in {"继续", "接着", "继续完成这批", "继续上次任务"}:
            return "继续上次任务"
        chapter_range = re.search(r"(?:第\s*)?(\d+)\s*(?:到|至|～|—|-)\s*(?:第\s*)?(\d+)\s*章", first)
        chapter_single = re.search(r"第\s*(\d+)\s*章", first)
        if chapter_range or chapter_single:
            scope = (
                f"第 {chapter_range.group(1)}～{chapter_range.group(2)} 章" if chapter_range
                else f"第 {chapter_single.group(1)} 章"
            )
            if "计划" in first or "章节卡" in first:
                activity = "安排计划"
            elif any(word in first for word in ("审核", "审查", "复审")):
                activity = "修订并复审" if any(word in first for word in ("修", "改")) else "复审"
            elif any(word in first for word in ("写", "正文", "草稿")):
                activity = "写作"
            elif any(word in first for word in ("修", "改")):
                activity = "修订"
            else:
                activity = "继续处理"
            return f"{scope} · {activity}"
        if first.startswith(("继续刚才的任务", "继续上次任务")):
            return "继续上次任务"
        first = re.sub(r"^(?:请|现在|接着|然后|麻烦你|帮我)", "", first).strip()
        first = first.rstrip("吧呢啊")
        return first[:34] + ("…" if len(first) > 34 else "")
    if method == "workflow.run":
        label = action_names.get(str(action or ""), "执行小说工作流")
        start = params.get("start_chapter_no", params.get("start_chapter", params.get("chapter_no")))
        end = params.get("end_chapter_no", params.get("end_chapter"))
        try:
            first_no = int(start)
            last_no = int(end) if end is not None else first_no
        except (TypeError, ValueError):
            return label
        scope = f"第 {first_no} 章" if first_no == last_no else f"第 {first_no}～{last_no} 章"
        return f"{scope} · {label}"
    return {
        "reference.search": "查找参考资料", "reference.fetch": "导入参考资料",
        "reference.analyze": "分析参考资料", "document.revise_selection": "修订选中文本",
        "task.retry": "重试任务",
    }.get(method, "墨流任务")


def chapter_retry_state(project: InkFlowProject, chapter_no: int) -> dict[str, Any]:
    """Fingerprint only the target chapter before allowing an old write retry."""
    root = Path(project.root).resolve()
    chapter = project.db.get_chapter(chapter_no)
    files: dict[str, str] = {}
    candidates = {
        Path("chapters") / f"chapter_{chapter_no:05d}.draft.md",
        Path("chapters") / f"chapter_{chapter_no:05d}.md",
    }
    if chapter and chapter.get("path"):
        candidates.add(Path(str(chapter["path"])))
    for relative in sorted(candidates, key=lambda item: item.as_posix()):
        if relative.is_absolute() or ".." in relative.parts:
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            files[relative.as_posix()] = content_hash(path.read_bytes())
    return {
        "chapter_no": chapter_no,
        "record": None if chapter is None else {
            "status": chapter.get("status"),
            "version": chapter.get("version"),
            "path": chapter.get("path"),
            "content_hash": chapter.get("content_hash"),
        },
        "files": files,
    }


class StudioDatabase:
    """非正史的桌面辅助数据。

    `inkflow.db` 仍然是小说正史。这里保存编辑快照、行级批注、场景笔记
    和任务配置快照；任务配置及运行关联是恢复依据，不可当作缓存清理。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self, *, timeout: float = 5.0) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(STUDIO_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_runs)")}
            if "error_code" not in columns:
                connection.execute("ALTER TABLE task_runs ADD COLUMN error_code TEXT")
            if "retry_allowed" not in columns:
                connection.execute("ALTER TABLE task_runs ADD COLUMN retry_allowed INTEGER")
            connection.execute(
                """
                INSERT INTO studio_metadata(key, value_json, updated_at)
                VALUES ('schema_version', '4', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (utc_now(),),
            )
            connection.commit()

    def capture_version(
        self,
        relative_path: str,
        content: str,
        *,
        parent_hash: str | None,
        source: str,
        applied: bool = True,
    ) -> dict[str, Any]:
        version_id = f"version-{uuid.uuid4().hex}"
        digest = content_hash(content)
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO document_versions(
                    version_id, relative_path, parent_hash, content_hash,
                    content, source, applied, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    relative_path,
                    parent_hash,
                    digest,
                    content,
                    source,
                    1 if applied else 0,
                    now,
                ),
            )
            connection.commit()
        return {
            "version_id": version_id,
            "relative_path": relative_path,
            "content_hash": digest,
            "source": source,
            "applied": applied,
            "created_at": now,
        }

    def list_versions(self, relative_path: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT version_id, relative_path, parent_hash, content_hash,
                       source, applied, created_at, length(content) AS characters
                FROM document_versions
                WHERE relative_path=?
                ORDER BY created_at DESC LIMIT ?
                """,
                (relative_path, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_version(self, version_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT version_id, relative_path, parent_hash, content_hash,
                       content, source, applied, created_at
                FROM document_versions WHERE version_id=?
                """,
                (version_id,),
            ).fetchone()
        if row is None:
            raise ProjectError(f"找不到文档版本：{version_id}")
        return dict(row)

    def create_annotation(
        self,
        *,
        relative_path: str,
        document_hash: str,
        start_offset: int,
        end_offset: int,
        quote: str,
        comment: str,
    ) -> dict[str, Any]:
        annotation_id = f"annotation-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO annotations(
                    annotation_id, relative_path, document_hash, start_offset,
                    end_offset, quote, quote_hash, comment, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    annotation_id,
                    relative_path,
                    document_hash,
                    start_offset,
                    end_offset,
                    quote,
                    content_hash(quote),
                    comment,
                    now,
                    now,
                ),
            )
            connection.commit()
        return {
            "annotation_id": annotation_id,
            "relative_path": relative_path,
            "document_hash": document_hash,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "quote": quote,
            "comment": comment,
            "status": "open",
            "created_at": now,
            "updated_at": now,
        }

    def list_annotations(self, relative_path: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM annotations
                WHERE relative_path=?
                ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'orphaned' THEN 1 ELSE 2 END,
                         created_at
                """,
                (relative_path,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_annotation_anchor(
        self,
        annotation_id: str,
        *,
        document_hash: str,
        start_offset: int,
        end_offset: int,
        status: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE annotations
                SET document_hash=?, start_offset=?, end_offset=?, status=?, updated_at=?
                WHERE annotation_id=?
                """,
                (document_hash, start_offset, end_offset, status, utc_now(), annotation_id),
            )
            connection.commit()

    def set_annotation_status(self, annotation_id: str, status: str) -> dict[str, Any]:
        if status not in {"open", "resolved", "dismissed"}:
            raise ProjectError("批注状态只能是 open、resolved 或 dismissed。")
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE annotations SET status=?, updated_at=? WHERE annotation_id=?",
                (status, utc_now(), annotation_id),
            )
            if cursor.rowcount != 1:
                raise ProjectError(f"找不到批注：{annotation_id}")
            row = connection.execute(
                "SELECT * FROM annotations WHERE annotation_id=?", (annotation_id,)
            ).fetchone()
            connection.commit()
        return dict(row)

    def upsert_bible_entry(
        self,
        *,
        entry_id: str | None,
        kind: str,
        name: str,
        aliases: list[str],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        if kind not in {"character", "location", "organization", "item", "lore", "style"}:
            raise ProjectError("故事圣经类型不受支持。")
        clean_name = name.strip()
        if not clean_name:
            raise ProjectError("故事圣经条目名称不能为空。")
        item_id = entry_id or f"bible-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO bible_entries(entry_id, kind, name, aliases_json, data_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    kind=excluded.kind,
                    name=excluded.name,
                    aliases_json=excluded.aliases_json,
                    data_json=excluded.data_json,
                    status='active',
                    updated_at=excluded.updated_at
                """,
                (
                    item_id,
                    kind,
                    clean_name,
                    json_dumps(sorted({item.strip() for item in aliases if item.strip()}), indent=None),
                    json_dumps(data, indent=None),
                    now,
                ),
            )
            connection.commit()
        return {
            "entry_id": item_id,
            "kind": kind,
            "name": clean_name,
            "aliases": sorted({item.strip() for item in aliases if item.strip()}),
            "data": data,
            "status": "active",
            "updated_at": now,
        }

    def list_bible_entries(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bible_entries WHERE status='active' ORDER BY kind, name"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["aliases"] = json.loads(item.pop("aliases_json"))
            item["data"] = json.loads(item.pop("data_json"))
            result.append(item)
        return result

    def upsert_scene_note(self, chapter_no: int, scene_no: int, data: dict[str, Any]) -> dict[str, Any]:
        if chapter_no < 1 or scene_no < 1:
            raise ProjectError("章节号和场景号必须大于零。")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO scene_notes(chapter_no, scene_no, data_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chapter_no, scene_no) DO UPDATE SET
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                (chapter_no, scene_no, json_dumps(data, indent=None), now),
            )
            connection.commit()
        return {"chapter_no": chapter_no, "scene_no": scene_no, "data": data, "updated_at": now}

    def scene_notes(self, chapter_no: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM scene_notes WHERE chapter_no=? ORDER BY scene_no",
                (chapter_no,),
            ).fetchall()
        return [
            {
                "chapter_no": int(row["chapter_no"]),
                "scene_no": int(row["scene_no"]),
                "data": json.loads(row["data_json"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_context_pin(
        self,
        source_id: str,
        *,
        chapter_no: int | None = None,
        note: str = "",
        pinned: bool = True,
    ) -> dict[str, Any]:
        """Lock at most six non-canon work sources for intentional compression."""

        clean_source = source_id.strip()
        if not clean_source:
            raise ValidationGateError("要锁定的上下文资料不能为空。")
        now = utc_now()
        with self.connect() as connection:
            is_scene_note = bool(re.fullmatch(r"scene-note:\d+:\d+", clean_source))
            is_bible_entry = connection.execute(
                "SELECT 1 FROM bible_entries WHERE entry_id=? AND status='active'",
                (clean_source,),
            ).fetchone()
            if not is_scene_note and not is_bible_entry:
                raise ValidationGateError("只能锁定当前的场景笔记或人工故事圣经；正史和模型输入本来就受保护。")
            existing = connection.execute(
                "SELECT pin_id FROM context_pins WHERE source_id=? AND chapter_no IS ?",
                (clean_source, chapter_no),
            ).fetchone()
            if pinned and not existing:
                count = connection.execute(
                    "SELECT COUNT(*) AS amount FROM context_pins WHERE status='active' AND (chapter_no IS NULL OR chapter_no=?)",
                    (chapter_no,),
                ).fetchone()
                if int(count["amount"]) >= 6:
                    raise ValidationGateError("最多同时锁定 6 条工作资料；请先解除不再需要的锁定。")
            pin_id = str(existing["pin_id"]) if existing else f"context-pin-{uuid.uuid4().hex}"
            clean_note = note.strip()[:500]
            connection.execute(
                """
                INSERT INTO context_pins(pin_id,source_id,chapter_no,note,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(source_id,chapter_no) DO UPDATE SET
                    note=excluded.note,status=excluded.status,updated_at=excluded.updated_at
                """,
                (pin_id, clean_source, chapter_no, clean_note, "active" if pinned else "released", now, now),
            )
            connection.commit()
        return {"pin_id": pin_id, "source_id": clean_source, "chapter_no": chapter_no, "note": clean_note, "status": "active" if pinned else "released"}

    def list_context_pins(self, chapter_no: int | None = None) -> list[dict[str, Any]]:
        clauses = ["status='active'"]
        params: list[Any] = []
        if chapter_no is not None:
            clauses.append("(chapter_no IS NULL OR chapter_no=?)")
            params.append(chapter_no)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM context_pins WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _task_settings_record(row: sqlite3.Row, novel_id: str | None = None) -> dict[str, Any]:
        if not row["snapshot_json"]:
            raise TaskSettingsError("任务运行已有关联，但配置快照缺失，不能用当前设置回填。")
        try:
            snapshot = json.loads(row["snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskSettingsError("任务配置快照无法读取，不能用当前设置回填。") from exc
        validate_task_settings_snapshot(snapshot, novel_id=novel_id, task_id=row["task_id"])
        for key in ("novel_id", "snapshot_hash", "captured_at", "source"):
            if snapshot[key] != row[key]:
                raise TaskSettingsError("任务配置快照与存储索引不一致。")
        return snapshot

    def prepare_batch_settings(
        self,
        *,
        novel_id: str,
        reference: dict[str, Any] | None,
        settings: Settings | None = None,
        workspace_root: str | Path,
        legacy: bool = False,
    ) -> TaskSettingsScope:
        """A batch owns a snapshot, not a replacement association for its caller's run."""
        root = Path(workspace_root).resolve()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if reference is not None:
                if not isinstance(reference, dict):
                    raise TaskSettingsError("批次配置引用不完整，不能用当前设置替换。")
                if reference.get("novel_id") != novel_id or not isinstance(reference.get("task_id"), str):
                    raise TaskSettingsError("批次配置不属于当前小说。")
                row = connection.execute(
                    "SELECT * FROM task_settings_snapshots WHERE task_id=?", (reference["task_id"],)
                ).fetchone()
                if row is None:
                    raise TaskSettingsError("批次引用的配置快照缺失，不能用当前设置回填。")
                snapshot = self._task_settings_record(row, novel_id)
                scope = restore_task_settings(snapshot, novel_id=novel_id, workspace_root=root)
                if type(reference.get("schema_version")) is not int or reference != scope.public_summary():
                    raise TaskSettingsError("批次配置引用与原快照不一致，未恢复执行。")
                return scope
            if not isinstance(settings, Settings):
                raise TaskSettingsError("新批次或旧版批次缺少可固定的当前配置。")
            if settings.workspace_root is not None and settings.workspace_root.resolve() != root:
                raise TaskSettingsError("批次配置工作区与当前小说不一致。")
            parent = active_task_settings.get()
            inherit_mode = parent is not None and parent.novel_id == novel_id and not legacy
            snapshot = capture_task_settings(
                settings, novel_id=novel_id, source="legacy_recovery" if legacy else "task_start",
                role_protocol_version=parent.role_protocol_version if inherit_mode else 1,
                collaboration_mode=parent.collaboration_mode if inherit_mode else "everyday",
            )
            scope = restore_task_settings(snapshot, novel_id=novel_id, workspace_root=root)
            connection.execute(
                """INSERT INTO task_settings_snapshots
                   (task_id,novel_id,snapshot_hash,snapshot_json,captured_at,source) VALUES (?,?,?,?,?,?)""",
                (scope.task_id, novel_id, scope.snapshot_hash, json_dumps(snapshot), scope.captured_at, scope.source),
            )
            connection.commit()
            return scope

    def prepare_task_settings(
        self,
        run_id: str,
        *,
        novel_id: str,
        settings: Settings | Callable[[], Settings] | None = None,
        resume_run_id: str | None = None,
        workspace_root: str | Path | None = None,
        capture_source: str = "task_start",
        registered_now: bool = False,
        role_protocol_version: int | None = None,
        collaboration_mode: str | None = None,
    ) -> TaskSettingsScope:
        """Capture once or bind a fresh run to an existing task's exact snapshot.

        The settings supplier is evaluated only for a new task or a genuinely
        unrecorded legacy run. A corrupt/missing linked snapshot never falls back.
        """
        if not isinstance(run_id, str) or not run_id.strip():
            raise TaskSettingsError("任务运行编号不能为空。")
        if not isinstance(novel_id, str) or not novel_id.strip():
            raise TaskSettingsError("任务配置必须绑定当前小说身份。")
        root = Path(workspace_root).resolve() if workspace_root is not None else self.path.resolve().parent.parent

        def requested_mode_matches(scope: TaskSettingsScope) -> TaskSettingsScope:
            if role_protocol_version is not None and role_protocol_version != scope.role_protocol_version:
                raise TaskSettingsError("恢复任务不能切换角色协议；请新建任务并明确选择模式。")
            if collaboration_mode is not None and collaboration_mode != scope.collaboration_mode:
                raise TaskSettingsError("恢复任务不能切换协作模式；请新建任务并明确选择模式。")
            return scope

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def linked(run: str) -> sqlite3.Row | None:
                return connection.execute(
                    """SELECT link.task_id,s.novel_id,s.snapshot_hash,s.snapshot_json,s.captured_at,s.source
                       FROM task_run_settings link LEFT JOIN task_settings_snapshots s ON s.task_id=link.task_id
                       WHERE link.run_id=?""", (run,),
                ).fetchone()

            current = linked(run_id)
            if current is not None:
                if resume_run_id:
                    previous = linked(resume_run_id)
                    if previous is None or previous["task_id"] != current["task_id"]:
                        raise TaskSettingsError("此运行已经绑定其他任务配置，不能重新指定恢复来源。")
                snapshot = self._task_settings_record(current, novel_id)
                return requested_mode_matches(restore_task_settings(snapshot, novel_id=novel_id, workspace_root=root))

            source = "task_start"
            previous = None
            if resume_run_id is not None:
                if not isinstance(resume_run_id, str) or not resume_run_id.strip() or resume_run_id == run_id:
                    raise TaskSettingsError("恢复任务必须使用有效的原运行编号和新的运行编号。")
                original = connection.execute(
                    "SELECT status FROM task_runs WHERE run_id=?", (resume_run_id,),
                ).fetchone()
                if original is None:
                    raise TaskSettingsError("找不到要恢复的原运行，不能创建替代配置。")
                if original["status"] not in {"failed", "cancelled", "interrupted", "waiting_condition"}:
                    raise TaskSettingsError("原运行尚未停止或已完成，不能作为恢复任务重新启动。")
                previous = linked(resume_run_id)
                if previous is None:
                    source = "legacy_recovery"
            else:
                existing_run = connection.execute(
                    "SELECT status FROM task_runs WHERE run_id=?", (run_id,),
                ).fetchone()
                if existing_run is not None and not (
                    registered_now and existing_run["status"] == "running"
                ):
                    raise TaskSettingsError("旧运行没有配置快照，请使用新的运行编号明确恢复。")

            if previous is not None:
                snapshot = self._task_settings_record(previous, novel_id)
            else:
                if not isinstance(capture_source, str) or capture_source not in {"task_start", "legacy_recovery"}:
                    raise TaskSettingsError("新任务配置的捕获来源无效。")
                if source != "legacy_recovery":
                    source = capture_source
                current_settings = settings() if callable(settings) else settings
                if not isinstance(current_settings, Settings):
                    raise TaskSettingsError("新任务或旧版恢复任务缺少可捕获的当前配置。")
                if current_settings.workspace_root is not None and current_settings.workspace_root.resolve() != root:
                    raise TaskSettingsError("当前配置的工作区与小说任务不一致。")
                snapshot = capture_task_settings(
                    current_settings, novel_id=novel_id, source=source,
                    role_protocol_version=(role_protocol_version if role_protocol_version is not None
                                           else 2 if source == "task_start" else 1),
                    collaboration_mode=collaboration_mode if collaboration_mode is not None else "everyday",
                )

            # Validate before either insertion; a failure leaves no partial link.
            scope = requested_mode_matches(restore_task_settings(snapshot, novel_id=novel_id, workspace_root=root))
            if previous is None:
                connection.execute(
                    """INSERT INTO task_settings_snapshots
                       (task_id,novel_id,snapshot_hash,snapshot_json,captured_at,source) VALUES (?,?,?,?,?,?)""",
                    (scope.task_id, novel_id, scope.snapshot_hash, json_dumps(snapshot), scope.captured_at, source),
                )
                if resume_run_id is not None:
                    connection.execute("INSERT INTO task_run_settings(run_id,task_id) VALUES (?,?)", (resume_run_id, scope.task_id))
            connection.execute("INSERT INTO task_run_settings(run_id,task_id) VALUES (?,?)", (run_id, scope.task_id))
            connection.commit()
            return scope

    def start_task(
        self,
        run_id: str,
        *,
        owner_id: str,
        method: str,
        params: dict[str, Any],
        suggested_title: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        action = str(params.get("action") or "") or None
        with self.connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO task_runs(
                        run_id, owner_id, method, action, status, params_json,
                        summary, error_message, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL, ?, ?)
                    """,
                    (run_id, owner_id, method, action, json_dumps(params, indent=None), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ProjectError("运行编号已有记录；请先查看原任务状态，不要重发整项请求。") from exc
            if suggested_title:
                clean_title = re.sub(r"\s+", " ", suggested_title).strip()[:60]
                if clean_title:
                    connection.execute(
                        "INSERT INTO task_presentation(run_id, title, updated_at) VALUES (?, ?, ?)",
                        (run_id, clean_title, now),
                    )
            connection.commit()
        return self.get_task(run_id)

    def finish_task(
        self,
        run_id: str,
        *,
        status: str,
        summary: str = "",
        error_message: str = "",
        error_code: str | None = None,
        retry_allowed: bool | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"completed", "failed", "cancelled", "interrupted", "waiting_user", "waiting_condition", "dismissed"}:
            raise ProjectError(f"不支持的任务状态：{status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE task_runs
                SET status=?, summary=?, error_message=?, error_code=?, retry_allowed=?, updated_at=?
                WHERE run_id=?
                """,
                (
                    status, summary[:1000], error_message[:2000], error_code,
                    None if retry_allowed is None else int(retry_allowed), utc_now(), run_id,
                ),
            )
            connection.commit()
        return self.get_task(run_id)

    def claim_task_retry(self, run_id: str) -> bool:
        """Atomically consume one approved retry so duplicate clicks cannot replay it."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_runs SET retry_allowed=0, updated_at=?
                WHERE run_id=? AND status IN ('failed','cancelled','interrupted') AND retry_allowed=1
                """,
                (utc_now(), run_id),
            )
            connection.commit()
            return cursor.rowcount == 1

    def latest_task_run_for_settings(self, task_id: str) -> dict[str, Any] | None:
        """Find the latest run linked to an immutable task-settings snapshot."""
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.run_id FROM task_runs r
                   JOIN task_run_settings link ON link.run_id=r.run_id
                   WHERE link.task_id=? ORDER BY r.started_at DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        return self.get_task(str(row["run_id"])) if row else None

    def claim_batch_resume(self, batch_id: str, run_id: str) -> bool:
        """Allow one active run to resume a batch; terminal attempts may be superseded."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM task_runs WHERE run_id=?", (run_id,),
            ).fetchone()
            if current is None or current["status"] != "running":
                connection.rollback()
                return False
            claim = connection.execute(
                "SELECT run_id FROM batch_resume_claims WHERE batch_id=?", (batch_id,),
            ).fetchone()
            if claim is not None:
                previous = connection.execute(
                    "SELECT status FROM task_runs WHERE run_id=?", (claim["run_id"],),
                ).fetchone()
                if previous is not None and previous["status"] == "running":
                    connection.rollback()
                    return False
                connection.execute(
                    "UPDATE batch_resume_claims SET run_id=?, claimed_at=? WHERE batch_id=?",
                    (run_id, utc_now(), batch_id),
                )
            else:
                connection.execute(
                    "INSERT INTO batch_resume_claims(batch_id,run_id,claimed_at) VALUES (?,?,?)",
                    (batch_id, run_id, utc_now()),
                )
            connection.commit()
            return True

    def rename_task(self, run_id: str, title: str) -> dict[str, Any]:
        """Save a user's display name without changing the task request or workflow."""
        clean_title = re.sub(r"\s+", " ", title).strip()
        if not clean_title or len(clean_title) > 60:
            raise ProjectError("任务名称应为 1～60 个字；原名称没有改变。")
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM task_runs WHERE run_id=? AND status<>'dismissed'", (run_id,)).fetchone() is None:
                raise ProjectError("任务记录不存在或已隐藏，无法改名。")
            connection.execute(
                """INSERT INTO task_presentation(run_id, title, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET title=excluded.title, updated_at=excluded.updated_at""",
                (run_id, clean_title, utc_now()),
            )
            connection.commit()
        return self.get_task(run_id)

    def record_task_progress(self, run_id: str, event_type: str, summary: str) -> None:
        """Keep only a short, public, durable activity trail for current work."""
        text = re.sub(r"\s+", " ", summary).strip()[:200]
        if not text:
            return
        with self.connect(timeout=0.2) as connection:
            row = connection.execute(
                "SELECT progress_json FROM task_presentation WHERE run_id=?", (run_id,),
            ).fetchone()
            try:
                steps = json.loads(row["progress_json"]) if row else []
                if not isinstance(steps, list):
                    steps = []
                steps = [item for item in steps if isinstance(item, dict) and isinstance(item.get("summary"), str)]
            except (TypeError, json.JSONDecodeError):
                steps = []
            if steps and steps[-1]["summary"] == text:
                return
            steps = [*steps, {"type": event_type[:80], "summary": text, "at": utc_now()}][-5:]
            connection.execute(
                """INSERT INTO task_presentation(run_id, progress_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     progress_json=excluded.progress_json, updated_at=excluded.updated_at""",
                (run_id, json_dumps(steps, indent=None), utc_now()),
            )
            connection.commit()

    def reconcile_interrupted_tasks(self, owner_id: str) -> int:
        """把已经退出的本地引擎遗留任务标记为中断。

        桌面版和 VS Code 可以同时打开同一本小说，因此不能把“不是当前
        server id”的运行都视作崩溃。新 owner id 带进程号；只有确认对应
        进程已经退出时，才把任务改为 interrupted。
        """

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT run_id, owner_id, method, action, params_json FROM task_runs WHERE status='running' AND owner_id<>?",
                (owner_id,),
            ).fetchall()
            interrupted = [row for row in rows if _owner_process_alive(row["owner_id"]) is False]
            if not interrupted:
                return 0

            def guarded_write(row: sqlite3.Row) -> bool:
                try:
                    params = json.loads(row["params_json"])
                except (TypeError, json.JSONDecodeError):
                    return False
                return (
                    row["method"] == "workflow.run"
                    and row["action"] == "write"
                    and isinstance(params, dict)
                    and isinstance(params.get("_retry_guard"), dict)
                )

            connection.executemany(
                """
                UPDATE task_runs
                SET status='interrupted',
                    summary=?, error_message=?, error_code='engine_process_exited', retry_allowed=?,
                    updated_at=?
                WHERE run_id=? AND status='running'
                """,
                [
                    (
                        "上次本地引擎进程已退出，没有收到任务最终结果；先核对本地文件和保存断点。",
                        f"本地引擎进程 {row['owner_id']} 已退出，任务结束响应未能写入记录。"
                        "已有文件不自动回退；系统保留当前版本并要求先核对再续接。",
                        int(guarded_write(row)),
                        utc_now(),
                        row["run_id"],
                    )
                    for row in interrupted
                ],
            )
            connection.commit()
            return len(interrupted)

    def get_task(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT r.*,link.task_id,s.novel_id,s.snapshot_hash,s.snapshot_json,s.captured_at,s.source,
                          presentation.title AS custom_title,presentation.progress_json
                   FROM task_runs r LEFT JOIN task_run_settings link ON link.run_id=r.run_id
                   LEFT JOIN task_settings_snapshots s ON s.task_id=link.task_id
                   LEFT JOIN task_presentation presentation ON presentation.run_id=r.run_id
                   WHERE r.run_id=?""", (run_id,),
            ).fetchone()
        if row is None:
            raise ProjectError(f"任务不存在：{run_id}")
        return self._task_row(row)

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.*,link.task_id,s.novel_id,s.snapshot_hash,s.snapshot_json,s.captured_at,s.source,
                          presentation.title AS custom_title,presentation.progress_json
                   FROM task_runs r LEFT JOIN task_run_settings link ON link.run_id=r.run_id
                   LEFT JOIN task_settings_snapshots s ON s.task_id=link.task_id
                   LEFT JOIN task_presentation presentation ON presentation.run_id=r.run_id
                   WHERE r.status<>'dismissed' ORDER BY r.updated_at DESC LIMIT ?""",
                (safe_limit,),
            ).fetchall()
        return [self._task_row(row) for row in rows]

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        params = json.loads(row["params_json"])
        action = row["action"]
        retry_guard = params.get("_retry_guard")
        safe_write_retry = (
            row["method"] == "workflow.run"
            and action == "write"
            and isinstance(retry_guard, dict)
            and isinstance(retry_guard.get("chapter_no"), int)
        )
        safe_retry = row["method"] in {"reference.fetch", "reference.analyze"} or safe_write_retry
        retryable = (
            row["status"] in {"failed", "cancelled", "interrupted"}
            and safe_retry and row["retry_allowed"] == 1
        )
        retry_note = ""
        status = str(row["status"])
        if status == "completed":
            next_step = "结果已保存；如界面尚未更新，刷新项目后打开对应正文或报告核对，不要重复发送原请求。"
        elif status == "running":
            next_step = "正在执行；可以查看当前阶段，其他独立工作不必等待本任务结束。"
        elif status == "cancelled":
            next_step = "任务已停止，已有内容保留；不会自行重启。需要继续时先核对当前成果和断点。"
        elif status == "waiting_user":
            next_step = "请回答任务提出的具体问题；墨流不会把等待答复误写成已完成。"
        elif status == "waiting_condition":
            next_step = "当前步骤在等待条件；已有成果保留。条件满足后先核对保存进度，再续接未完成部分，不自动重发整项任务。"
        elif retryable and status in {"failed", "interrupted"}:
            next_step = "可从此记录再次运行，但会创建新请求；先确认当前项目仍需要这一步。"
        elif status == "failed" and row["retry_allowed"] != 1:
            next_step = "目前没有依据安全重放整条任务；先按具体原因修正配置、输入或当前版本，再从未完成步骤继续。"
        elif row["method"] == "workflow.run" and action == "write" and not safe_write_retry:
            next_step = "旧写作任务没有可靠的章节版本快照，不能安全重放；请先核对草稿，再从当前状态续接。"
        elif row["method"] == "workflow.run" and action in {"revise", "batch_draft"}:
            next_step = "该任务可能已保存部分章节；请从批次断点或当前草稿续接，不重放整条写作请求。"
        elif action in {"rollback_restore", "rollback_recover", "batch_accept", "accept"}:
            next_step = "这类操作可能改变正史或文件。先查看当前状态与影响预览，不能直接重放旧请求。"
        elif row["method"] == "conversation.send":
            next_step = "自然语言任务不能整条自动重发。先核对已保存的草稿、审查和正史；若仍未完成，再按当前状态续接受影响步骤。"
        else:
            next_step = "先查看完整原因和当前文件版本；只处理尚未完成的部分，不重复已保存的结果。"
        if status in {"failed", "cancelled", "interrupted"}:
            if retryable:
                retry_note = "再次运行可能调用模型；原任务记录和已保存成果不会被删除。"
            elif row["retry_allowed"] != 1:
                retry_note = "系统没有将整条任务判定为可安全重放；如有保存断点，请使用断点续接，避免重复调用或覆盖后续成果。"
            elif row["method"] == "conversation.send":
                retry_note = "这里不提供一键重发整段对话，以免重复计费或覆盖后来完成的工作。"
            elif action == "checkpoint_create":
                retry_note = "创建检查点可能已落盘；请先查看现有检查点，再决定是否新建。"
            elif row["method"] == "workflow.run" and action in {"write", "revise", "batch_draft"}:
                retry_note = "重放可能重复调用 Writer 或覆盖较新的草稿；请使用当前版本和已保存断点续接。"
        settings_summary: dict[str, Any] = {"status": "legacy_unrecorded", "source": "unrecorded"}
        if row["task_id"]:
            try:
                snapshot = StudioDatabase._task_settings_record(row)
                settings_summary = {"status": "captured", **validate_task_settings_snapshot(snapshot)}
            except TaskSettingsError as exc:
                settings_summary = {"status": "invalid", "error": str(exc)}
                retryable = False
                retry_note = str(exc)
                next_step = "任务设置快照不能可靠恢复；保留现有成果，先查看配置版本和错误详情。"
        status_check_pending = status == "running" and _owner_process_alive(str(row["owner_id"])) is None
        if status_check_pending:
            next_step = "暂时无法确认原引擎进程是否还在运行；请稍后刷新状态，不要重发整项请求。"
        try:
            progress = json.loads(row["progress_json"]) if row["progress_json"] else []
            if not isinstance(progress, list):
                progress = []
            progress = [item for item in progress if isinstance(item, dict) and isinstance(item.get("summary"), str)]
        except (TypeError, json.JSONDecodeError):
            progress = []
        return {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "title": row["custom_title"] or _task_display_title(row["method"], action, params),
            "objective": re.sub(r"\s+", " ", str(
                params.get("message") or params.get("instruction") or _task_display_title(row["method"], action, params)
            ).split("\n# 用户的运行中引导", 1)[0]).strip()[:240],
            "progress": progress,
            "settings_snapshot": settings_summary,
            "method": row["method"],
            "action": action,
            "status": row["status"],
            "params": params,
            "summary": row["summary"] or "",
            "error_message": row["error_message"] or "",
            "error_code": row["error_code"] or "",
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "retryable": retryable,
            "retry_note": retry_note,
            "next_step": next_step,
            "status_check_pending": status_check_pending,
        }


def _owner_process_alive(owner_id: str) -> bool | None:
    """尽力判断同一台电脑上的引擎进程是否仍在运行，不增加运行依赖。"""

    match = re.fullmatch(r"server-(\d+)-[0-9a-f]+", str(owner_id))
    if not match:
        return None
    pid = int(match.group(1))
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            # Access denied does not prove the process has exited. Only the
            # invalid-PID result is safe to classify as dead.
            return False if ctypes.get_last_error() == 87 else None
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


class StudioService:
    def __init__(self, project: InkFlowProject):
        self.project = project
        self.db = StudioDatabase(project.internal / "studio.db")

    def dashboard(self) -> dict[str, Any]:
        brief = self.project.db.get_brief()
        plan = self.project.db.get_current_plan_bundle()
        accepted = self.project.db.accepted_chapters()
        total_characters = 0
        for chapter in accepted:
            canonical = self.project.db.canonical_chapter_content(int(chapter["chapter_no"]))
            if canonical is not None and content_hash(canonical) == chapter["content_hash"]:
                total_characters += text_statistics(canonical)["characters"]
            else:
                path = self.project.root / chapter["path"]
                if path.is_file():
                    projected = path.read_text(encoding="utf-8")
                    if content_hash(projected) == chapter["content_hash"]:
                        total_characters += text_statistics(projected)["characters"]
        return {
            "project_id": self.project.project_id,
            "root": str(self.project.root),
            "recovery_warnings": self.project.recovery_warnings,
            "brief": brief.model_dump(mode="json"),
            "status": self.project.db.project_status(),
            "current_plan": (
                {
                    "volume": plan.current_volume.model_dump(mode="json"),
                    "arc": plan.current_arc.model_dump(mode="json"),
                }
                if plan
                else None
            ),
            "facts": self.project.db.current_facts(),
            "threads": self.project.db.open_threads(),
            "bible_entries": self.db.list_bible_entries(),
            "accepted_characters": total_characters,
            "quality_hold": self.project.latest_accepted_quality_hold(),
            "planning_impact": self.project.db.planning_source_impact(),
            "pending_planning_publication": {
                key: pending.get(key) for key in ("run_id", "anchor", "end")
            } if isinstance((pending := self.project.db.get_metadata("pending_planning_publication", {})), dict)
            and pending.get("run_id") else None,
        }

    def tree(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for name, label, kind in (
            ("BOOK.md", "书籍设定", "book"),
            ("OUTLINE.md", "全书大纲", "plan"),
            ("STORY_DETAIL.md", "剧情细纲", "plan"),
            ("RECENT_PLAN.md", "近期章节规划", "plan"),
            ("PLAN.md", "近期计划", "plan"),
            ("STATE.md", "正史状态", "state"),
            ("DIALOGUE.md", "对话记录", "dialogue"),
        ):
            path = self.project.root / name
            if path.is_file():
                items.append(self._tree_file(path, label=label, kind=kind))

        chapters: list[dict[str, Any]] = []
        for path in sorted((self.project.root / "chapters").glob("*.md")):
            match = re.search(r"chapter_(\d+)", path.name)
            chapter_no = int(match.group(1)) if match else None
            record = self.project.db.get_chapter(chapter_no) if chapter_no else None
            label = f"第 {chapter_no} 章" if chapter_no else path.stem
            if path.name.endswith(".draft.md"):
                label += " · 草稿"
            elif record and record.get("status") == "accepted":
                label += " · 正史"
            chapters.append(
                self._tree_file(
                    path,
                    label=label,
                    kind="chapter",
                    extra={"chapter_no": chapter_no, "status": (record or {}).get("status", "file")},
                )
            )

        reviews: list[dict[str, Any]] = []
        for path in sorted((self.project.root / "reviews").glob("*.md"), reverse=True):
            match = re.search(r"chapter_(\d+)", path.name)
            chapter_no = int(match.group(1)) if match else None
            reviews.append(
                self._tree_file(
                    path,
                    label=f"第 {chapter_no} 章审查" if chapter_no else path.stem,
                    kind="review",
                    extra={"chapter_no": chapter_no} if chapter_no else None,
                )
            )
        planning_dir = self.project.root / "planning"
        planning = (
            [self._tree_file(path, label=path.stem, kind="planning") for path in sorted(planning_dir.glob("*.md"))]
            if planning_dir.is_dir()
            else []
        )
        batches_dir = self.project.root / "batches"
        batches = (
            [self._tree_file(path, label=path.stem, kind="batch") for path in sorted(batches_dir.glob("*.md"), reverse=True)]
            if batches_dir.is_dir()
            else []
        )
        return {
            "project_id": self.project.project_id,
            "root": str(self.project.root),
            "items": items,
            "groups": [
                {"id": "chapters", "label": "章节", "items": chapters},
                {"id": "planning", "label": "规划判断", "items": planning},
                {"id": "reviews", "label": "审查报告", "items": reviews},
                {"id": "batches", "label": "批量草稿", "items": batches},
            ],
        }

    def read_document(self, relative_path: str) -> dict[str, Any]:
        path = self.project.resolve_user_path(relative_path)
        if not path.is_file():
            raise ProjectError(f"文件不存在：{relative_path}")
        content = path.read_text(encoding="utf-8")
        digest = content_hash(content)
        annotations = self._reanchor_annotations(relative_path, content, digest)
        protection = self._document_protection(relative_path)
        return {
            "relative_path": relative_path.replace("\\", "/"),
            "content": content,
            "content_hash": digest,
            "statistics": text_statistics(content),
            "annotations": annotations,
            "versions": self.db.list_versions(relative_path.replace("\\", "/"), 20),
            **protection,
        }

    def save_document(
        self,
        relative_path: str,
        content: str,
        *,
        expected_hash: str | None,
        source: str = "desktop_manual",
    ) -> dict[str, Any]:
        with project_write_lock_sync(self.project.root):
            return self._save_document_unlocked(
                relative_path,
                content,
                expected_hash=expected_hash,
                source=source,
            )

    def delete_document(self, relative_path: str, *, expected_hash: str) -> dict[str, Any]:
        """Move one unchanged, non-canon file to the project recovery area."""
        relative = relative_path.replace("\\", "/")
        with project_write_lock_sync(self.project.root):
            candidate = self.project.root / relative
            if candidate.is_symlink():
                raise ProjectError("符号链接不能从文档工作区删除。")
            path = self.project.resolve_user_path(relative)
            if not path.is_file():
                raise ProjectError("这里只能删除单个普通文件，文件夹请逐项处理。")
            actual_hash = content_hash(path.read_text(encoding="utf-8"))
            if not expected_hash or actual_hash != expected_hash:
                raise ValidationGateError("文件在预览后发生变化，未删除。请重新打开后再操作。")
            protection = self._document_protection(relative)
            if protection["read_only"]:
                raise ValidationGateError("已接受的正史正文不能按普通文件删除；请先走正史修订/分支流程，避免数据库仍把它当作当前事实。")
            chapter_no = _chapter_number_from_path(relative) if relative.endswith(".draft.md") else None
            chapter = self.project.db.get_chapter(chapter_no) if chapter_no else None
            if chapter and chapter.get("status") == "accepted":
                raise ValidationGateError("该章节已经进入正史，普通文件删除不会改变正史状态。请从正史修订流程处理。")
            if chapter and str(chapter.get("path") or "").replace("\\", "/") != relative:
                raise ValidationGateError("该文件不是章节记录当前指向的草稿，未删除以免影响另一份草稿。")
            result = self.project._delete_file_locked(relative, permanent=False)
            if chapter_no and chapter and chapter.get("status") == "draft":
                if not self.project.db.mark_draft_discarded(chapter_no, relative, actual_hash):
                    self.project.restore_document(str(result["trash_id"]), expected_hash=actual_hash)
                    raise ValidationGateError("章节草稿记录刚刚变化，文件已恢复原位；请刷新后重试。")
            return {**result, "relative_path": relative, "document_hash": actual_hash}

    def _save_document_unlocked(
        self,
        relative_path: str,
        content: str,
        *,
        expected_hash: str | None,
        source: str = "desktop_manual",
    ) -> dict[str, Any]:
        relative = relative_path.replace("\\", "/")
        path = self.project.resolve_user_path(relative)
        old_content = path.read_text(encoding="utf-8") if path.is_file() else ""
        old_hash = content_hash(old_content)
        if expected_hash and expected_hash != old_hash:
            raise ValidationGateError("文件已被其他操作修改。请重新载入并检查 Diff，墨流没有覆盖新内容。")
        protection = self._document_protection(relative)
        if protection["read_only"]:
            proposal = self.db.capture_version(
                relative,
                content,
                parent_hash=old_hash,
                source="accepted_chapter_revision_proposal",
                applied=False,
            )
            return {
                "saved": False,
                "proposal": proposal,
                "gate": protection["reason"],
                "requires_canon_revision": True,
            }
        if old_content:
            self.db.capture_version(
                relative,
                old_content,
                parent_hash=None,
                source=f"before:{source}",
                applied=True,
            )
        atomic_write_text(path, content)
        chapter_no = _chapter_number_from_path(relative)
        if chapter_no and relative.endswith(".draft.md"):
            title = _title_from_document(content, chapter_no)
            self.project.db.upsert_draft(chapter_no, title, relative, content)
        current_hash = content_hash(content)
        return {
            "saved": True,
            "relative_path": relative,
            "content_hash": current_hash,
            "statistics": text_statistics(content),
            "annotations": self._reanchor_annotations(relative, content, current_hash),
        }

    def create_annotation(
        self,
        relative_path: str,
        start_offset: int,
        end_offset: int,
        comment: str,
    ) -> dict[str, Any]:
        with project_write_lock_sync(self.project.root):
            return self._create_annotation_unlocked(relative_path, start_offset, end_offset, comment)

    def _create_annotation_unlocked(
        self,
        relative_path: str,
        start_offset: int,
        end_offset: int,
        comment: str,
    ) -> dict[str, Any]:
        relative = relative_path.replace("\\", "/")
        path = self.project.resolve_user_path(relative)
        if not path.is_file():
            raise ProjectError(f"文件不存在：{relative}")
        content = path.read_text(encoding="utf-8")
        if not (0 <= start_offset < end_offset <= len(content)):
            raise ProjectError("批注选区超出正文范围。")
        quote = content[start_offset:end_offset]
        if not quote.strip():
            raise ProjectError("不能给空白内容添加批注。")
        if not comment.strip():
            raise ProjectError("批注内容不能为空。")
        return self.db.create_annotation(
            relative_path=relative,
            document_hash=content_hash(content),
            start_offset=start_offset,
            end_offset=end_offset,
            quote=quote,
            comment=comment.strip(),
        )

    def set_annotation_status(self, annotation_id: str, status: str) -> dict[str, Any]:
        with project_write_lock_sync(self.project.root):
            return self.db.set_annotation_status(annotation_id, status)

    def upsert_scene_note(self, chapter_no: int, scene_no: int, data: dict[str, Any]) -> dict[str, Any]:
        with project_write_lock_sync(self.project.root):
            return self.db.upsert_scene_note(chapter_no, scene_no, data)

    def upsert_bible_entry(self, **kwargs: Any) -> dict[str, Any]:
        with project_write_lock_sync(self.project.root):
            return self.db.upsert_bible_entry(**kwargs)

    def search(self, query: str, limit: int = 100) -> dict[str, Any]:
        needle = query.strip()
        if not needle:
            raise ProjectError("搜索内容不能为空。")
        candidates: list[Path] = []
        for name in ("BOOK.md", "OUTLINE.md", "STORY_DETAIL.md", "PLAN.md", "STATE.md", "DIALOGUE.md"):
            path = self.project.root / name
            if path.is_file():
                candidates.append(path)
        for directory in ("chapters", "reviews", "planning"):
            folder = self.project.root / directory
            if folder.is_dir():
                candidates.extend(sorted(folder.glob("*.md")))
        matches: list[dict[str, Any]] = []
        folded = needle.casefold()
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                at = line.casefold().find(folded)
                if at < 0:
                    continue
                matches.append(
                    {
                        "relative_path": path.relative_to(self.project.root).as_posix(),
                        "line": line_no,
                        "column": at + 1,
                        "preview": line.strip()[:300],
                    }
                )
                if len(matches) >= max(1, min(limit, 500)):
                    return {"query": needle, "matches": matches, "truncated": True}
        return {"query": needle, "matches": matches, "truncated": False}

    def chapter_workspace(self, chapter_no: int) -> dict[str, Any]:
        card = self.project.db.get_chapter_card(chapter_no)
        record = self.project.db.get_chapter(chapter_no)
        review_record = self.project.db.latest_review_record(chapter_no)
        artifacts = self.project.db.list_agent_artifacts(chapter_no=chapter_no, limit=30)
        current_version = int(record["version"]) if record else None
        current_hook = next(
            (
                item
                for item in artifacts
                if item.get("artifact_type") == "writer_hook_note"
                and int(item.get("chapter_version") or 0) == int(current_version or 0)
            ),
            None,
        )
        current_blueprint = next(
            (
                item
                for item in artifacts
                if item.get("artifact_type") == "writer_scene_blueprint"
                and int(item.get("chapter_version") or 0) == int(current_version or 0)
            ),
            None,
        )
        current_context_manifest = next(
            (
                item
                for item in artifacts
                if item.get("artifact_type") == "writer_context_manifest"
                and int(item.get("chapter_version") or 0) == int(current_version or 0)
            ),
            None,
        )
        review = None
        if review_record:
            review = {
                "chapter_version": review_record["chapter_version"],
                "path": review_record["path"],
                "report": review_record["report"].model_dump(mode="json"),
                "matches_current_version": review_record["chapter_version"] == current_version,
            }
        return {
            "chapter_no": chapter_no,
            "card": card,
            "record": record,
            "review": review,
            "hook_note": current_hook["data"] if current_hook else None,
            "scene_blueprint": current_blueprint["data"] if current_blueprint else None,
            "context_manifest": current_context_manifest["data"] if current_context_manifest else None,
            "context_pins": self.db.list_context_pins(chapter_no),
            "can_accept": bool(
                record
                and record.get("status") == "draft"
                and review
                and review["matches_current_version"]
                and review["report"].get("verdict") == "pass"
            ),
            "active_preferences": self.project.db.effective_preferences(),
            "scene_notes": self.db.scene_notes(chapter_no),
            "inherited_facts": self.project.db.current_facts(),
            "open_threads": self.project.db.open_threads(),
        }

    def _document_protection(self, relative_path: str) -> dict[str, Any]:
        chapter_no = _chapter_number_from_path(relative_path)
        if chapter_no and relative_path.endswith(f"chapter_{chapter_no:05d}.md"):
            record = self.project.db.get_chapter(chapter_no)
            if record and record.get("status") == "accepted":
                return {
                    "read_only": True,
                    "reason": (
                        "这是已进入正史的正文。修改内容已保存为未应用提案；"
                        "请先进行影响预览和检查点，再由 Writer 修订、Reviewer 重审并重新验收。"
                    ),
                }
        return {"read_only": False, "reason": ""}

    def _reanchor_annotations(
        self,
        relative_path: str,
        content: str,
        document_hash: str,
    ) -> list[dict[str, Any]]:
        annotations = self.db.list_annotations(relative_path)
        for item in annotations:
            if item["status"] not in {"open", "orphaned"}:
                continue
            start = int(item["start_offset"])
            end = int(item["end_offset"])
            if item["document_hash"] == document_hash and content[start:end] == item["quote"]:
                continue
            positions = [match.start() for match in re.finditer(re.escape(item["quote"]), content)]
            if positions:
                new_start = min(positions, key=lambda value: abs(value - start))
                new_end = new_start + len(item["quote"])
                self.db.update_annotation_anchor(
                    item["annotation_id"],
                    document_hash=document_hash,
                    start_offset=new_start,
                    end_offset=new_end,
                    status="open",
                )
                item.update(
                    {
                        "document_hash": document_hash,
                        "start_offset": new_start,
                        "end_offset": new_end,
                        "status": "open",
                    }
                )
            else:
                self.db.update_annotation_anchor(
                    item["annotation_id"],
                    document_hash=document_hash,
                    start_offset=start,
                    end_offset=end,
                    status="orphaned",
                )
                item.update({"document_hash": document_hash, "status": "orphaned"})
        return annotations

    def _tree_file(
        self,
        path: Path,
        *,
        label: str,
        kind: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = {
            "id": path.relative_to(self.project.root).as_posix(),
            "label": label,
            "kind": kind,
            "relative_path": path.relative_to(self.project.root).as_posix(),
            "modified_at": path.stat().st_mtime,
        }
        if extra:
            item.update(extra)
        return item


def text_statistics(content: str) -> dict[str, Any]:
    paragraphs = [item for item in re.split(r"\n\s*\n", content) if item.strip()]
    dialogue_matches = re.findall(r"“([^”]+)”|「([^」]+)」|\"([^\"\n]+)\"", content)
    dialogue_text = "".join("".join(group) for group in dialogue_matches)
    dialogue_characters = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", dialogue_text))
    characters = effective_character_count(content)
    return {
        "characters": characters,
        "raw_characters": len(content),
        "paragraphs": len(paragraphs),
        "dialogue_segments": len(dialogue_matches),
        "dialogue_ratio": round(dialogue_characters / characters, 4) if characters else 0,
        "estimated_reading_minutes": max(1, math.ceil(characters / 500)) if characters else 0,
    }


def _chapter_number_from_path(relative_path: str) -> int | None:
    match = re.fullmatch(r"chapters/chapter_(\d{5})(?:\.draft)?\.md", relative_path.replace("\\", "/"))
    return int(match.group(1)) if match else None


def _title_from_document(content: str, chapter_no: int) -> str:
    first = next((line.strip() for line in content.splitlines() if line.strip()), "")
    first = re.sub(r"^#+\s*", "", first)
    first = re.sub(rf"^第\s*{chapter_no}\s*章\s*[:：—-]?\s*", "", first)
    return first or f"第 {chapter_no} 章"
