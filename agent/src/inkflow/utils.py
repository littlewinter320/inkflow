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
    """读取工作流的当前真实原因，保留作者交接边界，不读取任意模型资料。"""
    if not isinstance(result, dict):
        return ""
    placeholders = {"任务未完成", "本次操作未完成", "本次未完成", "尚未完成"}
    gate = str(result["gate"]) if result.get("gate") else ""
    if gate and gate.rstrip("。.!！ ") not in placeholders:
        return gate
    # An explicit author handoff is a control boundary, not a failed recovery node.
    # Dashboard status is a data object; tuple membership also accepts non-enum values safely.
    if result.get("status") in ("waiting_user", "needs_input"):
        return gate
    resumable_boundary = result.get("resumable") is True and result.get("status") == "interrupted"
    failed = not resumable_boundary and (
        result.get("status") in ("failed", "gate_stop", "needs_revision", "interrupted")
        or result.get("stop_reason") or gate
    )
    reason = next((value.strip() for key in ("stop_reason", "detail", "reason", "summary")
        if isinstance(value := result.get(key), str) and value.strip()
        and value.strip().rstrip("。.!！ ") not in placeholders), "") if failed else ""
    if reason:
        return reason
    # 外层只有“未完成”时继续找停止节点，避免把其实际缺项覆盖成空泛状态。
    nested = workflow_failure_reason(result.get("result"))
    if nested:
        return nested
    steps = result.get("steps")
    if isinstance(steps, list):
        nested = next((reason for step in steps if (reason := workflow_failure_reason(step))), "")
        if nested:
            return nested
    return gate or ("任务未完成" if failed else "")


def workflow_result_status(result: Any) -> str:
    """Classify explicit workflow outcomes without guessing from prose."""
    if not isinstance(result, dict):
        return "completed"
    status = result.get("status")
    if result.get("post_commit_status") == "waiting_condition":
        return "waiting_condition"
    if result.get("verdict") in ("unknown", "patch", "replan", "revise", "insufficient_context"):
        return "waiting_condition"
    if status == "needs_input":
        return "waiting_user"
    if status == "needs_evidence":
        return "waiting_condition"
    if status in ("waiting_user", "waiting_condition"):
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


def review_result_summary(result: dict[str, Any]) -> str:
    """把结构化审核结果归为结论、待办和下一步；原报告保留完整证据，不靠截断原话总结。"""
    number, verdict = result.get("chapter_no"), result.get("verdict")
    title = f"第 {number} 章" if type(number) is int and number > 0 else "本章"
    conclusion = {"pass": "审核已通过。", "patch": "有需要修补的地方。",
                  "replan": "有规划问题需要核对。", "unknown": "审核还没完成。"}.get(verdict, "审核结果还需确认。")
    lines = [title + conclusion]
    findings = [item for item in result.get("findings", []) if isinstance(item, dict)]
    confirmed = [item for item in findings if item.get("severity") in {"major", "blocking"}
                 and item.get("verification_status") == "anchored" and item.get("semantic_status") == "supported"]
    if confirmed:
        lines.append(f"需要修改：有 {len(confirmed)} 项严重问题已找到原文依据，具体位置和改法在审核页。")
    elif verdict == "pass":
        lines.append("检查结果：没有保留必须修改的严重问题。")
    recovery = result.get("evidence_recovery") or {}
    roles = recovery.get("roles", {}) if isinstance(recovery, dict) else {}
    names = {"editor": "编辑核对", "reviewer": "专项核对", "memory_keeper": "记忆交接"}
    pending = []
    for role, detail in roles.items() if isinstance(roles, dict) else []:
        if not isinstance(detail, dict):
            continue
        gaps = detail.get("remaining_gaps") if "remaining_gaps" in detail else detail.get("gaps")
        gaps = list(dict.fromkeys(str(gap) for gap in gaps)) if isinstance(gaps, list) else []
        error = str(detail.get("error") or "")
        if not gaps and not error and detail.get("status") not in {"budget_exhausted", "context_budget_exhausted", "repair_failed", "narrow_repair_unavailable"}:
            continue
        topics = []
        gap_text = "；".join(str(gap) for gap in gaps)
        for word, label in (("关系依据", "事件之间的关系"), ("附加证据", "附加引用"), ("引文", "原句引用"), ("来源", "对照资料")):
            if word in gap_text and label not in topics:
                topics.append(label)
        reason = "、".join(topics) + "还没核实" if topics else "还有检查未完成"
        counts = [(label, sum(gap.startswith(prefixes) for gap in gaps)) for label, prefixes in (
            ("设定对照", ("设定《",)), ("伏笔依据", ("承诺 ",)), ("事件关系", ("事件 ",)),
            ("审查指控", ("指控依据待核：",)),
            ("创作说明", ("说明第", "伏笔交接 ", "Writer意图")))]
        if any(count for _, count in counts):
            # 缺口按实际内容显示，不把Editor负责的设定/记忆一概说成文字表达问题。
            reason = "、".join(f"{label}{count}项" for label, count in counts if count) + "尚未核实"
            other = len(gaps) - sum(count for _, count in counts)
            if other:
                reason += f"，另有{other}项待核"
        # 只翻译已有的具体故障；未知失败不猜成断网，也不把未回报用量当零。
        if "incomplete chunked read" in error or "peer closed connection" in error:
            reason += "；补核时连接提前断开，结果没收完整"
        elif detail.get("status") == "narrow_repair_unavailable":
            reason += "；目前不能安全地只补这一部分，已保留断点"
        elif detail.get("status") == "budget_exhausted":
            reason += "；原任务的补核次数已用完"
        elif detail.get("status") == "context_budget_exhausted":
            reason += "；本次资料容量不足"
        elif error:
            reason += "；本次补核没有完成，具体原因保留在任务记录"
        pending.append(f"{names.get(role, '相关检查')}：{reason}。")
    if pending:
        lines.append("还差什么：\n" + "\n".join(pending))
    elif verdict != "pass":
        lines.append("待处理：请在审核页查看尚未通过的项目；现有正文和报告都已保留。")
    optional = sum(item.get("severity") == "minor" and item.get("semantic_status") != "contradicted" for item in findings)
    if optional:
        lines.append(f"可选建议：另有 {optional} 项轻微意见，可展开查看，不会仅凭这些意见自动改稿。")
    lines.append("下一步：按原任务完成记忆交接和正式提交，拿到提交回执后才算全部完成。" if verdict == "pass"
                 else "下一步：从上述未完成步骤继续，先复用仍然有效的结果；新增请求仍受原任务次数和费用限制。")
    return "\n\n".join(lines)


def workflow_result_summary(result: Any) -> str:
    """从实际返回值提取公开答复，递归最后的结果，不把路由意图或已调用步骤当完成。"""
    seen: set[int] = set()

    def visit(value: Any) -> str:
        """只沿 result/steps 读取交付结果；未知和待处理状态不能借用早先步骤宣称成功。"""
        if not isinstance(value, dict) or id(value) in seen or len(seen) >= 64:
            return ""
        seen.add(id(value))
        status = value.get("status")
        if status in ("unknown", "unavailable"):
            return "本次处理结果尚未确认，请先核对任务记录，再从原断点继续。"
        failure = workflow_failure_reason(value)
        if failure:
            return failure
        if workflow_result_status(value) in ("waiting_user", "waiting_condition", "failed"):
            for key in ("reply", "reason", "gate", "message", "summary", "next_action"):
                if isinstance(text := value.get(key), str) and text.strip():
                    return text.strip()
            return "本次仍有必要条件待处理，请查看原任务的具体原因和下一步。"
        # accepted 是引擎真实提交回执；仅有 completed、pass 或 memory.accept 名称都不够。
        if status == "accepted" and type(value.get("chapter_no")) is int and value["chapter_no"] > 0:
            warnings = "；".join(str(item) for item in value["warnings"] if item) if isinstance(value.get("warnings"), list) else ""
            return (f"第 {value['chapter_no']} 章已通过审核并收进正文，相关记忆已保存。"
                + (f"另有待处理项：{warnings}" if warnings else "本次操作已完成。"))
        if status == "planned":
            revision = value.get("revision_no")
            title = f"第 {revision} 版规划已正式生效。" if type(revision) is int and revision > 0 else "章节规划已更新。"
            window = value.get("future_range")
            scope = (f"本次后续安排：第 {window[0]}～{window[1]} 章。"
                     if isinstance(window, list) and len(window) == 2
                     and all(type(number) is int and number > 0 for number in window) and window[0] <= window[1] else "")
            return "\n\n".join(item for item in (title, scope, "可以打开完整规划阅读具体安排，再按当前规划继续写作。") if item)
        for key in ("reply", "gate", "message", "summary"):
            if isinstance(text := value.get(key), str) and text.strip():
                return text.strip()
        nested = visit(value.get("result"))
        if nested:
            return nested
        steps = value.get("steps")
        if isinstance(steps, list):
            for step in reversed(steps):
                if isinstance(step, dict) and not step.get("skipped") and (nested := visit(step)):
                    return nested
        # 外层的下一步不能盖过内层已经返回的交付结果。
        if isinstance(text := value.get("next_action"), str) and text.strip():
            return text.strip()
        return ""

    return visit(result)


def restored_completed_dialogue(entry: dict[str, Any], task: dict[str, Any], process: dict[str, Any], *,
                               project_root: str | Path, conversation: str,
                               task_database_path: str | Path | None = None) -> str:
    """只读补回旧成功占位；必须同项目、会话、运行、原话，并有真实引擎提交完成证据。"""
    run_id = entry.get("run_id")
    params = task.get("params")
    if (not isinstance(run_id, str) or not run_id or not isinstance(params, dict)
            or task.get("run_id") != run_id or process.get("runId") != run_id
            or task.get("method") != "conversation.send" or task.get("status") != "completed"
            or process.get("status") != "completed" or params.get("message") != entry.get("user")
            or str(params.get("conversation_id") or "main") != conversation):
        return ""
    if not Path(project_root).is_absolute():
        return ""
    current_root = os.path.normcase(os.path.normpath(str(project_root)))
    # 旧任务白名单不保存 project_root；项目归属由实际读取该行的 StudioDB 单独证明，不能补造旧参数。
    if task_database_path is not None:
        database_path = Path(task_database_path)
        expected_path = Path(project_root) / ".inkflow" / "studio.db"
        if (not database_path.is_absolute()
                or os.path.normcase(os.path.normpath(str(database_path))) != os.path.normcase(os.path.normpath(str(expected_path)))):
            return ""
    elif "project_root" not in params:
        return ""  # 没有旧路径也没有可信数据库归属时，继续保留原回复，不能猜测项目。
    if "project_root" in params:
        saved_root = params["project_root"]
        if (not isinstance(saved_root, str) or not Path(saved_root).is_absolute()
                or os.path.normcase(os.path.normpath(saved_root)) != current_root):
            return ""  # 旧参数若明确指向其他项目，可信数据库参数也不能覆盖这个冲突。
    steps = process.get("steps")
    if not isinstance(steps, list):
        return ""
    events = [item for item in steps if isinstance(item, dict) and item.get("run_id") == run_id]
    saved_reply = entry.get("assistant")
    generic = {"工作流返回了可查看结果。", "受限工作流已返回结果", "终端自然语言请求处理完成"}
    # 只认本 run 的精确意图占位，已有实质回复即使不够漂亮也不能改写。
    if saved_reply not in generic and not any(item.get("type") == "workflow.planned"
            and item.get("summary") == saved_reply for item in events):
        return ""
    tickets = [item["task_ticket"] for item in events if item.get("type") in {"controller.routed", "workflow.planned"}
               and isinstance(item.get("task_ticket"), dict)]
    numbers = {item.get("chapter_no") for item in tickets if type(item.get("chapter_no")) is int}
    if (len(numbers) != 1 or not tickets or any(item.get("chapter_no") not in numbers
            or item.get("end_chapter_no") not in (None, item.get("chapter_no")) for item in tickets)):
        return ""
    # 计划只证明本次范围；提交事件与引擎实际收尾事件同时存在，才证明已经接受。
    accepted_events = [item for item in events if item.get("stage") == "run" and item.get("summary") in
        {"章节已接受并进入正史", "章节已进入正史，部分附属记录待补齐"}]
    if (not any(item.get("stage") == "memory.commit" and item.get("status") == "completed" for item in events)
            or not accepted_events or accepted_events[-1].get("status") != "completed"
            or accepted_events[-1].get("summary") != "章节已接受并进入正史"):
        return ""
    return workflow_result_summary({"status": "accepted", "chapter_no": next(iter(numbers))})


def content_hash(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def source_display_name(project: Any, source: str) -> str:
    """Describe a user-facing reference without changing its stored identity."""
    normalized = source.replace("\\", "/")
    if normalized.startswith("story-setting:"):
        from .story_settings import StorySettingsService
        service = StorySettingsService(project)
        record = service._get(normalized.split(":", 1)[1], "record")
        collection = service._get(record["collection_id"], "collection") if record else None
        return f"{collection['name'] if collection else '设定参考'}：{record['title']}" if record else "需要定位的设定参考条目"
    name = Path(normalized).name
    known = {"BOOK.md": "本书设定", "OUTLINE.md": "全书大纲", "STORY_DETAIL.md": "卷细纲",
        "RECENT_PLAN.md": "近期章节规划", "PLAN.md": "旧版近期规划", "STATE.md": "正史与故事状态", "DIALOGUE.md": "对话记录"}
    if name in known:
        return known[name]
    match = re.search(r"chapter_(\d+)", name)
    if match:
        kind = "审查报告" if normalized.startswith("reviews/") else "草稿" if "draft" in name else "正文"
        return f"第{int(match[1])}章{kind}"
    if normalized.startswith("planning/history/"):
        from .planning_cleanup import planning_document_label
        return planning_document_label(normalized)
    return "后台核对记录" if ".inkflow/" in normalized or re.search(r"[a-f0-9]{16,}", name) else name


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
