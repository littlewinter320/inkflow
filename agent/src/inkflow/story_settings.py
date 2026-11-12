"""Customizable story references; deliberately separate from canonical memory.

Collections and records reuse the desktop's bible_entries table. Their revision
history is committed in the same row, so a failed update cannot lose its audit.
Source quotes are located by code; semantic approval remains a review decision.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
import uuid

from .errors import ProjectError
from .project_lock import project_write_lock_sync
from .utils import content_hash, json_dumps, utc_now


READ_ROLES = {"coordinator", "writer", "editor", "reviewer", "memory_keeper"}
WRITE_ROLES = READ_ROLES
REVIEW_ROLES = {"editor", "reviewer", "memory_keeper"}
MAINTENANCE_ROLES = REVIEW_ROLES | {"coordinator"}
SCHEMA = "story-settings-v1"
MARKER = "story_settings_type"


def restore_setting_quote_sources(candidate: dict[str, Any], read_sources: dict[str, str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """只用双引号显示差异寻找唯一连续原句，再复制真实原文；不放宽最终逐字门禁。"""
    restored, proofs = deepcopy(candidate), []
    display = str.maketrans({'“': '"', '”': '"'})
    for index, ref in enumerate(restored.get("evidence_refs", [])):
        if ref.get("source_kind") != "canonical" or type(ref.get("chapter_no")) is not int:
            continue
        source_id = f"chapter:{ref['chapter_no']:05d}"
        text, quote = read_sources.get(source_id), ref.get("quote")
        if not isinstance(text, str) or not isinstance(quote, str) or not quote or quote in text:
            continue
        # 显示映射仅用于搜索位置，任何字词、空白或其他标点差异都不能因此获得通过。
        search_text, search_quote = text.translate(display), quote.translate(display)
        start = search_text.find(search_quote)
        if start < 0 or search_text.find(search_quote, start + 1) >= 0:
            continue
        actual = text[start:start + len(quote)]
        if actual == quote or any(a != b and not {a, b} <= {'"', '“', '”'} for a, b in zip(actual, quote)):
            continue
        ref["quote"] = actual
        proofs.append({"path": ["records", 0, "evidence_refs", index, "quote"], "source_id": source_id,
            "source_hash": content_hash(text), "start_offset": start, "end_offset": start + len(actual),
            "original_quote": quote, "source_quote": actual, "method": "unique_contiguous_double_quote_display"})
    return restored, proofs


def load_setting_record_resume(root: Path, resume: dict[str, Any], *, change: dict[str, Any], instruction: str,
        user_message: str, task_settings: dict[str, Any], role: str, source_fingerprint: str,
        story_source_fingerprint: str, read_sources: dict[str, str],
        review_current_settings: bool = False, current_collection: dict[str, Any] | None = None,
        reconstruct_story_fingerprint: Callable[[str], str] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """核原任务、原提示身份和已保存单候选后只读恢复，不重发模型或修改原运行审计。"""
    runs = (root / ".inkflow" / "runs").resolve()

    def run_path(name: Any) -> Path:
        """仅允许显式指向本书runs中的一级运行目录，拒绝越界与链接逃逸。"""
        if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
            raise ProjectError("原设定运行身份无效，未查找其他任务代替。")
        path = (runs / name).resolve()
        if path.parent != runs:
            raise ProjectError("原设定运行位置越界，未读取候选。")
        return path

    def event_metadata(path: Path, stage: str, *, output_only: bool = False) -> dict[str, Any]:
        """来源事件必须唯一，缺失或重复均停止恢复，不能选最近记录猜测绑定。"""
        events = [json.loads(line) for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        matches = [{**event.get("metadata", {}), "_event_timestamp":event.get("timestamp")} for event in events if event.get("stage") == stage
                   and (not output_only or event.get("metadata", {}).get("output_path"))]
        if len(matches) != 1:
            raise ProjectError("原设定来源事件缺失或不唯一，未重用候选。")
        return matches[0]

    record_run, terminal_run = run_path(resume.get("trace_id")), run_path(resume.get("terminal_run_id"))
    if not (terminal_run / "events.jsonl").is_file():
        # 桌面runtime.run_id与TraceRecorder目录ID不同。旧checkpoint仅存前者时，
        # 只按原设置结果、完整意图及冻结scope关联唯一终端回执，不按时间或最近目录猜测。
        matches = []
        for path in runs.glob("*-terminal-session-*"):
            try:
                path = run_path(path.name)
                entry = event_metadata(path, "session.intent")
                result = event_metadata(path, "session.result_checkpoint")
            except (OSError, ValueError, ProjectError):
                continue
            old_scope = entry.get("task_settings", {})
            if (entry.get("intent") == resume.get("intent")
                    and result.get("response", {}).get("result", {}).get("trace_id") == record_run.name
                    and all(task_settings.get(key) and old_scope.get(key) == task_settings[key]
                            for key in ("task_id", "novel_id", "snapshot_hash"))):
                matches.append(path)
        if len(matches) != 1:
            raise ProjectError("原桌面任务未关联唯一终端结果回执，未猜测或采用最近候选。")
        terminal_run = run_path(matches[0].name)
    original = event_metadata(terminal_run, "session.intent")
    old_intent = original.get("intent", {})
    identity = {"change": change, "instruction": instruction, "user_message": user_message}
    if (old_intent != resume.get("intent") or old_intent.get("action") != "story_settings_manage"
            or old_intent.get("authorization") != "approved" or old_intent.get("setting_change") != change
            or old_intent.get("operation_instruction") != instruction or old_intent.get("user_message") != user_message):
        raise ProjectError("原设定请求身份或范围不一致，未将旧候选套入新请求。")
    checkpoint = event_metadata(terminal_run, "session.result_checkpoint")
    bound = checkpoint.get("response", {}).get("result", {})
    if bound.get("trace_id") != record_run.name or bound.get("records") or bound.get("status") != "failed":
        raise ProjectError("原终端结果未绑定这份未保存候选，未重放已经完成的保存。")
    source = event_metadata(record_run, "settings.record.sources")
    reconstructed_story = None
    if review_current_settings and source.get("story_source_fingerprint") != story_source_fingerprint:
        # 规划指纹本身也含custom_settings。只替回原设定哈希重算整体，必须精确命中原指纹；
        # 事实、正文、规划、偏好等任何另一项变化仍会失败，不能直接忽略整个规划指纹。
        old_settings = source.get("source_fingerprint")
        if isinstance(old_settings, str) and old_settings and reconstruct_story_fingerprint is not None:
            reconstructed_story = reconstruct_story_fingerprint(old_settings)
    if review_current_settings:
        from datetime import datetime
        # 新许可只重核当前设定环境；目标合集若曾变更职责或权限，不能借补核恢复原写入授权。
        if not current_collection or not source.get("_event_timestamp"):
            raise ProjectError("当前来源补核缺少原合集身份与时间依据，未发送请求。")
        boundary = datetime.fromisoformat(source["_event_timestamp"])
        if any(datetime.fromisoformat(event["at"]) > boundary for event in current_collection.get("history", [])):
            raise ProjectError("原目标合集职责或权限修订已经变化，未将原候选套入新合集。")
    for snapshot in (original.get("task_settings", {}), checkpoint.get("task_settings", {}), source.get("task_settings", {})):
        if any(not task_settings.get(key) or snapshot.get(key) != task_settings[key] for key in ("task_id", "novel_id", "snapshot_hash")):
            raise ProjectError("原设定任务或冻结配置不一致，未用当前配置替换。")
    if (source.get("operation") != "record_supplement" or source.get("evidence_mode") != "replace"
            or any(source.get(key) != change.get(key) for key in ("collection_id", "record_id", "expected_revision"))
            or (source.get("source_fingerprint") != source_fingerprint and not review_current_settings)
            or (source.get("story_source_fingerprint") != story_source_fingerprint
                and (reconstructed_story is None or reconstructed_story != source.get("story_source_fingerprint")))
            or source.get("read_sources") != {key: content_hash(value) for key, value in read_sources.items()}
            or source.get("request_identity", content_hash(json_dumps(identity))) != content_hash(json_dumps(identity))):
        raise ProjectError("原设定集合、修订号或实际读取来源已经变化，未恢复旧候选。")
    model = event_metadata(record_run, "settings.record", output_only=True)
    if model.get("canonical_agent_role", model.get("agent_role")) != role:
        raise ProjectError("原候选责任角色与冻结任务不一致，未由其他角色代签。")
    output = Path(str(model.get("output_path", ""))).resolve()
    if output.parent != record_run or not output.name.endswith(".output.json"):
        raise ProjectError("原候选文件未绑定原运行目录，未读取其他结果。")
    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)
    records = payload.get("records")
    if (not isinstance(records, list) or len(records) != 1
            or any(records[0].get(key) != change.get(key) for key in ("collection_id", "record_id", "expected_revision"))):
        raise ProjectError("原候选不是指定修订的一条完整记录，未改写或合并其他结果。")
    return payload, {"origin_trace_id": record_run.name, "origin_terminal_run_id": terminal_run.name,
        "output_path": str(output), "original_output_hash": content_hash(raw),
        "original_settings_fingerprint": source.get("source_fingerprint"),
        "current_settings_fingerprint": source_fingerprint,
        "original_story_source_fingerprint":source.get("story_source_fingerprint"),
        "current_story_source_fingerprint":story_source_fingerprint,
        "reconstructed_story_source_fingerprint":reconstructed_story,
        "requires_current_settings_review": review_current_settings,
        "request_identity": content_hash(json_dumps(identity))}


def setting_structured_sources(project, read_sources: dict[str, str]) -> dict[str, Any]:
    """Omit only proven duplicate projections; source texts and storage stay intact."""
    from types import SimpleNamespace
    from .planning_pipeline import PlanningNeedsAttention, load_active_planning, v2_execution_bundle
    from .project import render_book_brief

    brief = project.db.get_brief()
    plan = project.db.get_current_plan_bundle()
    notes = {"plan": "未来规划是假设，不是已发生正史。"}
    result = {"book_brief": brief.model_dump(mode="json"),
              "plan": plan.model_dump(mode="json") if plan else None, "source_notes": notes}
    if read_sources.get("BOOK.md") == render_book_brief(brief):
        result["book_brief"] = {"duplicate_projection_omitted": True, "source": "BOOK.md",
                                "source_hash": content_hash(read_sources["BOOK.md"])}
        notes["book_brief"] = "书籍契约与已装入的BOOK原文精确相同，内容只读取该原文。"
    else:
        notes["book_brief"] = "BOOK原文缺失或与数据库契约不完全相同，保留完整契约供对照，未省略差异。"
    if plan is None:
        notes["plan"] += " 数据库没有结构化规划。"
        return result
    try:
        # The loader's failure notification must not write pending work from a projection helper.
        view = SimpleNamespace(root=project.root, record_pending_work=lambda **_: None)
        active = load_active_planning(view)
        if active is None:
            notes["plan"] += " 未找到完整生效的三层规划证明，保留数据库规划全文，未省略。"
            return result
        manifest, outline, detail, window = active
        hashes = {name: manifest[key] for name, key in (
            ("OUTLINE.md", "outline_hash"), ("STORY_DETAIL.md", "volume_detail_hash"),
            ("RECENT_PLAN.md", "recent_plan_hash"))}
        if any(name not in read_sources or content_hash(read_sources[name]) != expected
               for name, expected in hashes.items()):
            notes["plan"] += " 已装入的规划原文与当前生效来源不一致，保留数据库规划全文，未省略差异。"
            return result
        expected = v2_execution_bundle(project, manifest, outline, detail, window)
        if plan.model_dump(mode="json") != expected.model_dump(mode="json"):
            notes["plan"] += " 数据库执行视图含不同于生效原文投影的内容，保留全文，未省略独有内容。"
            return result
        result["plan"] = {"duplicate_projection_omitted": True, "source": "planning/active-v2.json",
                          "revision_no": manifest.get("revision_no", 1), "trace_id": manifest["trace_id"],
                          "volume_no": manifest["volume_no"], "chapter_window": manifest["chapter_window"],
                          "source_hashes": hashes}
        notes["plan"] += " 数据库执行视图与生效三层规划精确相同，正文内容只读取已装入的三份原文。"
    except (PlanningNeedsAttention, OSError, ValueError, KeyError, TypeError, StopIteration):
        notes["plan"] += " 生效规划未能完整核验或发布尚未完成，保留数据库规划全文与原文，未省略差异。"
    return result


def _fields(*items: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [{"key": key, "label": label, "description": description, "default": ""}
            for key, label, description in items]


_TEMPLATES = [
    {"template_id": "character", "name": "人物设定", "category": "人物",
     "description": "人物身份、背景、变化、首次登场和最后出现；信念与客观状态分开。",
     "fields": _fields(("identity", "身份与定位", "主角、配角、反派等，由用户决定"),
        ("background", "背景", "经历与出身"), ("traits", "性格与动机", "目标和行为原因"),
        ("state", "当前状态", "身体、处境和已发生变化"), ("belief", "人物信念", "不自动当客观事实"),
        ("first_appearance", "首次登场", "章节号及是否仅被提及"),
        ("last_seen", "最后出现", "章节、场景与状态"), ("relationships", "关键关系", "关系及变化"))},
    {"template_id": "location", "name": "场景与地点", "category": "场景",
     "description": "地点描述、空间约束、出现章节及关键人物事件。",
     "fields": _fields(("description", "场景描述", "读者能感知的空间特征"),
        ("background", "背景", "地点历史"), ("layout", "位置与布局", "距离、出入口和空间限制"),
        ("first_appearance", "首次出现或提及", "来源章节"), ("last_seen", "最后出现", "来源章节"),
        ("characters", "关键人物", "与地点相关的人物"), ("events", "关键事件", "已发生与拟发生分开"))},
    {"template_id": "item", "name": "道具与物品", "category": "道具",
     "description": "特殊物品的用途、限制、持有人、位置和流转。",
     "fields": _fields(("description", "外观与来源", "可识别特征"), ("purpose", "用途", "作用"),
        ("limits", "限制与代价", "不能做到什么"), ("owner", "持有人", "信念与实有分开"),
        ("location", "当前去向", "已证实位置"), ("first_appearance", "首次出现", "来源章节"),
        ("changes", "流转与变化", "交接、损坏、消耗等及其原句"))},
    {"template_id": "world", "name": "世界与背景", "category": "世界",
     "description": "整体时代、地理、环境和世界运行前提。",
     "fields": _fields(("era", "时代", "时间背景"), ("geography", "地理", "区域和边界"),
        ("environment", "环境", "自然与社会状况"), ("premise", "基本前提", "已确定和未定分开"),
        ("limits", "世界限制", "故事不应随意突破的边界"), ("changes", "世界变化", "来源及生效范围"))},
    {"template_id": "faction", "name": "组织与势力", "category": "组织",
     "description": "组织目的、资源、成员、势力关系及状态。",
     "fields": _fields(("goal", "目标", "公开与真实目标分开"), ("background", "背景", "组织历史"),
        ("members", "关键成员", "身份及职能"), ("resources", "资源", "影响范围与能力"),
        ("relations", "势力关系", "结盟、敌对与传闻"), ("state", "当前状态", "已发生变化"),
        ("first_appearance", "首次提及", "来源章节"), ("last_seen", "最后涉及", "来源章节"))},
    {"template_id": "power", "name": "能力与力量体系", "category": "规则",
     "description": "能力表现、学习条件、等级、代价与例外。",
     "fields": _fields(("effect", "效果", "可做的事"), ("conditions", "使用条件", "资源与前置条件"),
        ("cost", "代价", "消耗与副作用"), ("levels", "层级", "等级及判断依据"),
        ("limits", "限制", "禁止或不可能事项"), ("exceptions", "例外", "例外成立的来源和范围"))},
    {"template_id": "timeline", "name": "时间线与事件", "category": "时间",
     "description": "事件顺序、持续时间、参与者和叙述时间。",
     "fields": _fields(("time", "发生时间", "明确时间或相对顺序"), ("duration", "持续时间", "未定时不编造"),
        ("participants", "参与者", "人物及行动"), ("event", "事件", "发生内容"),
        ("causes", "前因后果", "直接原因和后续影响"), ("narrative_time", "叙述时间", "回忆或现实"))},
    {"template_id": "relationship", "name": "人物关系", "category": "关系",
     "description": "关系双方、真实关系、各自认知和转折。",
     "fields": _fields(("parties", "关系双方", "关联人物"), ("actual", "已证实关系", "客观状态"),
        ("belief", "双方认知", "分别记录，不混为事实"), ("tension", "矛盾与期待", "未解决事项"),
        ("changes", "关系转折", "章节和原句"), ("last_seen", "最后变化", "当前状态来源"))},
    {"template_id": "species", "name": "种族与生物", "category": "生物",
     "description": "生物特征、栖息地、能力、弱点和个体差异。",
     "fields": _fields(("appearance", "外形", "识别特征"), ("habitat", "栖息地", "环境条件"),
        ("traits", "习性与能力", "共同特征"), ("limits", "弱点", "限制"),
        ("individuals", "代表个体", "个体差异"), ("first_appearance", "首次出现", "来源章节"))},
    {"template_id": "culture", "name": "文化与制度", "category": "社会",
     "description": "习俗、法律、信仰、阶层及地域差异。",
     "fields": _fields(("customs", "习俗", "日常约定"), ("institutions", "制度", "实际运作"),
        ("beliefs", "信仰", "人物相信和世界真实分开"), ("classes", "阶层", "身份与资源"),
        ("regions", "适用范围", "地域、时间与组织"), ("exceptions", "差异与例外", "有据的变化"))},
    {"template_id": "mystery", "name": "悬念与伏笔参考", "category": "伏笔",
     "description": "伏笔表现、读者所知、拟解释和关联原句；不替代正史伏笔。",
     "fields": _fields(("surface", "已出现内容", "正文展示的线索"), ("reader_knows", "读者已知", "无需作者说明的内容"),
        ("hidden", "未揭示部分", "设想不当作事实"), ("followup", "后续打算", "可调整假设"),
        ("payoff", "回收条件", "拟采用的方式"), ("related_threads", "关联伏笔", "正史伏笔 ID 或名字"))},
    {"template_id": "constraint", "name": "故事边界与约定", "category": "约束",
     "description": "用户对本书的约定、适用范围和允许例外。",
     "fields": _fields(("requirement", "要求", "用户明确表达"), ("scope", "适用范围", "章节、角色和任务"),
        ("nature", "要求性质", "硬约束或表达参考"), ("exceptions", "允许例外", "用户确认的范围"),
        ("reason", "目的", "希望得到的故事体验"), ("effective", "生效时间", "不自动改写历史正文"))},
]


def _text(value: Any, label: str, limit: int = 4000, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ProjectError(f"{label}须为{'非空' if required else ''}文字，最多 {limit} 字。")
    return value.strip()


def _revision(old: dict[str, Any] | None, expected: int | None) -> None:
    if old is None:
        if expected is not None and (type(expected) is not int or expected != 0):
            raise ProjectError("新条目不能带旧修订号。")
    elif type(expected) is not int or expected != old["revision"]:
        raise ProjectError("设定已变化，请刷新并按当前修订号保存；未覆盖新版。")


def _roles(values: Any, allowed: set[str], label: str) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(item, str) or item not in allowed for item in values):
        raise ProjectError(f"{label}须从允许角色中选择。")
    return sorted(set(values))


def has_setting_content(value: Any) -> bool:
    """Blank template fields are not a setting; zero and False can be meaningful."""
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(has_setting_content(item) for item in value.values())
    if isinstance(value, list):
        return any(has_setting_content(item) for item in value)
    return value is not None


def user_supplied_setting_values(text: str, values: dict[str, Any]) -> bool:
    """Only a literal user field object proves a Coordinator copied all values."""
    if not isinstance(values, dict) or not values:
        return False
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            supplied, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if supplied == values or isinstance(supplied, dict) and supplied.get("values") == values:
            return True
    return False


def setting_classification_issues(collection: dict[str, Any], values: dict[str, Any]) -> list[str]:
    if collection.get("template_id") != "world":
        return []
    changes = values.get("changes")
    if isinstance(changes, str) and re.search(
            r"(?:单章|每章|总字数|篇幅|字数).{0,20}[0-9一二三四五六七八九十百千万]+\s*字|"
            r"(?:第一卷|第1卷).{0,8}[0-9一二三四五六七八九十百]+\s*章|"
            r"(?:规划|预计|完结|全书).{0,20}[0-9一二三四五六七八九十百]+\s*[章卷]", changes):
        return ["世界变化中混入了章节数量或字数安排。这些属于本书写作要求，应对照当前规划核实；世界变化只记录故事里发生的变化。"]
    return []


def book_requirement_collection(brief: Any, book_text: str) -> dict[str, Any]:
    """Read-only projection: reuse setting readback without inventing stored records."""
    records = [{"record_id": "book-rule-" + content_hash(str(index) + rule)[:24],
        "revision": 1, "title": f"本书要求第{index}条", "values": {"requirement": rule},
        "epistemic_status": "user_constraint", "authority": "user_requirement",
        "status": "current" if rule in book_text else "stale_evidence",
        "evidence_refs": [{"source_kind": "planning", "path": "BOOK.md", "quote": rule,
            "source_hash": content_hash(book_text), "source_current": rule in book_text}]}
        for index, rule in enumerate(brief.user_rules, 1) if rule.strip()]
    return {"collection_id": "book-required-reading", "name": "本书必读要求", "category": "写作要求",
        "template_id": "constraint", "recording_allowed": False, "read_only": True,
        "instructions": "逐条阅读原话，按适用范围及当前要求核对。必读不等于把每项偏好都升级为硬约束；不适用也须说明原因。章节数量、字数和文风属于作者要求，世界法律与制度属于故事内规则，分别对照。",
        "records": records}


def collection_requires_recheck(collection: dict[str, Any], recorded_revision: int) -> bool:
    if recorded_revision == collection["revision"]:
        return False
    before = next((event.get("before") for event in collection.get("history", [])
                   if event.get("revision") == recorded_revision + 1), None)
    if before is None:
        return True
    return any(before.get(key) != collection.get(key) for key in
               ("instructions", "fields", "read_roles", "write_roles", "template_id"))


def setting_notice_reason(issues: list[dict[str, Any]], *, fallback: str) -> str:
    """把审核的具体分歧压成可行动短说明；原问题和原句仍留在记录中。"""
    reasons = [str(item.get("reason") or item.get("explanation") or item.get("summary") or item.get("message") or "").strip()
               for item in issues if isinstance(item, dict)]
    text = next((value for value in reasons if value), fallback)
    for raw, label in (("epistemic_status", "记录性质"), ("hypothesis", "设想"), ("objective", "客观记录"),
                       ("belief", "人物信念"), ("rumor", "传闻"), ("user_constraint", "作者约定")):
        text = text.replace(raw, label)
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= 260 else text[:259] + "…"


class _ReadOnlySettingsDatabase:
    """通知轮询只读两份现有数据库，不触发建库、迁移、项目恢复或模型调用。"""
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        """显式只读连接也须及时关闭，不能让周期通知长期占用SQLite句柄。"""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def get_chapter(self, chapter_no: int) -> dict[str, Any] | None:
        """通知只核正文身份，不读取章节内容或执行正式提交恢复。"""
        with self.connect() as connection:
            row = connection.execute("SELECT chapter_no,status,version,content_hash FROM chapters WHERE chapter_no=?", (chapter_no,)).fetchone()
        return dict(row) if row else None


def read_setting_maintenance(root: Path) -> dict[str, Any]:
    """轻量通知入口只投影待决定项目；完整设定、原句和审查链仍按需打开。"""
    from types import SimpleNamespace
    internal = root / ".inkflow"
    view = SimpleNamespace(root=root, internal=internal, db=_ReadOnlySettingsDatabase(internal / "inkflow.db"))
    service = StorySettingsService(view, read_only=True)
    collections = service.list_collections()
    candidates = service._handoff_candidates()
    service._attach_handoff_decisions(collections, candidates)
    return service.maintenance_status(collections, candidates=candidates)


class StorySettingsService:
    def __init__(self, project: Any, *, read_only: bool = False):
        """写入沿工作台服务，通知可选用显式只读连接，二者共享设定状态解释。"""
        # Local import avoids making studio's existing document helpers circular.
        from .studio import StudioDatabase
        self.project = project
        self.db = _ReadOnlySettingsDatabase(project.internal / "studio.db") if read_only else StudioDatabase(project.internal / "studio.db")

    @staticmethod
    def templates() -> list[dict[str, Any]]:
        return deepcopy(_TEMPLATES)

    def ensure_defaults(self) -> None:
        """Initialize the two user-requested foundations without touching existing records."""
        with project_write_lock_sync(self.project.root):
            for template_id, name in (("character", "人物设定"), ("world", "世界设定")):
                identity = "setting-base-" + template_id
                if self._get(identity, "collection") is not None:
                    continue
                template = next(item for item in _TEMPLATES if item["template_id"] == template_id)
                self._save(None, {MARKER: "collection", "schema": SCHEMA, "collection_id": identity,
                    "name": name, "category": template["category"], "template_id": template_id,
                    "instructions": template["description"], "fields": deepcopy(template["fields"]),
                    "read_roles": sorted(READ_ROLES), "write_roles": sorted(WRITE_ROLES),
                    "builtin": True, "auto_record": True, "status": "active"}, actor="user", action="initialize_base")

    def _all(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM bible_entries ORDER BY kind,name,entry_id").fetchall()
        result = []
        for row in rows:
            data = json.loads(row["data_json"])
            if not isinstance(data, dict) or data.get(MARKER) not in {"collection", "record"}:
                continue
            if not include_archived and row["status"] != "active":
                continue
            result.append({**data, "status": data.get("status", row["status"]),
                           "entry_status": row["status"], "updated_at": row["updated_at"]})
        return result

    def _get(self, item_id: str, item_type: str) -> dict[str, Any] | None:
        key = "collection_id" if item_type == "collection" else "record_id"
        return next((item for item in self._all(include_archived=True)
                     if item.get(MARKER) == item_type and item.get(key) == item_id), None)

    def list_collections(self, include_archived: bool = False, *, include_review_links: bool = False) -> list[dict[str, Any]]:
        items = self._all(include_archived=include_archived)
        if include_review_links:
            records = {item["record_id"]: item for item in items if item.get(MARKER) == "record"}
            collections = {item["collection_id"]: item for item in items if item.get(MARKER) == "collection"}
            cache: dict = {}
            for item in records.values():
                item["stored_status"] = item["status"]
                item["review_links"] = []
                collection = collections.get(item["collection_id"])
                live = self._context_record(item, collection, None, cache) if collection else None
                if live and item["status"] != "archived":
                    item["status"] = live["status"]
                    item["classification_issues"] = live.get("classification_issues", [])
            with self.project.db.connect() as connection:
                rows = connection.execute("""SELECT r.chapter_no,r.chapter_version,
                    json_extract(r.data_json,'$.source_hash') AS source_hash,
                    json_extract(r.data_json,'$.context_use_audit.setting_checks') AS checks,
                    c.version AS current_version,c.content_hash AS current_hash
                    FROM reviews r JOIN chapters c ON c.chapter_no=r.chapter_no
                    WHERE json_valid(r.data_json) AND json_extract(r.data_json,'$.context_use_audit.setting_checks') IS NOT NULL
                    ORDER BY r.id DESC LIMIT 80""").fetchall()
            for row in rows:
                for check in json.loads(row["checks"] or "[]"):
                    record = records.get(check.get("record_id"))
                    if record is not None and len(record["review_links"]) < 3:
                        record["review_links"].append({**check, "chapter_no": row["chapter_no"],
                            "current": check.get("record_revision") == record["revision"]
                                and record["status"] not in {"stale_evidence", "configuration_changed", "archived"}
                                and row["chapter_version"] == row["current_version"] and row["source_hash"] == row["current_hash"]})
        result = []
        for item in items:
            if item.get(MARKER) != "collection":
                continue
            records = [record for record in items if record.get(MARKER) == "record"
                       and record["collection_id"] == item["collection_id"]]
            names = [re.sub(r"\s+", "", record["title"]).casefold() for record in records if record["status"] != "archived"]
            for record in records:
                record["duplicate_name"] = record["status"] != "archived" and names.count(re.sub(r"\s+", "", record["title"]).casefold()) > 1
            result.append({**item, "records": records})
        if include_review_links:
            self._attach_handoff_decisions(result, self._handoff_candidates())
        return result

    @staticmethod
    def _attach_handoff_decisions(collections: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
        """把同版待处理附件叠到用户视图，原已核条目的保存状态和权威保持原样。"""
        visible = {record["record_id"]: record for item in collections for record in item["records"]}
        groups = {item["collection_id"]: item for item in collections}
        for candidate in candidates:
            collection = groups.get(candidate.get("collection_id"))
            if collection:
                # 对象不明确的提案也留在所属合集，不能因缺少记录编号从界面消失。
                collection.setdefault("pending_candidates", []).append(deepcopy(candidate))
            targets = candidate.get("target_records") or [{"record_id": candidate.get("record_id")}]
            for target in targets:
                record = visible.get(target.get("record_id"))
                if not record or record["status"] == "archived":
                    continue
                record.setdefault("stored_status", record["status"])
                shown = deepcopy(candidate)
                if target.get("revision") is not None and target["revision"] != record["revision"]:
                    shown["reason"] += " 当前记录又有更新，请按下面显示的现值重新决定。"
                    for diff in shown.get("differences", []):
                        diff["previous_before"] = diff.get("before")
                        diff["before"] = record.get(diff["field"]) if diff["field"] in {"title", "epistemic_status"} else record["values"].get(diff["field"])
                record.setdefault("pending_candidates", []).append(shown)
                if candidate["status"] == "needs_user":
                    # 修订号变化只会要求重新确认，不能自动隐藏尚未决定的分歧。
                    record["status"] = "needs_user"
                    review = deepcopy(record.get("review", {}))
                    review["issues"] = [*review.get("issues", []), {"reason": shown["reason"],
                        "differences": shown.get("differences", []), "source_chapter": candidate["source_chapter"]}]
                    record["review"] = review

    def records(self, collection_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        if self._get(collection_id, "collection") is None:
            raise ProjectError("设定合集不存在。")
        return [item for item in self._all(include_archived=include_archived)
                if item.get(MARKER) == "record" and item["collection_id"] == collection_id]

    def _save(self, old: dict[str, Any] | None, data: dict[str, Any], *, actor: str, action: str) -> dict[str, Any]:
        key = "collection_id" if data[MARKER] == "collection" else "record_id"
        ignored = {"revision", "history", "updated_at", "entry_status"}
        clean = {key: value for key, value in data.items() if key not in ignored}
        previous = {key: value for key, value in (old or {}).items() if key not in ignored}
        if old is not None and clean == previous:
            return {**old, "changed": False}
        now = utc_now()
        revision = int((old or {}).get("revision", 0)) + 1
        history = list((old or {}).get("history", []))
        history.append({"revision": revision, "actor": actor, "action": action, "at": now,
                        "before": previous or None})
        stored = {**clean, "revision": revision, "history": history}
        entry_status = "archived" if stored["status"] == "archived" else "active"
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data_json FROM bible_entries WHERE entry_id=?", (stored[key],)).fetchone()
            current = json.loads(row["data_json"]) if row else None
            if (current or {}).get("revision") != (old or {}).get("revision"):
                raise ProjectError("设定在保存期间已变化，未覆盖新版本。")
            connection.execute(
                """INSERT INTO bible_entries(entry_id,kind,name,aliases_json,data_json,status,updated_at)
                   VALUES (?,'lore',?,'[]',?,?,?) ON CONFLICT(entry_id) DO UPDATE SET
                   name=excluded.name,data_json=excluded.data_json,status=excluded.status,updated_at=excluded.updated_at""",
                (stored[key], stored.get("name", stored.get("title", "")), json_dumps(stored, indent=None), entry_status, now))
            connection.commit()
        return {**stored, "entry_status": entry_status, "updated_at": now, "changed": True}

    def save_collection(self, *, name: str, collection_id: str | None = None, template_id: str | None = None,
                        category: str = "", instructions: str | None = None, fields: list[dict[str, Any]] | None = None,
                        read_roles: list[str] | None = None, write_roles: list[str] | None = None,
                        expected_revision: int | None = None, actor: str = "user", auto_record: bool | None = None) -> dict[str, Any]:
        if actor != "user":
            raise ProjectError("设定合集的负责内容与权限由用户定义，模型不能自行扩大。")
        with project_write_lock_sync(self.project.root):
            old = self._get(collection_id, "collection") if collection_id else None
            if collection_id and old is None:
                raise ProjectError("设定合集不存在。")
            _revision(old, expected_revision)
            if auto_record is not None and type(auto_record) is not bool:
                raise ProjectError("自动记录须选择开启或关闭。")
            if old and old.get("builtin") and auto_record is False:
                raise ProjectError("人物与世界是基础设定，默认持续维护；可以修改具体记录说明。")
            template_id = template_id if template_id is not None else (old or {}).get("template_id", "")
            template = next((item for item in _TEMPLATES if item["template_id"] == template_id), None)
            if template_id and template is None:
                raise ProjectError("设定模板不存在。")
            selected = fields if fields is not None else (old or {}).get("fields", (template or {}).get("fields", []))
            if not isinstance(selected, list) or not 1 <= len(selected) <= 40:
                raise ProjectError("每份设定须定义 1 至 40 个记录字段。")
            checked_fields = []
            for field in selected:
                if not isinstance(field, dict):
                    raise ProjectError("设定字段格式不正确。")
                key = _text(field.get("key"), "字段键", 48, required=True)
                if not re.fullmatch(r"[^\s./\\]{1,48}", key) or key in {item["key"] for item in checked_fields}:
                    raise ProjectError("字段键不能重复、含空白或路径分隔符。")
                checked_fields.append({"key": key, "label": _text(field.get("label", key), "字段名称", 100, required=True),
                    "description": _text(field.get("description", ""), "字段说明", 1000),
                    "default": _text(field.get("default", ""), "字段预填", 2000)})
            data = {MARKER: "collection", "schema": SCHEMA,
                "collection_id": collection_id or f"setting-{uuid.uuid4().hex}",
                "name": _text(name, "设定名称", 120, required=True),
                "category": _text(category or (old or {}).get("category", (template or {}).get("category", "自定义")), "设定分类", 100),
                "template_id": template_id,
                "instructions": _text(instructions if instructions is not None else (old or {}).get("instructions", (template or {}).get("description", "")), "记录任务", 6000),
                "fields": checked_fields,
                "read_roles": _roles(read_roles if read_roles is not None else (old or {}).get("read_roles", sorted(READ_ROLES)), READ_ROLES, "读取角色"),
                "write_roles": _roles(write_roles if write_roles is not None else (old or {}).get("write_roles", sorted(WRITE_ROLES)), WRITE_ROLES, "记录角色"),
                "builtin": bool((old or {}).get("builtin")),
                "auto_record": auto_record if auto_record is not None else (old or {}).get("auto_record", True),
                "status": "active"}
            return self._save(old, data, actor=actor, action="configure_collection")

    def archive_collection(self, collection_id: str, expected_revision: int, actor: str = "user") -> dict[str, Any]:
        if actor != "user":
            raise ProjectError("只有用户可以移出整个设定合集。")
        with project_write_lock_sync(self.project.root):
            old = self._get(collection_id, "collection")
            if old is None:
                raise ProjectError("设定合集不存在。")
            if old.get("builtin"):
                raise ProjectError("人物与世界是基础设定，不能删除；可以编辑内容和记录说明。")
            _revision(old, expected_revision)
            return self._save(old, {**old, "status": "archived"}, actor=actor, action="archive_collection")

    def _source(self, evidence: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        kind = evidence.get("source_kind", evidence.get("kind", "canonical"))
        if not isinstance(kind, str):
            raise ProjectError("证据来源类型须为文字。")
        chapter_no = evidence.get("chapter_no", evidence.get("source_chapter"))
        if kind in {"canonical", "draft"}:
            if type(chapter_no) is not int or chapter_no < 1:
                raise ProjectError("正文证据须注明正整数章节号。")
            chapter = self.project.db.get_chapter(chapter_no)
            if kind == "draft" and chapter and chapter["status"] == "accepted":
                if evidence.get("source_hash") == chapter["content_hash"] and evidence.get("source_version") == chapter["version"]:
                    kind = "canonical"
                else:
                    raise ProjectError("已接受正文不能冒充未绑定版本的草稿引用。")
            if not chapter or (kind == "canonical" and chapter["status"] != "accepted") or (kind == "draft" and chapter["status"] != "draft"):
                raise ProjectError("证据章节状态不匹配；草稿不能冒充正史。")
            content = self.project.db.canonical_chapter_content(chapter_no) if kind == "canonical" else None
            path = self._project_path(str(chapter["path"]), require_file=content is None)
            if content is None:
                content = path.read_text(encoding="utf-8")
            if content_hash(content) != chapter["content_hash"]:
                raise ProjectError("证据正文与记录版本不一致，请先处理手动修改。")
            return content, {"source_kind": kind, "chapter_no": chapter_no,
                "path": path.relative_to(self.project.root).as_posix(), "source_version": chapter["version"],
                "source_hash": chapter["content_hash"], "authority": "accepted_text" if kind == "canonical" else "draft_reference"}
        if kind == "planning":
            path = self._project_path(str(evidence.get("path", "")))
            if path.parent != self.project.root or path.name not in {"BOOK.md", "PLAN.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"}:
                raise ProjectError("规划证据只读取当前设定与生效规划，不读取旧规划历史。")
            if path.name == "PLAN.md" and (self.project.root / "planning" / "active-v2.json").is_file():
                raise ProjectError("已有三层正式规划，旧PLAN投影不能作自动设定依据；请选择当前大纲、卷细纲或近期规划。")
            content = path.read_text(encoding="utf-8")
            if path.name in {"OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"} and (self.project.root / "planning" / "active-v2.json").is_file():
                from types import SimpleNamespace
                from .planning_pipeline import PlanningNeedsAttention, load_active_planning
                # Evidence reads must not publish or repair a partially updated planning bundle.
                try:
                    active = load_active_planning(SimpleNamespace(root=self.project.root, record_pending_work=lambda **_: None))
                except (PlanningNeedsAttention, OSError, ValueError, KeyError, TypeError) as exc:
                    raise ProjectError("规划版本暂时无法核对，原设定保留，未采用这份来源。") from exc
                hash_key = {"OUTLINE.md": "outline_hash", "STORY_DETAIL.md": "volume_detail_hash", "RECENT_PLAN.md": "recent_plan_hash"}[path.name]
                if active is None or active[0].get(hash_key) != content_hash(content):
                    raise ProjectError("规划尚未完整生效或版本已变化，暂不据此更新设定。")
            return content, {"source_kind": kind, "path": path.name, "source_hash": content_hash(content),
                             "authority": "creative_reference"}
        if kind == "writer_hook":
            artifact_id = _text(evidence.get("artifact_id"), "说明来源 ID", 100, required=True)
            with self.project.db.connect() as connection:
                row = connection.execute("SELECT * FROM agent_artifacts WHERE artifact_id=? AND artifact_type='writer_hook_note'",
                                         (artifact_id,)).fetchone()
            if row is None or row["status"] != "current":
                raise ProjectError("Writer 说明不是当前版本。")
            data = json.loads(row["data_json"])
            chapter = self.project.db.get_chapter(int(row["chapter_no"]))
            if not chapter or chapter["version"] != row["chapter_version"] or chapter["content_hash"] != data.get("content_hash"):
                raise ProjectError("Writer 说明与正文版本不一致。")
            def strings(value: Any) -> list[str]:
                if isinstance(value, str):
                    return [value]
                if isinstance(value, dict):
                    return [text for item in value.values() for text in strings(item)]
                if isinstance(value, list):
                    return [text for item in value for text in strings(item)]
                return []
            content = "\n".join(strings(data))
            return content, {"source_kind": kind, "artifact_id": artifact_id, "chapter_no": row["chapter_no"],
                "source_version": chapter["version"], "source_hash": content_hash(json_dumps(data, indent=None)), "body_hash": chapter["content_hash"],
                "authority": "writer_intent_only"}
        raise ProjectError("证据来源类型不受支持。")

    def _project_path(self, relative: str, *, require_file: bool = True) -> Path:
        value = Path(relative)
        if value.is_absolute() or ".." in value.parts:
            raise ProjectError("证据路径须位于当前小说项目中。")
        target = (self.project.root / value).resolve()
        if not target.is_relative_to(self.project.root) or (require_file and not target.is_file()):
            raise ProjectError("证据文件不存在或越出当前小说。")
        return target

    def _evidence(self, refs: Any) -> list[dict[str, Any]]:
        if not isinstance(refs, list) or len(refs) > 12:
            raise ProjectError("每条设定最多保留 12 处证据。")
        checked = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ProjectError("证据格式不正确。")
            quote = _text(ref.get("quote", ref.get("evidence", "")), "原文引用", 1200, required=True)
            content, source = self._source(ref)
            if quote not in content:
                raise ProjectError("设定引用未定位到来源原句；未保存无依据的引用。")
            if ref.get("source_hash") and ref["source_hash"] != source["source_hash"]:
                raise ProjectError("设定证据来源已变化，未沿用过期引用。")
            if ref.get("source_version") is not None and ref["source_version"] != source.get("source_version"):
                raise ProjectError("设定证据来源修订号已变化。")
            relation = ref.get("relation", "reference")
            if not isinstance(relation, str) or relation not in {"support", "counter", "belief", "reference"}:
                raise ProjectError("证据关系须为支持、反证、信念或参考。")
            checked.append({**source, "quote": quote, "relation": relation,
                            "start_offset": content.index(quote), "end_offset": content.index(quote) + len(quote),
                            "located": True, "semantic_support": "requires_review"})
        return checked

    def save_record(self, collection_id: str, *, title: str = "", name: str = "", record_id: str | None = None,
                    values: dict[str, Any], evidence_refs: list[dict[str, Any]] | None = None,
                    evidence: list[dict[str, Any]] | None = None, epistemic_status: str | None = None,
                    chapter_no: int | None = None, expected_revision: int | None = None,
                    actor: str = "user", operation: str = "new") -> dict[str, Any]:
        """Save a versioned reference candidate; maintenance permissions never confer approval or canon rights."""
        if actor not in WRITE_ROLES | {"user"} or operation not in {"new", "supplement", "correct"}:
            raise ProjectError("此角色或设定记录操作不被允许。")
        if chapter_no is not None and (type(chapter_no) is not int or chapter_no < 1):
            raise ProjectError("设定关联章节号须为正整数。")
        with project_write_lock_sync(self.project.root):
            collection = self._get(collection_id, "collection")
            if collection is None or collection["status"] != "active":
                raise ProjectError("设定合集不存在或已移出使用。")
            if actor != "user" and actor not in collection["write_roles"]:
                raise ProjectError("用户未允许此角色记录这份设定。")
            if actor != "user" and not collection.get("auto_record", True):
                raise ProjectError("这项设定已关闭自动记录，已有内容仍可查看；本次未自动补写。")
            old = self._get(record_id, "record") if record_id else None
            if actor in MAINTENANCE_ROLES and old is None and any(
                    re.sub(r"\s+", "", item["title"]).casefold() == re.sub(r"\s+", "", title or name).casefold()
                    for item in self.records(collection_id)):
                raise ProjectError("这份设定已有同名条目，请读取其编号和修订号后补充；不会再新建重复记录。")
            if record_id and old is None and not re.fullmatch(r"setting-record-[A-Za-z0-9_-]{1,100}", record_id):
                raise ProjectError("新增设定记录编号无效。")
            if record_id and ((old is None and operation != "new") or (old is not None and old["collection_id"] != collection_id)):
                raise ProjectError("设定记录不属于指定合集。")
            _revision(old, expected_revision)
            epistemic_status = epistemic_status if epistemic_status is not None else (old or {}).get("epistemic_status", "hypothesis")
            if not isinstance(epistemic_status, str) or epistemic_status not in {"hypothesis", "objective", "belief", "rumor", "user_constraint"}:
                raise ProjectError("设定性质须为设想、客观记录、信念、传闻或用户约定。")
            if not isinstance(values, dict) or not values or any(key not in {field["key"] for field in collection["fields"]} for key in values):
                raise ProjectError("记录内容须使用用户定义的字段。")
            if actor in MAINTENANCE_ROLES and old and operation == "supplement":
                # A partial correction may clear one field while retaining the record.
                values = {**old["values"], **values}
            if not has_setting_content(values):
                raise ProjectError("这条设定还没有填写内容。请至少填写一项；空条目不会保存或发起审查。")
            try:
                encoded = json.dumps(values, ensure_ascii=False, allow_nan=False)
                json.loads(encoded)
            except (TypeError, ValueError) as exc:
                raise ProjectError("记录内容必须是可保存的普通数据。") from exc
            if len(encoded) > 16000:
                raise ProjectError("单条设定记录最多 16000 字，请拆分条目。")
            raw_refs = evidence_refs if evidence_refs is not None else evidence
            refs = self._evidence(raw_refs if raw_refs is not None else (old or {}).get("evidence_refs", []))
            if actor in MAINTENANCE_ROLES:
                if not refs:
                    raise ProjectError("整理设定必须提供可定位的原文依据；对话想法先按用户约定或设想保存。")
                canon_reference = epistemic_status in {"objective", "belief", "rumor"} and all(ref["source_kind"] == "canonical" for ref in refs)
                book_requirement = epistemic_status == "user_constraint" and all(
                    ref["source_kind"] == "planning" and ref.get("path") == "BOOK.md" for ref in refs)
                planned_reference = epistemic_status == "hypothesis" and all(
                    ref["source_kind"] == "planning" and ref.get("path") in {"OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"} for ref in refs)
                if old is None and not (canon_reference or book_requirement or planned_reference):
                    raise ProjectError("首次整理须引用已接受正文、当前书籍要求，或将现行规划原句按未来设想记录；不能凭空补剧情。")
            if actor == "writer":
                epistemic_status = "hypothesis"
            if actor in MAINTENANCE_ROLES and old and operation == "supplement":
                combined = []
                for ref in [*old["evidence_refs"], *refs]:
                    if ref not in combined:
                        combined.append(ref)
                refs = self._evidence(combined)
            if actor != "user" and setting_classification_issues(collection, values):
                raise ProjectError(setting_classification_issues(collection, values)[0])
            data = {MARKER: "record", "schema": SCHEMA, "record_id": record_id or f"setting-record-{uuid.uuid4().hex}",
                "collection_id": collection_id, "title": _text(title or name or (old or {}).get("title", ""), "记录名称", 160, required=True),
                "values": deepcopy(values), "evidence_refs": refs, "epistemic_status": epistemic_status,
                "source_role": actor, "source_chapter": chapter_no if chapter_no is not None else (old or {}).get("source_chapter"), "operation": operation,
                "status": "pending_review", "authority": "reference_only", "review": {},
                "collection_revision": collection["revision"]}
            if old is not None:
                # Re-saving identical content must not manufacture an edit or clear approval.
                keys = ("title", "values", "evidence_refs", "epistemic_status", "source_chapter")
                if (all(old.get(key) == data.get(key) for key in keys) and old["status"] != "archived"
                        and not collection_requires_recheck(collection, old["collection_revision"])):
                    return {**old, "changed": False}
                if actor == "writer" and old["status"] in {"verified_reference", "user_kept_hypothesis"}:
                    raise ProjectError("Writer 不能改写已核对设定；请另建设想候选，由审查或记忆角色有据补充。")
            return self._save(old, data, actor=actor, action="record_" + operation)

    def archive_record(self, record_id: str, expected_revision: int, actor: str = "user") -> dict[str, Any]:
        if actor != "user":
            raise ProjectError("模型不能自行删除设定记录；可以提出纠正候选。")
        with project_write_lock_sync(self.project.root):
            old = self._get(record_id, "record")
            if old is None:
                raise ProjectError("设定记录不存在。")
            _revision(old, expected_revision)
            return self._save(old, {**old, "status": "archived"}, actor=actor, action="archive_record")

    def record_review(self, record_id: str, expected_revision: int, *, decision: str,
                      issues: list[dict[str, Any]] | None = None, actor: str = "editor",
                      source_fingerprint: str = "") -> dict[str, Any]:
        if actor not in REVIEW_ROLES or decision not in {"verified_reference", "needs_user", "needs_evidence"}:
            raise ProjectError("设定复核必须由审查或记忆责任角色给出明确结论。")
        if issues is None:
            issues = []
        if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
            raise ProjectError("设定复核问题格式或长度不正确。")
        try:
            if len(json.dumps(issues, ensure_ascii=False, allow_nan=False)) > 20000:
                raise ProjectError("设定复核问题格式或长度不正确。")
        except (TypeError, ValueError) as exc:
            raise ProjectError("设定复核问题须为普通数据。") from exc
        with project_write_lock_sync(self.project.root):
            if source_fingerprint and source_fingerprint != self.source_fingerprint():
                raise ProjectError("复核读取的设定或引用已变化，迟到结论未写回。")
            old = self._get(record_id, "record")
            if old is None or old["status"] == "archived":
                raise ProjectError("设定记录不存在或已移出使用。")
            _revision(old, expected_revision)
            collection = self._get(old["collection_id"], "collection")
            if not has_setting_content(old.get("values")):
                raise ProjectError("这条设定尚未填写，不能标记为核对通过；请先补充具体内容。")
            if collection is None or collection["status"] != "active" or actor not in collection["read_roles"]:
                raise ProjectError("此角色无权复核已移出或未授权读取的设定。")
            if collection_requires_recheck(collection, old["collection_revision"]) and not source_fingerprint:
                raise ProjectError("记录任务或字段已经调整，复核须绑定当前合集与来源指纹。")
            refs = self._evidence(old["evidence_refs"])
            if decision == "verified_reference" and setting_classification_issues(collection, old["values"]):
                raise ProjectError(setting_classification_issues(collection, old["values"])[0])
            if decision == "verified_reference" and old["epistemic_status"] in {"objective", "belief", "rumor"} and not refs:
                raise ProjectError("声称已有依据的设定仍缺原文，不能仅凭通过报告认证。")
            data = {**old, "status": decision, "collection_revision": collection["revision"],
                    "review": {"actor": actor, "decision": decision,
                    "issues": issues or [], "source_fingerprint": source_fingerprint,
                    "authority": "表达与来源参考；不是正史提交或用户同意"}}
            return self._save(old, data, actor=actor, action="review_record")

    def record_decision(self, record_id: str, expected_revision: int, *, choice: str | None = None,
                        decision: str | None = None, reason: str = "") -> dict[str, Any]:
        if choice and decision and choice != decision:
            raise ProjectError("设定决定含有不同选择，请保留一项明确选择。")
        choice = choice or decision
        reason = _text(reason, "决定理由", 4000)
        if choice not in {"keep_hypothesis", "archive"}:
            raise ProjectError("请明确选择保留为设想或移出使用。")
        with project_write_lock_sync(self.project.root):
            old = self._get(record_id, "record")
            if old is None:
                raise ProjectError("设定记录不存在。")
            _revision(old, expected_revision)
            return self._save(old, {**old, "status": "archived" if choice == "archive" else "user_kept_hypothesis",
                "epistemic_status": "hypothesis" if choice == "keep_hypothesis" else old["epistemic_status"],
                "user_decision": {"choice": choice, "reason": reason, "original_review": old.get("review", {}),
                                  "authority": "用户保留设想，不表示历史正文已改变"}}, actor="user", action="decide_record")

    def _live_refs(self, refs: list[dict[str, Any]], cache: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
        result = []
        cache = cache if cache is not None else {}
        for ref in refs:
            try:
                key = (ref["source_kind"], ref.get("chapter_no"), ref.get("path"), ref.get("artifact_id"))
                if key not in cache:
                    cache[key] = self._source(ref)
                content, source = cache[key]
                valid = (source["source_hash"] == ref["source_hash"] and ref["quote"] in content
                         and source.get("source_version") == ref.get("source_version"))
                result.append({**ref, "source_current": valid, "current_source_hash": source["source_hash"],
                               "current_source_kind": source["source_kind"]})
            except (OSError, ValueError, KeyError, ProjectError) as exc:
                result.append({**ref, "source_current": False, "source_problem": str(exc)})
        return result

    def source_fingerprint(self, *, items: list[dict[str, Any]] | None = None) -> str:
        """绑定设定及真实引用；恢复核验可对原历史快照求值，不改实际保存的记录。"""
        items = [{key: value for key, value in item.items() if key not in {"history", "updated_at"}}
                 for item in (deepcopy(items) if items is not None else self._all(include_archived=True))]
        cache: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
        for item in items:
            if item.get(MARKER) == "record":
                item["live_evidence"] = self._live_refs(item["evidence_refs"], cache)
        return content_hash(json_dumps(items, indent=None))

    def review_history_snapshot(self, cutoff: str, chapter_no: int) -> dict[str, Any] | None:
        """仅重建审查后发生的无语义设定维护；正文、性质、权限或原句变化一律不借此恢复。"""
        from datetime import datetime
        try:
            boundary = datetime.fromisoformat(cutoff)
            current = self._all(include_archived=True)
            previous, changes = [], []
            audit_fields = {"revision", "history", "updated_at", "entry_status", "source_role", "source_chapter", "operation", "review"}
            for item in current:
                events = [event for event in item.get("history", []) if datetime.fromisoformat(event["at"]) > boundary]
                if not events:
                    previous.append(deepcopy(item))
                    continue
                events.sort(key=lambda event: event["revision"])
                first = events[0]
                before = first.get("before")
                if (item.get(MARKER) != "record" or not isinstance(before, dict)
                        or [event["revision"] for event in events] != list(range(first["revision"], item["revision"] + 1))
                        or any(event.get("action") not in {"record_correct", "record_supplement", "review_record"} for event in events)
                        or item.get("status") != before.get("status")):
                    return None
                # 每个中间修订都须只改维护元数据，不能把改过内容又恢复的链冒充纯审核更新。
                states = [before, *[event["before"] for event in events[1:]], item]
                semantic = {key: value for key, value in before.items() if key not in audit_fields | {"status"}}
                for state in states:
                    # 手动保存会先进入pending_review，再由原设定审核返回；只容许这一中间态，最终结论必须与原来相同。
                    if (not isinstance(state, dict) or {key: value for key, value in state.items() if key not in audit_fields | {"status"}} != semantic
                            or state.get("status") not in {before.get("status"), "pending_review"}
                            or state.get("review") and {key: state["review"].get(key) for key in ("decision", "authority")}
                                != {key: before.get("review", {}).get(key) for key in ("decision", "authority")}):
                        return None
                    old_chapter, new_chapter = before.get("source_chapter"), state.get("source_chapter")
                    if (any(value is not None and type(value) is not int for value in (old_chapter, new_chapter))
                            or bool(old_chapter and old_chapter > chapter_no) != bool(new_chapter and new_chapter > chapter_no)
                            or old_chapter != new_chapter and old_chapter is not None and new_chapter is not None):
                        return None
                previous.append({**deepcopy(before), "revision": first["revision"] - 1,
                    "entry_status": "archived" if before["status"] == "archived" else "active"})
                changes.append({"record_id": item["record_id"], "old_revision": first["revision"] - 1,
                    "current_revision": item["revision"], "changed_audit_fields": [key for key in sorted(audit_fields)
                        if key not in {"history", "updated_at", "entry_status"} and before.get(key) != item.get(key)]})
            return {"items": previous, "changes": changes} if changes else None
        except (ValueError, TypeError, KeyError):
            # 缺时间、断历史或不认识的旧表示保持不可恢复，不用当前值补造原快照。
            return None

    def _context_record(self, record: dict[str, Any], collection: dict[str, Any],
                        chapter_no: int | None, cache: dict) -> dict[str, Any] | None:
        if chapter_no and record.get("source_chapter") and record["source_chapter"] > chapter_no:
            return None
        refs = self._live_refs(record["evidence_refs"], cache)
        if chapter_no and any(ref.get("chapter_no", 0) > chapter_no or
                (ref.get("chapter_no", 0) == chapter_no and
                 ref.get("current_source_kind", ref["source_kind"]) == "canonical") for ref in refs):
            return None
        item = {key: value for key, value in record.items()
                if key not in {"history", "updated_at", "entry_status"}}
        item["evidence_refs"] = refs
        if any(not ref["source_current"] for ref in refs):
            item["status"] = "stale_evidence"
        elif collection_requires_recheck(collection, record["collection_revision"]):
            item["status"] = "configuration_changed"
        item["classification_issues"] = setting_classification_issues(collection, record["values"])
        if item["classification_issues"] and item["status"] not in {"stale_evidence", "configuration_changed"}:
            item["status"] = "needs_classification"
        return item

    def retrieval_records(self, *, chapter_no: int, actor: str) -> list[dict[str, Any]]:
        """Expose the same permitted, time-bounded reference records to local retrieval."""
        if actor not in READ_ROLES:
            raise ProjectError("读取设定的角色不被允许。")
        result = []
        cache: dict = {}
        for collection in self.list_collections():
            if actor not in collection["read_roles"]:
                continue
            for record in collection["records"]:
                item = self._context_record(record, collection, chapter_no, cache)
                if item is not None:
                    result.append({**item, "collection_name": collection["name"],
                                   "authority": "reference_only"})
        return result

    def context_payload(self, chapter_no: int | None = None, query: str = "", actor: str = "writer", max_chars: int = 16000) -> dict[str, Any]:
        """Read live, permitted references, retaining recent changes when ordinary references exceed budget."""
        if actor not in READ_ROLES:
            raise ProjectError("读取设定的角色不被允许。")
        sections, coverage = [], []
        remaining = max(1000, min(int(max_chars), 64000))
        query = query.casefold()
        cache: dict = {}
        collections = sorted(self.list_collections(), key=lambda item: (
            not any(record.get("epistemic_status") == "user_constraint" for record in item["records"]),
            item["name"].casefold() not in query))
        for collection in collections:
            # Do not expose names or contents that this role cannot read.
            if actor not in collection["read_roles"]:
                continue
            audit = {"collection_id": collection["collection_id"], "name": collection["name"],
                     "selected_record_ids": [], "omitted": [], "empty_records": 0}
            coverage.append(audit)
            payload = {key: value for key, value in collection.items()
                       if key not in {"history", "records", "updated_at", "entry_status"}}
            payload["classification_path"] = [part.strip() for part in collection["category"].split("/") if part.strip()]
            payload["records"] = []
            payload["recording_allowed"] = collection.get("auto_record", True) and actor in collection["write_roles"]
            eligible = []
            for record in collection["records"]:
                if not has_setting_content(record.get("values")):
                    audit["empty_records"] += 1
                    continue
                item = self._context_record(record, collection, chapter_no, cache)
                if item is not None:
                    eligible.append(item)
            # New developments must not remain hidden behind old alphabetically ordered records.
            changed_at = {record["record_id"]: record.get("updated_at", "") for record in collection["records"]}
            eligible.sort(key=lambda item: changed_at.get(item["record_id"], ""), reverse=True)
            # User requirements must not lose their budget to ordinary references.
            eligible.sort(key=lambda item: (item.get("epistemic_status") != "user_constraint", item["title"].casefold() not in query))
            audit["available_record_count"] = len(eligible)
            # 索引保存全部可读适用记录，详细内容才按预算选择，避免第33条起无从申请补读。
            audit["record_index"] = [{"record_id": item["record_id"], "title": item["title"], "revision": item["revision"]}
                for item in eligible]
            for item in eligible:
                trial = {**payload, "records": [*payload["records"], item]}
                if len(json_dumps(trial, indent=None)) > remaining and item.get("epistemic_status") != "user_constraint":
                    audit["omitted"].append({"record_id": item["record_id"], "title": item["title"],
                        "reason": "超过本次资料预算", "required": item.get("epistemic_status") == "user_constraint"})
                    continue
                payload = trial
                audit["selected_record_ids"].append(item["record_id"])
            audit["status"] = "loaded" if payload["records"] else "not_loaded" if eligible else "empty"
            if not eligible:
                audit["reason"] = "本章范围内尚无具体内容；模板字段不算设定，也不算已审查。"
            text = json_dumps(payload, indent=None)
            if len(text) <= remaining or any(item.get("epistemic_status") == "user_constraint" for item in payload["records"]):
                sections.append(payload)
                remaining -= len(text)
        return {"authority": "参考与用户约定，不是已发生正史",
            "instructions": "模板只定义记录方式。空合集不宣称设定审查通过，也不凭空补写。逐条核对已装入记录的适用性、当前正文及规划；未装入不等于不存在。用户约定按当前明确要求优先，设想、信念和传闻不能当客观事实。相关条目缺失时报告具体缺口，复用同次输出提出有来源的设定候选；无关模板不用强行填满。",
            "coverage": coverage, "collections": sections}

    def context(self, chapter_no: int | None = None, query: str = "", actor: str = "writer", max_chars: int = 16000) -> str:
        return json_dumps(self.context_payload(chapter_no, query, actor, max_chars), indent=None)

    def prepare_maintenance_proposal(self, proposal: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """对齐已核候选与现有对象，只安全续接身份及未被改过的字段，不猜测内容。"""
        args = deepcopy(proposal)
        collection = self._get(str(args.get("collection_id") or ""), "collection")
        if (not collection or collection["status"] != "active" or actor not in MAINTENANCE_ROLES
                or actor not in collection["read_roles"] or actor not in collection["write_roles"]
                or not collection.get("auto_record", True)):
            raise ProjectError("这份设定尚未允许当前责任角色持续维护，原记录已保留。")
        # 同名只用于定位候选对象；连续原句、字段和性质仍走原保存/核验门禁。
        refs = self._evidence(args.get("evidence_refs", []))
        # 定位偏移、权威和语义待核标记属于存储审计，不是角色提案Schema字段。
        ref_keys = {"source_kind", "chapter_no", "path", "artifact_id", "quote", "relation", "source_hash", "source_version"}
        args["evidence_refs"] = [{key: value for key, value in ref.items() if key in ref_keys} for ref in refs]
        records = self.records(collection["collection_id"], include_archived=True)
        if args.get("record_id"):
            matches = [item for item in records if item["record_id"] == args["record_id"]]
        else:
            name = re.sub(r"\s+", "", str(args.get("title") or "")).casefold()
            matches = [item for item in records if item["status"] != "archived"
                       and re.sub(r"\s+", "", item["title"]).casefold() == name]
        targets = [{"record_id": item["record_id"], "revision": item["revision"]} for item in matches]
        result = {"proposal": args, "target_records": targets, "differences": []}
        if len(matches) > 1:
            return {**result, "status": "needs_user", "reason": "同一合集有多个同名条目，无法确定本次应补哪一条；请先核对对象。"}
        if not matches:
            if args.get("record_id"):
                raise ProjectError("原设定条目已不存在，不能把旧更新当成新对象创建。")
            return {**result, "status": "ready", "reason": "没有同名条目，将按已核原句建立新记录。"}
        current = matches[0]
        result["record"] = current
        if current["status"] == "archived":
            return {**result, "status": "superseded", "reason": "该条目已由作者移出使用，不恢复旧提案。"}
        supplied_revision = args.get("expected_revision") if args.get("record_id") else None
        if (current.get("user_decision") and supplied_revision != current["revision"]):
            return {**result, "status": "superseded", "reason": "作者已对这条设定作出决定，旧候选不再重放。"}
        if current["status"] in {"needs_user", "conflict"}:
            return {**result, "status": "needs_user", "reason": setting_notice_reason(
                current.get("review", {}).get("issues", []), fallback="这条设定仍有待作者决定的分歧，先保留原值和候选。")}
        if current.get("user_decision") and (args.get("epistemic_status") != current["epistemic_status"]
                or any(current["values"].get(key) != value for key, value in (args.get("values") or {}).items())):
            return {**result, "status": "needs_user", "reason": "作者已明确决定这条设定的用法，新候选不能自动替换该决定；请先查看原值与新依据。"}
        if supplied_revision != current["revision"] and current["status"] != "verified_reference":
            # 只可复用已核的其他字段；不能把当前尚未核对的新修订一并签成通过。
            return {**result, "status": "needs_evidence", "reason": "这条设定还有未核对的新修订，先完成当前记录核对，再合并本章补充。"}
        if collection_requires_recheck(collection, current["collection_revision"]):
            return {**result, "status": "needs_evidence", "reason": "合集字段或记录要求已经调整，须按当前要求核对候选。"}
        fields = {field["key"]: field["label"] for field in collection["fields"]}
        proposed = args.get("values") or {}
        if not has_setting_content(proposed) or any(key not in fields for key in proposed):
            raise ProjectError("候选没有可记录内容，或使用了这份合集之外的字段。")
        # 保存时的修订号可以因无关字段补充或审查记录而增加；只对照本提案实际触及的字段。
        before = next((event.get("before") for event in current.get("history", [])
                       if type(supplied_revision) is int and event.get("revision") == supplied_revision + 1), None)
        changed = [key for key, value in proposed.items() if current["values"].get(key) != value]
        conflicts = []
        if supplied_revision != current["revision"]:
            for key in changed:
                if args.get("record_id"):
                    safe = before is not None and current["values"].get(key) == before.get("values", {}).get(key)
                else:
                    # 没有原修订号的同名提案仅补空白字段；已有不同内容须请作者或原角色明确对象。
                    safe = not has_setting_content(current["values"].get(key))
                if not safe:
                    conflicts.append({"field": key, "label": fields[key], "before": current["values"].get(key), "proposed": proposed[key]})
            if args.get("epistemic_status") != current["epistemic_status"]:
                if changed or supplied_revision is not None:
                    conflicts.append({"field": "epistemic_status", "label": "记录性质", "before": current["epistemic_status"], "proposed": args.get("epistemic_status")})
                else:
                    return {**result, "status": "unchanged", "reason": "已有同内容条目，保留其已核性质，不另建一份设想。"}
            if (args.get("record_id") and args.get("title") != current["title"]
                    and (before is None or before.get("title") != current["title"])):
                conflicts.append({"field": "title", "label": "条目名称", "before": current["title"], "proposed": args.get("title")})
        if conflicts:
            return {**result, "status": "needs_user", "differences": conflicts,
                "reason": "候选与现有条目的" + "、".join(item["label"] for item in conflicts) + "不同；原值与新依据都保留，请查看差异后决定。"}
        args.update(record_id=current["record_id"], expected_revision=current["revision"])
        return {**result, "proposal": args, "status": "ready", "reason": "已对齐同一对象与当前修订号，只补本候选有依据的字段。"}

    def _accepted_handoff_receipts(self) -> list[tuple[str, dict[str, Any]]]:
        """只定位仍对应已接受正文的原回执，候选决定不会另开任务或修改正文。"""
        with self.project.db.connect() as connection:
            rows = connection.execute("SELECT key,value_json FROM metadata WHERE key LIKE 'workflow_accept:%' OR key LIKE 'accept_tail:%'").fetchall()
        result = []
        for row in rows:
            receipt = json.loads(row["value_json"])
            chapter_no = receipt.get("chapter_no")
            chapter = self.project.db.get_chapter(chapter_no) if type(chapter_no) is int else None
            if (not chapter or chapter["status"] != "accepted" or chapter["version"] != receipt.get("version")
                    or chapter["content_hash"] != receipt.get("content_hash")):
                continue
            result.append((row["key"], receipt))
        return result

    @staticmethod
    def _application_signature(record: dict[str, Any]) -> str:
        """记录采用时的具体内容；审核导致的状态和修订号变化不算内容被改写。"""
        return content_hash(json_dumps({key: record.get(key) for key in
            ("record_id", "title", "values", "evidence_refs", "epistemic_status", "source_chapter")}))

    def _candidate_state(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """按已保存新修订的真实审查结果解释采用进度，不能把入队当成审核完成。"""
        result = deepcopy(candidate)
        if result.get("status") != "applied_pending_review":
            return result
        record = self._get(result.get("record_id", ""), "record")
        if not record:
            return {**result, "status": "needs_user", "reason": "候选采用后的记录已不存在，请核对是否继续保留这份候选。"}
        if self._application_signature(record) == result.get("applied_signature"):
            if record["status"] == "verified_reference":
                result.update(status="completed", reason="采用后的同一份设定已由责任角色核对。")
            elif record["status"] in {"needs_user", "conflict"}:
                result.update(status="needs_user", reason=setting_notice_reason(record.get("review", {}).get("issues", []),
                    fallback="采用后的记录仍有分歧，请查看审查依据后决定。"))
        elif record.get("user_decision") or record["status"] == "archived":
            result.update(status="superseded", reason="作者已对采用后的设定作出新决定，原候选不再覆盖。")
        else:
            result.update(status="needs_user", reason="候选采用后的内容又有修改，请重新查看现值与候选；旧审核不替新版作决定。")
        return result

    def _handoff_candidates(self) -> list[dict[str, Any]]:
        """读取同版附件的待办与延期，不修改回执；明确关闭的候选不再自动重放。"""
        candidates, seen = [], set()
        for _, receipt in self._accepted_handoff_receipts():
            for candidate in receipt.get("deferred_setting_candidates", []):
                value = self._candidate_state(candidate)
                identity = (receipt["chapter_no"], candidate.get("review_id"), candidate.get("candidate_hash"), candidate.get("candidate_index"))
                if value.get("status") in {"needs_user", "pending_review", "needs_evidence", "deferred_by_user", "applied_pending_review"} and identity not in seen:
                    candidates.append({**value, "source_chapter": receipt["chapter_no"]})
                    seen.add(identity)
        return candidates

    def decide_candidate(self, candidate_hash: str, *, decision: str, record_id: str = "",
                         expected_revision: int | None = None, source_chapter: int | None = None,
                         reason: str = "") -> dict[str, Any]:
        """处理原交接候选；保留或延期不改现有设定，采用仍须原审核队列核对。"""
        if decision not in {"keep_current", "defer", "adopt_candidate"}:
            raise ProjectError("请选择保留现有设定、稍后决定，或采用候选并重新核对。")
        reason = _text(reason, "决定理由", 4000)
        if not isinstance(candidate_hash, str) or not candidate_hash:
            raise ProjectError("这份候选缺少身份，请刷新设定后重试。")
        with project_write_lock_sync(self.project.root):
            matches = [(key, receipt, index, value) for key, receipt in self._accepted_handoff_receipts()
                for index, value in enumerate(receipt.get("deferred_setting_candidates", []))
                if value.get("candidate_hash") == candidate_hash
                and (source_chapter is None or receipt["chapter_no"] == source_chapter)]
            identities = {(receipt["chapter_no"], receipt["version"], receipt["content_hash"], value.get("review_id"))
                for _, receipt, _, value in matches}
            if not matches or len(identities) != 1:
                raise ProjectError("候选对应的正文或来源已变化，请刷新后选择同一章节的候选。")
            _, origin, _, original = matches[0]
            candidate = self._candidate_state(original)
            if candidate.get("status") not in {"needs_user", "pending_review", "needs_evidence", "deferred_by_user"}:
                raise ProjectError("这份候选已处理或正在核对，请刷新查看最新结果。")
            targets = candidate.get("target_records") or ([{"record_id": candidate["record_id"]}] if candidate.get("record_id") else [])
            target_ids = {item["record_id"] for item in targets if item.get("record_id")}
            if record_id and record_id not in target_ids:
                raise ProjectError("当前记录不是这份候选原先指向的对象，未改写其他设定。")
            record = self._get(record_id, "record") if record_id else None
            if record_id:
                if not record:
                    raise ProjectError("设定记录已不存在，请刷新后处理候选。")
                _revision(record, expected_revision)
            elif target_ids and len(target_ids) == 1:
                raise ProjectError("请打开对应记录，再按当前修订号处理这份候选。")
            saved = None
            if decision == "adopt_candidate":
                if len(target_ids) > 1 or (target_ids and not record):
                    raise ProjectError("这份候选对应多个同名条目，请先明确对象；未选择任何一条来覆盖。")
                if record and record["status"] == "archived":
                    raise ProjectError("原记录已移出使用，不能用旧候选直接恢复。")
                proposal = candidate.get("proposal") or {}
                collection_id = candidate.get("collection_id")
                if not collection_id or proposal.get("collection_id") != collection_id:
                    raise ProjectError("候选缺少完整的合集与原内容，先回源补齐；未凭空建立记录。")
                if not proposal.get("evidence_refs") or not has_setting_content(proposal.get("values")):
                    raise ProjectError("这份候选还缺具体内容或原文依据，请先回源补齐；未建立无依据的记录。")
                if not record and any(re.sub(r"\s+", "", item["title"]).casefold() == re.sub(r"\s+", "", str(proposal.get("title") or "")).casefold()
                        for item in self.records(collection_id)):
                    raise ProjectError("合集已有同名记录，这份候选尚未绑定对象；请先核对原记录，不再新增重复条目。")
                # 只合并这份候选实际提出的字段，其他现值保留；原句再次回源，不沿用过期证据。
                refs = []
                for ref in [*((record or {}).get("evidence_refs") or []), *(proposal.get("evidence_refs") or [])]:
                    if ref not in refs:
                        refs.append(ref)
                saved = self.save_record(collection_id, record_id=record_id or None,
                    title=proposal.get("title") or (record or {}).get("title", ""),
                    values={**(record or {}).get("values", {}), **(proposal.get("values") or {})},
                    evidence_refs=refs, epistemic_status=proposal.get("epistemic_status"),
                    expected_revision=expected_revision if record else 0, chapter_no=origin["chapter_no"],
                    actor="user", operation="supplement" if record else "new")
                # 保存只得到待核修订，原角色审核完成之前，附件维持未完成。
                candidate.update(status="applied_pending_review", record_id=saved["record_id"],
                    record_revision=saved["revision"], target_records=[{"record_id": saved["record_id"], "revision": saved["revision"]}],
                    applied_signature=self._application_signature(saved), reason="候选已保存，正在按原设定审核流程核对；当前还不能视为已通过。")
            else:
                candidate.update(status="rejected_by_user" if decision == "keep_current" else "deferred_by_user",
                    reason="作者保留现有设定，未采用这份候选。" if decision == "keep_current" else "作者选择稍后决定，候选和原依据继续保留。")
            candidate.setdefault("decision_history", []).append({"decision": decision, "reason": reason,
                "at": utc_now(), "record_id": record_id, "record_revision": expected_revision})
            # 决定写回原接受回执，不改提交状态或任务预算。重复来源副本一并保留同一明确决定。
            for key, receipt, index, _ in matches:
                receipt["deferred_setting_candidates"][index] = deepcopy(candidate)
                self.project.db.set_metadata(key, receipt)
            return {"candidate": {**candidate, "source_chapter": origin["chapter_no"]}, "record": saved,
                "maintenance": self.maintenance_status()}

    def maintenance_status(self, collections: list[dict[str, Any]] | None = None, *, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """给主界面短通知；真正用户分歧与后台补证分开，不回传书稿或长日志。"""
        collections = collections if collections is not None else self.list_collections(include_review_links=True)
        decisions, pending = {}, set()
        for collection in collections:
            if collection["status"] == "archived":
                continue
            for record in collection["records"]:
                if record["status"] == "archived":
                    continue
                original = record.get("stored_status", record["status"])
                if record["status"] in {"pending_review", "needs_evidence", "stale_evidence", "configuration_changed", "needs_classification"}:
                    pending.add(record["record_id"])
                if record["status"] in {"needs_user", "conflict"} or original in {"needs_user", "conflict"}:
                    has_candidate = any(item.get("status") == "needs_user" for item in record.get("pending_candidates", []))
                    decisions[record["record_id"]] = {"record_id": record["record_id"], "revision": record["revision"],
                        "title": record["title"], "collection_id": collection["collection_id"], "collection_name": collection["name"],
                        "reason": setting_notice_reason(record.get("review", {}).get("issues", []),
                            fallback="这条设定出现不同解释，需要你结合原句决定。"),
                        "next_action": "打开这条设定对照原值与候选；可保留现有设定、稍后决定，或采用候选并重新核对。" if has_candidate
                            else "打开这条设定查看审查依据；可保留为设想，或修改内容后重新核对。"}
        # 尚未生成单独记录的附件候选也算未完成，不把没有记录误报为已同步。
        for candidate in candidates if candidates is not None else self._handoff_candidates():
            identity = candidate.get("candidate_hash") or json_dumps({key: candidate.get(key) for key in ("source_chapter", "candidate_index", "role")})
            targets = candidate.get("target_records") or [{"record_id": candidate.get("record_id")}]
            if candidate["status"] == "needs_user" and not any(item.get("record_id") in decisions for item in targets):
                collection = next((item for item in collections if item["collection_id"] == candidate.get("collection_id")), {})
                decisions[identity] = {"record_id": "", "revision": None, "collection_id": candidate.get("collection_id"),
                    "candidate_hash": candidate.get("candidate_hash"), "source_chapter": candidate.get("source_chapter"),
                    "title": (candidate.get("proposal") or {}).get("title") or "待明确对象的设定候选",
                    "collection_name": collection.get("name", "设定候选"),
                    "reason": setting_notice_reason([candidate], fallback="这份候选还不能确定应采用的内容，请查看原依据。"),
                    "next_action": "打开设定合集查看这份候选；可保留现有设定或稍后决定，明确对象后再采用。"}
            elif candidate["status"] != "needs_user":
                pending.add(identity)
        return {"decisions": list(decisions.values()), "pending_count": len(pending),
            "auto_record_collections": sum(item["status"] != "archived" and item.get("auto_record", True) for item in collections)}

    def impact_summary(self, collection_id: str | None = None) -> dict[str, Any]:
        collections = self.list_collections(include_archived=True)
        if collection_id:
            collections = [item for item in collections if item["collection_id"] == collection_id]
        records = [record for collection in collections for record in collection["records"]
                   if record["status"] != "archived"]
        paths = sorted({ref.get("path", "") for record in records for ref in record["evidence_refs"] if ref.get("path")})
        chapters = sorted({ref["chapter_no"] for record in records for ref in record["evidence_refs"] if ref.get("chapter_no")})
        return {"collection_ids": [item["collection_id"] for item in collections], "record_count": len(records),
                "referenced_paths": paths, "referenced_chapters": chapters,
                "pending_records": [item["record_id"] for item in records if item["status"] in {"pending_review", "needs_user", "needs_evidence"}],
                "scope": "直接记录与引用；不是全书语义依赖分析", "canon_changed": False,
                "next_step": "核对受影响设定和未来规划；已接受正文保持原版，确证矛盾须单独选择修订范围。"}
