"""本书统一知识：原资料保留身份，知识页与有向关系共享来源和版本。"""
from __future__ import annotations

import json
import re
from uuid import uuid4
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .errors import InkFlowError, ValidationGateError
from .schemas import StrictModel
from .utils import content_hash, json_dumps, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_sources (
    source_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, data_json TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_segments (
    segment_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, status TEXT NOT NULL,
    result_json TEXT, error TEXT NOT NULL DEFAULT '', task_id TEXT, run_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS knowledge_segment_source ON knowledge_segments(source_id,source_hash,status);
CREATE TABLE IF NOT EXISTS knowledge_annotations (
    entity_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_imports (
    import_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
    characters INTEGER NOT NULL DEFAULT 0, parts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT ''
);
"""
KINDS = {"character": "人物", "faction": "势力", "location": "地点", "item": "物品",
         "world": "世界规则", "event": "事件", "thread": "伏笔与承诺", "plan": "规划",
         "method": "写作方法", "reference": "参考资料", "relationship": "人物关系", "power": "力量规则", "species": "种族与生物", "entity": "待分类对象"}
STATES = {"canon": "正文事实", "derived": "有据归纳", "plan": "未来规划", "rule": "作者要求",
          "reference": "参考", "hypothesis": "设想", "belief": "人物信念", "rumor": "传闻",
          "draft": "草稿", "conflict": "待确认"}
STATES["unverified"] = "待核线索"
STATES["author_note"] = "作者补充"
STATES["dismissed"] = "已搁置"


class KnowledgeClaim(StrictModel):
    """模型只提出一条有连续原文支持的知识，不得覆盖既有正史。"""
    subject: str = Field(min_length=1, max_length=160)
    kind: Literal["character", "faction", "location", "item", "world", "event", "thread", "plan", "method", "reference", "entity"]
    predicate: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2400)
    quote: str = Field(min_length=2, max_length=1600)
    target: str = Field(default="", max_length=160)
    target_kind: Literal["character", "faction", "location", "item", "world", "event", "thread", "plan", "method", "reference", "entity"] = "entity"
    state: Literal["objective", "belief", "rumor", "hypothesis", "plan", "method", "reference"]
    event_time: str = Field(default="", max_length=160)
    importance: Literal["core", "normal"] = "normal"
    existing_entity_id: str = Field(default="", max_length=100)
    conflict: str = Field(default="", max_length=700)


class KnowledgeExtraction(StrictModel):
    """逐段完整阅读的回执，资料不足与冲突保留给维护队列。"""
    summary: str = Field(min_length=1, max_length=3000)
    claims: list[KnowledgeClaim] = Field(max_length=100)
    unresolved: list[str] = Field(default_factory=list, max_length=20)


def _identity(kind: str, name: str, namespace: str = "book") -> str:
    """身份不依赖页面顺序；外部资料的人物与本书对象分域。"""
    return "entity-" + content_hash(namespace + "\0" + kind + "\0" + re.sub(r"\s+", "", name).casefold())[:24]


def knowledge_gap_id(segment_id: str, question: str) -> str:
    """同一缺口的编辑与补证都绑定原段和问题。"""
    return content_hash(json_dumps(["gap", segment_id, question]))[:24]


def knowledge_gap_category(question: str) -> str:
    """将逐段阅读回执中的原句缺失、矛盾、资料省略和故事未知分开。"""
    if "原引文未匹配" in question or re.search(r"引文.{0,8}(缺失|未定位|不匹配)", question):
        return "citation"
    clauses = [part.strip() for part in re.split("[；。]", question) if part.strip()]
    omission = r"(未随|未提供|未展开|省略|未展示|未附|未读取|未完整提供)"
    if re.search(omission, question) and all(
            re.search(omission, part) and re.search(r"资料|记录|上下文|本轮|本次|未随本段|未随本章", part)
            or re.match(r"(无法|仍需|仍待).{0,12}(全部|完整|逐项|充分|全貌)", part)
            for part in clauses
    ) and not re.search(r"矛盾|冲突|不一致|前后|时间线|日序|身份.{0,8}是否|是否同一", question):
        return "context"
    if re.search(r"矛盾|冲突|不一致|对不上|日序|重复.{0,5}(疑点|叙事)|歧义|衔接.{0,6}(疑点|待核|需要|需核)|"
                 r"前后.{0,12}(差异|不符|核对)|需[要再]?(?:核对|复核).{0,10}(原文|版本|顺序|位置|路线|时间|日期)", question):
        return "consistency"
    return "open_question"


def _text(value: Any) -> str:
    """保留所有字段的可读正文，不把结构化记录压成一句摘要。"""
    if isinstance(value, dict):
        return "\n".join(f"{key}：{_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    return str(value if value is not None else "")


def segment_spans(text: str, characters: int):
    """连续覆盖全文，优先在段落边界切分；上限是单次阅读量而非全文截断。"""
    start = 0
    while start < len(text):
        end = min(len(text), start + characters)
        if end < len(text):
            boundary = text.rfind("\n", start + characters // 2, end)
            if boundary >= 0:
                end = boundary + 1
        assert start < end <= len(text), "知识阅读范围必须向前推进"
        yield start, end
        start = end


class KnowledgeService:
    """派生知识保存在现有项目数据库，原正文、规划和设定仍由原服务提交。"""

    def __init__(self, project):
        self.project = project
        self.db = project.db

    def sources(self, *, actor: str = "user", before_chapter: int | None = None) -> tuple[list[dict], list[str]]:
        """回读有效来源。每次消费都重新核版本，迟到结果不能覆盖新内容。"""
        from .story_settings import StorySettingsService
        from .planning_pipeline import load_active_planning
        from .references import ReferenceService

        result, problems = [], []

        def add(identity, title, body, state, **fields):
            if body.strip():
                result.append({"id": identity, "title": title, "body": body, "state": state,
                    "hash": content_hash(body), "chapter": None, "path": "", **fields})

        for chapter in self.db.accepted_chapters():
            number = chapter["chapter_no"]
            if before_chapter and number >= before_chapter:
                continue
            body = self.db.canonical_chapter_content(number)
            if body is None:
                path = (self.project.root / chapter["path"]).resolve()
                body = path.read_text(encoding="utf-8") if path.is_relative_to(self.project.root) and path.is_file() else ""
            if not body or content_hash(body) != chapter["content_hash"]:
                problems.append(f"第 {number} 章原文与保存版本不一致，暂不整理。")
                continue
            add(f"chapter:{number}", f"第 {number} 章 · {chapter['title']}", body, "canon",
                chapter=number, version=chapter["version"], path=chapter["path"])
        brief = self.db.get_brief()
        add("book:requirements", "本书要求", _text(brief.model_dump(mode="json")), "rule", path="BOOK.md")
        try:
            active = load_active_planning(self.project)
            if active:
                for filename, title in (("OUTLINE.md", "全书大纲"), ("STORY_DETAIL.md", "剧情细纲"), ("RECENT_PLAN.md", "近期规划")):
                    add("plan:" + filename, title, (self.project.root / filename).read_text(encoding="utf-8"), "plan", path=filename)
            else:
                bundle = self.db.get_current_plan_bundle()
                if bundle:
                    for key, value in bundle.model_dump(mode="json").items():
                        add("plan:" + key, "生效规划 · " + key, _text(value), "plan")
        except (OSError, ValueError, InkFlowError) as exc:
            problems.append("规划尚未完成版本核对：" + str(exc))
        settings = StorySettingsService(self.project, read_only=True)
        collections = {item["collection_id"]: item for item in settings.list_collections()}
        if actor == "user":
            records = [settings._context_record(record, collection, before_chapter, {})
                       for collection in collections.values() for record in collection["records"]]
        else:
            records = settings.retrieval_records(chapter_no=before_chapter or self.db.latest_accepted_chapter_no() + 1, actor=actor)
        for record in records:
            if record is None:
                continue
            if record["status"] in {"stale_evidence", "configuration_changed", "archived"}:
                detail = "的依据或设定结构已变化，待核对。"
                if actor == "user" and record["status"] == "stale_evidence":
                    # 原句仍在时提供换版线索；正式设定审核继续绑定它实际读过的版本。
                    stale = [ref for ref in record["evidence_refs"] if not ref["source_current"]]
                    located = []
                    for ref in stale:
                        try:
                            body, current = settings._source(ref)
                        except (OSError, ValueError, KeyError, InkFlowError):
                            continue
                        if ref["quote"] and ref["quote"] in body:
                            located.append(f"第 {current['chapter_no']} 章" if current.get("chapter_no") else "本书要求")
                    if stale and len(located) == len(stale):
                        detail = (f"的 {len(located)} 条旧版引文已在当前{'、'.join(dict.fromkeys(located))}重新定位。"
                                  "原设定审核仍绑定旧版，可从本书设定重新核对。")
                problems.append(f"「{record['title']}」{detail}")
                continue
            collection = collections[record["collection_id"]]
            labels = {field["key"]: field["label"] for field in collection["fields"]}
            state = ("rule" if record["epistemic_status"] == "user_constraint" and record.get("source_role") == "user"
                     else record["epistemic_status"] if record["epistemic_status"] in {"belief", "rumor", "hypothesis"} else "reference")
            add("setting:" + record["record_id"], record["title"],
                _text({labels.get(k, k): v for k, v in record["values"].items()}), state,
                kind=collection.get("template_id", "entity"), record_id=record["record_id"],
                collection_id=record["collection_id"], revision=record["revision"],
                read_roles=collection["read_roles"], references=record.get("evidence_refs", []), chapter=record.get("source_chapter"))
        for reference in ReferenceService(self.project).list_references():
            relative = reference.get("local_path") or reference.get("path") or ""
            paths = list((self.project.internal / "references" / "raw").glob(str(reference["reference_id"]) + "-*.txt"))
            path = paths[0].resolve() if paths else (self.project.root / relative).resolve()
            if path.is_file() and path.is_relative_to(self.project.root):
                add("reference:" + reference["reference_id"], path.stem, path.read_text(encoding="utf-8"), "reference",
                    path=str(path.relative_to(self.project.root)), namespace="reference:" + reference["reference_id"])
        with self.db.connect() as connection:
            # 主动导入的资料仍属参考；同书角色与外部作品同名不自动合并。
            for row in connection.execute("SELECT source_id,source_hash,json_extract(data_json,'$.title') AS title,"
                    "json_extract(data_json,'$.document_title') AS document_title,json_extract(data_json,'$.namespace') AS namespace,"
                    "json_extract(data_json,'$.characters') AS characters FROM knowledge_sources WHERE source_id LIKE 'import:%' AND active=1"):
                result.append({"id": row["source_id"], "hash": row["source_hash"], "title": row["title"],
                    "document_title": row["document_title"], "namespace": row["namespace"], "characters": row["characters"],
                    "state": "reference", "body": "", "chapter": None, "path": "", "stored": True})
        # 共享库只接收作者主动导入的参考，小说实体和推断不会自动跨书流动。
        from .preferences import author_connection
        with author_connection() as connection:
            present = connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_shared_sources'").fetchone()
            if present:
                for row in connection.execute("SELECT source_id,json_extract(data_json,'$.hash'),json_extract(data_json,'$.title'),"
                        "json_extract(data_json,'$.document_title'),json_extract(data_json,'$.namespace'),json_extract(data_json,'$.characters') "
                        "FROM knowledge_shared_sources WHERE json_extract(data_json,'$.ready')=1"):
                    result.append({"id": row[0], "hash": row[1], "title": row[2], "document_title": row[3],
                        "namespace": row[4], "characters": row[5], "state": "reference", "body": "", "chapter": None, "path": "", "stored": True})
        return result, problems

    def sync(self, *, settings=None) -> dict:
        """登记新增版本与缺口，创建幂等阅读单元，不在这里发模型请求。"""
        from .config import Settings
        from .role_protocol import check_owners_for_mode
        settings = settings or Settings.from_env(self.project.root)
        from .task_settings import active_task_settings
        scope = active_task_settings.get()
        mode = scope.effective_collaboration_mode if scope else ("memory_boost" if settings.collaboration_preset == "adaptive" else settings.collaboration_preset)
        owner = check_owners_for_mode(mode)["memory"]
        soft, hard = settings.context_budget_for(owner)
        # 沿本次容量自适应，给已有档案、规则、结构化输出留空间；不限制全文累计量。
        available = min(soft, hard) // 2 - 2048
        if available < 512:
            raise ValidationGateError("当前记忆角色上下文空间不足以完成阅读和核对，请提高该角色容量。")
        characters = max(512, settings.input_estimate_budget(available))
        sources, problems = self.sources()
        with self.db.connect() as connection:
            connection.execute("UPDATE knowledge_sources SET active=0 WHERE source_id NOT LIKE 'import:%'")
            for source in sources:
                if source.get("stored"):
                    # 大文件正文已逐块落盘，目录刷新不读回整份或覆盖成空文本。
                    if source["id"].startswith("shared:"):
                        connection.execute("INSERT OR IGNORE INTO knowledge_sources VALUES(?,?,?,1,?)",
                            (source["id"], source["hash"], json_dumps(source), utc_now()))
                        identity = content_hash(f"{source['id']}:{source['hash']}:0:{source['characters']}")
                        connection.execute("INSERT OR IGNORE INTO knowledge_segments "
                            "(segment_id,source_id,source_hash,start_offset,end_offset,status,updated_at) VALUES(?,?,?,?,?,'pending',?)",
                            (identity, source["id"], source["hash"], 0, source["characters"], utc_now()))
                    connection.execute("UPDATE knowledge_sources SET active=1 WHERE source_id=?", (source["id"],))
                    continue
                connection.execute("INSERT INTO knowledge_sources VALUES(?,?,?,1,?) ON CONFLICT(source_id) DO UPDATE SET "
                    "source_hash=excluded.source_hash,data_json=excluded.data_json,active=1,updated_at=excluded.updated_at",
                    (source["id"], source["hash"], json_dumps(source), utc_now()))
                # 已登记版本沿用原边界，设置改变不重复创建另一套覆盖单元。
                exists = connection.execute("SELECT 1 FROM knowledge_segments WHERE source_id=? AND source_hash=? LIMIT 1",
                    (source["id"], source["hash"])).fetchone()
                for start, end in (() if exists else segment_spans(source["body"], characters)):
                    identity = content_hash(f"{source['id']}:{source['hash']}:{start}:{end}")
                    connection.execute("INSERT OR IGNORE INTO knowledge_segments "
                        "(segment_id,source_id,source_hash,start_offset,end_offset,status,updated_at) VALUES(?,?,?,?,?,'pending',?)",
                        (identity, source["id"], source["hash"], start, end, utc_now()))
                if source["id"].startswith("setting:"):
                    # 已有结构化字段直接完整接入，继续沿原设定审核服务维护。
                    connection.execute("UPDATE knowledge_segments SET status='indexed' WHERE source_id=? AND source_hash=? AND status='pending'",
                        (source["id"], source["hash"]))
            connection.commit()
        return {"sources": len(sources), "problems": problems, **self.progress()}

    def progress(self) -> dict:
        """阅读覆盖度与待确认分别统计，完成读取不等于无遗漏或全部正确。"""
        with self.db.connect() as connection:
            rows = connection.execute("SELECT g.status,COUNT(*) AS count,SUM(g.end_offset-g.start_offset) AS chars "
                "FROM knowledge_segments g JOIN knowledge_sources s ON s.source_id=g.source_id AND s.source_hash=g.source_hash "
                "WHERE s.active=1 AND g.status!='supplemented' GROUP BY g.status").fetchall()
            imports = [dict(row) for row in connection.execute("SELECT * FROM knowledge_imports ORDER BY updated_at DESC LIMIT 12")]
        counts = {row["status"]: row["count"] for row in rows}
        return {"total": sum(counts.values()) - counts.get("indexed", 0), "structured": counts.get("indexed", 0), "completed": counts.get("completed", 0),
            "pending": counts.get("pending", 0), "failed": counts.get("failed", 0),
            "reading": counts.get("reading", 0), "characters": sum(row["chars"] or 0 for row in rows if row["status"] != "indexed"),
            "read_characters": sum(row["chars"] or 0 for row in rows if row["status"] == "completed"),
            "imports": imports, "control": {"auto": True, **self.db.get_metadata("knowledge.control", {})}}

    def pending_segments(self) -> list[dict]:
        """返回当前版本未完成单元；失败与取消不自动重发。"""
        with self.db.connect() as connection:
            rows = connection.execute("SELECT g.* FROM knowledge_segments g JOIN knowledge_sources s "
                "ON s.source_id=g.source_id AND s.source_hash=g.source_hash WHERE s.active=1 AND g.status='pending' "
                "ORDER BY COALESCE(json_extract(s.data_json,'$.chapter'),0),s.source_id,g.start_offset").fetchall()
        return [dict(row) for row in rows]

    def stored_source(self, source_id: str) -> dict | None:
        """按需读一个已保存单元；共享参考也只读当前需要的这一块。"""
        if source_id.startswith("shared:"):
            from .preferences import author_connection
            with author_connection() as connection:
                row = connection.execute("SELECT data_json FROM knowledge_shared_sources WHERE source_id=? AND json_extract(data_json,'$.ready')=1", (source_id,)).fetchone()
        else:
            with self.db.connect() as connection:
                row = connection.execute("SELECT data_json FROM knowledge_sources WHERE source_id=? AND active=1", (source_id,)).fetchone()
        value = json.loads(row[0]) if row else None
        if value and content_hash(value["body"]) != value["hash"]:
            raise ValidationGateError("已保存的资料块校验不一致，请重新导入原资料。")
        return value

    def claim_segment(self, segment: dict, *, task_id: str, run_id: str) -> bool:
        """原子领取单元，跨窗口或进程也不会重复调用。"""
        with self.db.connect() as connection:
            changed = connection.execute("UPDATE knowledge_segments SET status='reading',task_id=?,run_id=?,updated_at=? "
                "WHERE segment_id=? AND status='pending'", (task_id, run_id, utc_now(), segment["segment_id"])).rowcount
            connection.commit()
        return changed == 1

    def finish_segment(self, segment: dict, result: KnowledgeExtraction, *, run_id: str):
        """连续引文、已读身份和来源版本全部通过后，保存派生知识而不改正史。"""
        current = (self.stored_source(segment["source_id"]) if segment["source_id"].startswith(("import:", "shared:"))
                   else {source["id"]: source for source in self.sources()[0]}.get(segment["source_id"]))
        if not current or current["hash"] != segment["source_hash"]:
            raise ValidationGateError("阅读期间来源已改变，旧结果没有应用，请从新版本继续。")
        text = current["body"][segment["start_offset"]:segment["end_offset"]]
        if any(claim.quote not in text for claim in result.claims):
            raise ValidationGateError("知识提案中有引文未出现在本次阅读原文，未把它保存为成功。")
        with self.db.connect() as connection:
            changed = connection.execute("UPDATE knowledge_segments SET status='completed',result_json=?,error='',updated_at=? "
                "WHERE segment_id=? AND status='reading' AND run_id=?", (result.model_dump_json(), utc_now(), segment["segment_id"], run_id)).rowcount
            if changed != 1:
                raise ValidationGateError("这段阅读的执行身份已变化，未覆盖其他任务的结果。")
            connection.commit()

    def fail_segment(self, segment_id: str, reason: str, *, run_id: str):
        """未知请求与失败均保留原次数，等待显式续接。"""
        with self.db.connect() as connection:
            connection.execute("UPDATE knowledge_segments SET status='failed',error=?,updated_at=? WHERE segment_id=? "
                "AND status='reading' AND run_id=?", (reason[:1200], utc_now(), segment_id, run_id))
            connection.commit()

    def snapshot(self, *, actor: str = "user", before_chapter: int | None = None) -> dict:
        """把同版事实、设定、规划、全文归纳编为同一组可读页面和有向关系。"""
        sources, problems = self.sources(actor=actor, before_chapter=before_chapter)
        source_problems = list(problems)
        autonomous = self.db.get_metadata("knowledge.control", {}).get("autonomous") is True
        source_map = {source["id"]: source for source in sources}
        nodes: dict[str, dict] = {}
        edges: dict[str, dict] = {}
        with self.db.connect() as connection:
            annotations = {row["entity_id"]: {**json.loads(row["data_json"]), "revision": row["revision"]}
                           for row in connection.execute("SELECT * FROM knowledge_annotations")}
            extracted = [dict(row) for row in connection.execute("SELECT * FROM knowledge_segments WHERE status IN ('completed','supplemented')")]
            decisions = {row["key"].removeprefix("knowledge.decision:"): json.loads(row["value_json"])
                         for row in connection.execute("SELECT key,value_json FROM metadata WHERE key LIKE 'knowledge.decision:%'")}
            supplement_links = {row["key"].removeprefix("knowledge.supplement:"): json.loads(row["value_json"])
                                for row in connection.execute("SELECT key,value_json FROM metadata WHERE key LIKE 'knowledge.supplement:%'")}
            facts = [dict(row) for row in connection.execute("SELECT * FROM facts WHERE branch_id='main' AND status IN ('active','superseded') ORDER BY source_chapter,created_at")]
        alias_ids = {(item.get("namespace", "book"), alias): identity for identity, item in annotations.items() for alias in item.get("aliases", [])}

        def entity(name, kind="entity", namespace="book", preferred=""):
            kind = kind if kind in KINDS else "entity"
            identity = preferred or alias_ids.get((namespace, name)) or _identity(kind, name, namespace)
            if identity not in nodes:
                annotation = annotations.get(identity, {})
                nodes[identity] = {"id": identity, "name": name, "kind": kind, "namespace": namespace,
                    "aliases": annotation.get("aliases", []), "note": annotation.get("note", ""),
                    "revision": annotation.get("revision", 0), "sections": [], "sources": []}
            elif nodes[identity]["kind"] == "entity" and kind != "entity":
                nodes[identity]["kind"] = kind
            return identity

        def append(identity, title, body, source, state, *, quote="", importance="normal", **fields):
            item_id = content_hash(json_dumps([identity, title, body, source["id"], state]))[:24]
            section = {"id": item_id, "title": title, "body": body, "source_id": source["id"],
                "chapter": source["chapter"], "state": state, "quote": quote, "importance": importance, **fields}
            if not any(old["id"] == item_id for old in nodes[identity]["sections"]):
                nodes[identity]["sections"].append(section)
            if source["id"] not in nodes[identity]["sources"]:
                nodes[identity]["sources"].append(source["id"])
            return item_id

        def connect(origin, target, relation, source, state, *, quote="", **fields):
            identity = content_hash(json_dumps([origin, target, relation, source["id"], quote]))[:24]
            edges[identity] = {"id": identity, "source": origin, "target": target, "relation": relation,
                "source_id": source["id"], "chapter": source["chapter"], "state": state, "quote": quote, **fields}

        # 先建立有明确类型的设定身份；无类型旧事实只在同名对象唯一时复用。
        for source in sources:
            if source["id"].startswith("setting:"):
                identity = entity(source["title"], source.get("kind", "entity"))
                append(identity, "设定档案", source["body"], source, source["state"], record_id=source["record_id"],
                       collection_id=source["collection_id"])

        # 先注册正式资料的稳定身份，章节中的旧引用可直接命中大纲和书籍页。
        for source in sources:
            if source["state"] in {"plan", "rule"}:
                entity(source["title"], "plan" if source["state"] == "plan" else "world",
                       source.get("namespace", "book"))

        def resolve(name, kind="entity", namespace="book", preferred=""):
            matches = [item["id"] for item in nodes.values() if item["namespace"] == namespace
                       and (item["name"] == name or name in item["aliases"])
                       and (kind == "entity" or item["kind"] in {kind, "entity"})]
            if preferred and preferred not in matches:
                # 同域同类型的唯一名称可接回当前页，只有无法唯一定位的旧引用仍提示待核。
                if len(matches) != 1 and preferred != _identity(kind, name, namespace):
                    problems.append(f"「{name}」的旧对象身份已变化，需要核对关联。")
                preferred = ""
            return entity(name, kind, namespace, preferred or (matches[0] if len(matches) == 1 else ""))

        current_ids = {item["fact_id"] for item in (self.db.facts_as_of(before_chapter - 1) if before_chapter else self.db.current_facts(include_evidence=False))}
        for fact in facts:
            source = source_map.get(f"chapter:{fact['source_chapter']}")
            if (not source or fact["source_version"] not in {None, source.get("version")}
                    or fact["source_hash"] not in {None, source["hash"]} or fact["evidence"] not in source["body"]):
                continue
            identity = resolve(fact["subject"])
            state = "canon" if fact["epistemic_kind"] == "objective" else fact["epistemic_kind"]
            append(identity, fact["predicate"], _text(json.loads(fact["value_json"])), source, state,
                   quote=fact["evidence"], current=fact["fact_id"] in current_ids, event_time=fact["event_time"] or "")

        from .narrative_memory import event_history
        history = event_history(self.db.narrative_history_rows(before_chapter or self.db.latest_accepted_chapter_no() + 1))
        for event in history["events"]:
            source = source_map.get(f"chapter:{event['chapter_no']}")
            if not source:
                continue
            identity = entity(event["summary"][:100], "event", preferred="event-" + content_hash(event["source_id"])[:24])
            state = "canon" if event["narrative_mode"] in {"current", "flashback"} else "hypothesis" if event["narrative_mode"] in {"dream", "hypothetical"} else event["narrative_mode"]
            append(identity, "事件经过", event["summary"], source, state, quote=event["evidence"], event_time=event.get("event_time") or "")
            for name in event["participants"]:
                target = resolve(name, "character")
                connect(target, identity, "参与事件", source, state, quote=event["evidence"])
            if event["location"]:
                connect(identity, resolve(event["location"], "location"), "发生于", source, state, quote=event["evidence"])

        threads = self.db.threads_state_as_of((before_chapter or self.db.latest_accepted_chapter_no() + 1) - 1)
        for thread in threads["threads"]:
            number = thread.get("last_advanced_chapter") or thread.get("planted_chapter")
            source = source_map.get(f"chapter:{number}")
            if source:
                identity = entity(thread["title"], "thread", preferred="thread-" + content_hash(thread["thread_id"])[:24])
                append(identity, "伏笔进展", thread["description"], source, "canon", importance="core", thread_status=thread["status"])

        gaps = []
        gap_evidence = {}
        for row in extracted:
            source = source_map.get(row["source_id"])
            if not source or source["hash"] != row["source_hash"]:
                continue
            output = KnowledgeExtraction.model_validate_json(row["result_json"])
            namespace = source.get("namespace", "book")
            for claim_number, claim in enumerate(output.claims):
                # 外部方法可共享；外部故事的具体人物永远保留来源域。
                identity = resolve(claim.subject, claim.kind, namespace, claim.existing_entity_id)
                candidate_id = content_hash(row["segment_id"] + ":" + str(claim_number))[:24]
                decision = decisions.get(candidate_id, {})
                choice = decision.get("decision")
                if choice == "dismiss" and actor != "user":
                    continue
                state = ("conflict" if claim.conflict else source["state"] if source["state"] != "canon"
                         else "derived" if claim.state == "objective" else "reference" if claim.state == "method" else claim.state)
                if state == "conflict" and autonomous:
                    # 作者委托的后台维护保留不确定线索及理由，继续找依据；不凭推测改写确认事实。
                    state = "unverified"
                if choice == "hypothesis":
                    state = "hypothesis"
                elif choice == "edit":
                    state = "author_note"
                elif choice == "dismiss":
                    state = "dismissed"
                title = ("补证 · " if row["status"] == "supplemented" else "") + claim.predicate
                link = supplement_links.get(row["segment_id"], {})
                for gap_id in link:
                    gap_evidence.setdefault(gap_id, []).append({"source_id": source["id"], "title": source["title"],
                        "body": decision["body"] if choice == "edit" else claim.description, "state": state,
                        "answered": isinstance(link, dict) and link[gap_id] == "answered" and state == "derived"})
                append(identity, title, decision["body"] if choice == "edit" else claim.description,
                    source, state, quote=claim.quote,
                    importance=claim.importance, event_time=claim.event_time, conflict=claim.conflict,
                    candidate_id=candidate_id, decision=choice, decision_revision=decision.get("revision", 0))
                # 文字改写是作者补充，原模型关系不随新表述自动生效。
                if claim.target and choice not in {"edit", "dismiss"}:
                    connect(identity, resolve(claim.target, claim.target_kind, namespace), claim.predicate, source, state,
                            quote=claim.quote, event_time=claim.event_time, conflict=claim.conflict, candidate_id=candidate_id)
            for question in output.unresolved:
                # 缺口绑定原阅读单元和文字，作者取舍不会沿用到不同版本的提问。
                identity = knowledge_gap_id(row["segment_id"], question)
                decision = decisions.get(identity, {})
                choice = decision.get("decision")
                gap_state = {"edit": "author_note", "hypothesis": "hypothesis", "dismiss": "dismissed"}.get(choice, "unverified")
                category = knowledge_gap_category(question)
                if actor == "user" or gap_state == "unverified":
                    gaps.append({"candidate_id": identity, "source_id": source["id"], "title": source["title"],
                        "question": decision["body"] if choice == "edit" else question, "state": gap_state,
                        "category": category, "decision": choice, "decision_revision": decision.get("revision", 0)})
        for gap in gaps:
            # 找到部分证据也保留原问题，作者可以据此选择编辑或搁置。
            gap["evidence"] = gap_evidence.get(gap["candidate_id"], [])
            gap["answered"] = any(item["answered"] for item in gap["evidence"])

        # 每份规划与素材本身也是可全文阅读的节点，避免没有抽取时资料消失。
        for source in sources:
            if source["state"] in {"plan", "rule"} or source["id"].startswith(("reference:", "import:", "shared:")):
                identity = entity(source.get("document_title") or source["title"], "plan" if source["state"] == "plan" else "world" if source["state"] == "rule" else "reference",
                                  source.get("namespace", "book"))
                if source.get("stored"):
                    if not nodes[identity]["sections"]:
                        append(identity, "原始资料", "全文已分段保存，点击来源可逐段阅读；知识整理沿全部段落继续。", source, source["state"])
                else:
                    append(identity, "完整内容", source["body"], source, source["state"])
        for item in nodes.values():
            item["sections"].sort(key=lambda section: (section.get("chapter") or 0, section["title"]))
        return {"nodes": list(nodes.values()), "edges": list(edges.values()), "sources": sources,
                "problems": list(dict.fromkeys(problems)), "gaps": gaps, "source_problems": source_problems,
                "coverage": history["coverage"], "progress": self.progress(),
                "kinds": KINDS, "states": STATES}

    def save_supplements(self, entries: list[tuple[dict, KnowledgeClaim]], *, actor: str, task_id: str, run_id: str) -> int:
        """补证单独留档，逐条核对当前有效来源，阅读分母仍只统计原始段落。"""
        current = {source["id"]: source for source in self.sources(actor=actor)[0]}
        saved = 0
        with self.db.connect() as connection:
            for evidence, claim in entries:
                source = current.get(evidence["source_id"])
                if (not source or source["hash"] != evidence["hash"] or not claim.quote.strip()
                        or claim.quote not in evidence["text"]):
                    continue
                offset = source["body"].find(claim.quote)
                if offset < 0:
                    continue
                result = KnowledgeExtraction(summary="关联缺口补证", claims=[claim])
                identity = content_hash(json_dumps(["supplement", source["id"], source["hash"], claim.model_dump()]))
                saved += connection.execute(
                    "INSERT OR IGNORE INTO knowledge_segments VALUES(?,?,?,?,?,'supplemented',?,'',?,?,?)",
                    (identity, source["id"], source["hash"], offset, offset + len(claim.quote),
                     result.model_dump_json(), task_id, run_id, utc_now())).rowcount
                if gap_id := evidence.get("candidate_id"):
                    key = "knowledge.supplement:" + identity
                    row = connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
                    linked = json.loads(row[0]) if row else {}
                    if isinstance(linked, list):
                        linked = dict.fromkeys(linked, "partial")
                    resolved = ("answered" if evidence.get("resolution") == "answered"
                                and evidence.get("category") != "consistency" and claim.state == "objective"
                                and not claim.conflict else "partial")
                    if gap_id not in linked or (resolved == "answered" and linked[gap_id] != resolved):
                        linked[gap_id] = resolved
                        connection.execute("INSERT OR REPLACE INTO metadata(key,value_json,updated_at) VALUES(?,?,?)",
                                           (key, json_dumps(linked), utc_now()))
            connection.commit()
        return saved

    def search(self, query: str, *, actor="user", before_chapter=None, limit=30) -> dict:
        """中文名称、别名和全文为主，再展开匹配对象的一跳关系。"""
        snapshot = self.snapshot(actor=actor, before_chapter=before_chapter)
        terms = [term.casefold() for term in re.findall(r"[\w\u3400-\u9fff]+", query)]
        if not terms:
            return {"nodes": [], "sources": [], "edges": []}
        matches = {node["id"] for node in snapshot["nodes"] if any(term in (node["name"] + " " + " ".join(node["aliases"])).casefold() for term in terms)
                   or any(name and name.casefold() in query.casefold() for name in [node["name"], *node["aliases"]])}
        direct = set(matches)
        edges = [edge for edge in snapshot["edges"] if edge["source"] in direct or edge["target"] in direct]
        matches.update(edge[key] for edge in edges for key in ("source", "target"))
        nodes = [node for node in snapshot["nodes"] if node["id"] in matches]
        source_ids = {source for node in nodes for source in node["sources"]}
        sources = [source for source in snapshot["sources"] if source["id"] in source_ids or any(term in source["body"].casefold() for term in terms)]
        # 大资料仅在 SQLite 中搜索正文，命中后最多取回一页结果，避免重载整份文件。
        with self.db.connect() as connection:
            rows = connection.execute("SELECT data_json FROM knowledge_sources WHERE active=1 AND source_id LIKE 'import:%' "
                "AND instr(lower(json_extract(data_json,'$.body')),lower(?))>0 LIMIT ?", (query, limit)).fetchall()
            sources.extend(json.loads(row[0]) for row in rows)
        from .preferences import author_connection
        with author_connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_shared_sources'").fetchone():
                sources.extend(json.loads(row[0]) for row in connection.execute("SELECT data_json FROM knowledge_shared_sources "
                    "WHERE json_extract(data_json,'$.ready')=1 AND instr(lower(json_extract(data_json,'$.body')),lower(?))>0 LIMIT ?", (query, limit)))
        sources = list({source["id"]: source for source in sources}.values())
        return {"nodes": nodes[:limit], "sources": sources[:limit], "edges": edges, "problems": snapshot["problems"]}

    def reference_hits(self, query: str, *, limit: int = 6) -> list[dict]:
        """未完成模型整理的已导入原文也可检索，全文搜索不依赖向量或抽取成功。"""
        with self.db.connect() as connection:
            rows = connection.execute("SELECT data_json FROM knowledge_sources WHERE active=1 AND source_id LIKE 'import:%' "
                "AND instr(lower(json_extract(data_json,'$.body')),lower(?))>0 LIMIT ?", (query, limit)).fetchall()
        results = [json.loads(row[0]) for row in rows]
        from .preferences import author_connection
        with author_connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_shared_sources'").fetchone():
                results.extend(json.loads(row[0]) for row in connection.execute("SELECT data_json FROM knowledge_shared_sources "
                    "WHERE json_extract(data_json,'$.ready')=1 AND instr(lower(json_extract(data_json,'$.body')),lower(?))>0 LIMIT ?", (query, limit)))
        return results[:limit]

    def annotate(self, entity_id: str, *, note: str, aliases: list[str], expected_revision: int) -> dict:
        """作者维护档案补充与别名；这类编辑不改正文，也不把备注升级成事实。"""
        if len(note) > 16000 or len(aliases) > 30 or any(not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 160 for alias in aliases):
            raise ValueError("备注最多 16000 字，别名最多 30 个，每个不超过 160 字。")
        snapshot = self.snapshot()
        target = next((node for node in snapshot["nodes"] if node["id"] == entity_id), None)
        if target is None:
            raise ValueError("这个对象已变化，请刷新知识页。")
        aliases = list(dict.fromkeys(alias.strip() for alias in aliases))
        if any((set(node["aliases"]) | {node["name"]}).intersection(aliases) for node in snapshot["nodes"]
               if node["id"] != entity_id and node["namespace"] == target["namespace"]):
            raise ValueError("别名与其他对象重名，请先区分身份，不能直接合并。")
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute("SELECT revision FROM knowledge_annotations WHERE entity_id=?", (entity_id,)).fetchone()
            if (old[0] if old else 0) != expected_revision:
                raise ValidationGateError("这份档案已有新修改，请刷新后再保存。")
            connection.execute("INSERT OR REPLACE INTO knowledge_annotations VALUES(?,?,?,?)",
                (entity_id, expected_revision + 1, json_dumps({"note": note, "aliases": aliases, "namespace": target["namespace"]}), utc_now()))
            connection.commit()
        return {"revision": expected_revision + 1}

    def decide(self, candidate_id: str, decision: str, *, body: str = "", expected_revision: int = 0) -> dict:
        """对同版归纳编辑、取舍或恢复，原模型候选与原文仍保留。"""
        if decision not in {"dismiss", "hypothesis", "edit", "restore"}:
            raise ValueError("请选择编辑补充、留为设想、搁置或恢复。")
        if decision == "edit" and not 1 <= len(body.strip()) <= 16000:
            raise ValueError("作者补充需填写 1 至 16000 字。")
        snapshot = self.snapshot()
        if (not any(section.get("candidate_id") == candidate_id
                    for node in snapshot["nodes"] for section in node["sections"])
                and not any(gap["candidate_id"] == candidate_id for gap in snapshot["gaps"])):
            raise ValidationGateError("这条知识的来源已变化，请刷新后查看当前内容。")
        key = "knowledge.decision:" + candidate_id
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
            revision = json.loads(row[0]).get("revision", 0) if row else 0
            if revision != expected_revision:
                raise ValidationGateError("这条知识已有新取舍，请刷新后重试。")
            updated = utc_now()
            value = {"decision": decision, "revision": revision + 1, "updated_at": updated}
            if decision == "edit":
                value["body"] = body.strip()
            connection.execute("INSERT OR REPLACE INTO metadata VALUES(?,?,?)", (key, json_dumps(value), updated))
            connection.commit()
        return {"status": "completed"}

    def import_sources(self, files: list[str], *, shared: bool, stop) -> dict:
        """1 GB 文件逐块解析、逐块保存，全部读取成功才发布，内存不随文件大小增长。"""
        from .preference_materials import MaterialRequest, iter_material_sources
        from .preferences import author_connection
        from .config import Settings
        from .runtime import active_runtime
        request = MaterialRequest(level="project", scope="project", instruction="整理知识", excerpt="", files=files)
        identity = uuid4().hex
        title = "、".join(Path(path).name for path in files)
        prefix = ("shared:" if shared else "import:") + identity + ":"
        namespaces = ["reference:" + content_hash(str(Path(path).resolve()).casefold())[:24] for path in files]
        part, characters = 0, 0
        with self.db.connect() as connection:
            runtime = active_runtime.get()
            connection.execute("INSERT INTO knowledge_imports VALUES(?,?,'reading',0,0,'',?,?)", (identity, title, utc_now(), runtime.run_id if runtime else ""))
            connection.commit()
        if shared:
            with author_connection() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS knowledge_shared_sources(source_id TEXT PRIMARY KEY,data_json TEXT NOT NULL,updated_at TEXT NOT NULL)")
                connection.commit()
        def grouped_text():
            """把流式小块合并为当前容量下的阅读单元，避免每几千字创建一次独立模型任务。"""
            settings = Settings.from_env(self.project.root)
            soft, hard = settings.context_budget_for("editor")
            target = max(4096, settings.input_estimate_budget(min(soft, hard) // 2 - 2048))
            for file_number, filename in enumerate(files):
                blocks, size = [], 0
                for item in iter_material_sources(request.model_copy(update={"files": [filename]}), stop):
                    if "\0" in item.text:
                        raise ValueError("资料包含无法识别的控制字符，请转为可读文字后重试。")
                    text = item.coverage() + "\n" + item.text
                    if size + len(text) > target and blocks:
                        yield file_number, "\n\n".join(blocks)
                        blocks, size = [], 0
                    blocks.append(text)
                    size += len(text) + 2
                if blocks:
                    yield file_number, "\n\n".join(blocks)

        try:
            for file_number, body in grouped_text():
                if stop.is_set():
                    raise ValueError("资料导入已停止，未发布不完整的全文。")
                # 读取器逐块返回。每个单元独立持久化，位置标签随内容保留。
                part += 1
                characters += len(body)
                file_title = Path(files[file_number]).name
                source = {"id": prefix + f"{part:08d}", "hash": content_hash(body), "title": f"{file_title} · 第 {part} 段",
                    "document_title": file_title, "body": body, "characters": len(body), "state": "reference", "chapter": None,
                    "path": "", "namespace": namespaces[file_number], "scope": "shared" if shared else "book", "ready": False,
                    "previous": prefix + f"{part - 1:08d}" if part > 1 else ""}
                if shared:
                    with author_connection() as connection:
                        connection.execute("INSERT INTO knowledge_shared_sources VALUES(?,?,?)", (source["id"], json_dumps(source), utc_now()))
                        connection.commit()
                else:
                    with self.db.connect() as connection:
                        connection.execute("INSERT INTO knowledge_sources VALUES(?,?,?,0,?)", (source["id"], source["hash"], json_dumps(source), utc_now()))
                        segment_id = content_hash(f"{source['id']}:{source['hash']}:0:{len(body)}")
                        connection.execute("INSERT INTO knowledge_segments (segment_id,source_id,source_hash,start_offset,end_offset,status,updated_at) "
                            "VALUES(?,?,?,0,?,'pending',?)", (segment_id, source["id"], source["hash"], len(body), utc_now()))
                        connection.commit()
                if part % 32 == 0:
                    with self.db.connect() as connection:
                        connection.execute("UPDATE knowledge_imports SET characters=?,parts=?,updated_at=? WHERE import_id=?", (characters, part, utc_now(), identity))
                        connection.commit()
            if stop.is_set():
                raise ValueError("资料导入已停止，已读部分保留，未发布为完整资料。")
            if shared:
                with author_connection() as connection:
                    for namespace in namespaces:
                        connection.execute("UPDATE knowledge_shared_sources SET data_json=json_set(data_json,'$.ready',0) "
                            "WHERE json_extract(data_json,'$.namespace')=? AND source_id NOT LIKE ?", (namespace, prefix + "%"))
                    connection.execute("UPDATE knowledge_shared_sources SET data_json=json_set(data_json,'$.ready',1) WHERE source_id LIKE ?", (prefix + "%",))
                    connection.commit()
            with self.db.connect() as connection:
                if not shared:
                    for namespace in namespaces:
                        connection.execute("UPDATE knowledge_sources SET active=0 WHERE json_extract(data_json,'$.namespace')=? AND source_id NOT LIKE ?", (namespace, prefix + "%"))
                    connection.execute("UPDATE knowledge_sources SET active=1 WHERE source_id LIKE ?", (prefix + "%",))
                connection.execute("UPDATE knowledge_imports SET status='completed',characters=?,parts=?,updated_at=? WHERE import_id=?", (characters, part, utc_now(), identity))
                connection.commit()
        except BaseException as exc:
            with self.db.connect() as connection:
                connection.execute("UPDATE knowledge_imports SET status='failed',characters=?,parts=?,error=?,updated_at=? WHERE import_id=?",
                    (characters, part, str(exc), utc_now(), identity))
                connection.commit()
            raise
        return {"source_id": prefix + "00000001", "characters": characters, "summary": f"全文已导入，共 {part} 段，等待知识整理。"}

    def export_wiki(self) -> dict:
        """导出可阅读的双向链接页面；原资料与正式提交仍归原服务管理。"""
        from .utils import atomic_write_text, atomic_write_json
        snapshot = self.snapshot()
        directory = self.project.internal / "knowledge" / "wiki"
        directory.mkdir(parents=True, exist_ok=True)
        names = {node["id"]: node["name"] for node in snapshot["nodes"]}
        index = ["# 本书知识库", "", "导出内容保留来源与知识性质。修改请在墨流知识库或对应原资料中保存。", ""]
        for node in snapshot["nodes"]:
            index.append(f"- [[{node['id']}|{node['name']}]] · {KINDS[node['kind']]}")
            lines = ["# " + node["name"], "", node["note"], ""]
            for section in node["sections"]:
                lines += ["## " + section["title"], "", section["body"], "", "性质：" + STATES.get(section["state"], section["state"]),
                          "来源：" + section["source_id"], "", "> " + section["quote"].replace("\n", "\n> ") if section["quote"] else "", ""]
            lines += ["## 关联", ""]
            for edge in snapshot["edges"]:
                if node["id"] in {edge["source"], edge["target"]}:
                    lines.append(f"- [[{edge['source']}|{names[edge['source']]}]] → {edge['relation']} → [[{edge['target']}|{names[edge['target']]}]]")
            atomic_write_text(directory / (node["id"] + ".md"), "\n".join(lines))
        atomic_write_text(directory / "index.md", "\n".join(index))
        atomic_write_json(directory / "graph.json", {"nodes": snapshot["nodes"], "edges": snapshot["edges"]})
        return {"directory": str(directory), "pages": len(snapshot["nodes"])}
