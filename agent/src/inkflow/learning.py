from __future__ import annotations

import json
import math
import uuid
from typing import Any

from .errors import ValidationGateError
from .project import InkFlowProject
from .utils import atomic_write_text, content_hash, utc_now


class LearningService:
    """项目内的反馈记录与偏好排序。"""

    def __init__(self, project: InkFlowProject):
        self.project = project
        self.root = project.internal / "learning"
        self.root.mkdir(parents=True, exist_ok=True)

    def overview(self) -> dict[str, Any]:
        settings = self.project.db.get_metadata(
            "learning_settings", {"enabled": True, "allow_training_exports": False}
        )
        guidance = self.project.db.learning_guidance()
        exports = sorted((self.root / "exports").glob("*.jsonl")) if (self.root / "exports").exists() else []
        return {"settings": settings, "guidance": guidance, "exports": len(exports)}

    def export_dataset(self, *, include_prose: bool = False) -> dict[str, Any]:
        settings = self.project.db.get_metadata("learning_settings", {})
        if not settings.get("allow_training_exports"):
            raise ValidationGateError("请先在学习设置中允许导出本项目反馈。")
        rows: list[dict[str, Any]] = []
        events = self.project.db.list_learning_events(200)
        user_signal_origins = {"user_preference", "user_comparison", "user_revision"}
        eligible_events = [event for event in events if event.get("signal_origin") in user_signal_origins]
        for event in reversed(eligible_events):
            row = {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "signal_origin": event["signal_origin"],
                "chapter_no": event["chapter_no"],
                "chapter_version": event["chapter_version"],
                "feedback": {
                    key: value for key, value in event["payload"].items()
                    if key != "_signal_origin"
                },
                "created_at": event["created_at"],
            }
            if include_prose and event["chapter_no"]:
                chapter = self.project.db.get_chapter(int(event["chapter_no"]))
                if chapter and chapter.get("path"):
                    path = self.project.root / str(chapter["path"])
                    if path.is_file():
                        row["project_owned_prose"] = path.read_text(encoding="utf-8")
            rows.append(row)
        payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        export_id = f"dataset-{uuid.uuid4().hex[:12]}"
        path = self.root / "exports" / f"{export_id}.jsonl"
        atomic_write_text(path, payload)
        return {
            "export_id": export_id,
            "path": str(path),
            "records": len(rows),
            "excluded_non_user_events": len(events) - len(eligible_events),
            "signal_scope": "explicit_user_feedback_only",
            "includes_prose": include_prose,
            "sha256": content_hash(payload),
        }

    def train_preference_model(self) -> dict[str, Any]:
        with self.project.db.connect() as connection:
            rows = connection.execute("SELECT features_json FROM preference_pairs ORDER BY created_at").fetchall()
        totals: dict[str, float] = {}
        for row in rows:
            for key, value in json.loads(row["features_json"]).items():
                totals[key] = totals.get(key, 0.0) + float(value)
        norm = math.sqrt(sum(value * value for value in totals.values())) or 1.0
        weights = {key: round(value / norm, 6) for key, value in sorted(totals.items())}
        model = {"kind": "local_pairwise_linear", "pairs": len(rows), "weights": weights, "trained_at": utc_now()}
        path = self.root / "preference-model.json"
        atomic_write_text(path, json.dumps(model, ensure_ascii=False, indent=2) + "\n")
        return {**model, "path": str(path)}
