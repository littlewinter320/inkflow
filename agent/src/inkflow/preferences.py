"""Author defaults and project preferences share one audited storage contract."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from .config import user_settings_path
from .utils import content_hash, json_dumps, utc_now


active_preference_decisions = ContextVar("inkflow_preference_decisions", default=())
PREFERENCE_USE_CONTRACT = (
    "当前明确要求优先于本书偏好，本书优先于作者默认；先核适用范围，再比较要求。"
    "topic不同不代表没有语义冲突，topic相同也不代表适用于所有章节。"
    "普通偏好可以按本次内容有理由取舍，候选只作未确认反馈参考，不能变成硬门禁。"
    "确实不能同时满足的要求须按本次职责公开说明双方原句、来源和取舍原因。"
    "不同题材、人物、场景的差异不报冲突；同级硬要求互斥时等待用户选择，不派Writer反复改稿。"
    "取舍只限本任务，不暂停、改写或删除保存的习惯。"
)
PREFERENCE_CONFLICT_OUTPUT = (
    "确实冲突才填写preference_conflicts：sources逐项填输入里的preference_id/revision/逐字text引文，"
    "reason说明为何不能同时满足，普通偏好可用suggested_id建议本次选用；"
    "当前明确要求覆盖旧习惯时current_task_quote逐字来自当前用户要求。没有冲突留空。"
)


@contextmanager
def use_preference_decisions(decisions):
    token = active_preference_decisions.set(tuple(decisions))
    try:
        yield
    finally:
        active_preference_decisions.reset(token)


def resolve_preference_conflicts(conflicts, items, message):
    """Verify identities locally; semantic incompatibility belongs to the calling role."""
    known = {item["preference_id"]: item for item in items}
    decisions = []
    for conflict in conflicts:
        sources = conflict.sources
        ids = [source.preference_id for source in sources]
        valid = (len(ids) == 2 or bool(conflict.current_task_quote.strip())) and len(set(ids)) == len(ids) and all(
            source.preference_id in known
            and bool(source.quote.strip())
            and int(known[source.preference_id].get("revision", 0)) == source.revision
            and source.quote.strip() in known[source.preference_id]["text"]
            for source in sources)
        current = conflict.current_task_quote.strip()
        decision = {"preference_ids": ids, "source_quotes": [source.quote for source in sources],
                    "revisions": [source.revision for source in sources], "reason": conflict.reason,
                    "current_task_quote": current, "selected_id": "", "status": "stale"}
        if not valid or (current and current not in message):
            decision["reason"] = "冲突来源或当前要求未匹配原句与修订号；需重新核对。" + conflict.reason
        else:
            candidates = [known[item_id] for item_id in ids]
            rank = lambda item: (item.get("status") != "candidate", item.get("level", "project") == "project")
            highest = max(rank(item) for item in candidates)
            preferred = [item for item in candidates if rank(item) == highest]
            if current:
                chosen = next((item_id for item_id in ids if current == "本次采用偏好 " + item_id
                    or re.fullmatch(r"我来回答刚才的问题：\n1\. [\s\S]+\n我的回答：本次采用偏好 "
                        + re.escape(item_id) + r"\n请结合这些答案继续理解原来的目标；如果此前已经明确要求执行且信息足够，就继续原任务，否则先总结你理解到的方案。", current)), "current_task")
                decision.update(status="resolved", selected_id=chosen)
            elif len(preferred) == 1:
                decision.update(status="resolved", selected_id=preferred[0]["preference_id"])
            elif sum(item.get("strength") == "hard" for item in preferred) > 1:
                decision["status"] = "needs_choice"
            else:
                hard = [item for item in preferred if item.get("strength") == "hard"]
                preferred = hard or preferred
                chosen = next((item for item in preferred if item["preference_id"] == conflict.suggested_id), None)
                if chosen is None:
                    chosen = max(preferred, key=lambda item: (item.get("strength") == "hard", item.get("updated_at", ""), item["preference_id"]))
                decision.update(status="resolved", selected_id=chosen["preference_id"])
        decisions.append(decision)
    return decisions


def apply_preference_decisions(items, *, scope_filtered=False):
    """Task-local projection, never a change to stored preferences or their fingerprint."""
    excluded = set()
    for decision in active_preference_decisions.get():
        if decision["status"] != "resolved":
            continue
        known = {item["preference_id"]: item for item in items}
        if scope_filtered and any(item_id not in known for item_id in decision["preference_ids"]):
            continue  # This pair does not jointly apply to the current chapter or scene.
        if any(item_id not in known or int(known[item_id].get("revision", 0)) != revision
               for item_id, revision in zip(decision["preference_ids"], decision["revisions"])):
            from .errors import ValidationGateError
            raise ValidationGateError("本任务的偏好取舍来源已变化，请按当前习惯重新核对；未沿用旧选择。")
        if not scope_filtered and len({known[item_id]["scope"] for item_id in decision["preference_ids"]}) > 1:
            continue  # Defer a scoped override until the task's applicability filter.
        excluded.update(item_id for item_id in decision["preference_ids"] if item_id != decision["selected_id"])
        if decision["selected_id"] in known:
            excluded.discard(decision["selected_id"])
    return [item for item in items if item["preference_id"] not in excluded]


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
    defaults = [item for item in defaults if item["preference_id"] not in disabled and
                not any(item.get("topic") and item["topic"] == other.get("topic")
                        and (other["scope"] == "project" or other["scope"] == item["scope"])
                        for other in local)]
    # Author habits are defaults, never additional hard gates on a new book.
    return local + [item | {"strength": "weak"} for item in defaults]


def preference_candidates(database):
    with author_connection() as connection:
        defaults = list_items(connection, active_only=False, level="author")
    disabled = set(database.get_metadata("author_preferences_disabled", []))
    return [item | {"strength": "weak"} for item in database.list_preferences(active_only=False) + defaults
            if item["status"] == "candidate" and item["preference_id"] not in disabled]


def preference_prompt(database=None):
    if database is None:
        with author_connection() as connection:
            items = list_items(connection, level="author")
    else:
        items = effective_preferences(database) + preference_candidates(database)
    return "\n作者默认习惯与本书偏好（非正史）：" + PREFERENCE_USE_CONTRACT + "\n" + json_dumps([
        {key: item.get(key) for key in ("preference_id", "text", "scope", "level", "strength", "topic", "revision", "status", "source_quote")}
        for item in items])


def applicable_preferences(items, task, card):
    haystack = (task + "\n" + json.dumps(card, ensure_ascii=False)).casefold()
    result = []
    for item in items:
        kind, _, target = item["scope"].partition(":")
        if kind == "project" or (kind == "chapter" and target == str(card.get("chapter_no"))) or (
                kind != "chapter" and target and target.casefold() in haystack):
            result.append(item)
    result = apply_preference_decisions(result, scope_filtered=True)
    topics = {item.get("topic") for item in result if item.get("level", "project") == "project"
              and item.get("status", "active") == "active" and item.get("topic")}
    return [item for item in result if not (item.get("level") == "author"
            and item.get("status", "active") == "active" and item.get("topic") in topics)]


def preference_section(database):
    from .schemas import ContextSection
    items = apply_preference_decisions(effective_preferences(database) + preference_candidates(database))
    return ContextSection(key="author-preferences", title="作者习惯与本书偏好（当前指令优先）",
                          content=PREFERENCE_USE_CONTRACT + "\n" + json_dumps([
                              {key: item.get(key) for key in ("preference_id", "text", "scope", "level", "strength", "topic", "revision", "status", "source_quote")}
                              for item in items]),
                          source_ids=[item["preference_id"] for item in items], cache_scope="book")


def capture_observations(database, observations, message, run_id):
    saved = []
    for observation in observations:
        quote = observation.source_quote.strip()
        if not quote or quote not in message:
            continue
        author_explicit = bool(re.search(r"跨书|所有小说|所有书|默认习惯|全局|每本书", quote))
        project_explicit = bool(re.search(r"这本书|本书|以后|记住|一直|长期", quote))
        level = "project" if observation.level == "task" else observation.level
        active = observation.explicit and (author_explicit if level == "author" else project_explicit)
        if observation.level == "task":
            active = False
        if level == "author" and not author_explicit:
            level = "project"
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
        if not active and database.get_metadata("learning_settings", {}).get("enabled", True) is False:
            continue
        # An inferred observation must never demote an already confirmed habit.
        if level == "author":
            with author_connection() as connection:
                known = list_items(connection, active_only=False, level=level)
                existing = next((item for item in known if item["text"] == observation.text and item["scope"] == values["scope"]), None)
                if observation.preference_id:
                    existing = next((item for item in known if item["preference_id"] == observation.preference_id
                        and item["scope"] == values["scope"] and item.get("revision", 0) == observation.expected_revision), None)
                    if existing is None:
                        continue
                    if not active:
                        values["text"] = existing["text"]
                if existing:
                    if existing.get("source_ref") == values["source_ref"] and existing.get("source_quote") == quote:
                        continue
                    if not active and existing["status"] in {"paused", "deleted"}:
                        continue
                    values.update(preference_id=existing["preference_id"], expected_revision=existing.get("revision", 0),
                                  strength=existing["strength"], status="active" if active else existing["status"])
                    if not active:
                        values["topic"] = existing.get("topic", "")
                saved.append(change_item(connection, level=level, **values))
        else:
            known = database.list_preferences(active_only=False)
            existing = next((item for item in known
                             if item["text"] == observation.text and item["scope"] == values["scope"]), None)
            if observation.preference_id:
                existing = next((item for item in known if item["preference_id"] == observation.preference_id
                    and item["scope"] == values["scope"] and item.get("revision", 0) == observation.expected_revision), None)
                if existing is None:
                    continue
                if not active:
                    values["text"] = existing["text"]
            if existing:
                if existing.get("source_ref") == values["source_ref"] and existing.get("source_quote") == quote:
                    continue
                if not active and existing["status"] in {"paused", "deleted"}:
                    continue
                values.update(preference_id=existing["preference_id"], expected_revision=existing.get("revision", 0),
                              strength=existing["strength"], status="active" if active else existing["status"])
                if not active:
                    values["topic"] = existing.get("topic", "")
            saved.append(database.upsert_preference(**values))
    return saved
