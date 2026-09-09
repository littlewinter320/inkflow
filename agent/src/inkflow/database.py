from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .schemas import BookBrief, MemoryPatch, PlanBundle, ReviewReport
from .utils import content_hash, json_dumps, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    kind TEXT NOT NULL,
    plan_key TEXT NOT NULL,
    parent_key TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, plan_key)
);

CREATE TABLE IF NOT EXISTS chapters (
    chapter_no INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_text TEXT,
    summary TEXT,
    updated_at TEXT NOT NULL,
    accepted_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_no INTEGER NOT NULL,
    chapter_version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    data_json TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value_json TEXT NOT NULL,
    valid_from_chapter INTEGER NOT NULL,
    valid_to_chapter INTEGER,
    source_chapter INTEGER NOT NULL,
    confidence REAL NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_current
ON facts(subject, predicate, status, valid_to_chapter);

CREATE TABLE IF NOT EXISTS plot_threads (
    thread_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    planted_chapter INTEGER,
    due_chapter INTEGER,
    last_advanced_chapter INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_patches (
    chapter_no INTEGER PRIMARY KEY,
    data_json TEXT NOT NULL,
    committed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provisional_memory_patches (
    batch_id TEXT NOT NULL,
    chapter_no INTEGER NOT NULL,
    chapter_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    data_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    invalidation_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    promoted_at TEXT,
    PRIMARY KEY (batch_id, chapter_no)
);

CREATE INDEX IF NOT EXISTS idx_provisional_memory_active
ON provisional_memory_patches(batch_id, status, chapter_no);

CREATE TABLE IF NOT EXISTS collaboration_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    sender_role TEXT NOT NULL,
    recipient_role TEXT NOT NULL,
    message_type TEXT NOT NULL,
    chapter_no INTEGER,
    chapter_version INTEGER,
    context_packet_id TEXT,
    claim TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    requested_response TEXT NOT NULL,
    status TEXT NOT NULL,
    expires_at TEXT,
    response_to TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_collaboration_active
ON collaboration_messages(status, recipient_role, chapter_no, chapter_version);

CREATE TABLE IF NOT EXISTS user_preferences (
    preference_id TEXT PRIMARY KEY,
    strength TEXT NOT NULL,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    chapter_no INTEGER,
    chapter_version INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retrieval_feedback (
    feedback_id TEXT PRIMARY KEY,
    query_hash TEXT NOT NULL,
    source_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    role TEXT NOT NULL,
    chapter_no INTEGER,
    packet_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retrieval_feedback_source
ON retrieval_feedback(source_id, outcome);
"""


class ProjectDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def canonical_content_migration_required(self) -> bool:
        """Whether this legacy database needs the accepted-text column.

        Opening a project must remain read-only with respect to a schema change:
        the desktop asks for an explicit confirmation before `migrate_*` is run.
        """

        with self.connect() as connection:
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(chapters)").fetchall()
            }
        return "content_text" not in columns

    def migrate_canonical_content(self) -> None:
        """Perform the confirmed, additive legacy schema upgrade."""

        with self.connect() as connection:
            columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(chapters)").fetchall()
            }
            if "content_text" not in columns:
                connection.execute("ALTER TABLE chapters ADD COLUMN content_text TEXT")
            connection.commit()

    def _require_canonical_content_schema(self) -> None:
        if self.canonical_content_migration_required():
            raise ValueError("项目需要先确认“正史正文数据库备份与升级”，才能修改或恢复正史。")

    def set_metadata(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, json_dumps(value, indent=None), utc_now()),
            )
            connection.commit()

    def get_metadata(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def set_brief(self, brief: BookBrief) -> None:
        self.set_metadata("book_brief", brief.model_dump(mode="json"))

    def get_brief(self) -> BookBrief:
        value = self.get_metadata("book_brief")
        if not value:
            raise ValueError("项目缺少 book_brief")
        return BookBrief.model_validate(value)

    def save_plan_bundle(self, bundle: PlanBundle) -> None:
        now = utc_now()
        volume_key = f"volume:{bundle.current_volume.volume_no:03d}"
        rows: list[tuple[str, str, str | None, str]] = [
            ("book", "book", None, bundle.book.model_dump_json()),
            (
                "volume",
                volume_key,
                "book",
                bundle.current_volume.model_dump_json(),
            ),
            (
                "arc",
                bundle.current_arc.arc_id,
                volume_key,
                bundle.current_arc.model_dump_json(),
            ),
        ]
        rows.extend(
            (
                "chapter",
                f"chapter:{card.chapter_no:05d}",
                bundle.current_arc.arc_id,
                card.model_dump_json(),
            )
            for card in bundle.current_arc.chapter_cards
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for kind, key, parent, data in rows:
                connection.execute(
                    """
                    INSERT INTO plans(kind, plan_key, parent_key, data_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(kind, plan_key) DO UPDATE SET
                        parent_key=excluded.parent_key,
                        version=plans.version+1,
                        status='active',
                        data_json=excluded.data_json,
                        updated_at=excluded.updated_at
                    """,
                    (kind, key, parent, data, now),
                )
            # A pointer is safer than `ORDER BY updated_at`: several planning
            # commits can legitimately happen inside the same clock second.
            pointer = {
                "book_key": "book",
                "volume_key": volume_key,
                "arc_key": bundle.current_arc.arc_id,
            }
            connection.execute(
                """
                INSERT INTO metadata(key, value_json, updated_at) VALUES ('current_plan', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (json_dumps(pointer, indent=None), now),
            )
            connection.commit()

    def get_plan(self, kind: str, plan_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM plans WHERE kind=? AND plan_key=? AND status='active'",
                (kind, plan_key),
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def get_chapter_card(self, chapter_no: int) -> dict[str, Any] | None:
        return self.get_plan("chapter", f"chapter:{chapter_no:05d}")

    def get_current_plan_bundle(self) -> PlanBundle | None:
        with self.connect() as connection:
            pointer_row = connection.execute(
                "SELECT value_json FROM metadata WHERE key='current_plan'"
            ).fetchone()
            if pointer_row:
                pointer = json.loads(pointer_row["value_json"])
                book = connection.execute(
                    "SELECT data_json FROM plans WHERE kind='book' AND plan_key=? AND status='active'",
                    (pointer["book_key"],),
                ).fetchone()
                volume = connection.execute(
                    "SELECT data_json FROM plans WHERE kind='volume' AND plan_key=? AND status='active'",
                    (pointer["volume_key"],),
                ).fetchone()
                arc = connection.execute(
                    "SELECT data_json FROM plans WHERE kind='arc' AND plan_key=? AND status='active'",
                    (pointer["arc_key"],),
                ).fetchone()
            else:
                # Backward-compatible recovery for projects created before the
                # explicit pointer was introduced.
                book = connection.execute(
                    "SELECT data_json FROM plans WHERE kind='book' AND status='active' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                volume = connection.execute(
                    "SELECT data_json FROM plans WHERE kind='volume' AND status='active' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                arc = connection.execute(
                    "SELECT data_json FROM plans WHERE kind='arc' AND status='active' ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
        if not (book and volume and arc):
            return None
        return PlanBundle.model_validate(
            {
                "book": json.loads(book["data_json"]),
                "current_volume": json.loads(volume["data_json"]),
                "current_arc": json.loads(arc["data_json"]),
            }
        )

    def upsert_draft(self, chapter_no: int, title: str, path: str, content: str) -> int:
        needs_canon_migration = self.canonical_content_migration_required()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT version,status FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            if existing and existing["status"] == "accepted" and needs_canon_migration:
                raise ValueError("旧版正史需先确认数据库备份与升级，不能直接覆盖为草稿。")
            version = int(existing["version"]) + 1 if existing else 1
            connection.execute(
                """
                INSERT INTO chapters(chapter_no, title, status, version, path, content_hash, updated_at)
                VALUES (?, ?, 'draft', ?, ?, ?, ?)
                ON CONFLICT(chapter_no) DO UPDATE SET
                    title=excluded.title,
                    status='draft',
                    version=excluded.version,
                    path=excluded.path,
                    content_hash=excluded.content_hash,
                    updated_at=excluded.updated_at,
                    accepted_at=NULL
                """,
                (chapter_no, title, version, path, content_hash(content), utc_now()),
            )
            connection.commit()
        return version

    def get_chapter(self, chapter_no: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT chapter_no,title,status,version,path,content_hash,summary,updated_at,accepted_at
                FROM chapters WHERE chapter_no=?""",
                (chapter_no,),
            ).fetchone()
        return dict(row) if row else None

    def chapter_numbers_by_status(self, status: str) -> list[int]:
        """Return chapter numbers for one known lifecycle state.

        This is used by the conversation router to resolve phrases such as
        "刚才那章" only when the project has exactly one matching draft.
        """

        if status not in {"draft", "accepted"}:
            raise ValueError(f"不支持的章节状态：{status}")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT chapter_no FROM chapters WHERE status=? ORDER BY chapter_no",
                (status,),
            ).fetchall()
        return [int(row["chapter_no"]) for row in rows]

    def recent_accepted_chapters(self, before_chapter: int, limit: int = 3) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT chapter_no,title,status,version,path,content_hash,summary,updated_at,accepted_at FROM chapters
                WHERE status='accepted' AND chapter_no < ?
                ORDER BY chapter_no DESC LIMIT ?
                """,
                (before_chapter, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_accepted_chapter_no(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(chapter_no), 0) AS chapter_no FROM chapters WHERE status='accepted'"
            ).fetchone()
        return int(row["chapter_no"])

    def accepted_chapter_numbers(self, chapter_start: int, chapter_end: int) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT chapter_no FROM chapters
                WHERE status='accepted' AND chapter_no BETWEEN ? AND ?
                ORDER BY chapter_no
                """,
                (chapter_start, chapter_end),
            ).fetchall()
        return [int(row["chapter_no"]) for row in rows]

    def accepted_chapters(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT chapter_no,title,status,version,path,content_hash,summary,updated_at,accepted_at
                FROM chapters WHERE status='accepted' ORDER BY chapter_no"""
            ).fetchall()
        return [dict(row) for row in rows]

    def save_review(self, chapter_no: int, chapter_version: int, report: ReviewReport, path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO reviews(chapter_no, chapter_version, verdict, data_json, path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chapter_no,
                    chapter_version,
                    report.verdict,
                    report.model_dump_json(),
                    path,
                    utc_now(),
                ),
            )
            connection.commit()

    def latest_review(self, chapter_no: int) -> ReviewReport | None:
        record = self.latest_review_record(chapter_no)
        return record["report"] if record else None

    def latest_review_record(self, chapter_no: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT chapter_version, data_json, path FROM reviews WHERE chapter_no=? ORDER BY id DESC LIMIT 1",
                (chapter_no,),
            ).fetchone()
        if not row:
            return None
        return {
            "chapter_version": int(row["chapter_version"]),
            "report": ReviewReport.model_validate_json(row["data_json"]),
            "path": str(row["path"]),
        }

    def current_facts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM facts WHERE status='active' AND valid_to_chapter IS NULL ORDER BY subject, predicate"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            result.append(item)
        return result

    def open_threads(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM plot_threads
                WHERE status IN ('open', 'advanced', 'delayed')
                ORDER BY COALESCE(due_chapter, 999999), thread_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def save_provisional_memory_patch(
        self,
        batch_id: str,
        chapter_no: int,
        chapter_version: int,
        content: str,
        patch: MemoryPatch,
    ) -> None:
        """Store only the current reviewed patch for one batch chapter.

        This table is a staging ledger.  Its rows never participate in the
        canonical facts/threads queries until accept_chapter promotes them.
        """

        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO provisional_memory_patches(
                    batch_id, chapter_no, chapter_version, content_hash, data_json,
                    status, invalidation_reason, created_at, updated_at, promoted_at
                ) VALUES (?, ?, ?, ?, ?, 'active', NULL, ?, ?, NULL)
                ON CONFLICT(batch_id, chapter_no) DO UPDATE SET
                    chapter_version=excluded.chapter_version,
                    content_hash=excluded.content_hash,
                    data_json=excluded.data_json,
                    status='active',
                    invalidation_reason=NULL,
                    updated_at=excluded.updated_at,
                    promoted_at=NULL
                """,
                (
                    batch_id,
                    chapter_no,
                    chapter_version,
                    content_hash(content),
                    patch.model_dump_json(),
                    now,
                    now,
                ),
            )
            connection.commit()

    def get_provisional_memory_patch(self, batch_id: str, chapter_no: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM provisional_memory_patches
                WHERE batch_id=? AND chapter_no=?
                """,
                (batch_id, chapter_no),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["patch"] = MemoryPatch.model_validate_json(item.pop("data_json"))
        return item

    def active_provisional_memory(
        self,
        batch_id: str,
        *,
        before_chapter: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM provisional_memory_patches "
            "WHERE batch_id=? AND status='active'"
        )
        params: list[Any] = [batch_id]
        if before_chapter is not None:
            query += " AND chapter_no < ?"
            params.append(before_chapter)
        query += " ORDER BY chapter_no"
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["patch"] = MemoryPatch.model_validate_json(item.pop("data_json"))
            result.append(item)
        return result

    def invalidate_provisional_memory_from(self, batch_id: str, chapter_no: int, reason: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE provisional_memory_patches
                SET status='invalidated', invalidation_reason=?, updated_at=?
                WHERE batch_id=? AND chapter_no>=? AND status='active'
                """,
                (reason, utc_now(), batch_id, chapter_no),
            )
            connection.commit()
        return int(cursor.rowcount)

    def accept_chapter(
        self,
        chapter_no: int,
        title: str,
        final_path: str,
        content: str,
        patch: MemoryPatch,
        *,
        provisional_batch_id: str | None = None,
    ) -> None:
        self._require_canonical_content_schema()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT version, content_hash FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            if not current:
                raise ValueError(f"第 {chapter_no} 章没有草稿记录")
            if provisional_batch_id:
                staged = connection.execute(
                    """
                    SELECT chapter_version, content_hash, data_json, status
                    FROM provisional_memory_patches
                    WHERE batch_id=? AND chapter_no=?
                    """,
                    (provisional_batch_id, chapter_no),
                ).fetchone()
                if not staged or staged["status"] != "active":
                    raise ValueError(f"第 {chapter_no} 章没有可提升的批次临时记忆")
                if int(staged["chapter_version"]) != int(current["version"]):
                    raise ValueError(f"第 {chapter_no} 章临时记忆对应的草稿版本已经失效")
                if staged["content_hash"] != content_hash(content):
                    raise ValueError(f"第 {chapter_no} 章临时记忆对应的正文内容已经变化")
                staged_patch = MemoryPatch.model_validate_json(staged["data_json"])
                if staged_patch.model_dump(mode="json") != patch.model_dump(mode="json"):
                    raise ValueError(f"第 {chapter_no} 章待提升补丁与临时记忆不一致")
            for fact in patch.facts:
                existing_identity = connection.execute(
                    "SELECT subject, predicate FROM facts WHERE fact_id=?",
                    (fact.fact_id,),
                ).fetchone()
                if existing_identity and (
                    existing_identity["subject"] != fact.subject
                    or existing_identity["predicate"] != fact.predicate
                ):
                    raise ValueError(f"fact_id {fact.fact_id} 已属于其他主体关系，不能覆盖")
                connection.execute(
                    """
                    UPDATE facts SET valid_to_chapter=?, status='superseded'
                    WHERE subject=? AND predicate=? AND status='active' AND valid_to_chapter IS NULL
                    """,
                    (chapter_no - 1, fact.subject, fact.predicate),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO facts(
                        fact_id, subject, predicate, value_json, valid_from_chapter, valid_to_chapter,
                        source_chapter, confidence, evidence, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'active', ?)
                    """,
                    (
                        fact.fact_id,
                        fact.subject,
                        fact.predicate,
                        json_dumps(fact.value, indent=None),
                        fact.valid_from_chapter,
                        chapter_no,
                        fact.confidence,
                        fact.evidence,
                        now,
                    ),
                )
            for thread in patch.threads:
                connection.execute(
                    """
                    INSERT INTO plot_threads(
                        thread_id, kind, title, status, description, planted_chapter,
                        due_chapter, last_advanced_chapter, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        kind=excluded.kind,
                        title=excluded.title,
                        status=excluded.status,
                        description=excluded.description,
                        planted_chapter=COALESCE(plot_threads.planted_chapter, excluded.planted_chapter),
                        due_chapter=excluded.due_chapter,
                        last_advanced_chapter=excluded.last_advanced_chapter,
                        updated_at=excluded.updated_at
                    """,
                    (
                        thread.thread_id,
                        thread.kind,
                        thread.title,
                        thread.status,
                        thread.description,
                        thread.planted_chapter,
                        thread.due_chapter,
                        chapter_no,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE chapters SET title=?, status='accepted', path=?, content_hash=?, content_text=?, summary=?,
                    updated_at=?, accepted_at=? WHERE chapter_no=?
                """,
                (title, final_path, content_hash(content), content, patch.chapter_summary, now, now, chapter_no),
            )
            connection.execute(
                "INSERT OR REPLACE INTO memory_patches(chapter_no, data_json, committed_at) VALUES (?, ?, ?)",
                (chapter_no, patch.model_dump_json(), now),
            )
            if provisional_batch_id:
                connection.execute(
                    """
                    UPDATE provisional_memory_patches
                    SET status='promoted', invalidation_reason=NULL, updated_at=?, promoted_at=?
                    WHERE batch_id=? AND chapter_no=?
                    """,
                    (now, now, provisional_batch_id, chapter_no),
                )
            connection.commit()

    def canonical_chapter_content(self, chapter_no: int) -> str | None:
        self._require_canonical_content_schema()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT content_text FROM chapters WHERE chapter_no=? AND status='accepted'",
                (chapter_no,),
            ).fetchone()
        return str(row["content_text"]) if row and row["content_text"] is not None else None

    def backfill_canonical_chapter_content(self, chapter_no: int, content: str) -> bool:
        """Upgrade an old accepted row only when its stored hash proves the projection is authentic."""

        self._require_canonical_content_schema()

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapters SET content_text=?
                WHERE chapter_no=? AND status='accepted' AND content_text IS NULL AND content_hash=?
                """,
                (content, chapter_no, content_hash(content)),
            )
            connection.commit()
        return bool(cursor.rowcount)

    def append_collaboration_message(
        self,
        *,
        thread_id: str,
        run_id: str,
        sender_role: str,
        recipient_role: str,
        message_type: str,
        claim: str,
        evidence_refs: list[str] | None = None,
        requested_response: str = "",
        chapter_no: int | None = None,
        chapter_version: int | None = None,
        context_packet_id: str = "",
        status: str = "pending",
        expires_at: str | None = None,
        response_to: str | None = None,
    ) -> dict[str, Any]:
        roles = {"coordinator", "writer", "reviewer", "memory_keeper", "user"}
        message_types = {"task_assignment", "fact_query", "handoff", "review_issue", "revision_request", "objection", "risk", "memory_sync", "answer"}
        statuses = {"pending", "responded", "resolved", "escalated", "expired"}
        if sender_role not in roles or recipient_role not in roles:
            raise ValueError("协作消息角色不受支持")
        if message_type not in message_types or status not in statuses:
            raise ValueError("协作消息类型或状态不受支持")
        message_id = f"message-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collaboration_messages(
                    message_id, thread_id, run_id, sender_role, recipient_role, message_type,
                    chapter_no, chapter_version, context_packet_id, claim, evidence_refs_json,
                    requested_response, status, expires_at, response_to, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, thread_id, run_id, sender_role, recipient_role, message_type,
                    chapter_no, chapter_version, context_packet_id, claim,
                    json_dumps(sorted(set(evidence_refs or [])), indent=None), requested_response,
                    status, expires_at, response_to, now,
                ),
            )
            connection.commit()
        return {
            "message_id": message_id, "thread_id": thread_id, "run_id": run_id,
            "sender_role": sender_role, "recipient_role": recipient_role,
            "message_type": message_type, "chapter_no": chapter_no,
            "chapter_version": chapter_version, "context_packet_id": context_packet_id,
            "claim": claim, "evidence_refs": sorted(set(evidence_refs or [])),
            "requested_response": requested_response, "status": status,
            "expires_at": expires_at, "response_to": response_to, "created_at": now,
        }

    def list_collaboration_messages(
        self,
        *,
        chapter_no: int | None = None,
        recipient_role: str | None = None,
        active_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if chapter_no is not None:
            clauses.append("(chapter_no IS NULL OR chapter_no=?)")
            params.append(chapter_no)
        if recipient_role:
            clauses.append("recipient_role=?")
            params.append(recipient_role)
        if active_only:
            clauses.append("status IN ('pending','responded','escalated')")
            clauses.append("(expires_at IS NULL OR expires_at>?)")
            params.append(utc_now())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collaboration_messages" + where + " ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["evidence_refs"] = json.loads(item.pop("evidence_refs_json"))
            result.append(item)
        return result

    def resolve_pending_collaboration(
        self,
        *,
        chapter_no: int,
        recipient_role: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE collaboration_messages SET status='resolved', resolved_at=?
                WHERE chapter_no=? AND recipient_role=? AND status IN ('pending','responded')
                """,
                (utc_now(), chapter_no, recipient_role),
            )
            connection.commit()
        return int(cursor.rowcount)

    def resolve_collaboration_thread(self, thread_id: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE collaboration_messages SET status='resolved', resolved_at=?
                WHERE thread_id=? AND status IN ('pending','responded')
                """,
                (utc_now(), thread_id),
            )
            connection.commit()
        return int(cursor.rowcount)

    def upsert_preference(
        self,
        *,
        text: str,
        strength: str = "weak",
        scope: str = "project",
        source: str = "user",
        preference_id: str | None = None,
    ) -> dict[str, Any]:
        if strength not in {"hard", "weak"}:
            raise ValueError("偏好强度只能是 hard 或 weak")
        clean = text.strip()
        if not clean:
            raise ValueError("偏好内容不能为空")
        item_id = preference_id or f"preference-{content_hash(scope + clean)[:16]}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences(preference_id, strength, scope, text, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(preference_id) DO UPDATE SET strength=excluded.strength, scope=excluded.scope,
                    text=excluded.text, source=excluded.source, status='active', updated_at=excluded.updated_at
                """,
                (item_id, strength, scope, clean, source, now, now),
            )
            connection.commit()
        return {"preference_id": item_id, "strength": strength, "scope": scope, "text": clean, "source": source, "status": "active", "updated_at": now}

    def list_preferences(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        where = " WHERE status='active'" if active_only else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM user_preferences" + where + " ORDER BY CASE strength WHEN 'hard' THEN 0 ELSE 1 END, updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_learning_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        chapter_no: int | None = None,
        chapter_version: int | None = None,
    ) -> str:
        if event_type not in {"accepted", "rejected", "revised", "rolled_back", "preference_changed"}:
            raise ValueError("学习事件类型不受支持")
        event_id = f"learning-{uuid.uuid4().hex}"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO learning_events(event_id,event_type,chapter_no,chapter_version,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (event_id, event_type, chapter_no, chapter_version, json_dumps(payload, indent=None), utc_now()),
            )
            connection.commit()
        return event_id

    def list_learning_events(self, limit: int = 30) -> list[dict[str, Any]]:
        """Expose only structured internal feedback, never model reasoning or prose."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id,event_type,chapter_no,chapter_version,payload_json,created_at
                FROM learning_events ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def learning_guidance(self) -> dict[str, Any]:
        """Derive small, explainable local signals; this is not model training."""

        events = self.list_learning_events(80)
        counts = Counter(str(item["event_type"]) for item in events)
        rule_counts: Counter[str] = Counter()
        for item in events:
            if item["event_type"] != "rejected":
                continue
            for rule in item.get("payload", {}).get("finding_rules", []):
                if isinstance(rule, str) and rule:
                    rule_counts[rule] += 1
        return {
            "window_events": len(events),
            "accepted": counts["accepted"],
            "rejected": counts["rejected"],
            "revised": counts["revised"],
            "priority_review_rules": [rule for rule, _ in rule_counts.most_common(5)],
            "notice": "来自本地接受、拒绝和修订记录；不包含模型思维链，也不会上传正文。",
        }

    def record_retrieval_feedback(
        self,
        *,
        query: str,
        source_id: str,
        outcome: str,
        role: str,
        chapter_no: int | None = None,
        packet_id: str | None = None,
    ) -> str:
        if outcome not in {"cited", "ignored", "misleading"}:
            raise ValueError("检索反馈只能是 cited、ignored 或 misleading")
        feedback_id = f"retrieval-{uuid.uuid4().hex}"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO retrieval_feedback(feedback_id,query_hash,source_id,outcome,role,chapter_no,packet_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (feedback_id, content_hash(query), source_id, outcome, role, chapter_no, packet_id, utc_now()),
            )
            connection.commit()
        return feedback_id

    def retrieval_feedback_scores(self) -> dict[str, float]:
        weights = {"cited": 0.15, "ignored": -0.02, "misleading": -0.5}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT source_id, outcome, COUNT(*) AS count FROM retrieval_feedback GROUP BY source_id, outcome"
            ).fetchall()
        scores: dict[str, float] = {}
        for row in rows:
            scores[str(row["source_id"])] = scores.get(str(row["source_id"]), 0.0) + weights[str(row["outcome"])] * int(row["count"])
        return scores

    def project_status(self) -> dict[str, Any]:
        with self.connect() as connection:
            chapter_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM chapters GROUP BY status"
                ).fetchall()
            }
            fact_count = connection.execute(
                "SELECT COUNT(*) AS count FROM facts WHERE status='active'"
            ).fetchone()["count"]
            open_thread_count = connection.execute(
                "SELECT COUNT(*) AS count FROM plot_threads WHERE status IN ('open','advanced','delayed')"
            ).fetchone()["count"]
            plan_count = connection.execute(
                "SELECT COUNT(*) AS count FROM plans WHERE status='active'"
            ).fetchone()["count"]
            provisional_memory_count = connection.execute(
                "SELECT COUNT(*) AS count FROM provisional_memory_patches WHERE status='active'"
            ).fetchone()["count"]
        return {
            "chapters": chapter_counts,
            "active_facts": fact_count,
            "open_threads": open_thread_count,
            "plan_records": plan_count,
            "provisional_memory_patches": provisional_memory_count,
        }
