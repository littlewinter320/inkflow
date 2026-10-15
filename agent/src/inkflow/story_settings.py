"""Customizable story references; deliberately separate from canonical memory.

Collections and records reuse the desktop's bible_entries table. Their revision
history is committed in the same row, so a failed update cannot lose its audit.
Source quotes are located by code; semantic approval remains a review decision.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any
import uuid

from .errors import ProjectError
from .project_lock import project_write_lock_sync
from .utils import content_hash, json_dumps, utc_now


READ_ROLES = {"coordinator", "writer", "editor", "reviewer", "memory_keeper"}
WRITE_ROLES = READ_ROLES - {"coordinator"}
REVIEW_ROLES = {"editor", "reviewer", "memory_keeper"}
SCHEMA = "story-settings-v1"
MARKER = "story_settings_type"


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


class StorySettingsService:
    def __init__(self, project: Any):
        # Local import avoids making studio's existing document helpers circular.
        from .studio import StudioDatabase
        self.project = project
        self.db = StudioDatabase(project.internal / "studio.db")

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
        return result

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
            if actor in REVIEW_ROLES and old is None and any(
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
            if actor in REVIEW_ROLES and old and operation == "supplement":
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
            if actor in REVIEW_ROLES:
                if not refs:
                    raise ProjectError("审查与记忆角色记录设定必须提供可定位的原文依据。")
                canon_reference = epistemic_status in {"objective", "belief", "rumor"} and all(ref["source_kind"] == "canonical" for ref in refs)
                book_requirement = epistemic_status == "user_constraint" and all(
                    ref["source_kind"] == "planning" and ref.get("path") == "BOOK.md" for ref in refs)
                if old is None and not (canon_reference or book_requirement):
                    raise ProjectError("首次整理须引用已接受正文，或以用户约定性质引用当前书籍要求；新创作设想仍由 Writer 提出。")
            if actor == "writer":
                epistemic_status = "hypothesis"
            if actor in REVIEW_ROLES and old and operation == "supplement":
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
                "source_role": actor, "source_chapter": chapter_no, "operation": operation,
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

    def source_fingerprint(self) -> str:
        items = [{key: value for key, value in item.items() if key not in {"history", "updated_at"}}
                 for item in self._all(include_archived=True)]
        cache: dict[tuple[Any, ...], tuple[str, dict[str, Any]]] = {}
        for item in items:
            if item.get(MARKER) == "record":
                item["live_evidence"] = self._live_refs(item["evidence_refs"], cache)
        return content_hash(json_dumps(items, indent=None))

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
            # User requirements must not lose their budget to ordinary references.
            eligible.sort(key=lambda item: (item.get("epistemic_status") != "user_constraint", item["title"].casefold() not in query))
            audit["available_record_count"] = len(eligible)
            audit["record_index"] = [{"record_id": item["record_id"], "title": item["title"], "revision": item["revision"]} for item in eligible[:32]]
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
