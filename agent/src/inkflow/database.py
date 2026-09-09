from __future__ import annotations

import json
import sqlite3
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
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT version FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
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
            row = connection.execute("SELECT * FROM chapters WHERE chapter_no=?", (chapter_no,)).fetchone()
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
                SELECT * FROM chapters
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
                "SELECT * FROM chapters WHERE status='accepted' ORDER BY chapter_no"
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

    def accept_chapter(self, chapter_no: int, title: str, final_path: str, content: str, patch: MemoryPatch) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT version FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            if not current:
                raise ValueError(f"第 {chapter_no} 章没有草稿记录")
            for fact in patch.facts:
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
                UPDATE chapters SET title=?, status='accepted', path=?, content_hash=?, summary=?,
                    updated_at=?, accepted_at=? WHERE chapter_no=?
                """,
                (title, final_path, content_hash(content), patch.chapter_summary, now, now, chapter_no),
            )
            connection.execute(
                "INSERT OR REPLACE INTO memory_patches(chapter_no, data_json, committed_at) VALUES (?, ?, ?)",
                (chapter_no, patch.model_dump_json(), now),
            )
            connection.commit()

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
        return {
            "chapters": chapter_counts,
            "active_facts": fact_count,
            "open_threads": open_thread_count,
            "plan_records": plan_count,
        }
