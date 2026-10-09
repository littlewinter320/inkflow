from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import ValidationGateError
from .role_protocol import ROLE_PROTOCOL_VERSION, adapt_role, normalize_role
from .schemas import BookBrief, MemoryPatch, MemorySummaryRebuild, PlanBundle, ReviewReport
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
    created_at TEXT NOT NULL,
    role_protocol_version INTEGER NOT NULL DEFAULT 2,
    review_role TEXT NOT NULL DEFAULT 'reviewer'
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
    epistemic_kind TEXT NOT NULL DEFAULT 'objective',
    event_time TEXT,
    narrative_time TEXT,
    source_version INTEGER,
    source_hash TEXT,
    branch_id TEXT NOT NULL DEFAULT 'main',
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
    source_version INTEGER,
    source_hash TEXT,
    branch_id TEXT NOT NULL DEFAULT 'main',
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
    role_protocol_version INTEGER NOT NULL DEFAULT 2,
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

CREATE TABLE IF NOT EXISTS collaboration_threads (
    thread_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    chapter_no INTEGER,
    chapter_version INTEGER,
    context_packet_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    current_round INTEGER NOT NULL DEFAULT 1,
    max_rounds INTEGER NOT NULL DEFAULT 2,
    resolution TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    run_id TEXT NOT NULL,
    chapter_no INTEGER,
    chapter_version INTEGER,
    role TEXT NOT NULL,
    role_protocol_version INTEGER NOT NULL DEFAULT 2,
    dimension TEXT,
    status TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_chapter
ON agent_artifacts(chapter_no, chapter_version, artifact_type, status);

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

CREATE TABLE IF NOT EXISTS computer_action_requests (
    request_id TEXT PRIMARY KEY,
    action_kind TEXT NOT NULL,
    command TEXT NOT NULL,
    command_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    completed_at TEXT,
    exit_code INTEGER,
    error TEXT NOT NULL DEFAULT ''
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

CREATE TABLE IF NOT EXISTS retrieval_embeddings (
    source_id TEXT NOT NULL,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_id, model)
);

CREATE TABLE IF NOT EXISTS learning_strategies (
    strategy_key TEXT PRIMARY KEY,
    trials INTEGER NOT NULL DEFAULT 0,
    reward REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preference_pairs (
    pair_id TEXT PRIMARY KEY,
    chosen_artifact_id TEXT NOT NULL,
    rejected_artifact_id TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _stored_role(role: str, protocol_version: int | None = None) -> str:
    return role if role in {"engine", "user"} else normalize_role(role)


def _adapt_stored_record(record: dict[str, Any]) -> dict[str, Any]:
    """Consume an old storage envelope once; the returned view is canonical."""
    item = dict(record)
    marker = item.pop("role_protocol_version", ROLE_PROTOCOL_VERSION)
    legacy = type(marker) is int and marker == 1
    for key in ("role", "sender_role", "recipient_role", "review_role"):
        if key in item and item[key] not in {"engine", "user"}:
            item[key] = adapt_role(item[key], legacy=legacy)
    return item


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
            # Existing rows belong to the legacy protocol. Additive defaults keep
            # their original role names while making later records unambiguous.
            role_columns = {
                "reviews": {
                    "role_protocol_version": "INTEGER NOT NULL DEFAULT 1",
                    "review_role": "TEXT NOT NULL DEFAULT 'reviewer'",
                },
                "collaboration_messages": {
                    "role_protocol_version": "INTEGER NOT NULL DEFAULT 1",
                },
                "agent_artifacts": {
                    "role_protocol_version": "INTEGER NOT NULL DEFAULT 1",
                },
                "facts": {
                    "epistemic_kind": "TEXT NOT NULL DEFAULT 'objective'",
                    "event_time": "TEXT",
                    "narrative_time": "TEXT",
                    "source_version": "INTEGER",
                    "source_hash": "TEXT",
                    "branch_id": "TEXT NOT NULL DEFAULT 'main'",
                },
                "plot_threads": {
                    "source_version": "INTEGER",
                    "source_hash": "TEXT",
                    "branch_id": "TEXT NOT NULL DEFAULT 'main'",
                },
            }
            for table, definitions in role_columns.items():
                existing = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, definition in definitions.items():
                    if column not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            chapter_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(chapters)")}
            if "content_text" in chapter_columns:
                # Older accepted facts had no source version. Pin only those
                # whose evidence is still present in the accepted text.
                connection.execute(
                    """UPDATE facts SET source_version=(SELECT version FROM chapters WHERE chapter_no=source_chapter),
                       source_hash=(SELECT content_hash FROM chapters WHERE chapter_no=source_chapter)
                       WHERE source_version IS NULL AND source_hash IS NULL AND EXISTS (
                         SELECT 1 FROM chapters WHERE chapter_no=source_chapter AND status='accepted'
                           AND content_text IS NOT NULL AND instr(content_text,facts.evidence)>0)"""
                )
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

    @staticmethod
    def _canonical_content_sources(connection: sqlite3.Connection) -> dict[str, Any]:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(chapters)")}
        missing = "content_text IS NULL" if "content_text" in columns else "1"
        rows = connection.execute(
            f"SELECT chapter_no,version,content_hash,path,{missing} AS canonical_missing "
            "FROM chapters WHERE status='accepted' ORDER BY chapter_no"
        ).fetchall()
        return {"schema_required": "content_text" not in columns, "sources": [dict(row) for row in rows]}

    def canonical_content_sources(self) -> dict[str, Any]:
        """Read the exact sources needing an explicitly confirmed text recovery."""
        with self.connect() as connection:
            return self._canonical_content_sources(connection)

    def migrate_canonical_content(
        self, *, expected_sources: dict[str, Any], verified_contents: dict[int, str], audit: dict[str, Any]
    ) -> list[int]:
        """Add the column and backfill verified text in one confirmed transaction."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._canonical_content_sources(connection) != expected_sources:
                raise ValueError("正史来源在迁移确认后变化，请重新查看影响并确认；数据库未升级。")
            sources = {int(row["chapter_no"]): row for row in expected_sources["sources"]}
            for chapter_no, text in verified_contents.items():
                source = sources.get(chapter_no)
                if not source or not source["canonical_missing"] or content_hash(text) != source["content_hash"]:
                    raise ValueError(f"第 {chapter_no} 章回填正文未通过原版本哈希核验。")
            if expected_sources["schema_required"]:
                connection.execute("ALTER TABLE chapters ADD COLUMN content_text TEXT")
            backfilled: list[int] = []
            for chapter_no, text in verified_contents.items():
                source = sources[chapter_no]
                updated = connection.execute(
                    "UPDATE chapters SET content_text=? WHERE chapter_no=? AND status='accepted' "
                    "AND version=? AND content_hash=? AND path=? AND content_text IS NULL",
                    (text, chapter_no, source["version"], source["content_hash"], source["path"]),
                )
                if updated.rowcount != 1:
                    raise ValueError(f"第 {chapter_no} 章回填目标变化；本次迁移已回滚。")
                backfilled.append(chapter_no)
            connection.execute(
                "INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                ("canon.content_migration", json_dumps({**audit, "backfilled_chapters": backfilled}, indent=None), utc_now()),
            )
            connection.commit()
        return backfilled

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

    @staticmethod
    def _summary_cache_matches(cache: Any, source: Any) -> bool:
        """Locate identity and quotes; semantic adequacy belongs to the memory owner."""
        return bool(isinstance(cache, dict) and source and source["status"] == "accepted"
            and type(cache.get("chapter_no")) is int and cache["chapter_no"] == source["chapter_no"]
            and type(cache.get("source_version")) is int and cache["source_version"] == source["version"]
            and cache.get("source_hash") == source["content_hash"]
            and isinstance(source["content_text"], str)
            and content_hash(source["content_text"]) == source["content_hash"]
            and isinstance(cache.get("chapter_summary"), str) and cache["chapter_summary"].strip()
            and isinstance(cache.get("evidence_quotes"), list) and cache["evidence_quotes"]
            and all(isinstance(quote, str) and quote.strip() and quote in source["content_text"]
                    for quote in cache["evidence_quotes"]))

    def save_rebuilt_chapter_summary(self, proposal: dict[str, Any] | MemorySummaryRebuild, *, actor: str,
                                     task_id: str, run_id: str) -> dict[str, Any]:
        """Save an independently derived cache; never alter accepted prose or its memory patch."""
        self._require_canonical_content_schema()
        proposal = MemorySummaryRebuild.model_validate(proposal).model_dump(mode="json")
        if (actor not in {"editor", "reviewer", "memory_keeper"}
                or not isinstance(task_id, str) or not task_id.strip()
                or not isinstance(run_id, str) or not run_id.strip()):
            raise ValueError("摘要重建须绑定有效源章、记忆责任角色和原任务运行。")
        cache = {key: proposal.get(key) for key in
                 ("chapter_no", "source_version", "source_hash", "chapter_summary", "evidence_quotes")}
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                "SELECT chapter_no,status,version,content_hash,content_text FROM chapters WHERE chapter_no=?",
                (cache["chapter_no"],)).fetchone()
            if not self._summary_cache_matches(cache, source):
                raise ValueError("摘要重建来源版本、正文哈希或原句已失效；缓存未保存，正史未改动。")
            cache = {**cache, "actor": actor, "task_id": task_id, "run_id": run_id,
                     "authority": "derived_summary", "created_at": utc_now()}
            connection.execute(
                "INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                (f"memory_summary_rebuild:{cache['chapter_no']}", json_dumps(cache, indent=None), cache["created_at"]))
            connection.commit()
        return cache

    def rebuilt_chapter_summary(self, chapter_no: int) -> dict[str, Any] | None:
        """Read only caches still supported by the current accepted source version."""
        if self.canonical_content_migration_required():
            return None
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM metadata WHERE key=?",
                                     (f"memory_summary_rebuild:{chapter_no}",)).fetchone()
            if row is None:
                return None
            try:
                cache = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError):
                return None
            source = connection.execute(
                "SELECT chapter_no,status,version,content_hash,content_text FROM chapters WHERE chapter_no=?",
                (chapter_no,)).fetchone()
            if (not self._summary_cache_matches(cache, source)
                    or cache.get("authority") != "derived_summary"
                    or cache.get("actor") not in {"editor", "reviewer", "memory_keeper"}
                    or not cache.get("task_id") or not cache.get("run_id")):
                return None
        return cache

    def set_brief(self, brief: BookBrief) -> None:
        self.set_metadata("book_brief", brief.model_dump(mode="json"))

    def get_brief(self) -> BookBrief:
        value = self.get_metadata("book_brief")
        if not value:
            raise ValueError("项目缺少 book_brief")
        return BookBrief.model_validate(value)

    def planning_source_hashes(self) -> dict[str, str | None]:
        root = self.path.parent.parent
        result: dict[str, str | None] = {}
        for name in ("BOOK.md", "OUTLINE.md", "STORY_DETAIL.md"):
            path = root / name
            result[name] = content_hash(path.read_text(encoding="utf-8")) if path.is_file() else None
        return result

    def planning_source_impact(self) -> dict[str, Any]:
        """Report stale future material without changing accepted chapters."""
        current = self.planning_source_hashes()
        plan_basis = self.get_metadata("current_plan_source_hashes", {})
        detail_basis = self.get_metadata("story_detail_outline_hash")
        accepted = self.accepted_chapters()
        affected = []
        if detail_basis and current["OUTLINE.md"] != detail_basis:
            affected.append("剧情细纲需按新版大纲核对")
        if plan_basis:
            changed = [name for name in current if current[name] != plan_basis.get(name)]
            if changed:
                affected.append("近期计划及未写章节卡需按新版依据核对")
        else:
            changed = []
        plan_path = self.path.parent.parent / "PLAN.md"
        plan_text_hash = self.get_metadata("current_plan_text_hash")
        plan_file_changed = bool(plan_text_hash and plan_path.is_file()
                                 and content_hash(plan_path.read_text(encoding="utf-8")) != plan_text_hash)
        if plan_file_changed:
            affected.append("近期计划文本与结构化章节卡不同步，请决定是否按文本更新章节卡")
        return {
            "changed_sources": changed,
            "affected": affected,
            "accepted_chapters_preserved": len(accepted),
            "plan_file_changed": plan_file_changed,
            "next_step": "只核对受影响的细纲和未来安排；已接受正文不自动改写。" if affected else "依据与当前计划一致。",
        }

    def save_plan_bundle(self, bundle: PlanBundle, *, supersede_after_chapter: int | None = None) -> None:
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
            previous_cards = {
                int(row["plan_key"].split(":", 1)[1]): json.loads(row["data_json"])
                for row in connection.execute("SELECT plan_key,data_json FROM plans WHERE kind='chapter'")
            } if supersede_after_chapter is not None else {}
            if supersede_after_chapter is not None:
                # A reviewed v2 publication replaces every unaccepted legacy
                # execution card. Accepted chapters keep their source cards.
                connection.execute(
                    "DELETE FROM plans WHERE kind='chapter' AND CAST(SUBSTR(plan_key, 9) AS INTEGER)>? "
                    "AND NOT EXISTS (SELECT 1 FROM chapters WHERE status='accepted' "
                    "AND chapter_no=CAST(SUBSTR(plans.plan_key,9) AS INTEGER))",
                    (supersede_after_chapter,),
                )
                connection.execute(
                    "DELETE FROM plans WHERE kind IN ('arc','supplement') "
                    "AND NOT EXISTS (SELECT 1 FROM plans child WHERE child.kind='chapter' "
                    "AND child.parent_key=plans.plan_key)"
                )
                connection.execute(
                    "DELETE FROM plans WHERE kind='volume' AND NOT EXISTS ("
                    "SELECT 1 FROM plans arc WHERE arc.kind='arc' "
                    "AND arc.parent_key=plans.plan_key)"
                )
            for kind, key, parent, data in rows:
                if supersede_after_chapter is not None and kind == "chapter":
                    number = int(key.split(":", 1)[1])
                    if number in previous_cards and connection.execute(
                            "SELECT 1 FROM chapters WHERE chapter_no=? AND status='accepted'", (number,)).fetchone():
                        continue
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
            if supersede_after_chapter is not None:
                for card in bundle.current_arc.chapter_cards:
                    if previous_cards.get(card.chapter_no) == card.model_dump(mode="json"):
                        continue
                    draft = connection.execute(
                        "SELECT version,content_hash FROM chapters WHERE chapter_no=? AND status='draft'",
                        (card.chapter_no,),
                    ).fetchone()
                    if draft:
                        marker = {"revision_id": bundle.current_arc.arc_id, "status": "needs_writer",
                                  "draft_version": draft["version"], "draft_hash": draft["content_hash"],
                                  "card_hash": content_hash(json_dumps(card.model_dump(mode="json"))),
                                  "instruction": "生效规划已更新，须按新版章节卡定向修订"}
                        connection.execute(
                            "INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                            (f"pending_plan_revision:{card.chapter_no}", json_dumps(marker), now),
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
            connection.execute(
                """INSERT INTO metadata(key, value_json, updated_at) VALUES ('current_plan_source_hashes', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (json_dumps(self.planning_source_hashes(), indent=None), now),
            )
            connection.commit()

    def replace_unaccepted_plan_window(
        self, bundle: PlanBundle, *, revision_id: str, accepted_boundary: int,
        old_cards: dict[int, dict[str, Any] | None], drafts: dict[int, dict[str, Any]],
        instruction: str, source_fingerprint: str,
    ) -> dict[str, Any]:
        """Commit only authorized execution cards; preserve canon and revision history."""
        arc = bundle.current_arc
        numbers = list(range(arc.chapter_start, arc.chapter_end + 1))
        if arc.chapter_start != accepted_boundary + 1 or set(old_cards) != set(numbers):
            raise ValidationGateError("近期重规划只能覆盖紧邻正史且明确授权的连续范围。")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            boundary = connection.execute("SELECT COALESCE(MAX(chapter_no),0) FROM chapters WHERE status='accepted'").fetchone()[0]
            if boundary != accepted_boundary:
                raise ValidationGateError("重规划期间正史边界已变化，候选未采用。")
            for number in numbers:
                row = connection.execute("SELECT data_json FROM plans WHERE kind='chapter' AND plan_key=? AND status='active'", (f"chapter:{number:05d}",)).fetchone()
                if (json.loads(row[0]) if row else None) != old_cards[number]:
                    raise ValidationGateError("重规划期间章节卡已变化，候选未采用。")
                chapter = connection.execute("SELECT status,version,content_hash FROM chapters WHERE chapter_no=?", (number,)).fetchone()
                expected = drafts.get(number)
                if chapter and (chapter["status"] != "draft" or expected is None
                                or chapter["version"] != expected["version"] or chapter["content_hash"] != expected["content_hash"]):
                    raise ValidationGateError("重规划期间正文版本已变化，候选未采用。")
                if expected is not None and chapter is None:
                    raise ValidationGateError("重规划期间原草稿已不存在，候选未采用。")
            record = {"revision_id": revision_id, "chapter_range": [arc.chapter_start, arc.chapter_end],
                      "instruction": instruction, "source_fingerprint": source_fingerprint,
                      "old_cards": old_cards, "old_drafts": drafts, "created_at": now}
            connection.execute("INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?)",
                               (f"plan_revision:{revision_id}", json_dumps(record), now))
            connection.execute("INSERT INTO metadata(key,value_json,updated_at) VALUES ('latest_pending_plan_revision',?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                               (json_dumps({"revision_id": revision_id, "chapter_range": [arc.chapter_start, arc.chapter_end], "instruction": instruction}), now))
            connection.execute("INSERT INTO plans(kind,plan_key,parent_key,data_json,updated_at) VALUES ('supplement',?,?,?,?)",
                               (revision_id, f"volume:{arc.volume_no:03d}", bundle.model_dump_json(), now))
            for card in arc.chapter_cards:
                connection.execute(
                    """INSERT INTO plans(kind,plan_key,parent_key,data_json,updated_at) VALUES ('chapter',?,?,?,?)
                       ON CONFLICT(kind,plan_key) DO UPDATE SET parent_key=excluded.parent_key,version=plans.version+1,
                       status='active',data_json=excluded.data_json,updated_at=excluded.updated_at""",
                    (f"chapter:{card.chapter_no:05d}", revision_id, card.model_dump_json(), now),
                )
                if card.chapter_no in drafts:
                    marker = {"revision_id": revision_id, "status": "needs_writer",
                              "draft_version": drafts[card.chapter_no]["version"],
                              "draft_hash": drafts[card.chapter_no]["content_hash"],
                              "card_hash": content_hash(json_dumps(card.model_dump(mode="json"))),
                              "instruction": instruction}
                    connection.execute("INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                                       (f"pending_plan_revision:{card.chapter_no}", json_dumps(marker), now))
            # Any staged suffix can depend on changed cards. Retain it as
            # history, but it must not bypass a fresh review/acceptance.
            connection.execute("UPDATE provisional_memory_patches SET status='invalidated',invalidation_reason=?,updated_at=? WHERE chapter_no>=? AND status='active'",
                               (f"近期计划更新：{revision_id}", now, arc.chapter_start))
            connection.commit()
        return record

    def pending_plan_revision(self, chapter_no: int) -> dict[str, Any] | None:
        value = self.get_metadata(f"pending_plan_revision:{chapter_no}")
        return value if isinstance(value, dict) and value.get("status") == "needs_writer" else None

    def complete_plan_revision_write(self, chapter_no: int, revision_id: str, version: int) -> None:
        marker = self.pending_plan_revision(chapter_no)
        chapter = self.get_chapter(chapter_no)
        card = self.get_chapter_card(chapter_no)
        if (not marker or marker["revision_id"] != revision_id or not chapter or chapter["status"] != "draft"
                or chapter["version"] != version or version <= marker["draft_version"]
                or content_hash(json_dumps(card)) != marker["card_hash"]):
            raise ValidationGateError("计划修订交接与当前草稿或章节卡不一致，未复用旧审查。")
        self.set_metadata(f"pending_plan_revision:{chapter_no}", {**marker, "status": "awaiting_review", "revised_version": version})

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

    def upsert_draft(self, chapter_no: int, title: str, path: str, content: str,
                     *, workflow_step: dict[str, Any] | None = None) -> int:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT version,status FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            if existing and existing["status"] == "accepted":
                raise ValueError("已接受正史不能直接覆盖为草稿；请先建立可恢复的分支修订。")
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
            if workflow_step:
                receipt = {**workflow_step, "chapter_no": chapter_no, "version": version,
                           "content_hash": content_hash(content), "path": path}
                connection.execute(
                    "INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (f"workflow_writer:{workflow_step['task_id']}:{chapter_no}:{workflow_step['step']}",
                     json.dumps(receipt, ensure_ascii=False), utc_now()))
            connection.commit()
        return version

    def mark_draft_discarded(self, chapter_no: int, path: str, expected_hash: str) -> bool:
        """Keep the chapter row auditable without treating a trashed draft as current."""
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE chapters SET status='discarded', updated_at=?
                   WHERE chapter_no=? AND status='draft' AND path=? AND content_hash=?""",
                (utc_now(), chapter_no, path.replace("\\", "/"), expected_hash),
            )
            connection.commit()
            return cursor.rowcount == 1

    def get_chapter(self, chapter_no: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT chapter.chapter_no,chapter.title,chapter.status,chapter.version,
                          chapter.path,chapter.content_hash,
                          CASE WHEN chapter.status='accepted' AND chapter.version>1
                                     AND (patch.committed_at IS NULL OR patch.committed_at<>chapter.updated_at)
                               THEN NULL ELSE chapter.summary END AS summary,
                          CASE WHEN chapter.status='accepted' AND chapter.version>1
                                     AND (patch.committed_at IS NULL OR patch.committed_at<>chapter.updated_at)
                               THEN 1 ELSE 0 END AS summary_stale,
                          chapter.updated_at,chapter.accepted_at
                   FROM chapters AS chapter
                   LEFT JOIN memory_patches AS patch ON patch.chapter_no=chapter.chapter_no
                   WHERE chapter.chapter_no=?""",
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
                SELECT chapter.chapter_no,chapter.title,chapter.status,chapter.version,chapter.path,
                       chapter.content_hash,
                       CASE WHEN chapter.version>1 AND (patch.committed_at IS NULL OR patch.committed_at<>chapter.updated_at)
                            THEN NULL ELSE chapter.summary END AS summary,
                       CASE WHEN chapter.version>1 AND (patch.committed_at IS NULL OR patch.committed_at<>chapter.updated_at)
                            THEN 1 ELSE 0 END AS summary_stale,
                       chapter.updated_at,chapter.accepted_at
                FROM chapters AS chapter
                LEFT JOIN memory_patches AS patch ON patch.chapter_no=chapter.chapter_no
                WHERE chapter.status='accepted' AND chapter.chapter_no < ?
                ORDER BY chapter.chapter_no DESC LIMIT ?
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
                """SELECT chapter.chapter_no,chapter.title,chapter.status,chapter.version,chapter.path,
                          chapter.content_hash,
                          CASE WHEN chapter.version>1 AND (patch.committed_at IS NULL OR patch.committed_at<>chapter.updated_at)
                               THEN NULL ELSE chapter.summary END AS summary,
                          CASE WHEN chapter.version>1 AND (patch.committed_at IS NULL OR patch.committed_at<>chapter.updated_at)
                               THEN 1 ELSE 0 END AS summary_stale,
                          chapter.updated_at,chapter.accepted_at
                   FROM chapters AS chapter
                   LEFT JOIN memory_patches AS patch ON patch.chapter_no=chapter.chapter_no
                   WHERE chapter.status='accepted' ORDER BY chapter.chapter_no"""
            ).fetchall()
        return [dict(row) for row in rows]

    def save_review(
        self, chapter_no: int, chapter_version: int, report: ReviewReport, path: str,
        *, role_protocol_version: int | None = None, review_role: str = "editor",
    ) -> int:
        protocol_version = ROLE_PROTOCOL_VERSION
        _stored_role(review_role, protocol_version)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reviews(
                    chapter_no, chapter_version, verdict, data_json, path, created_at,
                    role_protocol_version, review_role
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chapter_no,
                    chapter_version,
                    report.verdict,
                    report.model_dump_json(),
                    path,
                    utc_now(),
                    protocol_version,
                    review_role,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def save_mode_review(
        self, chapter_no: int, chapter_version: int, report: ReviewReport, path: str,
        *, run_id: str, primary_role: str, bundle: dict[str, Any],
    ) -> tuple[int, str]:
        """Atomically bind a merged review to its check-coverage record."""
        if primary_role not in {"editor", "reviewer"}:
            raise ValueError("综合审查必须注明负责表达或逻辑的主要审查角色。")
        artifact_id = f"artifact-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT INTO reviews(chapter_no,chapter_version,verdict,data_json,path,created_at,
                   role_protocol_version,review_role) VALUES (?,?,?,?,?,?,2,?)""",
                (chapter_no, chapter_version, report.verdict, report.model_dump_json(), path, now, primary_role),
            )
            review_id = int(cursor.lastrowid)
            data = {**bundle, "review_id": review_id}
            connection.execute(
                """INSERT INTO agent_artifacts(artifact_id,artifact_type,run_id,chapter_no,chapter_version,
                   role,role_protocol_version,dimension,status,data_json,created_at)
                   VALUES (?,'mode_review_bundle',?,?,?,?,2,?,'verified',?,?)""",
                (artifact_id, run_id, chapter_no, chapter_version, primary_role, bundle["mode"],
                 json_dumps(data, indent=None), now),
            )
            connection.commit()
        return review_id, artifact_id

    def latest_review(self, chapter_no: int) -> ReviewReport | None:
        record = self.latest_review_record(chapter_no)
        return record["report"] if record else None

    def latest_review_record(self, chapter_no: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, chapter_version, data_json, path, role_protocol_version, review_role
                FROM reviews WHERE chapter_no=? ORDER BY id DESC LIMIT 1""",
                (chapter_no,),
            ).fetchone()
        if not row:
            return None
        stored = _adapt_stored_record(dict(row))
        mode_bundle = None
        for item in self.list_agent_artifacts(
            chapter_no=chapter_no, artifact_type="mode_review_bundle", limit=50,
        ):
            data = item["data"]
            if item["status"] == "verified" and data.get("review_id") == int(row["id"]):
                mode_bundle = data
                break
        # Readable legacy reports without coverage are reference material.
        # Only the Engine can establish the required checks for acceptance.
        return {
            "id": int(row["id"]), "chapter_version": int(row["chapter_version"]),
            "report": ReviewReport.model_validate_json(row["data_json"]),
            "path": str(row["path"]), "review_role": stored["review_role"],
            "canonical_role": stored["review_role"], "mode_bundle": mode_bundle,
        }

    def current_facts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            has_canonical_text = any(str(row["name"]) == "content_text"
                                     for row in connection.execute("PRAGMA table_info(chapters)"))
            evidence_filter = ("AND (source.content_text IS NULL OR instr(source.content_text,fact.evidence)>0)"
                               if has_canonical_text else "")
            newer_evidence_filter = (
                "AND (newer_source.content_text IS NULL OR instr(newer_source.content_text,newer.evidence)>0)"
                if has_canonical_text else ""
            )
            rows = connection.execute(
                f"""
                SELECT fact.*
                FROM facts AS fact
                JOIN chapters AS source ON source.chapter_no=fact.source_chapter
                WHERE fact.status='active' AND fact.valid_to_chapter IS NULL
                  AND fact.branch_id='main' AND source.status='accepted'
                  AND (fact.source_version IS NULL OR fact.source_version=source.version)
                  AND (fact.source_hash IS NULL OR fact.source_hash=source.content_hash)
                  {evidence_filter}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM facts AS newer
                      JOIN chapters AS newer_source ON newer_source.chapter_no=newer.source_chapter
                      WHERE newer.subject=fact.subject
                        AND newer.predicate=fact.predicate
                        AND newer.status='active'
                        AND newer.valid_to_chapter IS NULL
                        AND newer.branch_id=fact.branch_id
                        AND newer.source_chapter > fact.source_chapter
                        AND newer_source.status='accepted'
                        AND (newer.source_version IS NULL OR newer.source_version=newer_source.version)
                        AND (newer.source_hash IS NULL OR newer.source_hash=newer_source.content_hash)
                        {newer_evidence_filter}
                  )
                ORDER BY fact.subject, fact.predicate
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            result.append(item)
        from .memory_records import attach_evidence
        return attach_evidence(self, result)

    def facts_as_of(self, chapter_no: int) -> list[dict[str, Any]]:
        """Facts available after an accepted chapter, including later-superseded facts.

        A later chapter must not erase the knowledge available to an earlier
        chapter's review or revision. A rewritten source chapter, however,
        invalidates facts from its old version even for historical queries.
        """
        if chapter_no < 1:
            return []
        with self.connect() as connection:
            has_canonical_text = any(str(row["name"]) == "content_text"
                                     for row in connection.execute("PRAGMA table_info(chapters)"))
            evidence_filter = ("AND (source.content_text IS NULL OR instr(source.content_text,fact.evidence)>0)"
                               if has_canonical_text else "")
            newer_evidence_filter = (
                "AND (newer_source.content_text IS NULL OR instr(newer_source.content_text,newer.evidence)>0)"
                if has_canonical_text else ""
            )
            rows = connection.execute(
                f"""
                SELECT fact.* FROM facts AS fact
                JOIN chapters AS source ON source.chapter_no=fact.source_chapter
                WHERE fact.status IN ('active','superseded')
                  AND fact.branch_id='main'
                  AND fact.source_chapter<=? AND fact.valid_from_chapter<=?
                  AND (fact.valid_to_chapter IS NULL OR fact.valid_to_chapter>=?)
                  AND source.status='accepted'
                  AND (fact.source_version IS NULL OR fact.source_version=source.version)
                  AND (fact.source_hash IS NULL OR fact.source_hash=source.content_hash)
                  {evidence_filter}
                  AND NOT EXISTS (
                      SELECT 1 FROM facts AS newer
                      JOIN chapters AS newer_source ON newer_source.chapter_no=newer.source_chapter
                      WHERE newer.subject=fact.subject AND newer.predicate=fact.predicate
                        AND newer.branch_id=fact.branch_id
                        AND newer.status IN ('active','superseded')
                        AND newer.source_chapter>fact.source_chapter
                        AND newer.source_chapter<=? AND newer.valid_from_chapter<=?
                        AND (newer.valid_to_chapter IS NULL OR newer.valid_to_chapter>=?)
                        AND newer_source.status='accepted'
                        AND (newer.source_version IS NULL OR newer.source_version=newer_source.version)
                        AND (newer.source_hash IS NULL OR newer.source_hash=newer_source.content_hash)
                        {newer_evidence_filter}
                  )
                ORDER BY fact.subject,fact.predicate
                """,
                (chapter_no,) * 6,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            result.append(item)
        from .memory_records import attach_evidence
        return attach_evidence(self, result, boundary=chapter_no)

    def open_threads(self) -> list[dict[str, Any]]:
        boundary = max((int(item["chapter_no"]) for item in self.accepted_chapters()), default=0)
        return self.threads_as_of(boundary)

    def threads_as_of(self, chapter_no: int) -> list[dict[str, Any]]:
        return self.threads_state_as_of(chapter_no)["threads"]

    def threads_state_as_of(self, chapter_no: int) -> dict[str, Any]:
        """Replay accepted memory patches to recover threads at a chapter boundary.

        plot_threads is only the latest projection: a mystery paid off in a
        later chapter was still open when an earlier chapter was written.
        """
        result: dict[str, Any] = {"threads": [], "coverage_gaps": [], "recovery_sources": []}
        if chapter_no < 1:
            return result
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT patch.chapter_no,patch.data_json,patch.committed_at,
                          chapter.version,chapter.content_hash,chapter.updated_at
                   FROM memory_patches AS patch
                   JOIN chapters AS chapter ON chapter.chapter_no=patch.chapter_no
                   WHERE patch.chapter_no<=? AND chapter.status='accepted'
                   ORDER BY patch.chapter_no""",
                (chapter_no,),
            ).fetchall()
            projections = connection.execute(
                "SELECT * FROM plot_threads WHERE branch_id='main' ORDER BY thread_id"
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        tainted: set[str] = set()
        for row in rows:
            try:
                patch = MemoryPatch.model_validate_json(row["data_json"])
            except ValueError:
                result["coverage_gaps"].append({"kind": "thread_history", "chapter_no": int(row["chapter_no"]),
                    "boundary_chapter": chapter_no, "state": "unknown", "owner": "memory_owner",
                    "reason": "已接受记忆补丁无法解析，不能据此确认本章的线索状态。"})
                # Isolate known affected IDs without interpreting damaged events.
                try:
                    raw_threads = json.loads(row["data_json"]).get("threads", [])
                    for raw in raw_threads:
                        if isinstance(raw, dict) and isinstance(raw.get("thread_id"), str):
                            latest.pop(raw["thread_id"], None)
                            tainted.add(raw["thread_id"])
                except (ValueError, TypeError, AttributeError):
                    pass
                continue
            if patch.chapter_no != int(row["chapter_no"]):
                for thread in patch.threads:
                    latest.pop(thread.thread_id, None)
                    tainted.add(thread.thread_id)
                result["coverage_gaps"].append({"kind": "thread_history", "chapter_no": int(row["chapter_no"]),
                    "boundary_chapter": chapter_no, "state": "unknown", "owner": "memory_owner",
                    "reason": "记忆补丁章节身份不一致，相关线索状态已隔离。"})
                continue
            current_patch = (int(row["version"]) == 1 or row["committed_at"] == row["updated_at"])
            for thread in patch.threads:
                if not current_patch:
                    # A later repair changed the source chapter without
                    # rebasing this memory. Do not revive its earlier state.
                    latest.pop(thread.thread_id, None)
                    tainted.add(thread.thread_id)
                    continue
                tainted.discard(thread.thread_id)
                latest[thread.thread_id] = {
                    **thread.model_dump(mode="json"),
                    "last_advanced_chapter": int(row["chapter_no"]),
                    "source_version": int(row["version"]),
                    "source_hash": str(row["content_hash"]),
                    "branch_id": "main",
                    "updated_at": str(row["committed_at"]),
                }
        # Latest projections cannot prove a past lifecycle without committed
        # events. Supply bounded canon to the memory owner, never invent events.
        recovery_numbers: set[int] = set()
        for row in projections:
            advanced = int(row["last_advanced_chapter"])
            known = latest.get(str(row["thread_id"]))
            if known and (advanced > chapter_no or advanced <= int(known["last_advanced_chapter"])):
                continue
            planted = row["planted_chapter"]
            if advanced <= chapter_no:
                recovery_numbers.add(advanced)
            elif planted and int(planted) <= chapter_no:
                recovery_numbers.add(int(planted))
        sources: dict[int, dict[str, Any]] = {}
        if recovery_numbers:
            with self.connect() as connection:
                placeholders = ",".join("?" for _ in recovery_numbers)
                sources = {int(row["chapter_no"]): dict(row) for row in connection.execute(
                    f"SELECT * FROM chapters WHERE status='accepted' AND chapter_no IN ({placeholders}) AND chapter_no<=?",
                    (*sorted(recovery_numbers), chapter_no))}
        recovered: set[int] = set()
        for raw in projections:
            thread = dict(raw)
            thread_id = str(thread["thread_id"])
            advanced = int(thread["last_advanced_chapter"])
            if thread_id in latest:
                if advanced <= chapter_no and advanced > int(latest[thread_id]["last_advanced_chapter"]):
                    # A newer legacy event is missing: the earlier proven open
                    # state cannot silently remain current past this event.
                    latest.pop(thread_id)
                else:
                    continue
            planted = thread.get("planted_chapter")
            if advanced > chapter_no and (not planted or int(planted) > chapter_no):
                continue
            source_no = advanced if advanced <= chapter_no else int(planted)
            gap: dict[str, Any] = {"kind": "thread_history", "thread_id": thread_id,
                "chapter_no": source_no, "boundary_chapter": chapter_no, "state": "unknown",
                "lifecycle_state": "unknown", "owner": "memory_owner",
                "reason": ("原记忆补丁与已接受正文版本不一致，历史状态已隔离。" if thread_id in tainted
                           else "缺少该时点的已接受记忆事件，当前投影不能证明历史线索状态。")}
            source = sources.get(source_no)
            text = str(source.get("content_text") or "") if source else ""
            if source and text and content_hash(text) == source["content_hash"]:
                matches_projection = (advanced <= chapter_no
                    and thread.get("source_version") == source["version"]
                    and thread.get("source_hash") == source["content_hash"])
                quote = str(thread.get("description") or "") if matches_projection else ""
                if quote and quote in text:
                    gap["evidence_quote"] = quote
                    gap["evidence_start"] = text.index(quote)
                    gap["evidence_end"] = text.index(quote) + len(quote)
                gap.update(state="canonical_source_available", source_version=int(source["version"]),
                           source_hash=source["content_hash"])
                if source_no not in recovered:
                    recovered.add(source_no)
                    result["recovery_sources"].append({"chapter_no": source_no,
                        "title": str(source["title"]), "content": text,
                        "source_version": int(source["version"]), "source_hash": source["content_hash"],
                        "authority": "已接受正文；历史线索生命周期待核，原句命中不等于状态证实"})
            else:
                gap["source_problem"] = "边界内已接受原文缺失或哈希不匹配，需定点恢复来源。"
            result["coverage_gaps"].append(gap)
        result["threads"] = sorted(
            (thread for thread in latest.values()
             if thread["status"] in {"open", "advanced", "delayed"}),
            key=lambda item: (item.get("due_chapter") or 999999, item["thread_id"]),
        )
        return result

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

    def apply_accepted_continuity_repair(
        self, chapter_no: int, content: str, *, expected_version: int,
        expected_hash: str, expected_review_id: int, run_id: str,
        diagnosis: dict[str, Any], verification: dict[str, Any],
        review_role: str, role_protocol_version: int | None = None,
        previous_memory_json: str | None = None,
        revised_memory: MemoryPatch | None = None,
    ) -> int:
        """Commit a bounded, independently checked repair without demoting canon."""
        self._require_canonical_content_schema()
        new_hash = content_hash(content)
        if new_hash == expected_hash:
            raise ValueError("修订没有改变正文。")
        _stored_role(review_role)
        role_protocol_version = ROLE_PROTOCOL_VERSION
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status,version,content_hash,content_text FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            latest = connection.execute(
                "SELECT COALESCE(MAX(chapter_no),0) FROM chapters WHERE status='accepted'"
            ).fetchone()[0]
            review = connection.execute(
                "SELECT id,chapter_version FROM reviews WHERE chapter_no=? ORDER BY id DESC LIMIT 1",
                (chapter_no,),
            ).fetchone()
            prior_verified = False
            if review and int(review["chapter_version"]) != expected_version:
                prior_rows = connection.execute(
                    """SELECT data_json FROM agent_artifacts
                       WHERE artifact_type='accepted_continuity_resolution' AND chapter_no=?
                         AND chapter_version=? AND status='verified'""",
                    (chapter_no, expected_version),
                ).fetchall()
                prior_verified = any(
                    (data := json.loads(row["data_json"])).get("source_review_id") == expected_review_id
                    and data.get("new_hash") == expected_hash
                    for row in prior_rows
                )
            if (not current or current["status"] != "accepted"
                    or int(current["version"]) != expected_version
                    or current["content_hash"] != expected_hash
                    or int(latest) != chapter_no
                    or not review or int(review["id"]) != expected_review_id
                    or (int(review["chapter_version"]) != expected_version and not prior_verified)):
                raise ValueError("正史、依赖章节或审查版本已变化；没有覆盖当前正文。")
            stored_memory = connection.execute(
                "SELECT data_json FROM memory_patches WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            current_memory_json = str(stored_memory["data_json"]) if stored_memory else None
            if current_memory_json != previous_memory_json:
                raise ValueError("修订期间本章记忆已变化；候选未覆盖当前版本。")
            if current_memory_json:
                previous_memory = MemoryPatch.model_validate_json(current_memory_json)
                from .memory_records import affected_fact_ids
                affected_ids = affected_fact_ids(previous_memory.facts, current["content_text"] or "", content)
                if revised_memory is None and bool(affected_ids):
                    raise ValueError("局部修订使记忆证据失效，但没有可核对的记忆更新。")
            if revised_memory is not None:
                if not current_memory_json or revised_memory.chapter_no != chapter_no or revised_memory.unresolved_conflicts:
                    raise ValueError("记忆更新缺少有效旧版本或仍有冲突。")
                old_facts = {fact.fact_id: fact for fact in previous_memory.facts}
                new_facts = {fact.fact_id: fact for fact in revised_memory.facts}
                if (revised_memory.operations != previous_memory.operations or set(old_facts) != set(new_facts)
                        or {thread.thread_id for thread in previous_memory.threads}
                        != {thread.thread_id for thread in revised_memory.threads}):
                    raise ValueError("记忆更新擅自增删了事实或线索编号。")
                for fact_id, fact in new_facts.items():
                    old = old_facts[fact_id]
                    if (fact.evidence not in content or fact.subject != old.subject
                            or fact.valid_from_chapter != old.valid_from_chapter
                            or (old.fact_id not in affected_ids and fact != old)):
                        raise ValueError("记忆更新改变了未受影响事实，或证据不在候选正文。")
            new_version = expected_version + 1
            connection.execute(
                """UPDATE chapters SET version=?,content_hash=?,content_text=?,summary=?,updated_at=?
                   WHERE chapter_no=?""",
                (new_version, new_hash, content,
                 revised_memory.chapter_summary if revised_memory is not None else None,
                 now, chapter_no),
            )
            if revised_memory is not None:
                for fact in revised_memory.facts:
                    if fact == old_facts[fact.fact_id]:
                        continue
                    updated = connection.execute(
                        """UPDATE facts SET predicate=?,value_json=?,confidence=?,evidence=?,
                           epistemic_kind=?,event_time=?,narrative_time=?,created_at=?
                           WHERE fact_id=? AND source_chapter=? AND status='active'""",
                        (fact.predicate, json_dumps(fact.value, indent=None), fact.confidence,
                         fact.evidence, fact.epistemic_kind, fact.event_time,
                         fact.narrative_time or f"第{chapter_no}章", now, fact.fact_id, chapter_no),
                    )
                    if updated.rowcount != 1:
                        raise ValueError(f"待更新事实 {fact.fact_id} 不属于当前已接受章。")
                for thread in revised_memory.threads:
                    old_thread = next(item for item in previous_memory.threads if item.thread_id == thread.thread_id)
                    if thread == old_thread:
                        continue
                    connection.execute(
                        """UPDATE plot_threads SET kind=?,title=?,status=?,description=?,
                           planted_chapter=?,due_chapter=?,updated_at=? WHERE thread_id=?""",
                        (thread.kind, thread.title, thread.status, thread.description,
                         thread.planted_chapter, thread.due_chapter, now, thread.thread_id),
                    )
                connection.execute(
                    "UPDATE memory_patches SET data_json=?,committed_at=? WHERE chapter_no=?",
                    (revised_memory.model_dump_json(), now, chapter_no),
                )
                connection.execute(
                    """INSERT INTO agent_artifacts(artifact_id,artifact_type,run_id,chapter_no,
                       chapter_version,role,role_protocol_version,dimension,status,data_json,created_at)
                       VALUES (?,'accepted_memory_rebase',?,?,?,?,?,'memory','verified',?,?)""",
                    (f"artifact-{uuid.uuid4().hex}", run_id, chapter_no, new_version,
                     review_role, role_protocol_version,
                     json_dumps({"old_hash": expected_hash, "new_hash": new_hash,
                                 "before": previous_memory.model_dump(mode="json"),
                                 "after": revised_memory.model_dump(mode="json")}, indent=None), now),
                )
            # A verified local repair is the same accepted chapter at a new
            # version. Rebind only its still-supported memory to that version.
            connection.execute(
                """UPDATE facts SET source_version=?,source_hash=?
                   WHERE source_chapter=? AND status='active' AND instr(?,evidence)>0""",
                (new_version, new_hash, chapter_no, content),
            )
            connection.execute(
                """UPDATE plot_threads SET source_version=?,source_hash=?
                   WHERE last_advanced_chapter=?""",
                (new_version, new_hash, chapter_no),
            )
            connection.execute(
                """INSERT INTO agent_artifacts(artifact_id,artifact_type,run_id,chapter_no,
                   chapter_version,role,role_protocol_version,dimension,status,data_json,created_at)
                   VALUES (?,'accepted_continuity_resolution',?,?,?,?,?,'continuity',
                   'verified',?,?)""",
                (f"artifact-{uuid.uuid4().hex}", run_id, chapter_no, new_version,
                 review_role, role_protocol_version,
                 json_dumps({"source_review_id": expected_review_id,
                             "old_hash": expected_hash, "new_hash": new_hash,
                             "diagnosis": diagnosis, "verification": verification}, indent=None), now),
            )
            if revised_memory is not None:
                from .memory_records import commit_evidence
                commit_evidence(connection, revised_memory.model_copy(update={"operations": []}), chapter_no, new_version, content)
            connection.commit()
        return new_version

    def accept_chapter(
        self,
        chapter_no: int,
        title: str,
        final_path: str,
        content: str,
        patch: MemoryPatch,
        *,
        expected_draft_version: int,
        expected_draft_hash: str,
        expected_review_id: int,
        provisional_batch_id: str | None = None,
        task_id: str = "",
        preview_artifact_id: str | None = None,
        preview_source_binding: dict[str, Any] | None = None,
    ) -> None:
        self._require_canonical_content_schema()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if preview_artifact_id:
                artifact = connection.execute("SELECT * FROM agent_artifacts WHERE artifact_id=?", (preview_artifact_id,)).fetchone()
                data = json.loads(artifact["data_json"]) if artifact else {}
                binding = data.get("preview_binding", {})
                if (provisional_batch_id or not artifact or artifact["artifact_type"] != "memory_patch_preview"
                        or artifact["status"] != "awaiting_commit" or artifact["chapter_no"] != chapter_no
                        or artifact["chapter_version"] != expected_draft_version
                        or not preview_source_binding or binding != preview_source_binding
                        or binding.get("task_id") != task_id or not binding.get("snapshot_hash") or not binding.get("run_id")
                        or binding.get("chapter_no") != chapter_no or binding.get("chapter_version") != expected_draft_version
                        or binding.get("content_hash") != expected_draft_hash or binding.get("review_id") != expected_review_id
                        or MemoryPatch.model_validate({key: value for key, value in data.items()
                            if key != "preview_binding"}).model_dump(mode="json") != patch.model_dump(mode="json")):
                    raise ValueError("正史预览的状态、原配置或正文/补丁来源已变化，没有提交记忆。")
            current = connection.execute(
                "SELECT status, version, content_hash FROM chapters WHERE chapter_no=?", (chapter_no,)
            ).fetchone()
            if not current:
                raise ValueError(f"第 {chapter_no} 章没有草稿记录")
            accepted_before = connection.execute(
                "SELECT COUNT(*) FROM chapters WHERE status='accepted' AND chapter_no<?", (chapter_no,)
            ).fetchone()[0]
            if accepted_before != chapter_no - 1:
                raise ValueError(f"第 {chapter_no} 章之前仍有未接受章节，不能越章写入正史")
            if (
                current["status"] != "draft"
                or int(current["version"]) != expected_draft_version
                or current["content_hash"] != expected_draft_hash
                or content_hash(content) != expected_draft_hash
            ):
                raise ValueError(f"第 {chapter_no} 章草稿在验收期间已变化；没有写入正史或重复提交记忆")
            latest_review = connection.execute(
                "SELECT id,chapter_version FROM reviews WHERE chapter_no=? ORDER BY id DESC LIMIT 1",
                (chapter_no,),
            ).fetchone()
            if (
                latest_review is None or int(latest_review["id"]) != expected_review_id
                or int(latest_review["chapter_version"]) != expected_draft_version
            ):
                raise ValueError(f"第 {chapter_no} 章审查版本在验收期间已变化；没有写入正史")
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
                from .memory_records import journal
                previous_facts = [dict(row) for row in connection.execute(
                    "SELECT * FROM facts WHERE subject=? AND predicate=? AND status='active'",
                    (fact.subject, fact.predicate))]
                existing_identity = connection.execute(
                    "SELECT subject, predicate, source_chapter FROM facts WHERE fact_id=?",
                    (fact.fact_id,),
                ).fetchone()
                if existing_identity and (
                    existing_identity["subject"] != fact.subject
                    or existing_identity["predicate"] != fact.predicate
                    or existing_identity["source_chapter"] != chapter_no
                ):
                    raise ValueError(f"fact_id {fact.fact_id} 已属于其他主体关系，不能覆盖")
                connection.execute(
                    """
                    UPDATE facts SET valid_to_chapter=?, status='superseded'
                    WHERE subject=? AND predicate=? AND status='active'
                      AND valid_to_chapter IS NULL AND source_chapter <= ?
                    """,
                    (chapter_no - 1, fact.subject, fact.predicate, chapter_no),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO facts(
                        fact_id, subject, predicate, value_json, valid_from_chapter, valid_to_chapter,
                        source_chapter, confidence, evidence, epistemic_kind, event_time,
                        narrative_time, source_version, source_hash, branch_id, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'main', 'active', ?)
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
                        "belief" if fact.epistemic_kind == "objective" and fact.predicate.startswith(("believes.", "knows.")) else ("rumor" if fact.epistemic_kind == "objective" and fact.predicate.startswith("rumor.") else fact.epistemic_kind),
                        fact.event_time,
                        fact.narrative_time or f"第{chapter_no}章",
                        int(current["version"]),
                        content_hash(content),
                        now,
                    ),
                )
                journal(connection, "replace" if previous_facts else "add", fact.fact_id,
                        previous_facts, fact.model_dump(mode="json"), "已审核正文中的事实演变", chapter_no)
            from .memory_records import commit_evidence
            commit_evidence(connection, patch, chapter_no, int(current["version"]), content)
            for thread in patch.threads:
                connection.execute(
                    """
                    INSERT INTO plot_threads(
                        thread_id, kind, title, status, description, planted_chapter,
                        due_chapter, last_advanced_chapter, source_version,
                        source_hash, branch_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'main', ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        kind=excluded.kind,
                        title=excluded.title,
                        status=excluded.status,
                        description=excluded.description,
                        planted_chapter=COALESCE(plot_threads.planted_chapter, excluded.planted_chapter),
                        due_chapter=excluded.due_chapter,
                        last_advanced_chapter=excluded.last_advanced_chapter,
                        source_version=excluded.source_version,
                        source_hash=excluded.source_hash,
                        branch_id=excluded.branch_id,
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
                        int(current["version"]),
                        content_hash(content),
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
            if task_id:
                connection.execute(
                    "INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (f"workflow_accept:{task_id}:{chapter_no}", json.dumps({"chapter_no": chapter_no,
                        "version": expected_draft_version, "content_hash": content_hash(content),
                        "review_id": expected_review_id, "path": final_path,
                        "post_commit": {"settings": "pending", "checkpoint": "pending"}}, ensure_ascii=False), now))
            if preview_artifact_id:
                connection.execute("UPDATE agent_artifacts SET status='committed' WHERE artifact_id=?", (preview_artifact_id,))
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
        role_protocol_version: int | None = None,
    ) -> dict[str, Any]:
        protocol_version = ROLE_PROTOCOL_VERSION
        message_types = {"task_assignment", "fact_query", "handoff", "review_issue", "revision_request", "objection", "risk", "memory_sync", "answer"}
        statuses = {"pending", "responded", "resolved", "escalated", "expired"}
        _stored_role(sender_role, protocol_version)
        _stored_role(recipient_role, protocol_version)
        if message_type not in message_types or status not in statuses:
            raise ValueError("协作消息类型或状态不受支持")
        message_id = f"message-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO collaboration_threads(
                    thread_id,run_id,topic,chapter_no,chapter_version,context_packet_id,
                    status,current_round,max_rounds,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,1,2,?,?)
                """,
                (thread_id, run_id, claim, chapter_no, chapter_version, context_packet_id, "resolved" if status == "resolved" else "open", now, now),
            )
            connection.execute(
                """
                INSERT INTO collaboration_messages(
                    message_id, thread_id, run_id, sender_role, recipient_role, role_protocol_version, message_type,
                    chapter_no, chapter_version, context_packet_id, claim, evidence_refs_json,
                    requested_response, status, expires_at, response_to, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id, thread_id, run_id, sender_role, recipient_role, protocol_version, message_type,
                    chapter_no, chapter_version, context_packet_id, claim,
                    json_dumps(sorted(set(evidence_refs or [])), indent=None), requested_response,
                    status, expires_at, response_to, now,
                ),
            )
            if response_to:
                connection.execute(
                    "UPDATE collaboration_messages SET status='responded' WHERE message_id=? AND status='pending'",
                    (response_to,),
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

    def open_collaboration_thread(
        self,
        *,
        run_id: str,
        topic: str,
        chapter_no: int | None = None,
        chapter_version: int | None = None,
        context_packet_id: str = "",
        max_rounds: int = 2,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        clean_topic = topic.strip()
        if not clean_topic:
            raise ValueError("协作议题不能为空")
        bounded_rounds = max(1, min(int(max_rounds), 2))
        item_id = thread_id or f"thread-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collaboration_threads(
                    thread_id,run_id,topic,chapter_no,chapter_version,context_packet_id,
                    status,current_round,max_rounds,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,'open',1,?,?,?)
                """,
                (item_id, run_id, clean_topic, chapter_no, chapter_version, context_packet_id, bounded_rounds, now, now),
            )
            connection.commit()
        return self.get_collaboration_thread(item_id)

    def get_collaboration_thread(self, thread_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM collaboration_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
        if row is None:
            raise ValueError("协作议题不存在")
        return dict(row)

    def advance_collaboration_thread(self, thread_id: str, *, resolution: str = "") -> dict[str, Any]:
        item = self.get_collaboration_thread(thread_id)
        if item["status"] not in {"open", "waiting"}:
            return item
        next_round = int(item["current_round"]) + 1
        status = "escalated" if next_round > int(item["max_rounds"]) else "open"
        with self.connect() as connection:
            connection.execute(
                "UPDATE collaboration_threads SET current_round=?,status=?,resolution=?,updated_at=? WHERE thread_id=?",
                (min(next_round, int(item["max_rounds"])), status, resolution or None, utc_now(), thread_id),
            )
            connection.commit()
        return self.get_collaboration_thread(thread_id)

    def close_collaboration_thread(self, thread_id: str, resolution: str) -> dict[str, Any]:
        if not resolution.strip():
            raise ValueError("关闭协作议题时必须记录可复核结论")
        with self.connect() as connection:
            connection.execute(
                "UPDATE collaboration_threads SET status='resolved',resolution=?,updated_at=? WHERE thread_id=?",
                (resolution.strip(), utc_now(), thread_id),
            )
            connection.commit()
        self.resolve_collaboration_thread(thread_id)
        return self.get_collaboration_thread(thread_id)

    def list_collaboration_threads(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collaboration_threads ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_agent_artifact(
        self,
        *,
        artifact_type: str,
        run_id: str,
        role: str,
        data: dict[str, Any],
        chapter_no: int | None = None,
        chapter_version: int | None = None,
        dimension: str = "",
        status: str = "candidate",
        role_protocol_version: int | None = None,
    ) -> dict[str, Any]:
        protocol_version = ROLE_PROTOCOL_VERSION
        _stored_role(role, protocol_version)
        artifact_id = f"artifact-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO agent_artifacts(
                    artifact_id, artifact_type, run_id, chapter_no, chapter_version, role,
                    role_protocol_version, dimension, status, data_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (artifact_id, artifact_type, run_id, chapter_no, chapter_version, role, protocol_version,
                 dimension or None, status, json_dumps(data, indent=None), now),
            )
            connection.commit()
        return {"artifact_id": artifact_id, "artifact_type": artifact_type, "run_id": run_id, "role": role,
                "chapter_no": chapter_no,
                "chapter_version": chapter_version, "dimension": dimension, "status": status,
                "data": data, "created_at": now}

    def list_agent_artifacts(self, *, chapter_no: int | None = None, artifact_type: str = "", limit: int = 50) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if chapter_no is not None:
            clauses.append("chapter_no=?")
            params.append(chapter_no)
        if artifact_type:
            clauses.append("artifact_type=?")
            params.append(artifact_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 200)))
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM agent_artifacts" + where + " ORDER BY created_at DESC LIMIT ?", params).fetchall()
        result = []
        for row in rows:
            item = _adapt_stored_record(dict(row))
            item["data"] = json.loads(item.pop("data_json"))
            result.append(item)
        return result

    def select_agent_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM agent_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
            if row is None:
                raise ValueError("候选方案不存在")
            connection.execute(
                "UPDATE agent_artifacts SET status=CASE WHEN artifact_id=? THEN 'selected' ELSE 'not_selected' END WHERE run_id=? AND artifact_type=?",
                (artifact_id, row["run_id"], row["artifact_type"]),
            )
            connection.commit()
        result = _adapt_stored_record(dict(row))
        result["data"] = json.loads(result.pop("data_json"))
        result["status"] = "selected"
        return result

    def set_agent_artifact_status(self, artifact_id: str, status: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute("UPDATE agent_artifacts SET status=? WHERE artifact_id=?", (status, artifact_id))
            connection.commit()
        if not cursor.rowcount:
            raise ValueError("Agent 产物不存在")

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
        if active_only:
            clauses.append("status IN ('pending','responded','escalated')")
            clauses.append("(expires_at IS NULL OR expires_at>?)")
            params.append(utc_now())
            clauses.append("NOT EXISTS (SELECT 1 FROM collaboration_threads t WHERE t.thread_id=collaboration_messages.thread_id AND t.status='resolved')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM collaboration_messages" + where + " ORDER BY created_at DESC,rowid DESC LIMIT ?",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = _adapt_stored_record(dict(row))
            item["evidence_refs"] = json.loads(item.pop("evidence_refs_json"))
            if not recipient_role or item["recipient_role"] == _stored_role(recipient_role):
                result.append(item)
        return result

    def reconcile_collaboration_queue(self) -> dict[str, int]:
        """Collapse obsolete Coordinator assignments without deleting evidence.

        A failed/repeated desktop request can leave several pending assignments
        for the same chapter version.  The Coordinator should see only the
        newest actionable handoff; resolved rows remain queryable as history.
        """

        active = self.list_collaboration_messages(active_only=True, limit=500)
        with self.connect() as connection:
            chapter_rows = connection.execute("SELECT * FROM chapters").fetchall()
        chapters = {int(item["chapter_no"]): dict(item) for item in chapter_rows}
        newest: dict[tuple[Any, ...], str] = {}
        stale_ids: list[str] = []
        for item in active:
            message_id = str(item["message_id"])
            chapter_no = item.get("chapter_no")
            chapter = chapters.get(int(chapter_no)) if chapter_no is not None else None
            chapter_version = item.get("chapter_version")
            if chapter is None:
                key = (
                    "global",
                    item.get("recipient_role"),
                    item.get("message_type"),
                )
            else:
                current_version = int(chapter.get("version") or 0)
                if chapter.get("status") == "accepted" and item.get("message_type") == "task_assignment":
                    stale_ids.append(message_id)
                    continue
                if chapter_version is not None and int(chapter_version) != current_version:
                    stale_ids.append(message_id)
                    continue
                key = (
                    int(chapter_no),
                    current_version,
                    item.get("recipient_role"),
                    item.get("message_type"),
                )
            key += (item.get("thread_id"), item.get("context_packet_id"))
            if item.get("message_type") != "task_assignment":
                key += (item.get("claim"), item.get("requested_response"))
            if key in newest:
                stale_ids.append(message_id)
                continue
            newest[key] = message_id
        if stale_ids:
            # stale_ids 只记录已经被新一条替代或明确过期的消息；
            # 当前 key 的最后一条不会进入这个集合。
            unique = sorted(set(stale_ids))
            with self.connect() as connection:
                connection.executemany(
                    "UPDATE collaboration_messages SET status='resolved', resolved_at=? WHERE message_id=? AND status IN ('pending','responded','escalated')",
                    [(utc_now(), message_id) for message_id in unique],
                )
                connection.execute(
                    """
                    UPDATE collaboration_threads
                    SET status='resolved', updated_at=?
                    WHERE thread_id IN (
                        SELECT DISTINCT thread_id FROM collaboration_messages
                        WHERE status='resolved' AND thread_id IN (
                            SELECT thread_id FROM collaboration_messages WHERE message_id IN ({placeholders})
                        )
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM collaboration_messages active
                        WHERE active.thread_id=collaboration_threads.thread_id
                          AND active.status IN ('pending','responded','escalated')
                    )
                    """.format(placeholders=",".join("?" for _ in unique)),
                    [utc_now(), *unique],
                ) if unique else None
                connection.commit()
            resolved = len(unique)
        else:
            resolved = 0
        with self.connect() as connection:
            connection.execute(
                """UPDATE collaboration_messages SET status='resolved',resolved_at=?
                   WHERE status IN ('pending','responded','escalated')
                   AND EXISTS (SELECT 1 FROM collaboration_threads t
                               WHERE t.thread_id=collaboration_messages.thread_id AND t.status='resolved')""", (utc_now(),))
            connection.execute(
                """UPDATE collaboration_messages SET status='resolved',resolved_at=?
                   WHERE status IN ('pending','responded','escalated')
                   AND expires_at IS NOT NULL AND expires_at<=?""", (utc_now(), utc_now()))
            closed = connection.execute(
                """UPDATE collaboration_threads SET status='resolved',updated_at=?,
                   resolution=COALESCE(NULLIF(resolution,''),'来源版本已更新或关联消息已处理；原记录保留历史。')
                   WHERE status IN ('open','waiting','escalated') AND (
                       EXISTS (SELECT 1 FROM chapters c WHERE c.chapter_no=collaboration_threads.chapter_no
                               AND collaboration_threads.chapter_version IS NOT NULL
                               AND c.version<>collaboration_threads.chapter_version)
                       OR (EXISTS (SELECT 1 FROM collaboration_messages m
                                   WHERE m.thread_id=collaboration_threads.thread_id)
                           AND NOT EXISTS (SELECT 1 FROM collaboration_messages m
                                           WHERE m.thread_id=collaboration_threads.thread_id
                                           AND m.status IN ('pending','responded','escalated'))))""", (utc_now(),))
            connection.commit()
            closed_threads = closed.rowcount
        remaining = len(self.list_collaboration_messages(active_only=True, limit=500))
        return {"resolved": resolved, "closed_threads": closed_threads, "remaining": remaining}

    def resolve_pending_collaboration(
        self,
        *,
        chapter_no: int,
        recipient_role: str,
        role_protocol_version: int | None = None,
    ) -> int:
        recipient = _stored_role(recipient_role)
        matching = [item["message_id"] for item in self.list_collaboration_messages(
            chapter_no=chapter_no, active_only=True, limit=500)
            if item.get("chapter_no") == chapter_no and item["recipient_role"] == recipient
            and item["status"] in {"pending", "responded"}]
        if not matching:
            return 0
        with self.connect() as connection:
            connection.executemany(
                "UPDATE collaboration_messages SET status='resolved', resolved_at=? WHERE message_id=?",
                [(utc_now(), message_id) for message_id in matching],
            )
            connection.commit()
        return len(matching)

    def resolve_collaboration_thread(self, thread_id: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE collaboration_messages SET status='resolved', resolved_at=?
                WHERE thread_id=? AND status IN ('pending','responded','escalated')
                """,
                (utc_now(), thread_id),
            )
            connection.execute(
                "UPDATE collaboration_threads SET status='resolved',updated_at=? WHERE thread_id=?",
                (utc_now(), thread_id),
            )
            connection.commit()
        return int(cursor.rowcount)

    def upsert_preference(self, *, text: str, strength: str = "weak", scope: str = "project",
                          source: str = "user", preference_id: str | None = None, **details) -> dict[str, Any]:
        from .preferences import change_item
        with self.connect() as connection:
            return change_item(connection, text=text, strength=strength, scope=scope, source=source,
                               preference_id=preference_id, **details)

    def list_preferences(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        from .preferences import list_items
        with self.connect() as connection:
            return list_items(connection, active_only=active_only)

    def effective_preferences(self) -> list[dict[str, Any]]:
        from .preferences import effective_preferences
        return effective_preferences(self)

    def set_preference_status(self, preference_id: str, status: str) -> dict[str, Any]:
        item = next((item for item in self.list_preferences(active_only=False)
                     if item["preference_id"] == preference_id), None)
        if item is None:
            raise ValueError("作品声音偏好不存在")
        return self.upsert_preference(text=item["text"], strength=item["strength"], scope=item["scope"],
            source=item["source"], preference_id=preference_id, status=status,
            expected_revision=item.get("revision", 0), reason="用户修改使用状态")

    def delete_preference(self, preference_id: str) -> dict[str, Any]:
        # Removal from use retains the audit trail; it is not physical erasure.
        return self.set_preference_status(preference_id, "deleted")

    def record_learning_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        chapter_no: int | None = None,
        chapter_version: int | None = None,
    ) -> str:
        learning_settings = self.get_metadata("learning_settings", {"enabled": True})
        if isinstance(learning_settings, dict) and learning_settings.get("enabled") is False:
            return ""
        if event_type not in {"accepted", "rejected", "revised", "rolled_back", "preference_changed", "comparison", "review_recovery"}:
            raise ValueError("学习事件类型不受支持")
        if event_type == "review_recovery":
            signal_origin = "workflow_recovery"
        elif event_type == "rejected":
            signal_origin = "machine_review"
        elif event_type == "accepted":
            signal_origin = "canon_commit"
        elif event_type == "preference_changed":
            signal_origin = "user_preference"
        elif event_type == "comparison":
            signal_origin = "user_comparison"
        elif event_type == "revised" and payload.get("source") == "user_selection":
            signal_origin = "user_revision"
        elif event_type == "rolled_back":
            signal_origin = "workflow_rollback"
        else:
            signal_origin = "workflow_revision"
        stored_payload = {**payload, "_signal_origin": signal_origin}
        event_id = f"learning-{uuid.uuid4().hex}"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO learning_events(event_id,event_type,chapter_no,chapter_version,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (event_id, event_type, chapter_no, chapter_version, json_dumps(stored_payload, indent=None), utc_now()),
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
            item["signal_origin"] = str(item["payload"].get("_signal_origin") or "legacy_unclassified")
            result.append(item)
        return result

    def learning_guidance(self, *, chapter_no: int | None = None, role: str | None = None) -> dict[str, Any]:
        """Derive small, explainable signals from attributable runtime records."""

        events = self.list_learning_events(80)
        counts = Counter(str(item["signal_origin"]) for item in events)
        remedies = {
            "source_reread": "必要资料缺失时，按当前对象与事件拆短查询，定点回读有效原文，再由原责任角色核对。",
            "claim_attribution": "指控未获支持时，先定位各自来源引文，区分状态变化、人物观点和排他矛盾；不据此改正文或降低资料权重。",
        }
        cases = []
        seen = set()
        for event in events:
            payload = event["payload"]
            if (event["signal_origin"] != "workflow_recovery" or not event["chapter_no"]
                    or (chapter_no is not None and event["chapter_no"] > chapter_no)
                    or (role not in {None, "coordinator"} and payload.get("role") != role)
                    or payload.get("action") not in remedies):
                continue
            chapter = self.get_chapter(event["chapter_no"])
            review = self.latest_review_record(event["chapter_no"])
            if (not chapter or not review or review["id"] != payload.get("review_id")
                    or chapter["version"] != event["chapter_version"]
                    or chapter["content_hash"] != payload.get("source_hash")
                    or review["report"].source_hash != payload.get("source_hash")):
                continue
            verified = payload.get("status") == "verified" and review["report"].verdict == "pass"
            key = (payload.get("role"), payload["action"], verified)
            if key in seen:
                continue
            seen.add(key)
            cases.append({"event_id": event["event_id"], "chapter_no": event["chapter_no"],
                "role": payload.get("role"), "status": "verified" if verified else "unresolved",
                "action": payload["action"], "guidance": remedies[payload["action"]],
                "gap_count": payload.get("gap_count", 0)})
            if len(cases) == 5:
                break
        return {
            "window_events": len(events),
            "explicit_user_feedback": sum(counts[key] for key in ("user_preference", "user_comparison", "user_revision")),
            "machine_review_events": counts["machine_review"],
            "canon_commits": counts["canon_commit"],
            "legacy_unclassified": counts["legacy_unclassified"],
            "recovery_cases": cases,
            "recovery_policy": "只参考处理方法，不复用旧章资料或事实，不提高硬门槛。未解决案例不是成功经验；本次仍查源并核门禁，沿原授权和预算限次恢复。",
            "notice": "只把用户明确偏好、比较或选区修订视为写法反馈；机器审核、正史提交和旧版来源不作为文风样本。",
        }

    def create_computer_action_request(self, command: str, command_hash: str, *, ttl_seconds: int = 300) -> dict[str, Any]:
        request_id = f"computer-action-{uuid.uuid4().hex}"
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(seconds=max(30, min(ttl_seconds, 600)))).isoformat()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO computer_action_requests
                   (request_id,action_kind,command,command_hash,status,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (request_id, "powershell", command, command_hash, "pending", created_at, expires_at),
            )
            connection.commit()
        return self.get_computer_action_request(request_id) or {}

    def list_pending_computer_action_requests(self, *, limit: int = 10) -> list[dict[str, Any]]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE computer_action_requests SET status='expired' WHERE status='pending' AND expires_at<=?",
                (now,),
            )
            rows = connection.execute(
                """SELECT * FROM computer_action_requests WHERE status='pending' AND expires_at>?
                   ORDER BY created_at LIMIT ?""",
                (now, max(1, min(limit, 50))),
            ).fetchall()
            connection.commit()
        return [dict(row) for row in rows]

    def get_computer_action_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM computer_action_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def resolve_computer_action_request(self, request_id: str, command_hash: str, *, approved: bool) -> dict[str, Any]:
        now = utc_now()
        status = "approved" if approved else "rejected"
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM computer_action_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValidationGateError("电脑操作确认请求不存在，可能已过期或已处理。")
            if row["status"] != "pending" or row["expires_at"] <= now:
                if row["status"] == "pending":
                    connection.execute(
                        "UPDATE computer_action_requests SET status='expired' WHERE request_id=? AND status='pending'",
                        (request_id,),
                    )
                    connection.commit()
                raise ValidationGateError("该电脑操作确认已过期或已处理，没有执行任何命令。")
            if row["command_hash"] != command_hash:
                raise ValidationGateError("确认内容与当前命令不一致，没有执行任何命令。")
            connection.execute(
                "UPDATE computer_action_requests SET status=?,decided_at=? WHERE request_id=? AND status='pending'",
                (status, now, request_id),
            )
            connection.commit()
        return self.get_computer_action_request(request_id) or {}

    def claim_approved_computer_action(self, request_id: str, command_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE computer_action_requests SET status='executing'
                   WHERE request_id=? AND command_hash=? AND status='approved' AND expires_at>?""",
                (request_id, command_hash, utc_now()),
            )
            connection.commit()
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM computer_action_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def cancel_approved_computer_action(self, request_id: str, command_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE computer_action_requests SET status='rejected',decided_at=?
                   WHERE request_id=? AND command_hash=? AND status='approved'""",
                (utc_now(), request_id, command_hash),
            )
            connection.commit()

    def finish_computer_action_request(self, request_id: str, *, exit_code: int | None, error: str = "") -> None:
        status = "completed" if exit_code == 0 else "failed"
        with self.connect() as connection:
            connection.execute(
                """UPDATE computer_action_requests SET status=?,completed_at=?,exit_code=?,error=?
                   WHERE request_id=? AND status='executing'""",
                (status, utc_now(), exit_code, error[:1000], request_id),
            )
            connection.commit()

    def update_learning_strategy(self, strategy_key: str, reward: float) -> dict[str, Any]:
        key = strategy_key.strip()
        if not key:
            raise ValueError("学习策略名称不能为空")
        bounded = max(-1.0, min(1.0, float(reward)))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_strategies(strategy_key,trials,reward,updated_at) VALUES (?,1,?,?)
                ON CONFLICT(strategy_key) DO UPDATE SET trials=trials+1,reward=reward+excluded.reward,updated_at=excluded.updated_at
                """,
                (key, bounded, utc_now()),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM learning_strategies WHERE strategy_key=?", (key,)).fetchone()
        return dict(row)

    def choose_learning_strategy(self, candidates: list[str]) -> dict[str, Any]:
        clean = sorted({item.strip() for item in candidates if item.strip()})
        if not clean:
            raise ValueError("至少需要一个候选策略")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM learning_strategies WHERE strategy_key IN (%s)" % ",".join("?" for _ in clean), clean
            ).fetchall()
        known = {str(row["strategy_key"]): dict(row) for row in rows}
        selected = min(
            clean,
            key=lambda item: (
                known.get(item, {}).get("trials", 0) > 0,
                -(known.get(item, {}).get("reward", 0.0) / max(1, known.get(item, {}).get("trials", 0))),
                item,
            ),
        )
        state = known.get(selected, {"strategy_key": selected, "trials": 0, "reward": 0.0})
        return {
            "selected": selected,
            "trials": state["trials"],
            "average_reward": float(state["reward"]) / max(1, int(state["trials"])),
            "method": "local_explainable_bandit",
        }

    def save_preference_pair(self, chosen_artifact_id: str, rejected_artifact_id: str, features: dict[str, float]) -> str:
        if chosen_artifact_id == rejected_artifact_id:
            raise ValueError("偏好比较的两个候选不能相同")
        pair_id = f"pair-{uuid.uuid4().hex}"
        clean_features = {str(key): float(value) for key, value in features.items()}
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO preference_pairs VALUES (?,?,?,?,?)",
                (pair_id, chosen_artifact_id, rejected_artifact_id, json_dumps(clean_features, indent=None), utc_now()),
            )
            connection.commit()
        return pair_id

    def get_cached_embedding(self, source_id: str, model: str, expected_hash: str) -> list[float] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT vector_json,content_hash FROM retrieval_embeddings WHERE source_id=? AND model=?",
                (source_id, model),
            ).fetchone()
        if row is None or row["content_hash"] != expected_hash:
            return None
        return [float(value) for value in json.loads(row["vector_json"])]

    def cache_embedding(self, source_id: str, model: str, source_hash: str, vector: list[float]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO retrieval_embeddings(source_id,model,content_hash,vector_json,updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(source_id,model) DO UPDATE SET content_hash=excluded.content_hash,vector_json=excluded.vector_json,updated_at=excluded.updated_at
                """,
                (source_id, model, source_hash, json_dumps(vector, indent=None), utc_now()),
            )
            connection.commit()

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
