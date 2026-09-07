from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from .craft import select_craft_guides
from .errors import ValidationGateError
from .project import InkFlowProject
from .schemas import ContextPacket, ContextSection
from .studio import StudioDatabase
from .utils import estimate_tokens


class ContextBuilder:
    """把内部多源数据压成模型唯一可见的 Context Packet。"""

    def __init__(self, project: InkFlowProject, soft_token_limit: int = 256_000):
        self.project = project
        self.soft_token_limit = soft_token_limit

    def build(
        self,
        chapter_no: int,
        task: str,
        *,
        mode: Literal["draft", "review", "revise"] = "draft",
        recent_limit: int | None = None,
        provisional_chapters: list[dict[str, Any]] | None = None,
    ) -> ContextPacket:
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
        studio_context = self._studio_context(chapter_no, task, card)
        effective_recent_limit = recent_limit if recent_limit is not None else {
            "draft": 2,
            "review": 1,
            "revise": 1,
        }[mode]
        recent = database.recent_accepted_chapters(chapter_no, limit=effective_recent_limit)
        recent_parts: list[str] = []
        recent_ids: list[str] = []
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

        recent_numbers = {int(item["chapter_no"]) for item in recent}
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
        relevant_history = _rank_chapter_summaries(
            database.accepted_chapters(),
            history_query,
            before_chapter=chapter_no,
            excluded_chapters=recent_numbers,
            limit=8,
        )

        reference_cards = self._load_reference_cards(limit=6)
        craft_guides = select_craft_guides(task=task, genre=brief.genre, card=card, limit=2)
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
                title="书/卷/篇章/章节规划切片",
                content=json.dumps(_plan_for_model(bundle, card), ensure_ascii=False, indent=2),
                source_ids=["plan:book", f"plan:volume:{bundle.current_volume.volume_no}", bundle.current_arc.arc_id],
                hard=True,
            ),
            ContextSection(
                key="D",
                title="当前人物状态与知识边界",
                content=json.dumps(
                    {
                        "正史状态与认知": [
                            _state_for_model(item)
                            for item in facts
                            if item["predicate"].startswith(("state.", "knows.", "believes."))
                        ],
                        "本章人工场景笔记": studio_context["scene_notes"],
                        "与本章相关的人工故事圣经": studio_context["bible"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=studio_context["source_ids"],
                hard=True,
            ),
            ContextSection(
                key="E",
                title="最近已接受章节与批次临时草稿",
                content="\n\n".join(recent_parts) or "尚无前章。",
                source_ids=recent_ids,
            ),
            ContextSection(
                key="F",
                title="相关旧章摘要与正史索引",
                content=json.dumps(
                    {
                        "按本章任务召回的旧章摘要": relevant_history,
                        "当前正史事实索引": _fact_index_for_model(facts),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[
                    *[f"chapter-summary:{item['章节']:05d}" for item in relevant_history],
                    *[str(item["fact_id"]) for item in facts],
                ],
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
                        "用户规则": brief.user_rules,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                hard=True,
            ),
            ContextSection(
                key="K",
                title="输出契约",
                content=_output_contract(mode, card),
                hard=True,
            ),
        ]
        warnings: list[str] = []
        packet = ContextPacket(
            project_id=self.project.project_id,
            chapter_no=chapter_no,
            task=task,
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
            warnings=warnings,
        )
        if packet.estimated_tokens > self.soft_token_limit:
            self._shrink_soft_sections(packet)
            packet.estimated_tokens = estimate_tokens("\n".join(item.content for item in packet.sections))
            packet.warnings.append("Context Packet 超过软预算，已缩减最近章节和参考特征。")
        return packet

    def _load_reference_cards(self, limit: int) -> list[dict]:
        folder = self.project.internal / "references" / "features"
        result: list[dict] = []
        for path in sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")))
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
        return {
            "scene_notes": scene_notes,
            "bible": chosen,
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
        if packet.estimated_tokens > self.soft_token_limit:
            self._shrink_soft_sections(packet)
            packet.estimated_tokens = estimate_tokens("\n".join(item.content for item in packet.sections))
            packet.warnings.append("篇章复审 Context Packet 超过软预算，已缩减软上下文。")
        return packet

    def _shrink_soft_sections(self, packet: ContextPacket) -> None:
        for key, limit in (("H", 6_000), ("F", 12_000), ("E", 28_000)):
            for section in packet.sections:
                if section.key == key and len(section.content) > limit:
                    section.content = section.content[-limit:] + "\n（软预算截取）"


def _rank_chapter_summaries(
    chapters: list[dict[str, Any]],
    query: str,
    *,
    before_chapter: int,
    excluded_chapters: set[int] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """用本地轻量 BM25 召回相关旧章摘要，不增加模型调用或数据库结构。"""

    excluded = excluded_chapters or set()
    candidates = [
        item
        for item in chapters
        if int(item.get("chapter_no") or 0) < before_chapter
        and int(item.get("chapter_no") or 0) not in excluded
        and (str(item.get("summary") or "").strip() or str(item.get("title") or "").strip())
    ]
    if not candidates or limit < 1:
        return []
    query_terms = _search_terms(query)
    if not query_terms:
        return []
    documents = [
        _search_terms(f"{item.get('title') or ''}\n{item.get('summary') or ''}")
        for item in candidates
    ]
    average_length = max(1.0, sum(len(document) for document in documents) / len(documents))
    document_frequency = Counter(term for document in documents for term in set(document))
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for item, document in zip(candidates, documents, strict=True):
        counts = Counter(document)
        length = max(1, len(document))
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            inverse_frequency = math.log(1 + (len(documents) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + 1.2 * (0.25 + 0.75 * length / average_length)
            score += inverse_frequency * frequency * 2.2 / denominator
        chapter_no = int(item["chapter_no"])
        if score > 0:
            ranked.append((score, chapter_no, item))
    ranked.sort(key=lambda value: (-value[0], -value[1]))
    return [
        {
            "章节": chapter_no,
            "标题": str(item.get("title") or ""),
            "摘要": str(item.get("summary") or ""),
        }
        for _, chapter_no, item in ranked[:limit]
    ]


def _search_terms(text: str) -> list[str]:
    normalized = text.casefold()
    latin = re.findall(r"[a-z0-9_]{2,}", normalized)
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    chinese: list[str] = []
    for run in chinese_runs:
        chinese.extend(run)
        chinese.extend(run[index : index + 2] for index in range(len(run) - 1))
    return latin + chinese


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
