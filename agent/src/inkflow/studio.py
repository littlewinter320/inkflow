from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import ProjectError, ValidationGateError
from .project import InkFlowProject
from .utils import atomic_write_text, content_hash, json_dumps, utc_now


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

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    method TEXT NOT NULL,
    action TEXT,
    status TEXT NOT NULL,
    params_json TEXT NOT NULL,
    summary TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_runs_status_updated
ON task_runs(status, updated_at DESC);
"""


class StudioDatabase:
    """非正史的桌面辅助数据。

    `inkflow.db` 仍然是小说正史。这里仅保存可删除重建或尚待验收的
    编辑快照、行级批注、场景笔记和用户手工故事圣经条目。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(STUDIO_SCHEMA)
            connection.execute(
                """
                INSERT INTO studio_metadata(key, value_json, updated_at)
                VALUES ('schema_version', '2', ?)
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

    def start_task(
        self,
        run_id: str,
        *,
        owner_id: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        action = str(params.get("action") or "") or None
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO task_runs(
                    run_id, owner_id, method, action, status, params_json,
                    summary, error_message, started_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    method=excluded.method,
                    action=excluded.action,
                    status='running',
                    params_json=excluded.params_json,
                    summary=NULL,
                    error_message=NULL,
                    updated_at=excluded.updated_at
                """,
                (run_id, owner_id, method, action, json_dumps(params, indent=None), now, now),
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
    ) -> dict[str, Any] | None:
        if status not in {"completed", "failed", "cancelled", "interrupted", "dismissed"}:
            raise ProjectError(f"不支持的任务状态：{status}")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE task_runs
                SET status=?, summary=?, error_message=?, updated_at=?
                WHERE run_id=?
                """,
                (status, summary[:1000], error_message[:2000], utc_now(), run_id),
            )
            connection.commit()
        return self.get_task(run_id)

    def reconcile_interrupted_tasks(self, owner_id: str) -> int:
        """把已经退出的本地引擎遗留任务标记为中断。

        桌面版和 VS Code 可以同时打开同一本小说，因此不能把“不是当前
        server id”的运行都视作崩溃。新 owner id 带进程号；只有确认对应
        进程已经退出时，才把任务改为 interrupted。
        """

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT run_id, owner_id FROM task_runs WHERE status='running' AND owner_id<>?",
                (owner_id,),
            ).fetchall()
            interrupted = [row["run_id"] for row in rows if not _owner_process_alive(row["owner_id"])]
            if not interrupted:
                return 0
            connection.executemany(
                """
                UPDATE task_runs
                SET status='interrupted',
                    summary='上一次墨流进程在任务结束前退出，可检查后重新运行。',
                    updated_at=?
                WHERE run_id=? AND status='running'
                """,
                [(utc_now(), run_id) for run_id in interrupted],
            )
            connection.commit()
            return len(interrupted)

    def get_task(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ProjectError(f"任务不存在：{run_id}")
        return self._task_row(row)

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE status<>'dismissed' ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._task_row(row) for row in rows]

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        params = json.loads(row["params_json"])
        action = row["action"]
        retryable_actions = {
            "plan",
            "write",
            "review",
            "revise",
            "batch_draft",
            "arc_audit",
            "checkpoint_create",
        }
        safe_retry = row["method"] in {"reference.fetch", "reference.analyze"} or (
            row["method"] == "workflow.run" and action in retryable_actions
        )
        retryable = row["status"] in {"failed", "cancelled", "interrupted"} and safe_retry
        retry_note = ""
        if row["status"] in {"failed", "cancelled", "interrupted"} and not safe_retry:
            retry_note = "为避免绕过验收或回退确认，请回到对话中重新说明并确认。"
        return {
            "run_id": row["run_id"],
            "method": row["method"],
            "action": action,
            "status": row["status"],
            "params": params,
            "summary": row["summary"] or "",
            "error_message": row["error_message"] or "",
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "retryable": retryable,
            "retry_note": retry_note,
        }


def _owner_process_alive(owner_id: str) -> bool:
    """尽力判断同一台电脑上的引擎进程是否仍在运行，不增加运行依赖。"""

    match = re.fullmatch(r"server-(\d+)-[0-9a-f]+", str(owner_id))
    if not match:
        return False
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
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
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
            path = self.project.root / chapter["path"]
            if path.is_file():
                total_characters += text_statistics(path.read_text(encoding="utf-8"))["characters"]
        return {
            "project_id": self.project.project_id,
            "root": str(self.project.root),
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
        }

    def tree(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for name, label, kind in (
            ("BOOK.md", "书籍设定", "book"),
            ("PLAN.md", "当前规划", "plan"),
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

        reviews = [
            self._tree_file(path, label=path.stem, kind="review")
            for path in sorted((self.project.root / "reviews").glob("*.md"), reverse=True)
        ]
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

    def search(self, query: str, limit: int = 100) -> dict[str, Any]:
        needle = query.strip()
        if not needle:
            raise ProjectError("搜索内容不能为空。")
        candidates: list[Path] = []
        for name in ("BOOK.md", "PLAN.md", "STATE.md", "DIALOGUE.md"):
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
        return {
            "chapter_no": chapter_no,
            "card": card,
            "record": record,
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
    meaningful = re.findall(r"[\u3400-\u9fffA-Za-z0-9]", content)
    paragraphs = [item for item in re.split(r"\n\s*\n", content) if item.strip()]
    dialogue_matches = re.findall(r"“([^”]+)”|「([^」]+)」|\"([^\"\n]+)\"", content)
    dialogue_text = "".join("".join(group) for group in dialogue_matches)
    dialogue_characters = len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", dialogue_text))
    characters = len(meaningful)
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
