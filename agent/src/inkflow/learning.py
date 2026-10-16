from __future__ import annotations

from typing import Any

from .project import InkFlowProject
from .preferences import preference_identity


class LearningService:
    """项目内的反馈记录与偏好排序。"""

    def __init__(self, project: InkFlowProject):
        self.project = project

    def overview(self) -> dict[str, Any]:
        saved_settings = self.project.db.get_metadata("learning_settings", {"enabled": True})
        settings = {"enabled": bool(saved_settings.get("enabled", True))}
        guidance = self.project.db.learning_guidance()
        return {"settings": settings, "guidance": guidance}

    def preference_scores(self, items: list[dict[str, Any]]) -> dict[str, float]:
        """Bounded tie-breaks for applicable references, never approval or correctness."""
        identities = {item["preference_id"]: preference_identity(item) for item in items
                      if item.get("status") in {"active", "candidate"}}
        totals: dict[str, float] = {}
        seen = set()
        for event in self.project.db.list_learning_events(200):
            payload = event["payload"]
            key = payload.get("signal_key")
            identity = payload.get("preference_id")
            if (event["signal_origin"] != "user_comparison" or payload.get("kind") != "preference_feedback"
                    or not key or key in seen or identity not in identities or not payload.get("source_quote")
                    or payload.get("preference_identity") != identities.get(identity)):
                continue
            reward = {"liked": 1.0, "corrective": 0.5, "disliked": -1.0}.get(payload.get("feedback_kind"))
            if reward is None:
                continue
            seen.add(key)
            totals[identity] = totals.get(identity, 0.0) + reward
        return {identity: max(-1.0, min(1.0, value / 3.0)) for identity, value in totals.items()}
