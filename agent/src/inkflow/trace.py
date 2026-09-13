from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .provider import ProviderResult
from .utils import atomic_write_text, json_dumps, utc_now


def recent_trace_runs(project_root: str | Path, limit: int = 12) -> list[dict[str, Any]]:
    """读取最近的可公开运行记录，供客户端展示调用过程和可打开文件。"""

    root = Path(project_root).resolve()
    runs_root = root / ".inkflow" / "runs"
    if not runs_root.is_dir():
        return []
    run_dirs = sorted(
        (item for item in runs_root.iterdir() if item.is_dir()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[: max(1, min(limit, 50))]
    output: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        events_path = run_dir / "events.jsonl"
        if not events_path.is_file():
            continue
        events: list[dict[str, Any]] = []
        try:
            raw_events = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in raw_events:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            references = _trace_file_references(root, metadata)
            event["metadata"] = metadata
            event["references"] = references
            events.append(event)
        if not events:
            continue
        trace_path = run_dir / "trace.md"
        trace_reference = _file_reference(root, trace_path, "完整运行记录")
        output.append(
            {
                "run_id": run_dir.name,
                "operation": _operation_from_run_id(run_dir.name),
                "status": str(events[-1].get("status") or "unknown"),
                "summary": str(events[-1].get("summary") or ""),
                "started_at": str(events[0].get("timestamp") or ""),
                "finished_at": str(events[-1].get("timestamp") or ""),
                "events": events,
                "trace_reference": trace_reference,
            }
        )
    return output


def _operation_from_run_id(run_id: str) -> str:
    parts = run_id.split("-")
    return "-".join(parts[1:-1]) if len(parts) > 2 else run_id


def _trace_file_references(root: Path, value: Any, label: str = "") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (key == "path" or key.endswith("_path")):
                reference = _file_reference(root, Path(item), key.replace("_", " "))
                if reference:
                    found.append(reference)
            elif isinstance(item, (dict, list)):
                found.extend(_trace_file_references(root, item, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_trace_file_references(root, item, label))
    unique: dict[str, dict[str, Any]] = {}
    for item in found:
        unique[str(item["absolute_path"]).casefold()] = item
    return list(unique.values())


def _file_reference(root: Path, path: Path, label: str) -> dict[str, Any] | None:
    candidate = path if path.is_absolute() else root / path
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return {
        "label": label or resolved.name,
        "absolute_path": str(resolved),
        "relative_path": resolved.relative_to(root).as_posix(),
        "exists": resolved.is_file(),
    }


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

    def record_model_started(
        self,
        stage: str,
        *,
        model: str,
        agent_role: str,
        max_tokens: int,
        timeout_seconds: float | None = None,
        thinking: bool | None = None,
    ) -> None:
        """Record a public model-call boundary before waiting for the provider.

        A long JSON request used to leave the trace at ``context.build``.  This
        event makes the wait observable without persisting private reasoning or
        prompt contents.
        """

        metadata: dict[str, Any] = {
            "model": model,
            "agent_role": agent_role,
            "max_tokens": int(max_tokens),
        }
        if timeout_seconds is not None:
            metadata["timeout_seconds"] = float(timeout_seconds)
        if thinking is not None:
            metadata["thinking"] = bool(thinking)
        self.record(stage, "started", "已提交模型请求，等待结构化结果", metadata=metadata)

    def record_model_failed(
        self,
        stage: str,
        exc: BaseException,
        *,
        model: str | None = None,
        agent_role: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {}
        if model:
            metadata["model"] = model
        if agent_role:
            metadata["agent_role"] = agent_role
        self.record(
            stage,
            "failed",
            "模型请求未完成；未写入新的正文或正史",
            _redact_error(str(exc)),
            metadata=metadata,
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


def _redact_error(value: str) -> str:
    """Keep provider diagnostics useful while removing credential-like text."""

    text = value[:2_000]
    text = re.sub(r"(?i)(bearer\s+|api[_ -]?key\s*[:=]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)sk-[A-Za-z0-9_-]{8,}", "[已隐藏密钥]", text)
    return text or "未知错误"
