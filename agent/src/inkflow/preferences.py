"""Author defaults and project preferences share one audited storage contract."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from .config import user_settings_path
from .utils import content_hash, json_dumps, utc_now


@contextmanager
def author_connection():
    path = user_settings_path().with_name("author-preferences.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                preference_id TEXT PRIMARY KEY, strength TEXT NOT NULL, scope TEXT NOT NULL,
                text TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
        yield connection
    finally:
        connection.close()


def read_meta(connection, key, default=None):
    row = connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write_meta(connection, key, value):
    connection.execute("INSERT INTO metadata(key,value_json,updated_at) VALUES (?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                       (key, json_dumps(value, indent=None), utc_now()))


def list_items(connection, *, active_only=True, level="project"):
    rows = connection.execute("SELECT * FROM user_preferences" +
        (" WHERE status='active'" if active_only else "") + " ORDER BY created_at,preference_id").fetchall()
    return [dict(row) | read_meta(connection, "preference.detail:" + row["preference_id"], {}) |
            {"level": level} for row in rows]


def change_item(connection, *, level="project", preference_id=None, text=None, strength="weak",
                scope="project", source="user", status="active", source_quote="", source_ref="",
                reason="用户设置", topic="", supersedes="", expected_revision=None):
    if status not in {"active", "paused", "candidate", "deleted"} or strength not in {"hard", "weak"}:
        raise ValueError("无效的偏好状态或强度")
    if not (scope == "project" or any(scope.startswith(kind + ":") and scope.split(":", 1)[1].strip()
                                    for kind in ("character", "scene", "arc", "chapter", "genre"))):
        raise ValueError("请指定全书、人物、场景、篇章、章节或题材范围")
    if level == "author" and scope.split(":", 1)[0] in {"character", "arc", "chapter"}:
        raise ValueError("人物和章节要求请保存到本书，不能自动跨书使用")
    connection.execute("BEGIN IMMEDIATE")
    try:
        items = {item["preference_id"]: item for item in list_items(connection, active_only=False, level=level)}
        clean = str(text or "").strip()
        item_id = preference_id or f"{level}-preference-{content_hash(scope + clean)[:16]}"
        old = items.get(item_id)
        if preference_id and old is None:
            raise ValueError("偏好不存在，请刷新设置")
        if expected_revision is not None and int(expected_revision) != int((old or {}).get("revision", 0)):
            raise ValueError("偏好已在其他窗口修改，请刷新后再保存")
        if not clean:
            clean = str((old or {}).get("text", ""))
        if not clean or len(clean) > 4000 or len(source_quote) > 8000 or len(reason) > 2000:
            raise ValueError("偏好或来源长度无效")
        if supersedes and (supersedes == item_id or supersedes not in items):
            raise ValueError("被替代偏好不存在或指向自身")
        now = utc_now()
        details = {"revision": int((old or {}).get("revision", 0)) + 1,
                   "source_quote": source_quote or (old or {}).get("source_quote", "") or clean,
                   "source_ref": source_ref or (old or {}).get("source_ref", ""),
                   "reason": reason, "topic": topic or (old or {}).get("topic", ""),
                   "supersedes": supersedes or (old or {}).get("supersedes", "")}
        item = {"preference_id": item_id, "text": clean, "strength": strength, "scope": scope,
                "source": source, "status": status, "created_at": (old or {}).get("created_at", now),
                "updated_at": now, "level": level, **details}
        connection.execute("INSERT INTO user_preferences VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(preference_id) DO UPDATE SET text=excluded.text,strength=excluded.strength,"
            "scope=excluded.scope,source=excluded.source,status=excluded.status,updated_at=excluded.updated_at",
            tuple(item[key] for key in ("preference_id", "strength", "scope", "text", "source", "status", "created_at", "updated_at")))
        write_meta(connection, "preference.detail:" + item_id, details)
        event = {"event_id": uuid4().hex, "preference_id": item_id, "before": old, "after": item,
                 "reason": reason, "created_at": now}
        write_meta(connection, "preference.event:" + event["event_id"], event)
        if supersedes and status == "active":
            previous = items[supersedes]
            connection.execute("UPDATE user_preferences SET status='paused',updated_at=? WHERE preference_id=?", (now, supersedes))
            previous_details = read_meta(connection, "preference.detail:" + supersedes, {})
            previous_details["revision"] = int(previous.get("revision", 0)) + 1
            write_meta(connection, "preference.detail:" + supersedes, previous_details)
            event = {"event_id": uuid4().hex, "preference_id": supersedes, "before": previous,
                     "after": previous | previous_details | {"status": "paused", "updated_at": now},
                     "reason": "被偏好 " + item_id + " 替代", "created_at": now}
            write_meta(connection, "preference.event:" + event["event_id"], event)
        connection.commit()
        return item
    except BaseException:
        connection.rollback()
        raise


def history(connection, preference_id=""):
    events = [json.loads(row[0]) for row in connection.execute(
        "SELECT value_json FROM metadata WHERE key LIKE 'preference.event:%' ORDER BY updated_at DESC,key DESC")]
    return [item for item in events if not preference_id or item["preference_id"] == preference_id]


def effective_preferences(database):
    with author_connection() as connection:
        defaults = list_items(connection, level="author")
    local = database.list_preferences()
    disabled = set(database.get_metadata("author_preferences_disabled", []))
    topics = {item.get("topic") for item in local if item.get("topic")}
    defaults = [item for item in defaults if item["preference_id"] not in disabled and
                not (item.get("topic") and item["topic"] in topics)]
    # Author habits are defaults, never additional hard gates on a new book.
    return local + [item | {"strength": "weak"} for item in defaults]


def preference_prompt(database=None):
    if database is None:
        with author_connection() as connection:
            items = list_items(connection, level="author")
    else:
        items = effective_preferences(database)
    return "\n作者默认习惯与本书偏好（非正史；当前要求优先，按适用范围使用，勿移植其他书剧情）：\n" + json_dumps([
        {key: item.get(key) for key in ("preference_id", "text", "scope", "level", "strength", "topic")}
        for item in items])


def applicable_preferences(items, task, card):
    haystack = (task + "\n" + json.dumps(card, ensure_ascii=False)).casefold()
    result = []
    for item in items:
        kind, _, target = item["scope"].partition(":")
        if kind == "project" or (kind == "chapter" and target == str(card.get("chapter_no"))) or (
                kind != "chapter" and target and target.casefold() in haystack):
            result.append(item)
    return result


def preference_section(database):
    from .schemas import ContextSection
    items = effective_preferences(database)
    return ContextSection(key="author-preferences", title="作者习惯与本书偏好（当前指令优先）",
                          content="当前要求、本书要求优先于作者默认；普通偏好不是硬门禁；只按适用范围使用，不移植其他书剧情。\n" + json_dumps([
                              {key: item.get(key) for key in ("preference_id", "text", "scope", "level", "strength", "topic")}
                              for item in items]),
                          source_ids=[item["preference_id"] for item in items], cache_scope="book")


def capture_observations(database, observations, message, run_id):
    saved = []
    for observation in observations:
        quote = observation.source_quote.strip()
        if quote not in message or observation.level == "task":
            continue
        author_explicit = bool(re.search(r"跨书|所有小说|所有书|默认习惯|全局|每本书", quote))
        project_explicit = bool(re.search(r"这本书|本书|以后|记住|一直|长期", quote))
        level = observation.level
        active = observation.explicit and (author_explicit if level == "author" else project_explicit)
        values = dict(text=observation.text, scope=observation.scope, topic=observation.topic,
                      source="conversation", source_quote=quote,
                      source_ref=f"{database.path.parent / 'runs' / run_id}",
                      status="active" if active else "candidate", reason="用户明确要求" if active else "待用户确认的反馈理解")
        kind, _, target = observation.scope.partition(":")
        if (kind not in {"project", "genre", "scene", "character", "arc", "chapter"}
                or (kind != "project" and not target.strip())
                or (level == "author" and kind in {"character", "arc", "chapter"})):
            values.update(scope="project", status="candidate", reason="反馈范围待用户在设置中确认")
            active = False
        # An inferred observation must never demote an already confirmed habit.
        if level == "author":
            with author_connection() as connection:
                known = list_items(connection, active_only=False, level=level)
                existing = next((item for item in known if item["text"] == observation.text and item["scope"] == values["scope"]), None)
                if existing:
                    values.update(preference_id=existing["preference_id"], expected_revision=existing.get("revision", 0),
                                  strength=existing["strength"], status="active" if active else existing["status"])
                saved.append(change_item(connection, level=level, **values))
        else:
            existing = next((item for item in database.list_preferences(active_only=False)
                             if item["text"] == observation.text and item["scope"] == values["scope"]), None)
            if existing:
                values.update(preference_id=existing["preference_id"], expected_revision=existing.get("revision", 0),
                              strength=existing["strength"], status="active" if active else existing["status"])
            saved.append(database.upsert_preference(**values))
    return saved
