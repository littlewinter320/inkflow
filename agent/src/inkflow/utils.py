from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_RECOVERY_TOKEN_LIMIT = 40_000


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def workflow_failure_reason(result: Any) -> str:
    """Inspect workflow envelopes, not arbitrary model data or earlier findings."""
    if not isinstance(result, dict):
        return ""
    if result.get("gate"):
        return str(result["gate"])
    resumable_boundary = result.get("resumable") is True and result.get("status") == "interrupted"
    if not resumable_boundary and (
        result.get("status") in {"failed", "gate_stop", "needs_revision", "interrupted"}
        or result.get("stop_reason")
    ):
        return str(result.get("stop_reason") or result.get("detail") or result.get("summary") or "任务未完成")
    nested = workflow_failure_reason(result.get("result"))
    if nested:
        return nested
    steps = result.get("steps")
    if not isinstance(steps, list):
        return ""
    return next((reason for step in steps if (reason := workflow_failure_reason(step))), "")


def workflow_result_status(result: Any) -> str:
    """Classify explicit workflow outcomes without guessing from prose."""
    if not isinstance(result, dict):
        return "completed"
    status = result.get("status")
    if result.get("post_commit_status") == "waiting_condition":
        return "waiting_condition"
    if result.get("verdict") in {"unknown", "patch", "replan", "revise", "insufficient_context"}:
        return "waiting_condition"
    if status == "needs_input":
        return "waiting_user"
    if status in {"waiting_user", "waiting_condition"}:
        return str(status)
    if result.get("resumable") is True and status == "interrupted":
        return "waiting_condition"

    steps = result.get("steps")
    children = [result.get("result")]
    if isinstance(steps, list):
        children.extend(steps)
    children = [item for item in children if isinstance(item, dict)]
    child_statuses = [workflow_result_status(item) for item in children]
    if "failed" in child_statuses or workflow_failure_reason(result):
        return "failed"
    if "waiting_user" in child_statuses:
        return "waiting_user"
    if "waiting_condition" in child_statuses:
        return "waiting_condition"
    if result.get("needs_clarification") is True or result.get("questions"):
        return "waiting_user"
    return "completed"


def content_hash(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def effective_character_count(content: str) -> int:
    body = re.sub(r"\A\s*#{1,6}[^\n]*(?:\n|$)", "", content, count=1)
    return len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", body))


def project_source_revision(project_root: str | Path, internal: str | Path | None = None) -> str:
    """Return a cheap revision token for files that can affect a Context Packet."""

    root = Path(project_root)
    internal_path = Path(internal) if internal is not None else root / ".inkflow"
    candidates: set[Path] = set()
    for relative in ("BOOK.md", "OUTLINE.md", "STORY_DETAIL.md", "PLAN.md", "STATE.md", "DIALOGUE.md"):
        candidates.add(root / relative)
    for name in ("project.json", "inkflow.db", "studio.db"):
        candidates.add(internal_path / name)
    for pattern in (
        "chapters/**/*.md",
        "reviews/**/*.md",
        "references/**/*.md",
        ".inkflow/references/features/**/*.json",
    ):
        candidates.update(root.glob(pattern))

    entries: list[str] = []
    for path in sorted(candidates):
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_file():
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = path.name
            entries.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
    return content_hash("\n".join(entries))[:24]


def json_dumps(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=False)


def atomic_write_text(path: str | Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def atomic_write_bytes(path: str | Path, content: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def atomic_write_json(path: str | Path, value: Any) -> Path:
    return atomic_write_text(path, json_dumps(value) + "\n")


def read_text_fallback(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def estimate_tokens(text: str) -> int:
    """无需额外 tokenizer 的保守估计，中文约 1.3～1.8 字/token。"""

    han = len(re.findall(r"[\u3400-\u9fff]", text))
    other = max(0, len(text) - han)
    return max(1, int(han / 1.45 + other / 3.6))


def safe_filename(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" ._")
    return cleaned[:100] or fallback


def strip_json_fence(content: str) -> str:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()
