from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

from .errors import ValidationGateError
from .project import InkFlowProject
from .utils import atomic_write_text, content_hash, utc_now


class LearningService:
    """项目内的可解释学习与训练数据准备；不会自行上传或启动昂贵训练。"""

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
        manifests = sorted((self.root / "training").glob("*.json")) if (self.root / "training").exists() else []
        return {"settings": settings, "guidance": guidance, "exports": len(exports), "training_manifests": len(manifests)}

    def export_dataset(self, *, include_prose: bool = False) -> dict[str, Any]:
        settings = self.project.db.get_metadata("learning_settings", {})
        if not settings.get("allow_training_exports"):
            raise ValidationGateError("请先在学习设置中明确允许导出本项目训练数据。")
        rows: list[dict[str, Any]] = []
        for event in reversed(self.project.db.list_learning_events(200)):
            row = {
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "chapter_no": event["chapter_no"],
                "chapter_version": event["chapter_version"],
                "feedback": event["payload"],
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
        return {"export_id": export_id, "path": str(path), "records": len(rows), "includes_prose": include_prose, "sha256": content_hash(payload)}

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

    def prepare_training(self, *, export_id: str, base_model_path: str, method: str) -> dict[str, Any]:
        if method not in {"lora", "dpo"}:
            raise ValueError("训练方式只能是 lora 或 dpo")
        dataset = self.root / "exports" / f"{export_id}.jsonl"
        if not dataset.is_file():
            raise ValueError("训练数据导出不存在")
        model_path = Path(base_model_path).expanduser().resolve()
        if not model_path.exists():
            raise ValueError("本地基础模型路径不存在")
        dataset_hash = content_hash(dataset.read_text(encoding="utf-8"))
        token = f"TRAIN-{method.upper()}-{dataset_hash[:12]}"
        manifest = {
            "job_id": f"training-{uuid.uuid4().hex[:12]}",
            "method": method,
            "dataset": str(dataset),
            "dataset_hash": dataset_hash,
            "base_model_path": str(model_path),
            "status": "awaiting_explicit_confirmation",
            "confirmation_token": token,
            "notice": "该步骤只完成本地训练准备。实际训练会占用显卡、磁盘和较长时间，必须再次明确确认后由训练运行器启动。",
            "created_at": utc_now(),
        }
        path = self.root / "training" / f"{manifest['job_id']}.json"
        atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return {**manifest, "path": str(path)}
