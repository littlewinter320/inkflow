from __future__ import annotations

from typing import Any

from .project import InkFlowProject


class LearningService:
    """项目内的反馈记录与偏好排序。"""

    def __init__(self, project: InkFlowProject):
        self.project = project

    def overview(self) -> dict[str, Any]:
        saved_settings = self.project.db.get_metadata("learning_settings", {"enabled": True})
        settings = {"enabled": bool(saved_settings.get("enabled", True))}
        guidance = self.project.db.learning_guidance()
        return {"settings": settings, "guidance": guidance}
