from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from .craft import select_craft_guides
from .errors import ValidationGateError
from .project import InkFlowProject
from .retrieval import HybridRetriever
from .schemas import ContextPacket, ContextSection
from .studio import StudioDatabase
from .utils import atomic_write_text, estimate_tokens, json_dumps, utc_now


class ContextBuilder:
    """把内部多源数据压成模型唯一可见的 Context Packet。"""

    def __init__(
        self,
        project: InkFlowProject,
        soft_token_limit: int = 256_000,
        *,
        hard_token_limit: int | None = None,
        embedding_model: str = "",
        reranker_model: str = "",
    ):
        self.project = project
        self.soft_token_limit = soft_token_limit
        self.hard_token_limit = hard_token_limit or max(soft_token_limit, 512_000)
        self.configured_soft_token_limit = self.soft_token_limit
        self.configured_hard_token_limit = self.hard_token_limit
        self.output_reserve_tokens = 16_000
        self.retriever = HybridRetriever(
            project,
            embedding_model=embedding_model,
            reranker_model=reranker_model,
        )

    def build(
        self,
        chapter_no: int,
        task: str,
        *,
        mode: Literal["draft", "review", "revise"] = "draft",
        recent_limit: int | None = None,
        provisional_chapters: list[dict[str, Any]] | None = None,
        protected_input: str = "",
    ) -> ContextPacket:
        task_budget = {
            "draft": (192_000, 208_000),
            "review": (160_000, 176_000),
            "revise": (192_000, 208_000),
        }[mode]
        self.soft_token_limit = min(self.configured_soft_token_limit, task_budget[0])
        self.hard_token_limit = min(
            max(self.soft_token_limit, self.configured_hard_token_limit - self.output_reserve_tokens),
            task_budget[1],
        )
        database = self.project.db
        brief = database.get_brief()
        bundle = database.get_current_plan_bundle()
        card = database.get_chapter_card(chapter_no)
        if not bundle:
            raise ValidationGateError("尚未生成全书、卷和篇章规划。")
        if not card:
            raise ValidationGateError(f"缺少第 {chapter_no} 章章节卡，写作门禁拒绝继续。")

        facts = database.current_facts()
        threads = database.open_threads()
        preferences = database.list_preferences()
        learning_guidance = database.learning_guidance()
        studio_context = self._studio_context(chapter_no, task, card)
        pinned_sources = {str(item["source_id"]) for item in studio_context["pins"]}
        effective_recent_limit = recent_limit if recent_limit is not None else {
            "draft": 2,
            "review": 1,
            "revise": 1,
        }[mode]
        recent = database.recent_accepted_chapters(chapter_no, limit=effective_recent_limit)
        recent_parts: list[str] = []
        recent_ids: list[str] = []
        provisional_memory: list[dict[str, Any]] = []
        provisional_memory_ids: list[str] = []
        for item in recent:
            path = self.project.root / item["path"]
            content = path.read_text(encoding="utf-8") if path.exists() else "（章节文件缺失）"
            recent_parts.append(f"### 第 {item['chapter_no']} 章\n\n{content}")
            recent_ids.append(f"chapter:{item['chapter_no']:05d}")
        for item in (provisional_chapters or [])[-2:]:
            provisional_no = int(item["chapter_no"])
            provisional_content = str(item["content"])
            recent_parts.append(
                f"### 批次临时草稿 · 第 {provisional_no} 章（尚未进入正史）\n\n"
                + provisional_content
            )
            recent_ids.append(f"batch:{item.get('batch_id', 'current')}:chapter:{provisional_no:05d}")
        for item in provisional_chapters or []:
            memory_patch = item.get("memory_patch")
            if not isinstance(memory_patch, dict):
                continue
            provisional_no = int(item["chapter_no"])
            provisional_memory.append(
                {
                    "chapter_no": provisional_no,
                    "status": "provisional",
                    "summary": memory_patch.get("chapter_summary", ""),
                    "scene_summaries": memory_patch.get("scene_summaries", []),
                    "facts": memory_patch.get("facts", []),
                    "threads": memory_patch.get("threads", []),
                }
            )
            provisional_memory_ids.append(
                f"batch-memory:{item.get('batch_id', 'current')}:chapter:{provisional_no:05d}"
            )

        history_query = "\n".join(
            [
                task,
                str(card.get("title_working") or ""),
                str(card.get("function") or ""),
                str(card.get("goal") or ""),
                str(card.get("obstacle") or ""),
                str(card.get("information_release") or ""),
                " ".join(str(value) for value in card.get("foreshadow_advance") or []),
                " ".join(str(value) for value in card.get("payoff") or []),
            ]
        )
        chapter = database.get_chapter(chapter_no)
        retrieval_hits = self.retriever.retrieve(
            history_query,
            role="reviewer" if mode == "review" else "writer",
            chapter_no=chapter_no,
            chapter_version=int(chapter["version"]) if chapter else None,
        )

        reference_cards = self._load_reference_cards(limit=6)
        craft_guides = select_craft_guides(task=task, genre=brief.genre, card=card, limit=2)
        sections = [
            ContextSection(key="A", title="当前任务与用户要求", content=task, hard=True),
            ContextSection(
                key="A0",
                title="冲突优先级与资料边界",
                content=(
                    "用户当前明确指令 > 已验收正史 > 用户长期硬规则 > 当前章节卡 > "
                    "已确认工作决定 > 用户弱偏好 > Agent 建议 > 外部作品特征。\n"
                    "低优先级资料与高优先级资料冲突时，必须采用高优先级资料并报告冲突；"
                    "人工 Story Bible 和协作消息不是正史。"
                ),
                hard=True,
            ),
            ContextSection(
                key="B",
                title="不可违反的硬正史",
                content=json.dumps([_fact_for_model(item) for item in facts], ensure_ascii=False, indent=2)
                if facts
                else "当前尚无已提交事实。",
                source_ids=[str(item["fact_id"]) for item in facts],
                hard=True,
            ),
            ContextSection(
                key="C",
                title="书/卷/篇章/章节规划切片",
                content=json.dumps(_plan_for_model(bundle, card), ensure_ascii=False, indent=2),
                source_ids=["plan:book", f"plan:volume:{bundle.current_volume.volume_no}", bundle.current_arc.arc_id],
                hard=True,
            ),
            ContextSection(
                key="D",
                title="当前人物状态与知识边界",
                content=json.dumps(
                    [
                        _state_for_model(item)
                        for item in facts
                        if item["predicate"].startswith(("state.", "knows.", "believes."))
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[
                    str(item["fact_id"])
                    for item in facts
                    if item["predicate"].startswith(("state.", "knows.", "believes."))
                ],
                hard=True,
            ),
            ContextSection(
                key="D1",
                title="人工工作资料（非正史）",
                content=json.dumps(
                    {
                        "本章人工场景笔记": studio_context["scene_notes"],
                        "与本章相关的人工故事圣经": studio_context["bible"],
                        "使用约束": "若与 B/D 的已验收正史冲突，以正史为准并报告，不得混合为同一事实。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=studio_context["source_ids"],
            ),
            ContextSection(
                key="D2",
                title="用户锁定的工作资料（本次不可压缩）",
                content=(
                    json.dumps(studio_context["pinned_material"], ensure_ascii=False, indent=2)
                    if studio_context["pinned_material"]
                    else "当前没有用户锁定的工作资料。"
                ),
                source_ids=sorted(pinned_sources),
                hard=bool(pinned_sources),
            ),
            ContextSection(
                key="E0",
                title="本批次已审查通过的临时记忆",
                content=(
                    json.dumps(provisional_memory, ensure_ascii=False, indent=2)
                    if provisional_memory
                    else "当前没有批次临时记忆。"
                ),
                source_ids=provisional_memory_ids,
                hard=bool(provisional_memory),
            ),
            ContextSection(
                key="E",
                title="最近已接受章节与批次临时草稿",
                content="\n\n".join(recent_parts) or "尚无前章。",
                source_ids=recent_ids,
            ),
            ContextSection(
                key="F",
                title="分层混合检索结果",
                content=json.dumps(
                    {
                        "检索链": "精确查询 → 本地 BM25 → 可选 BGE-M3 → 融合 → 可选 reranker → 一跳关系扩展 → 权限/章节/版本过滤",
                        "自适应结果": retrieval_hits,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[str(item["source_id"]) for item in retrieval_hits],
            ),
            ContextSection(
                key="G",
                title="未结伏笔、故事承诺与钩子义务",
                content=json.dumps([_thread_for_model(item) for item in threads], ensure_ascii=False, indent=2)
                if threads
                else "当前无已提交未结线索。",
                source_ids=[str(item["thread_id"]) for item in threads],
                hard=True,
            ),
            ContextSection(
                key="H",
                title="参考作品特征卡",
                content=json.dumps(reference_cards, ensure_ascii=False, indent=2) if reference_cards else "本章未加载参考作品。",
            ),
            ContextSection(
                key="I",
                title="按本章任务选择的写作引导",
                content=json.dumps(craft_guides, ensure_ascii=False, indent=2),
                source_ids=[f"craft:{item['技能编号']}" for item in craft_guides],
            ),
            ContextSection(
                key="J",
                title="用户偏好与文风契约",
                content=json.dumps(
                    {
                        "题材": brief.genre,
                        "读者": brief.target_audience,
                        "目标字数": brief.target_chapter_words,
                        "核心卖点": brief.core_selling_point,
                        "本书硬规则": brief.user_rules,
                        "长期硬规则": [item["text"] for item in preferences if item["strength"] == "hard"],
                        "弱偏好": [item["text"] for item in preferences if item["strength"] == "weak"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                hard=True,
            ),
            ContextSection(
                key="J1",
                title="本地学习提示（非正史）",
                content=json.dumps(learning_guidance, ensure_ascii=False, indent=2),
                source_ids=["learning:local"],
            ),
            ContextSection(
                key="K",
                title="输出契约",
                content=_output_contract(mode, card),
                hard=True,
            ),
        ]
        canon_subjects = {str(item["subject"]).casefold() for item in facts}
        overlapping_manual = sorted(
            {
                str(item.get("名称") or "")
                for item in studio_context["bible"]
                if str(item.get("名称") or "").casefold() in canon_subjects
            }
        )
        warnings: list[str] = (
            [
                "人工 Story Bible 与正史同时描述以下主体，已分层提供；若内容冲突必须采用正史并报告："
                + "、".join(overlapping_manual)
            ]
            if overlapping_manual
            else []
        )
        packet = ContextPacket(
            project_id=self.project.project_id,
            chapter_no=chapter_no,
            task=task,
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join([*[item.content for item in sections], protected_input])),
            warnings=warnings,
        )
        before_compression = packet.estimated_tokens
        if packet.estimated_tokens >= int(self.soft_token_limit * 0.9):
            compressed = self._shrink_soft_sections(packet, protected_input=protected_input)
            packet.estimated_tokens = estimate_tokens(
                "\n".join([*[item.content for item in packet.sections], protected_input])
            )
            if compressed:
                packet.warnings.append(
                    "接近软预算，已按相关性和资料权限压缩：" + "、".join(compressed) + "；硬约束未动。"
                )
        self._publish_context_status(packet, before_compression, has_protected_input=bool(protected_input))
        if packet.estimated_tokens > self.hard_token_limit:
            raise ValidationGateError(
                "硬约束本身已超过最大上下文容量，墨流没有静默删除正史或用户指令；"
                "请在上下文面板查看占用并缩小任务范围。"
            )
        return packet

    def _load_reference_cards(self, limit: int) -> list[dict]:
        folder = self.project.internal / "references" / "features"
        result: list[dict] = []
        for path in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
            try:
                card = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(card, dict):
                    # 兼容旧特征卡，但不再把可复用的原文结尾片段送进 Context Packet。
                    card.pop("sample_chapter_endings", None)
                    card.pop("samples", None)
                    result.append(card)
            except (OSError, json.JSONDecodeError):
                continue
        return result

    def _studio_context(
        self,
        chapter_no: int,
        task: str,
        card: dict[str, Any],
    ) -> dict[str, Any]:
        """挑选相关人工条目；模型始终只看到编译后的单一 Packet。"""

        studio = StudioDatabase(self.project.internal / "studio.db")
        entries = studio.list_bible_entries()
        scene_notes = studio.scene_notes(chapter_no)
        pins = studio.list_context_pins(chapter_no)
        query = (task + "\n" + json.dumps(card, ensure_ascii=False)).casefold()
        ranked: list[tuple[int, dict[str, Any]]] = []
        for item in entries:
            names = [str(item.get("name") or ""), *[str(value) for value in item.get("aliases") or []]]
            direct = any(name and name.casefold() in query for name in names)
            always = item.get("kind") in {"style", "lore"}
            score = 2 if direct else 1 if always else 0
            ranked.append((score, item))
        ranked.sort(key=lambda value: (-value[0], str(value[1].get("kind")), str(value[1].get("name"))))
        chosen = [
            {
                "条目编号": item["entry_id"],
                "类型": item["kind"],
                "名称": item["name"],
                "别名": item["aliases"],
                "内容": item["data"],
            }
            for score, item in ranked
            if score > 0
        ][:24]
        source_material: dict[str, dict[str, Any]] = {
            str(item["entry_id"]): {"类型": "故事圣经", "名称": item["name"], "内容": item["data"]}
            for item in entries
        }
        source_material.update(
            {
                f"scene-note:{chapter_no}:{item['scene_no']}": {
                    "类型": "场景笔记", "场景": item["scene_no"], "内容": item["data"]
                }
                for item in scene_notes
            }
        )
        pinned_material = [
            {
                "来源": pin["source_id"],
                "锁定说明": pin["note"],
                "资料": source_material.get(str(pin["source_id"]), "资料目前不在本章节候选中"),
            }
            for pin in pins
        ]
        return {
            "scene_notes": scene_notes,
            "bible": chosen,
            "pins": pins,
            "pinned_material": pinned_material,
            "source_ids": [
                *[f"scene-note:{chapter_no}:{item['scene_no']}" for item in scene_notes],
                *[str(item["条目编号"]) for item in chosen],
            ],
        }

    def build_arc_audit(
        self,
        start_chapter_no: int,
        end_chapter_no: int,
        *,
        provisional_chapters: list[dict[str, Any]] | None = None,
    ) -> ContextPacket:
        """Compile one range-audit packet from canonical and marked provisional prose."""

        if start_chapter_no < 1 or end_chapter_no < start_chapter_no:
            raise ValidationGateError("篇章复审范围不合法。")
        if end_chapter_no - start_chapter_no + 1 > 20:
            raise ValidationGateError("一次篇章复审最多 20 章；请分段复审。")
        database = self.project.db
        brief = database.get_brief()
        bundle = database.get_current_plan_bundle()
        if not bundle:
            raise ValidationGateError("尚未生成全书、卷和篇章规划。")
        accepted = {int(item["chapter_no"]): item for item in database.accepted_chapters()}
        provisional = {int(item["chapter_no"]): item for item in provisional_chapters or []}
        chapter_parts: list[str] = []
        source_ids: list[str] = []
        cards: list[dict[str, Any]] = []
        for chapter_no in range(start_chapter_no, end_chapter_no + 1):
            card = database.get_chapter_card(chapter_no)
            if not card:
                raise ValidationGateError(f"第 {chapter_no} 章缺少章节卡，不能进行篇章复审。")
            cards.append(_audit_card_for_model(card))
            if chapter_no in provisional:
                item = provisional[chapter_no]
                chapter_parts.append(
                    f"### 第 {chapter_no} 章（批次临时草稿，尚未进入正史）\n\n{item['content']}"
                )
                source_ids.append(f"batch:{item.get('batch_id', 'current')}:chapter:{chapter_no:05d}")
                continue
            item = accepted.get(chapter_no)
            if not item:
                raise ValidationGateError(
                    f"第 {chapter_no} 章既非已接受正文，也未包含在当前临时批次，不能做可靠复审。"
                )
            path = self.project.root / item["path"]
            if not path.is_file():
                raise ValidationGateError(f"第 {chapter_no} 章正史文件缺失。")
            chapter_parts.append(f"### 第 {chapter_no} 章（已接受正史）\n\n{path.read_text(encoding='utf-8')}")
            source_ids.append(f"chapter:{chapter_no:05d}")

        facts = database.current_facts()
        threads = database.open_threads()
        reference_cards = self._load_reference_cards(limit=6)
        task = f"复审第 {start_chapter_no}～{end_chapter_no} 章，并判断能否作为下一篇章可靠起点。"
        sections = [
            ContextSection(key="A", title="当前任务与用户要求", content=task, hard=True),
            ContextSection(
                key="B",
                title="不可违反的硬正史",
                content=json.dumps([_fact_for_model(item) for item in facts], ensure_ascii=False, indent=2)
                if facts
                else "当前尚无已提交事实。",
                source_ids=[str(item["fact_id"]) for item in facts],
                hard=True,
            ),
            ContextSection(
                key="C",
                title="篇章承诺与待复审章节卡",
                content=json.dumps(
                    {
                        "当前篇章": {
                            "编号": bundle.current_arc.arc_id,
                            "标题": bundle.current_arc.title,
                            "范围": f"{bundle.current_arc.chapter_start}-{bundle.current_arc.chapter_end}",
                            "承诺": bundle.current_arc.promise,
                            "核心冲突": bundle.current_arc.central_conflict,
                            "出口桥": bundle.current_arc.exit_bridge,
                        },
                        "待复审章节卡": cards,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[bundle.current_arc.arc_id],
                hard=True,
            ),
            ContextSection(
                key="D",
                title="当前人物状态与知识边界",
                content=json.dumps(
                    [_state_for_model(item) for item in facts if item["predicate"].startswith(("state.", "knows.", "believes."))],
                    ensure_ascii=False,
                    indent=2,
                ),
                hard=True,
            ),
            ContextSection(
                key="E",
                title="待复审章节正文",
                content="\n\n".join(chapter_parts),
                source_ids=source_ids,
                hard=True,
            ),
            ContextSection(
                key="F",
                title="相关历史索引",
                content=json.dumps(_fact_index_for_model(facts), ensure_ascii=False, indent=2) if facts else "无。",
                source_ids=[str(item["fact_id"]) for item in facts],
            ),
            ContextSection(
                key="G",
                title="未结伏笔、故事承诺与钩子义务",
                content=json.dumps([_thread_for_model(item) for item in threads], ensure_ascii=False, indent=2)
                if threads
                else "当前无已提交未结线索。",
                source_ids=[str(item["thread_id"]) for item in threads],
                hard=True,
            ),
            ContextSection(
                key="H",
                title="参考作品特征卡",
                content=json.dumps(reference_cards, ensure_ascii=False, indent=2) if reference_cards else "本次未加载参考作品。",
            ),
            ContextSection(
                key="I",
                title="用户偏好与文风契约",
                content=json.dumps(
                    {"题材": brief.genre, "读者": brief.target_audience, "用户规则": brief.user_rules},
                    ensure_ascii=False,
                    indent=2,
                ),
                hard=True,
            ),
            ContextSection(
                key="J",
                title="输出契约",
                content="只输出篇章复审结构。不得续写、修改章节卡或提交正史。",
                hard=True,
            ),
        ]
        packet = ContextPacket(
            project_id=self.project.project_id,
            chapter_no=end_chapter_no,
            task=task,
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
            warnings=[],
        )
        before_compression = packet.estimated_tokens
        if packet.estimated_tokens >= int(self.soft_token_limit * 0.9):
            compressed = self._shrink_soft_sections(packet)
            packet.estimated_tokens = estimate_tokens("\n".join(item.content for item in packet.sections))
            if compressed:
                packet.warnings.append("篇章复审接近软预算，已压缩：" + "、".join(compressed) + "；硬约束未动。")
        self._publish_context_status(packet, before_compression)
        if packet.estimated_tokens > self.hard_token_limit:
            raise ValidationGateError("篇章复审的硬材料超过最大上下文容量；请缩小复审章节范围。")
        return packet

    def _shrink_soft_sections(self, packet: ContextPacket, *, protected_input: str = "") -> list[str]:
        """只压缩低权威软资料；顺序固定且优先保留高相关候选和最近正文。"""

        actions: list[str] = []
        targets = {"H": 4_000, "I": 4_000, "J1": 2_000, "F": 10_000, "E": 24_000, "D1": 8_000}
        for key in ("H", "I", "J1", "F", "E", "D1"):
            if estimate_tokens(
                "\n".join([*[item.content for item in packet.sections], protected_input])
            ) <= int(self.soft_token_limit * 0.82):
                break
            section = next((item for item in packet.sections if item.key == key and not item.hard), None)
            if section is None:
                continue
            original = section.content
            if key == "F":
                try:
                    value = json.loads(original)
                    hits = value.get("自适应结果") if isinstance(value, dict) else None
                    if isinstance(hits, list) and len(hits) > 3:
                        value["自适应结果"] = hits[: max(3, len(hits) // 2)]
                        section.content = json.dumps(value, ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    pass
            elif key in {"H", "I"}:
                try:
                    value = json.loads(original)
                    if isinstance(value, list) and len(value) > 1:
                        section.content = json.dumps(value[: max(1, len(value) // 2)], ensure_ascii=False, indent=2)
                except json.JSONDecodeError:
                    pass
            limit = targets[key]
            if len(section.content) > limit:
                section.content = section.content[-limit:] + "\n（按优先级保留的相关尾部；完整资料仍在本地）"
            if section.content != original:
                actions.append(section.title)
        return actions

    def _publish_context_status(
        self,
        packet: ContextPacket,
        before_compression: int,
        *,
        has_protected_input: bool = False,
    ) -> None:
        soft_sections = [item for item in packet.sections if not item.hard]
        hard_sections = [item for item in packet.sections if item.hard]
        ratio = packet.estimated_tokens / max(1, self.hard_token_limit)
        status = "safe" if ratio < 0.7 else "watch" if ratio < 0.9 else "near_limit"
        payload = {
            "updated_at": utc_now(),
            "chapter_no": packet.chapter_no,
            "task": packet.task,
            "estimated_tokens": packet.estimated_tokens,
            "before_compression_tokens": before_compression,
            "soft_limit_tokens": self.soft_token_limit,
            "hard_limit_tokens": self.hard_token_limit,
            "configured_soft_limit_tokens": self.configured_soft_token_limit,
            "configured_hard_limit_tokens": self.configured_hard_token_limit,
            "output_reserve_tokens": self.output_reserve_tokens,
            "hard_usage_percent": round(ratio * 100, 1),
            "status": status,
            "hard_sections": [
                *[
                    {
                        "key": item.key,
                        "title": item.title,
                        "reason": "正史、用户当前指令、门禁或用户主动锁定，不能由容量策略删除。",
                        "source_ids": item.source_ids,
                    }
                    for item in hard_sections
                ],
                *(
                    [{
                        "key": "INPUT",
                        "title": "当前待处理正文与审查材料",
                        "reason": "当前模型任务的直接输入，不能在任务开始前被压缩。",
                        "source_ids": [],
                    }]
                    if has_protected_input else []
                ),
            ],
            "compressible_sections": [
                {
                    "key": item.key,
                    "title": item.title,
                    "reason": "非正史或可重新检索资料；接近软预算时按固定顺序和相关性压缩。",
                    "source_ids": item.source_ids,
                }
                for item in soft_sections
            ],
            "compression_applied": before_compression > packet.estimated_tokens,
            "budget_allocation": [
                {
                    "key": item.key,
                    "title": item.title,
                    "estimated_tokens": max(1, len(item.content) // 4),
                    "hard": item.hard,
                    "source_count": len(item.source_ids),
                }
                for item in packet.sections
            ],
            "retrieval_diagnostics": self.retriever.last_diagnostics,
            "warnings": packet.warnings,
        }
        atomic_write_text(self.project.internal / "context-status.json", json_dumps(payload))


def _fact_for_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "事实编号": item["fact_id"],
        "主体": item["subject"],
        "关系": item["predicate"],
        "内容": item["value"],
        "生效章节": item["valid_from_chapter"],
        "正文证据": item.get("evidence", ""),
    }


def _state_for_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "主体": item["subject"],
        "状态或认知": item["predicate"],
        "当前内容": item["value"],
        "事实编号": item["fact_id"],
    }


def _fact_index_for_model(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A short index replaces a third copy of the full canonical facts."""

    return [
        {"事实编号": item["fact_id"], "主体": item["subject"], "关系": item["predicate"]}
        for item in facts[-40:]
    ]


def _thread_for_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "线索编号": item["thread_id"],
        "类型": item["kind"],
        "标题": item["title"],
        "进度": item["status"],
        "说明": item["description"],
        "种下章节": item.get("planted_chapter"),
        "建议兑现章节": item.get("due_chapter"),
    }


def _plan_for_model(bundle: Any, card: dict[str, Any]) -> dict[str, Any]:
    book = bundle.book
    volume = bundle.current_volume
    arc = bundle.current_arc
    neighbours = [
        {
            "章节": item.chapter_no,
            "暂定标题": item.title_working,
            "章节功能": item.function,
            "目标": item.goal,
            "钩子": item.hook_type,
        }
        for item in arc.chapter_cards
        if abs(item.chapter_no - int(card["chapter_no"])) <= 1
    ]
    return {
        "全书罗盘": {
            "书名": book.title,
            "前提": book.premise,
            "读者承诺": book.reader_promise,
            "叙事发动机": book.narrative_engine,
            "主冲突": book.main_conflict,
        },
        "当前卷": {
            "卷序号": volume.volume_no,
            "卷名": volume.title,
            "章节范围": f"{volume.chapter_start}-{volume.chapter_end}",
            "阶段承诺": volume.promise,
            "中段转折": volume.midpoint_turn,
            "卷高潮": volume.climax,
        },
        "当前篇章": {
            "篇章编号": arc.arc_id,
            "篇名": arc.title,
            "章节范围": f"{arc.chapter_start}-{arc.chapter_end}",
            "承诺": arc.promise,
            "核心冲突": arc.central_conflict,
            "升级": arc.escalation,
            "揭示": arc.revelations,
            "篇章出口": arc.exit_bridge,
        },
        "本章卡": {
            "章节": card["chapter_no"],
            "暂定标题": card["title_working"],
            "视角": card["pov"],
            "时间地点": card["time_location"],
            "章节功能": card["function"],
            "目标": card["goal"],
            "阻力": card["obstacle"],
            "决定": card["decision"],
            "后果": card["consequence"],
            "不可逆变化": card["irreversible_delta"],
            "场景": card["scenes"],
            "本章释放信息": card["information_release"],
            "推进伏笔": card["foreshadow_advance"],
            "兑现事项": card["payoff"],
            "钩子类型": card["hook_type"],
            "钩子问题": card["hook_question"],
            "目标字数": card["target_words"],
        },
        "相邻章节提醒": neighbours,
    }


def _audit_card_for_model(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "章节": card["chapter_no"],
        "章节功能": card["function"],
        "目标": card["goal"],
        "阻力": card["obstacle"],
        "决定": card["decision"],
        "后果": card["consequence"],
        "不可逆变化": card["irreversible_delta"],
        "信息释放": card["information_release"],
        "兑现事项": card["payoff"],
        "钩子": card["hook_question"],
    }


def _output_contract(mode: str, card: dict[str, Any]) -> str:
    if mode == "review":
        return "只输出审查报告结构。没有可直接证明的硬问题时必须通过；不续写、不重写正文。"
    return (
        "只输出章节草稿结构。正文约 "
        f"{card['target_words']} 字，完成目标—阻力—决定—后果与不可逆变化即可；"
        "表达、场景调度与修辞可自由发挥，不附工作说明。"
    )
