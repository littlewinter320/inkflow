"""Offline check, run explicitly with PYTHONPATH=agent/src; no model or real user data."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json

from inkflow.database import ProjectDatabase
from inkflow.preferences import (author_connection, change_item, effective_preferences,
    applicable_preferences, capture_observations, preference_candidates,
    resolve_preference_conflicts, apply_preference_decisions, use_preference_decisions)
from inkflow.schemas import PreferenceObservation, PreferenceConflict, ContextPacket, ContextSection
from inkflow.review_verifier import packet_sources


def check():
    with TemporaryDirectory() as directory, patch("inkflow.preferences.user_settings_path",
            return_value=Path(directory) / "settings.json"):
        db = ProjectDatabase(Path(directory) / "book.sqlite3")
        with author_connection() as connection:
            author = change_item(connection, level="author", text="平时节奏舒缓", topic="节奏")
        local = db.upsert_preference(text="本书第十二章节奏紧凑", topic="节奏", scope="chapter:12")
        pool = effective_preferences(db)
        assert author["preference_id"] in {item["preference_id"] for item in applicable_preferences(pool, "写第十三章", {"chapter_no": 13})}
        assert author["preference_id"] not in {item["preference_id"] for item in applicable_preferences(pool, "写第十二章", {"chapter_no": 12})}
        scoped = [{"status": "resolved", "selected_id": local["preference_id"],
            "preference_ids": [local["preference_id"], author["preference_id"]],
            "revisions": [local["revision"], author["revision"]]}]
        with use_preference_decisions(scoped):
            scoped_pool = apply_preference_decisions(pool)
            assert author["preference_id"] in {item["preference_id"] for item in applicable_preferences(scoped_pool, "写第十三章", {"chapter_no": 13})}
        observation = PreferenceObservation(text="喜欢对白承担信息", source_quote="这次对白这样写我更喜欢", level="task")
        capture_observations(db, [observation], observation.source_quote, "one-run")
        capture_observations(db, [observation], observation.source_quote, "one-run")
        assert len(preference_candidates(db)) == 1
        assert len(db.list_preferences(active_only=False)) == 2
        db.set_metadata("learning_settings", {"enabled": False})
        other = PreferenceObservation(text="喜欢动作表现情绪", source_quote="这段动作不错", level="task")
        assert capture_observations(db, [other], other.source_quote, "two-run") == []
        first = db.upsert_preference(text="对白只许书面语", strength="hard")
        second = db.upsert_preference(text="对白只许口语", strength="hard")
        conflict = PreferenceConflict(sources=[{"preference_id": item["preference_id"],
            "revision": item["revision"], "quote": item["text"]} for item in (first, second)], reason="同一段对白不能同时仅用两种语体")
        decisions = resolve_preference_conflicts([conflict], effective_preferences(db), "写当前章")
        assert decisions[0]["status"] == "needs_choice"
        conflict = conflict.model_copy(update={"current_task_quote": "这次用口语"})
        decisions = resolve_preference_conflicts([conflict], effective_preferences(db), "这次用口语")
        with use_preference_decisions(decisions):
            used = apply_preference_decisions(effective_preferences(db) + preference_candidates(db))
            assert not {first["preference_id"], second["preference_id"]}.intersection(item["preference_id"] for item in used)
        choice = "本次采用偏好 " + second["preference_id"]
        chosen = resolve_preference_conflicts([conflict.model_copy(update={"current_task_quote": choice})],
            effective_preferences(db), choice)
        assert chosen[0]["selected_id"] == second["preference_id"]
        assert all(item["status"] == "active" for item in db.list_preferences() if item["preference_id"] in {first["preference_id"], second["preference_id"]})
        stale = conflict.model_copy(update={"sources": [source.model_copy(update={"revision": 999}) for source in conflict.sources]})
        assert resolve_preference_conflicts([stale], pool, "这次用口语")[0]["status"] == "stale"
        packet = ContextPacket(project_id="offline", chapter_no=13, task="核对引用身份", estimated_tokens=1,
            sections=[ContextSection(key="F", title="分层混合检索结果", source_ids=["one", "two", "omitted"],
                content=json.dumps({"自适应结果": [{"source_id": "one", "body": "甲以为铁盒已销毁"},
                    {"source_id": "two", "body": "铁盒仍在板房"}, {"source_id": "omitted", "title": "内容已被预算裁掉"}]}, ensure_ascii=False))])
        sources = packet_sources(packet)
        assert "铁盒仍在板房" not in sources["one"] and "omitted" not in sources


if __name__ == "__main__":
    check()
    print("Preference feedback, scope, conflict and task-only use check passed")
