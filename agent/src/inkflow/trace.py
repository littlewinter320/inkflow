from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .provider import ProviderResult
from .utils import atomic_write_text, json_dumps, utc_now


@dataclass(slots=True)
class TraceEvent:
    timestamp: str
    stage: str
    status: str
    summary: str
    details: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TraceRecorder:
    def __init__(self, project_root: str | Path, operation: str, trace_level: str = "full"):
        self.project_root = Path(project_root).resolve()
        stamp = utc_now().replace(":", "").replace("+00:00", "Z").replace("-", "")
        self.run_id = f"{stamp}-{operation}-{uuid.uuid4().hex[:8]}"
        self.operation = operation
        self.trace_level = trace_level
        self.run_dir = self.project_root / ".inkflow" / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.trace_path = self.run_dir / "trace.md"
        self.events: list[TraceEvent] = []
        self.record("run", "started", f"开始 {operation}")

    def record(
        self,
        stage: str,
        status: str,
        summary: str,
        details: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event = TraceEvent(
            timestamp=utc_now(),
            stage=stage,
            status=status,
            summary=summary,
            details=details,
            metadata=metadata or {},
        )
        self.events.append(event)
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        self._render()

    def record_model(self, stage: str, result: ProviderResult[Any], decision_summary: str) -> None:
        metadata = {
            "model": result.model,
            "response_id": result.response_id,
            "usage": result.usage,
        }
        self.record(stage, "completed", decision_summary, metadata=json.loads(json_dumps(metadata)))
        if self.trace_level == "full" and result.reasoning_content:
            self.record(
                f"{stage}.provider_reasoning",
                "info",
                "模型已完成内部推理；原始思维链不写入项目",
                metadata={"reasoning_characters": len(result.reasoning_content)},
            )

    def finish(self, status: str = "completed", summary: str = "运行完成") -> None:
        self.record("run", status, summary)

    def _render(self) -> None:
        lines = [
            f"# 墨流运行记录 · {self.operation}",
            "",
            f"> run_id: `{self.run_id}`  ",
            f"> trace_level: `{self.trace_level}`",
            "",
        ]
        for event in self.events:
            icon = {"completed": "✅", "started": "▶️", "failed": "❌", "warning": "⚠️"}.get(
                event.status, "ℹ️"
            )
            lines.extend(
                [
                    "<details>",
                    f"<summary>{icon} {event.stage} · {event.summary}</summary>",
                    "",
                    f"时间：{event.timestamp}",
                ]
            )
            if event.details:
                lines.extend(["", event.details])
            if event.metadata:
                lines.extend(["", "```json", json_dumps(event.metadata), "```"])
            lines.extend(["", "</details>", ""])
        atomic_write_text(self.trace_path, "\n".join(lines).rstrip() + "\n")
