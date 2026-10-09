from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from .craft import select_craft_guides
from .errors import ValidationGateError
from .project import InkFlowProject
from .planning_pipeline import load_active_planning, synchronize_v2_projection
from .project_lock import project_write_lock_sync
from .outline_context import outline_sections
from .retrieval import HybridRetriever, load_reference_cards
from .schemas import ArcPlan, ContextPacket, ContextSection, PlanBundle, VolumePlan
from .studio import StudioDatabase
from .utils import SOURCE_RECOVERY_TOKEN_LIMIT, atomic_write_text, content_hash, estimate_tokens, json_dumps, project_source_revision, utc_now
from .writer_notes import recent_approved_notes


_COMPRESSED_NOTE = "（已按完整条目压缩；完整资料仍保存在本地）"


def named_prior_chapters(task: str, before_chapter: int, loaded: set[int]) -> list[int]:
    """Keep task order, excluding loaded/current/future chapters before any budget decision."""
    return [number for number in dict.fromkeys(int(value) for value in
            re.findall(r"第\s*(\d+)\s*章", task))
            if 0 < number < before_chapter and number not in loaded]


def _chapter_history_query(task: str, card: dict[str, Any], mode: str, protected_input: str) -> str:
    query = "\n".join([task, *[str(card.get(key) or "") for key in
        ("title_working", "function", "goal", "obstacle", "information_release")],
        *[" ".join(str(value) for value in card.get(key) or []) for key in ("foreshadow_advance", "payoff")]])
    if mode in {"review", "revise"} and protected_input:
        # This material is supplied by the current task caller, including prose,
        # selection surroundings or its same-version review. It is never canon.
        query += "\n" + protected_input
    return query


def _packet_input_tokens(packet: ContextPacket, protected_input: str = "") -> int:
    return estimate_tokens(packet.to_model_prompt() + ("\n\n" + protected_input if protected_input else ""))


def planning_bundle_for_chapter(project: InkFlowProject, chapter_no: int) -> PlanBundle | None:
    """Resolve the card's persisted plan, not whichever window was opened last."""
    from .manual_edits import ManualEditsService
    ManualEditsService(project).assert_planning_ready(chapter_no)
    current = project.db.get_current_plan_bundle()
    active_v2 = load_active_planning(project)
    if active_v2 is not None and current is None:
        with project_write_lock_sync(project.root):
            synchronize_v2_projection(project)
        current = project.db.get_current_plan_bundle()
    if active_v2 is not None and chapter_no > active_v2[3].anchor_chapter:
        expected_prefix = f"v2:{active_v2[0]['trace_id']}:"
        if current is None or not current.current_arc.arc_id.startswith(expected_prefix):
            raise ValidationGateError("当前规划文档已生效，但数据库章节卡仍是旧版；请先重新发布规划，不能引用旧卡写作。")
    if current is None:
        return None
    with project.db.connect() as connection:
        row = connection.execute(
            "SELECT parent_key FROM plans WHERE kind='chapter' AND plan_key=? AND status='active'",
            (f"chapter:{chapter_no:05d}",),
        ).fetchone()
    if row is None:
        return current
    parent = str(row["parent_key"] or "")
    supplement = project.db.get_plan("supplement", parent)
    if supplement is not None:
        bundle = PlanBundle.model_validate(supplement)
    elif parent and parent != current.current_arc.arc_id:
        if active_v2 is not None and chapter_no <= active_v2[3].anchor_chapter:
            # Accepted chapters may still depend on the plan that was active
            # when they entered canon. The new volume key is reused, so read
            # that exact preserved database snapshot instead of mixing eras.
            history = project.root / "planning" / "history"
            for snapshot in sorted(history.glob("database-*.json"), key=lambda path: path.stat().st_mtime):
                try:
                    rows = json.loads(snapshot.read_text(encoding="utf-8"))["plans"]
                    values = {(row["kind"], row["plan_key"]): json.loads(row["data_json"])
                              for row in rows}
                    card_before = values.get(("chapter", f"chapter:{chapter_no:05d}"))
                    arc_before = values.get(("arc", parent))
                    if card_before != project.db.get_chapter_card(chapter_no) or arc_before is None:
                        continue
                    old_volume = values.get(("volume", f"volume:{arc_before['volume_no']:03d}"))
                    old_book = values.get(("book", "book"))
                    if old_volume and old_book:
                        return PlanBundle.model_validate({"book": old_book,
                                                          "current_volume": old_volume,
                                                          "current_arc": arc_before})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            raise ValidationGateError(f"第 {chapter_no} 章所依赖的旧版规划快照缺失，不能用新版卷纲冒充其来源。")
        arc_data = project.db.get_plan("arc", parent)
        if arc_data is None:
            raise ValidationGateError(f"第 {chapter_no} 章章节卡关联的近期计划缺失，不能混用其他篇章。")
        arc = ArcPlan.model_validate(arc_data)
        volume_data = project.db.get_plan("volume", f"volume:{arc.volume_no:03d}")
        if volume_data is None:
            raise ValidationGateError(f"第 {chapter_no} 章关联的卷规划缺失。")
        bundle = PlanBundle(book=current.book, current_volume=VolumePlan.model_validate(volume_data), current_arc=arc)
    else:
        bundle = current
    bound_card = next((item for item in bundle.current_arc.chapter_cards if item.chapter_no == chapter_no), None)
    if bound_card is None:
        raise ValidationGateError(f"第 {chapter_no} 章章节卡不在其关联的近期计划范围内。")
    return bundle


def _shorten_soft_text(value: str, limit: int, *, keep_tail: bool = False) -> str:
    if len(value) <= limit:
        return value
    room = max(0, limit - len(_COMPRESSED_NOTE) - 1)
    if keep_tail:
        suffix = value[-room:]
        boundaries = [position for position in (suffix.find("\n\n"), suffix.find("。"), suffix.find("！"), suffix.find("？"), suffix.find("\n")) if position >= 0]
        if boundaries:
            suffix = suffix[min(boundaries) + 1 :].lstrip()
        return _COMPRESSED_NOTE + "\n" + suffix
    prefix = value[:room]
    boundaries = [prefix.rfind(mark) for mark in ("\n\n", "。", "！", "？", "\n")]
    boundary = max(boundaries)
    if boundary >= max(80, room // 3):
        prefix = prefix[: boundary + (0 if prefix[boundary: boundary + 2] == "\n\n" else 1)]
    return prefix.rstrip() + "\n" + _COMPRESSED_NOTE


def _fit_json_value(value: Any, limit: int) -> Any:
    """在字符预算内保留完整 JSON 项，避免把半个对象交给模型。"""

    if isinstance(value, list):
        kept: list[Any] = []
        for item in value:
            candidate = [*kept, item]
            if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= limit:
                kept = candidate
                continue
            if not kept and isinstance(item, (list, dict, str)):
                compact = _fit_json_value(item, max(80, limit - 8))
                if len(json.dumps([compact], ensure_ascii=False, indent=2)) <= limit:
                    kept = [compact]
            break
        return kept
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            candidate = {**kept, key: item}
            if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= limit:
                kept[key] = item
                continue
            remaining = max(80, limit - len(json.dumps(kept, ensure_ascii=False, indent=2)) - len(str(key)) - 16)
            if isinstance(item, (list, dict)):
                compact = _fit_json_value(item, remaining)
                candidate = {**kept, key: compact}
                if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= limit:
                    kept[key] = compact
            elif isinstance(item, str) and remaining >= 80:
                compact = _shorten_soft_text(item, remaining)
                candidate = {**kept, key: compact}
                if len(json.dumps(candidate, ensure_ascii=False, indent=2)) <= limit:
                    kept[key] = compact
        return kept
    if isinstance(value, str):
        return _shorten_soft_text(value, limit)
    return value


def _fit_soft_content(content: str, limit: int, *, keep_tail: bool = False) -> str:
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return _shorten_soft_text(content, limit, keep_tail=keep_tail)
    compact = _fit_json_value(value, limit)
    rendered = json.dumps(compact, ensure_ascii=False, indent=2)
    if len(rendered) <= limit:
        return rendered
    empty = [] if isinstance(value, list) else {}
    return json.dumps(empty, ensure_ascii=False, indent=2)


def _voice_preferences_content(items: list[dict[str, Any]]) -> str:
    return json.dumps({
        "本章可用偏好": [{"编号": item["preference_id"], "范围": item["scope"],
            "层级": item.get("level", "project"), "状态": item.get("status", "active"),
            "要求": item["text"], "来源原话": item.get("source_quote", ""),
            "修订号": item.get("revision", 0)} for item in items],
        "使用边界": "当前指令和本书要求优先于作者默认；仅采用适用偏好，普通偏好不是硬门禁，不移植其他书剧情。"
                    "candidate仅作未确认的相关参考，不覆盖已确认要求，不升级为硬规则。",
    }, ensure_ascii=False, indent=2)


def _voice_preferences_for_context(
    preferences: list[dict[str, Any]], task: str, card: dict[str, Any], limit: int | None = None
) -> list[dict[str, Any]]:
    """Budget the exact JSON for upstream scope-filtered preferences and candidates."""
    ranked = []
    for index, item in enumerate(preferences):
        candidate = item.get("status") == "candidate"
        if item.get("strength") != "weak" and not candidate:
            continue
        targeted = str(item.get("scope") or "project").partition(":")[0] != "project"
        ranked.append((not candidate, targeted, -index, item))
    ranked.sort(key=lambda row: row[:3], reverse=True)
    selected = []
    for _, _, _, item in ranked:
        if estimate_tokens(_voice_preferences_content([*selected, item])) > 8_000:
            continue
        selected.append(item)
        if limit is not None and len(selected) >= limit:
            break
    return selected


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
        hook_strategy: str = "most_chapters",
        review_experience_detail: str = "standard",
        actor: str = "writer",
        output_reserve_tokens: int = 32_000,
    ):
        self.project = project
        self.actor = actor
        self.soft_token_limit = soft_token_limit
        self.hard_token_limit = hard_token_limit or max(soft_token_limit, 512_000)
        self.configured_soft_token_limit = self.soft_token_limit
        self.configured_hard_token_limit = self.hard_token_limit
        self.output_reserve_tokens = max(1, int(output_reserve_tokens))
        self.retriever = HybridRetriever(
            project,
            embedding_model=embedding_model,
            reranker_model=reranker_model,
        )
        self.hook_strategy = hook_strategy
        self.review_experience_detail = review_experience_detail

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
        self.hard_token_limit = self.configured_hard_token_limit - self.output_reserve_tokens
        if self.hard_token_limit <= 0:
            raise ValidationGateError("当前角色的输出预留已占满上下文，请调整该角色预算或单次输出上限。")
        self.soft_token_limit = min(self.configured_soft_token_limit, self.hard_token_limit)
        database = self.project.db
        brief = database.get_brief()
        bundle = planning_bundle_for_chapter(self.project, chapter_no)
        card = database.get_chapter_card(chapter_no)
        if not bundle:
            raise ValidationGateError("尚未生成全书、卷和篇章规划。")
        if not card:
            raise ValidationGateError(f"缺少第 {chapter_no} 章章节卡，写作门禁拒绝继续。")

        facts = database.facts_as_of(chapter_no - 1)
        thread_state = database.threads_state_as_of(chapter_no - 1)
        threads = thread_state["threads"]
        available_preferences = database.effective_preferences()
        from .preferences import applicable_preferences, apply_preference_decisions, preference_candidates
        available_candidates = preference_candidates(database)
        preference_scope = {**card, "genre": brief.genre, "chapter_no": chapter_no}
        available_pool = [*available_preferences, *available_candidates]
        task_preferences = apply_preference_decisions(available_pool)
        applicable = applicable_preferences(task_preferences, task, preference_scope)
        preferences = [item for item in applicable if item.get("status") != "candidate"]
        candidates = [item for item in applicable if item.get("status") == "candidate"]
        forced_preferences = [item for item in preferences if item["strength"] == "hard"]
        forced_texts = list(dict.fromkeys(str(item["text"]).strip() for item in forced_preferences if str(item["text"]).strip()))
        voice_preferences = _voice_preferences_for_context([*preferences, *candidates], task, preference_scope)
        studio_context = self._studio_context(chapter_no, task, card)
        pinned_sources = {str(item["source_id"]) for item in studio_context["pins"]}
        effective_recent_limit = recent_limit if recent_limit is not None else {
            "draft": 2,
            "review": 3,
            "revise": 1,
        }[mode]
        recent_for_patterns = database.recent_accepted_chapters(chapter_no, limit=max(6, effective_recent_limit))
        for item in recent_for_patterns:
            canonical = database.canonical_chapter_content(int(item["chapter_no"]))
            if canonical is None:
                # Legacy rows may predate DB-owned text. A Markdown projection
                # is usable only while it still matches the accepted hash.
                path = self.project.root / item["path"]
                canonical = path.read_text(encoding="utf-8") if path.is_file() else None
            if canonical is None or content_hash(canonical) != item["content_hash"]:
                raise ValidationGateError(
                    f"第 {item['chapter_no']} 章正史正文的数据库内容与已接受版本不符，"
                    "不能把缺失或被改动的 Markdown 当成模型记忆；请先恢复该章正史投影。"
                )
            item["content"] = canonical
        recent = recent_for_patterns[-effective_recent_limit:] if effective_recent_limit else []
        recent_parts: list[str] = []
        recent_ids: list[str] = []
        provisional_memory: list[dict[str, Any]] = []
        provisional_memory_ids: list[str] = []
        for item in recent:
            recent_parts.append(f"### 第 {item['chapter_no']} 章\n\n{item['content']}")
            recent_ids.append(f"chapter:{item['chapter_no']:05d}")
        cited_chapters = []
        named_numbers = named_prior_chapters(task, chapter_no, set())
        named_source_tokens = {int(item["chapter_no"]): estimate_tokens(item["content"])
                               for item in recent if int(item["chapter_no"]) in named_numbers}
        for number in named_prior_chapters(task, chapter_no, {int(item["chapter_no"]) for item in recent}):
            try:
                source = self.retriever._accepted_source(number, before_chapter=chapter_no)
            except (ValueError, OSError) as exc:
                reason = f"本次点名的第 {number} 章依据无法读取：{exc}"
                self.project.record_pending_work(kind="context_source", reason=reason,
                    source={"chapter_no": number, "boundary_chapter": chapter_no - 1},
                    next_action="恢复点名源章的已接受正文及版本后，从资料读取节点续接；不派 Writer 改稿。",
                    status="waiting_condition")
                raise ValidationGateError(reason) from exc
            named_source_tokens[number] = estimate_tokens(source["content"])
            cited_chapters.append(ContextSection(
                key=f"E-cited-{number}", title=f"用户点名的第 {number} 章已接受正文",
                content=source["content"], source_ids=[f"chapter:{number:05d}"], hard=True, cache_scope="chapter",
            ))
        ending_pattern_inputs = [*recent_for_patterns]
        ending_pattern_ids = [f"chapter:{item['chapter_no']:05d}" for item in recent_for_patterns]
        for item in (provisional_chapters or [])[-4:]:
            provisional_no = int(item["chapter_no"])
            ending_pattern_inputs.append(
                {
                    "chapter_no": provisional_no,
                    "content": str(item["content"]),
                    "source_kind": "批次临时草稿",
                }
            )
            ending_pattern_ids.append(
                f"batch:{item.get('batch_id', 'current')}:chapter:{provisional_no:05d}"
            )
        ending_patterns = _recent_ending_patterns(self.project.root, ending_pattern_inputs)
        for item in (provisional_chapters or [])[-4 if mode == "review" else -2:]:
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
                    "operations": memory_patch.get("operations", []),
                    "threads": memory_patch.get("threads", []),
                }
            )
            provisional_memory_ids.append(
                f"batch-memory:{item.get('batch_id', 'current')}:chapter:{provisional_no:05d}"
            )

        history_query = _chapter_history_query(task, card, mode, protected_input)
        reference_cards = self._load_reference_cards(limit=6)
        chapter = database.get_chapter(chapter_no)
        retrieval_hits = self.retriever.retrieve(
            history_query,
            role=self.actor,
            chapter_no=chapter_no,
            chapter_version=int(chapter["version"]) if chapter else None,
        )
        current_material_in_query = bool(mode in {"review", "revise"} and protected_input.strip())
        query_input = {"mode": mode, "actor": self.actor, "chapter_no": chapter_no,
            "chapter_version": int(chapter["version"]) if chapter else None,
            "current_material_in_query": current_material_in_query,
            "current_material_hash": content_hash(protected_input) if current_material_in_query else None,
            "current_material_characters": len(protected_input) if current_material_in_query else 0,
            "query_hash": content_hash(history_query),
            "meaning": "当前材料来自本次调用，包括正文/选区及同版说明或报告；仅用于取材，不成为正史或已核验依据。"}
        # The retriever already consumed the full query. Diagnostics retain only
        # a locator and identity, not a second copy of protected prose.
        self.retriever.last_diagnostics["query"] = _chapter_history_query(task, card, "draft", "")[:512]
        self.retriever.last_diagnostics["query_input"] = query_input
        def required_now(item: dict[str, Any]) -> bool:
            subject = str(item["subject"]).strip()
            predicate = str(item["predicate"])
            return (
                subject in {"世界", "世界观", "全书"}
                or predicate.startswith(("rule.", "world.", "constraint."))
                or bool(subject and subject.casefold() in history_query.casefold())
            )

        # Accepted facts arrive in subject order; chapter order keeps unchanged canon
        # ahead of newly accepted facts in consecutive Writer cache prefixes.
        ordered_facts = sorted(facts, key=lambda item: (int(item["source_chapter"]), str(item["fact_id"])))
        mandatory_facts = ordered_facts[:256] + [item for item in ordered_facts[256:] if required_now(item)]
        state_facts = [item for item in mandatory_facts if item["predicate"].startswith(("state.", "knows.", "believes.")) or item.get("epistemic_kind") != "objective"]
        general_facts = [item for item in mandatory_facts if item not in state_facts]
        already_loaded = {
            *[str(item["fact_id"]) for item in mandatory_facts],
            *[str(item["thread_id"]) for item in threads],
            *[str(item["preference_id"]) for item in [*forced_preferences, *voice_preferences]],
            *[f"reference:{item['reference_id']}" for item in reference_cards],
            *recent_ids,
            *[source_id for section in cited_chapters for source_id in section.source_ids],
        }
        duplicate_retrieval_ids = sorted(
            str(item["source_id"]) for item in retrieval_hits
            if str(item["source_id"]) in already_loaded
        )
        eligible_preference_ids = {str(item["preference_id"]) for item in applicable}
        preference_retrieval_omissions = []
        selected_hits = []
        preference_tokens = estimate_tokens(_voice_preferences_content(voice_preferences))
        for item in retrieval_hits:
            identity = str(item["source_id"])
            if identity in already_loaded:
                continue
            if item.get("source_type") == "user_preference":
                if identity not in eligible_preference_ids:
                    preference_retrieval_omissions.append({"source_id": identity, "reason": "本任务范围或已核验偏好取舍不适用"})
                    continue
                tokens = estimate_tokens(json.dumps(item, ensure_ascii=False, indent=2))
                if preference_tokens + tokens > 8_000:
                    preference_retrieval_omissions.append({"source_id": identity, "reason": "完整检索偏好条目超过本轮声音段共享8,000 token预算"})
                    continue
                preference_tokens += tokens
            selected_hits.append(item)
        retrieval_hits = selected_hits
        retrieved_canon = [item for item in retrieval_hits if item.get("source_type") == "canon_fact"]
        supplemental_hits = [item for item in retrieval_hits if item.get("source_type") != "canon_fact"]
        self.retriever.last_diagnostics["selected"] = [{key: item.get(key) for key in
            ("source_id", "source_type", "score", "reasons", "retrieval_reasons", "body_kind", "identity", "resolved_source")}
            for item in retrieval_hits]
        if duplicate_retrieval_ids or preference_retrieval_omissions:
            self.retriever.last_diagnostics["deduplicated_source_ids"] = duplicate_retrieval_ids
            self.retriever.last_diagnostics["preference_omissions"] = preference_retrieval_omissions
        self.retriever.last_diagnostics["already_in_context_count"] = len(duplicate_retrieval_ids)
        self.retriever.last_diagnostics["additional_selected_count"] = len(retrieval_hits)

        craft_guides = select_craft_guides(task=task, genre=brief.genre, card=card, limit=1)
        plan_view = _plan_for_model(bundle, card)
        from .story_settings import StorySettingsService
        setting_context = StorySettingsService(self.project).context(chapter_no=chapter_no, query=task, actor=self.actor)
        sections = [
            ContextSection(key="A", title="当前任务与用户要求", content=task, hard=True),
            ContextSection(key="CSET", title="用户定制设定：创作假设与证据参考（非正史）",
                           content=setting_context, hard=False, cache_scope="chapter"),
            *outline_sections(self.project.root, chapter_no, chapter_no),
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
                cache_scope="global",
            ),
            ContextSection(
                key="B",
                title="不可违反的硬正史",
                content=json.dumps([_fact_for_model(item) for item in general_facts], ensure_ascii=False, indent=2)
                if general_facts
                else "当前尚无已提交事实。",
                source_ids=[str(item["fact_id"]) for item in general_facts],
                hard=True,
                cache_scope="canon",
            ),
            ContextSection(
                key="C0", title="书/卷/篇章规划",
                content=json.dumps({key: value for key, value in plan_view.items()
                                    if key not in {"本章卡", "相邻章节提醒"}}, ensure_ascii=False, indent=2),
                source_ids=["plan:book", f"plan:volume:{bundle.current_volume.volume_no}", bundle.current_arc.arc_id],
                hard=True, cache_scope="book",
            ),
            ContextSection(
                key="C", title="本章卡与相邻章节提醒",
                content=json.dumps({key: value for key, value in plan_view.items()
                                    if key in {"本章卡", "相邻章节提醒"}}, ensure_ascii=False, indent=2),
                source_ids=[f"plan:chapter:{chapter_no:05d}"], hard=True, cache_scope="chapter",
            ),
            ContextSection(
                key="D",
                title="当前人物状态与知识边界",
                content=json.dumps(
                    [
                        _state_for_model(item)
                        for item in state_facts
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[
                    str(item["fact_id"])
                    for item in state_facts
                ],
                hard=True,
                cache_scope="canon",
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
                cache_scope="chapter",
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
                cache_scope="chapter",
            ),
            ContextSection(
                key="WN", title="近期已接受章的创作意图参考（非正史）",
                content=json_dumps(recent_approved_notes(self.project, chapter_no)),
                source_ids=[], cache_scope="chapter",
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
                cache_scope="chapter",
            ),
            ContextSection(
                key="E",
                title="最近已接受章节与批次临时草稿",
                content="\n\n".join(recent_parts) or "尚无前章。",
                source_ids=recent_ids,
                hard=bool(set(named_numbers) & {int(item["chapter_no"]) for item in recent}),
                cache_scope="chapter",
            ),
            *cited_chapters,
            ContextSection(
                key="E1",
                title="近期章节结尾模式摘要",
                content=json.dumps(
                    {
                        "最近模式": ending_patterns,
                        "用途": "只用于避免连续章节采用相同收束方式；不得据此改写章节卡、正史或因果。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=ending_pattern_ids,
                cache_scope="chapter",
            ),
            ContextSection(
                key="F",
                title="分层混合检索结果",
                content=json.dumps(
                    {
                        "检索链": "精确查询 → 本地 BM25 → 可选 BGE-M3 → 融合 → 可选 reranker → 一跳关系扩展 → 权限/章节/版本过滤",
                        "自适应结果": supplemental_hits,
                        "参考身份": "story_reference/expression_reference 属于参考与设计记忆；引文和核对状态随条目提供，不能冒充已发生正史。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[str(item["source_id"]) for item in supplemental_hits],
                cache_scope="chapter",
            ),
            ContextSection(
                key="F0", title="本章检索命中的正史证据",
                content=json.dumps(retrieved_canon, ensure_ascii=False, indent=2) if retrieved_canon else "无额外正史事实命中。",
                source_ids=[str(item["source_id"]) for item in retrieved_canon],
                hard=True, cache_scope="chapter",
            ),
            ContextSection(
                key="G",
                title="未结伏笔、故事承诺与钩子义务",
                content=json.dumps([_thread_for_model(item) for item in threads], ensure_ascii=False, indent=2)
                if threads
                else "当前无已提交未结线索。",
                source_ids=[str(item["thread_id"]) for item in threads],
                hard=True,
                cache_scope="chapter",
            ),
            ContextSection(
                key="H",
                title="参考与设计记忆：作品表达特征",
                content=json.dumps(reference_cards, ensure_ascii=False, indent=2) if reference_cards else "本章未加载参考作品。",
                source_ids=[
                    f"reference:{item['reference_id']}"
                    for item in reference_cards
                    if item.get("reference_id")
                ],
                cache_scope="chapter",
            ),
            ContextSection(
                key="I",
                title="按本章任务选择的写作引导",
                content=json.dumps(craft_guides, ensure_ascii=False, indent=2),
                source_ids=[f"craft:{item['技能编号']}" for item in craft_guides],
                cache_scope="chapter",
            ),
            ContextSection(
                key="J",
                title="用户强制记忆与作品方向",
                content=json.dumps(
                    {
                        "题材": brief.genre,
                        "读者": brief.target_audience,
                        "目标字数": brief.target_chapter_words,
                        "核心卖点": brief.core_selling_point,
                        "本书硬规则": brief.user_rules,
                        "强制记忆": forced_texts,
                        "使用要求": "Writer 必须逐条遵守；发现互相冲突时不得自行猜测，由 Reviewer 标出冲突并交给用户处理。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=[str(item["preference_id"]) for item in forced_preferences],
                hard=True,
                cache_scope="book",
            ),
            ContextSection(
                key="J2",
                title="作品声音契约（非正史）",
                content=_voice_preferences_content(voice_preferences),
                source_ids=[
                    str(item["preference_id"])
                    for item in voice_preferences
                ],
            ),
            ContextSection(
                key="K",
                title="输出契约",
                content=_output_contract(
                    mode,
                    card,
                    hook_strategy=self.hook_strategy,
                    review_experience_detail=self.review_experience_detail,
                ),
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
        if mode in {"review", "revise"} and not current_material_in_query:
            warnings.append("本次审查或续修未提供当前正文/选区材料，检索仅依据用户任务与章节卡；不能据此声称已按实际正文取材。")
        sections.extend(self._recovery_guidance(chapter_no))
        stale_summaries = [int(item["chapter_no"]) for item in recent_for_patterns
                           if item.get("summary_stale") and
                           not database.rebuilt_chapter_summary(int(item["chapter_no"]))]
        if stale_summaries:
            warnings.append(
                "以下已接受章节的旧摘要或线索补丁与当前正文版本不同，"
                "已停用未复核的派生记忆，改读数据库正史正文和版本有效的记忆；"
                "需要时从该章定向重核，不重写正史："
                + "、".join(f"第{number}章" for number in stale_summaries)
            )
        if len(forced_texts) > 24 or sum(len(item) for item in forced_texts) > 12_000:
            warnings.append(
                "强制记忆数量或体积偏大：已安全去除完全重复项，但没有删改任何原始记录；"
                "Writer 仍会读取全部内容，Reviewer 会核对冲突。建议在记忆页暂停过期项或合并同义规则。"
            )
        thread_recovery = self._thread_history_context(thread_state, sections,
            boundary=chapter_no - 1, protected_input=protected_input)
        source_gaps = self.retriever.last_diagnostics.get("source_gaps", [])
        for gap in source_gaps:
            if gap.get("kind") != "chapter_summary" or any(gap.get(key) is None
                    for key in ("chapter_no", "version", "source_hash")):
                # A missing source or unknown thread lifecycle is not an identified
                # stale summary. Preserve its diagnostic instead of inventing identity.
                continue
            work = self.project.record_pending_work(kind="memory_summary", reason=gap["reason"],
                source={key: gap.get(key) for key in ("chapter_no", "version", "source_hash", "owner")},
                next_action=("已本地回源到同版正史正文，记忆责任角色定向重核摘要；不为此重写正文。"
                             if gap["state"] == "canonical_source_available" else
                             "先恢复此源章的正史正文投影，再由记忆责任角色重核摘要；不能采用旧摘要。"),
                status="waiting_condition")
            self.project.update_pending_work(work["id"], progress={"step": "canonical_source_lookup",
                "status": gap["state"], "reason": gap.get("source_problem", gap["reason"] )})
        for work in self.project.pending_work():
            if work["kind"] != "memory_summary":
                continue
            source = work["source"]
            current = database.get_chapter(source["chapter_no"])
            if current and (current["version"] != source["version"] or current["content_hash"] != source["source_hash"]):
                self.project.update_pending_work(work["id"], status="superseded", resolution="源章版本已变化，旧摘要任务不沿用。")
            elif current and ((not current.get("summary_stale") and current.get("summary"))
                             or database.rebuilt_chapter_summary(source["chapter_no"])):
                self.project.update_pending_work(work["id"], status="completed",
                    resolution="同版正文已有有效原摘要或记忆责任角色核证的派生摘要缓存，正史未改动。")
        sections.append(ContextSection(key="MEMORY", title="本次四层记忆读取与回源诊断",
            content=json_dumps({"layers": ["正史", "任务临时", "作者偏好", "参考与设计"],
                "boundary_chapter": chapter_no - 1, "source_gaps": source_gaps,
                "query_input": query_input,
                "thread_history": thread_recovery,
                "retrieval": {**{key: self.retriever.last_diagnostics.get(key) for key in
                    ("query", "candidate_count", "retrieved_count", "already_in_context_count",
                     "additional_selected_count", "discarded", "discarded_display_limit", "source_reads", "preference_omissions")},
                    "branches": self.retriever.last_diagnostics.get("branches", {}),
                    "overlaps": self.retriever.last_diagnostics.get("overlaps", []),
                    "source_conflicts": self.retriever.last_diagnostics.get("source_conflicts", []),
                    "selected": [{key: item.get(key) for key in
                        ("source_id", "source_type", "score", "reasons", "retrieval_reasons",
                         "body_kind", "identity", "resolved_source")}
                        for item in self.retriever.last_diagnostics.get("selected", [])]},
                "meaning": "候选、检索命中、装入上下文和模型采用是不同阶段；未命中不表示不存在。"}),
            cache_scope="chapter"))
        pending = self.project.pending_work(chapter_no=chapter_no)
        if pending:
            sections.append(ContextSection(key="WORK", title="本章进度与待处理工作（不是正史或放行结论）",
                content=json_dumps([{key: item.get(key) for key in
                    ("id", "kind", "status", "reason", "next_action", "progress", "source", "resolution")}
                    for item in pending]), hard=True, cache_scope="chapter"))
        packet = ContextPacket(
            project_id=self.project.project_id,
            chapter_no=chapter_no,
            task=task,
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join([*[item.content for item in sections], protected_input])),
            warnings=warnings,
        )
        packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
        before_compression = packet.estimated_tokens
        if packet.estimated_tokens >= int(self.soft_token_limit * 0.9):
            compressed = self._shrink_soft_sections(packet, protected_input=protected_input)
            packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
            if compressed:
                packet.warnings.append(
                    "接近软预算，已按相关性和资料权限压缩：" + "、".join(compressed) + "；硬约束未动。"
                )
                packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
        self._publish_context_status(packet, before_compression, has_protected_input=bool(protected_input),
                                     protected_input=protected_input, role=self.actor)
        if packet.estimated_tokens > self.hard_token_limit:
            if named_numbers:
                required = [{"chapter_no": number,
                             "estimated_tokens": named_source_tokens[number]} for number in named_numbers]
                reason = (f"任务点名的旧章全文与必要材料合计 {packet.estimated_tokens} token，"
                          f"超过当前输入上限 {self.hard_token_limit}；点名来源："
                          + "、".join(f"第{item['chapter_no']}章（约{item['estimated_tokens']} token）" for item in required))
                self.project.record_pending_work(kind="context_source", reason=reason,
                    source={"required_chapters": required, "boundary_chapter": chapter_no - 1},
                    next_action="明确缩小本次必要点名范围或提高当前角色上下文容量后续接资料节点；不得静默只读前三章。",
                    status="waiting_condition")
                raise ValidationGateError(reason)
            raise ValidationGateError(
                "硬约束本身已超过最大上下文容量，墨流没有静默删除正史或用户指令；"
                "请在上下文面板查看占用并缩小任务范围。"
            )
        available_by_id = {str(item["preference_id"]): item for item in available_pool}
        eligible_ids = {str(item["preference_id"]) for item in applicable}
        task_ids = {str(item["preference_id"]) for item in task_preferences}
        preloaded_ids = {str(item["preference_id"]) for item in [*forced_preferences, *voice_preferences]}
        preloaded_ids.update(str(item["source_id"]) for item in supplemental_hits
                             if item.get("source_type") == "user_preference")
        usage_sources = {str(item["preference_id"]): "硬要求" for item in forced_preferences}
        voice_content = next((section.content for section in packet.sections if section.key == "J2"), "{}")
        final_voice = json.loads(voice_content).get("本章可用偏好", [])
        for item in final_voice:
            identity = str(item.get("编号", ""))
            if identity in available_by_id and item.get("要求") == available_by_id[identity]["text"]:
                usage_sources[identity] = "声音段"
        for section in packet.sections:
            if section.key != "F":
                continue
            for hit in json.loads(section.content).get("自适应结果", []):
                identity = str(hit.get("source_id", ""))
                if (hit.get("source_type") == "user_preference" and identity in eligible_ids
                        and identity in available_by_id
                        and available_by_id[identity]["text"] in str(hit.get("body", ""))):
                    usage_sources.setdefault(identity, "补充检索")
        omitted = []
        for identity, item in available_by_id.items():
            if identity in usage_sources:
                continue
            if identity not in task_ids:
                reason = "本任务已核验的偏好冲突取舍，保存条目仍保留"
            elif identity not in eligible_ids:
                reason = "本章适用范围不匹配，或被当前适用的本书同主题偏好覆盖"
            elif identity in preloaded_ids:
                reason = "整体上下文压缩后未完整装入；不计作已使用"
            else:
                reason = "完整条目超出8,000 token声音段预算，且本轮补充检索未选中"
            omitted.append({"preference_id": identity, "text": item["text"],
                            "status": item.get("status", "active"), "reason": reason})
        selection = {"chapter_no": chapter_no, "mode": mode, "actor": self.actor, "task": task,
            "recorded_at": utc_now(), "context_packet_id": content_hash(packet.to_model_prompt()),
            "voice_budget_tokens": 8_000, "voice_selected_tokens": estimate_tokens(voice_content),
            "selected": [{**item, "usage_source": usage_sources[str(item["preference_id"])]}
                         for item in available_by_id.values() if str(item["preference_id"]) in usage_sources],
            "omitted": omitted,
            "meaning": "已完整装入最终编译上下文，不表示模型已经正确采用；candidate仍是未确认参考。"}
        database.set_metadata("preference.selection", selection)
        database.set_metadata(f"preference.selection:{chapter_no}:{mode}:{self.actor}", selection)
        return packet

    def _recovery_guidance(self, chapter_no: int) -> list[ContextSection]:
        guidance = self.project.db.learning_guidance(chapter_no=chapter_no, role=self.actor)
        cases = guidance.get("recovery_cases", [])
        if not cases:
            return []
        return [ContextSection(key="RECOVERY", title="已核来源的流程恢复经验（非本章事实）",
            content=json_dumps({"cases": cases, "policy": guidance.get("recovery_policy", {})}),
            hard=False, cache_scope="chapter")]

    def _thread_history_context(self, state: dict[str, Any], sections: list[ContextSection], *,
                                boundary: int, protected_input: str = "") -> dict[str, Any]:
        """Read valid boundary-local prose for unknown history without guessing lifecycle."""
        loaded_ids = {source_id for section in sections for source_id in section.source_ids}
        hard_tokens = estimate_tokens("\n".join([protected_input,
            *[section.content for section in sections if section.hard]]))
        budget = min(SOURCE_RECOVERY_TOKEN_LIMIT, max(0, self.hard_token_limit - hard_tokens - 4096))
        # Adaptive retrieval excerpts and history recovery share the supplement cap.
        supplement = next((section for section in sections if section.key == "F"), None)
        if supplement and supplement.title == "分层混合检索结果":
            budget = max(0, budget - estimate_tokens(supplement.content))
        reads = []
        remaining_sources = max(0, 6 - sum(section.key.startswith("TH-source-") for section in sections))
        recovery = {int(item["chapter_no"]): item for item in state.get("recovery_sources", [])}
        for gap in state.get("coverage_gaps", []):
            number = gap.get("chapter_no")
            if number is not None and 0 < int(number) <= boundary and int(number) not in recovery:
                # Older DB rows may need the accepted Markdown projection. The
                # shared reader checks the managed path and hash before loading.
                recovery[int(number)] = {"chapter_no": int(number),
                    "source_version": gap.get("source_version"), "source_hash": gap.get("source_hash")}
        for item in recovery.values():
            number = int(item["chapter_no"])
            source_id = f"chapter:{number:05d}"
            record = {"chapter_no": number, "source_id": source_id,
                      "source_version": item.get("source_version"), "source_hash": item.get("source_hash")}
            if number > boundary:
                reads.append({**record, "state": "omitted", "reason": "超出本次正史回放边界"})
                continue
            if source_id in loaded_ids:
                reads.append({**record, "state": "already_in_context"})
                continue
            try:
                source = self.retriever._accepted_source(number, before_chapter=boundary + 1)
                if ((item.get("source_version") is not None and source["version"] != item["source_version"])
                        or (item.get("source_hash") is not None and source["content_hash"] != item["source_hash"])):
                    raise ValueError("回放来源版本已变化")
                record.update(source_version=source["version"], source_hash=source["content_hash"])
            except (ValueError, OSError, UnicodeError) as exc:
                reads.append({**record, "state": "source_unavailable", "reason": str(exc)})
                continue
            body = (f"来源 {source_id}，版本 {source['version']}，hash {source['content_hash']}。"
                    "原文供当前记忆责任角色核对；未重建的伏笔生命周期仍是 unknown。\n\n" + source["content"])
            tokens = estimate_tokens(body)
            if tokens > budget or remaining_sources <= 0:
                reads.append({**record, "state": "omitted", "reason": "完整原文超过六来源、剩余补读或当前角色输入预算",
                              "required_tokens": tokens, "remaining_tokens": budget})
                continue
            sections.append(ContextSection(key=f"TH-source-{number}", title=f"伏笔历史缺口回源：已接受第 {number} 章",
                content=body, source_ids=[source_id], hard=True, cache_scope="chapter"))
            loaded_ids.add(source_id)
            budget -= tokens
            remaining_sources -= 1
            reads.append({**record, "state": "canonical_source_loaded", "estimated_tokens": tokens})
        return {"boundary_chapter": boundary, "coverage_gaps": state.get("coverage_gaps", []),
                "source_reads": reads,
                "meaning": "unknown不是未结或已结；已读取原文只补依据，当前记忆责任角色同次核证候选，不据此改写正史。"}

    def _load_reference_cards(self, limit: int) -> list[dict]:
        return load_reference_cards(self.project, limit)

    def _studio_context(
        self,
        chapter_no: int,
        task: str,
        card: dict[str, Any],
    ) -> dict[str, Any]:
        """挑选相关人工条目；模型始终只看到编译后的单一 Packet。"""

        studio = StudioDatabase(self.project.internal / "studio.db")
        entries = [item for item in studio.list_bible_entries()
                   if not item.get("data", {}).get("story_settings_type")]
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
        database = self.project.db
        brief = database.get_brief()
        bundle = planning_bundle_for_chapter(self.project, start_chapter_no)
        if not bundle:
            raise ValidationGateError("尚未生成全书、卷和篇章规划。")
        accepted = {int(item["chapter_no"]): item for item in database.accepted_chapters()}
        provisional = {int(item["chapter_no"]): item for item in provisional_chapters or []}
        chapter_parts: list[str] = []
        source_ids: list[str] = []
        cards: list[dict[str, Any]] = []
        planning_scopes: dict[str, dict[str, Any]] = {}
        for chapter_no in range(start_chapter_no, end_chapter_no + 1):
            card = database.get_chapter_card(chapter_no)
            if not card:
                raise ValidationGateError(f"第 {chapter_no} 章缺少章节卡，不能进行篇章复审。")
            cards.append(_audit_card_for_model(card))
            chapter_plan = planning_bundle_for_chapter(self.project, chapter_no)
            if chapter_plan is None:
                raise ValidationGateError(f"第 {chapter_no} 章关联的近期计划缺失。")
            arc = chapter_plan.current_arc
            planning_scopes[arc.arc_id] = {
                "编号": arc.arc_id, "标题": arc.title, "范围": f"{arc.chapter_start}-{arc.chapter_end}",
                "承诺": arc.promise, "核心冲突": arc.central_conflict, "出口桥": arc.exit_bridge,
            }
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
            text = database.canonical_chapter_content(chapter_no)
            if text is None:
                path = self.project.root / item["path"]
                text = path.read_text(encoding="utf-8") if path.is_file() else ""
            if not text or content_hash(text) != item["content_hash"]:
                raise ValidationGateError(f"第 {chapter_no} 章正史原文缺失或版本不符，不能把投影改动当作已接受依据。")
            chapter_parts.append(f"### 第 {chapter_no} 章（已接受正史）\n\n{text}")
            source_ids.append(f"chapter:{chapter_no:05d}")

        facts = database.facts_as_of(end_chapter_no)
        root_thread_state = database.threads_state_as_of(start_chapter_no - 1)
        end_thread_state = database.threads_state_as_of(end_chapter_no)
        threads = end_thread_state["threads"]
        reference_cards = self._load_reference_cards(limit=6)
        task = f"复审第 {start_chapter_no}～{end_chapter_no} 章，并判断能否作为下一篇章可靠起点。"
        planning_documents = []
        for name, title in (
            ("OUTLINE.md", "当前全书大纲"),
            ("STORY_DETAIL.md", "当前卷细纲"),
            ("RECENT_PLAN.md", "当前近期章节规划"),
        ):
            path = self.project.root / name
            if not path.is_file():
                raise ValidationGateError(f"篇章复审缺少 {name}；不能仅靠旧章节卡声称对照了当前规划。")
            planning_documents.append(ContextSection(
                key=f"C{len(planning_documents) + 1}", title=title,
                content=path.read_text(encoding="utf-8"), source_ids=[name], hard=True,
                cache_scope="book" if name == "OUTLINE.md" else "chapter",
            ))
        prior_chapters = []
        for chapter_no in range(max(1, start_chapter_no - 2), start_chapter_no):
            item = accepted.get(chapter_no)
            if item:
                text = database.canonical_chapter_content(chapter_no)
                if text is None:
                    path = self.project.root / item["path"]
                    text = path.read_text(encoding="utf-8") if path.is_file() else ""
                if text and content_hash(text) == item["content_hash"]:
                    prior_chapters.append(ContextSection(
                        key=f"C{len(planning_documents) + len(prior_chapters) + 1}",
                        title=f"已接受第 {chapter_no} 章正文",
                        content=text,
                        source_ids=[f"chapter:{chapter_no:05d}"], hard=True, cache_scope="chapter",
                    ))
        sections = [
            ContextSection(key="A", title="当前任务与用户要求", content=task, hard=True),
            *planning_documents,
            *prior_chapters,
            ContextSection(
                key="B",
                title="不可违反的硬正史",
                content=json.dumps([_fact_for_model(item) for item in facts], ensure_ascii=False, indent=2)
                if facts
                else "当前尚无已提交事实。",
                source_ids=[str(item["fact_id"]) for item in facts],
                hard=True,
                cache_scope="canon",
            ),
            ContextSection(
                key="C",
                title="篇章承诺与待复审章节卡",
                content=json.dumps(
                    {
                        "涉及的近期计划": list(planning_scopes.values()),
                        "待复审章节卡": cards,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=list(planning_scopes),
                hard=True,
                cache_scope="chapter",
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
                cache_scope="canon",
            ),
            ContextSection(
                key="E",
                title="待复审章节正文",
                content="\n\n".join(chapter_parts),
                source_ids=source_ids,
                hard=True,
                cache_scope="chapter",
            ),
            ContextSection(
                key="F",
                title="相关历史索引",
                content=json.dumps(_fact_index_for_model(facts), ensure_ascii=False, indent=2) if facts else "无。",
                source_ids=[str(item["fact_id"]) for item in facts],
                cache_scope="canon",
            ),
            ContextSection(
                key="G",
                title="未结伏笔、故事承诺与钩子义务",
                content=json.dumps([_thread_for_model(item) for item in threads], ensure_ascii=False, indent=2)
                if threads
                else "当前无已提交未结线索。",
                source_ids=[str(item["thread_id"]) for item in threads],
                hard=True,
                cache_scope="canon",
            ),
            ContextSection(
                key="H",
                title="参考作品特征卡",
                content=json.dumps(reference_cards, ensure_ascii=False, indent=2) if reference_cards else "本次未加载参考作品。",
                source_ids=[
                    f"reference:{item['reference_id']}"
                    for item in reference_cards
                    if item.get("reference_id")
                ],
                cache_scope="chapter",
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
                cache_scope="book",
            ),
            ContextSection(
                key="J",
                title="输出契约",
                content="只输出篇章复审结构。不得续写、修改章节卡或提交正史。",
                hard=True,
            ),
        ]
        history = self._thread_history_context(root_thread_state, sections, boundary=start_chapter_no - 1)
        sections.append(ContextSection(key="TH", title="篇章起点前的伏笔回放与范围内历史缺口",
            content=json_dumps({"root_history": history,
                "root_threads": [_thread_for_model(item) for item in root_thread_state["threads"]],
                "range": {"start": start_chapter_no, "end": end_chapter_no,
                          "end_boundary_chapter": end_chapter_no,
                          "coverage_gaps": end_thread_state["coverage_gaps"]},
                "meaning": "起点前回放与待复审范围正文分别读取；临时章仍不是正史，unknown不得按未结线索推断。"}),
            hard=True, cache_scope="chapter"))
        work = [item for item in self.project.pending_work()
                if item.get("chapter_no") is None or start_chapter_no <= item["chapter_no"] <= end_chapter_no]
        if work:
            sections.append(ContextSection(key="WORK", title="复审范围内的进度与待处理工作",
                content=json.dumps([{key: item.get(key) for key in ("id", "kind", "status", "reason", "next_action", "source", "progress")}
                                    for item in work], ensure_ascii=False), hard=True))
        sections.extend(self._recovery_guidance(end_chapter_no))
        packet = ContextPacket(
            project_id=self.project.project_id,
            chapter_no=end_chapter_no,
            task=task,
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
            warnings=[],
        )
        packet.estimated_tokens = _packet_input_tokens(packet)
        before_compression = packet.estimated_tokens
        if packet.estimated_tokens >= int(self.soft_token_limit * 0.9):
            compressed = self._shrink_soft_sections(packet)
            packet.estimated_tokens = _packet_input_tokens(packet)
            if compressed:
                packet.warnings.append("篇章复审接近软预算，已压缩：" + "、".join(compressed) + "；硬约束未动。")
                packet.estimated_tokens = _packet_input_tokens(packet)
        self._publish_context_status(packet, before_compression, role=self.actor)
        if packet.estimated_tokens > self.hard_token_limit:
            raise ValidationGateError("篇章复审的硬材料超过最大上下文容量；请缩小复审章节范围。")
        return packet

    def _shrink_soft_sections(self, packet: ContextPacket, *, protected_input: str = "") -> list[str]:
        """只压缩低权威软资料；顺序固定且优先保留高相关候选和最近正文。"""

        actions: list[str] = []
        targets = {"RECOVERY": 4_000, "H": 4_000, "I": 4_000, "J1": 2_000, "J2": 4_000, "E1": 4_000, "F": 10_000, "E": 24_000, "D1": 8_000}
        for key in ("RECOVERY", "H", "J1", "I", "J2", "E1", "F", "E", "D1"):
            if _packet_input_tokens(packet, protected_input) <= int(self.soft_token_limit * 0.82):
                break
            section = next((item for item in packet.sections if item.key == key and not item.hard), None)
            if section is None:
                continue
            original = section.content
            if key in {"J2", "RECOVERY"}:
                value = json.loads(original)
                field = "本章可用偏好" if key == "J2" else "cases"
                kept = []
                for item in value.get(field, []):
                    trial = {**value, field: [*kept, item]}
                    if len(json.dumps(trial, ensure_ascii=False, indent=2)) <= targets[key]:
                        kept.append(item)
                value[field] = kept
                section.content = json.dumps(value, ensure_ascii=False, indent=2)
                if key == "J2":
                    section.source_ids = [str(item["编号"]) for item in kept]
            elif key == "F":
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
            if key not in {"J2", "RECOVERY"} and len(section.content) > limit:
                section.content = _fit_soft_content(section.content, limit, keep_tail=key == "E")
            if section.content != original:
                actions.append(section.title)
        return actions

    def _publish_context_status(
        self,
        packet: ContextPacket,
        before_compression: int,
        *,
        has_protected_input: bool = False,
        protected_input: str = "",
        role: str = "planner",
    ) -> None:
        soft_sections = [item for item in packet.sections if not item.hard]
        hard_sections = [item for item in packet.sections if item.hard]
        ratio = packet.estimated_tokens / max(1, self.hard_token_limit)
        status = "safe" if ratio < 0.7 else "watch" if ratio < 0.9 else "near_limit"
        status_path = self.project.internal / "context-status.json"
        previous: dict[str, Any] = {}
        try:
            raw_previous = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(raw_previous, dict) and raw_previous.get("project_id") in {None, "", self.project.project_id}:
                previous = raw_previous
        except (OSError, json.JSONDecodeError):
            previous = {}
        previous_updated_at = str(previous.get("updated_at") or "")
        previous_estimated = int(previous.get("estimated_tokens") or 0) if previous_updated_at else 0
        updated_at = utc_now()
        ordered = sorted(enumerate(packet.sections), key=lambda pair: (
            {"global": 0, "book": 1, "canon": 2, "chapter": 3, "request": 4}[pair[1].cache_scope],
            {"J": 0, "B": 1, "O0": 2, "O1": 3, "O2": 4, "C0": 5}.get(pair[1].key, 10 + pair[0]) if pair[1].cache_scope == "book" else pair[0],
        ))
        prefix_sections = [item for _, item in ordered if item.cache_scope in {"global", "book"}]
        prefix_hashes = {
            item.key: hashlib.sha256((
                f"## {item.key}. {item.title}\n\n{item.content or '（无）'}\n\n"
                f"来源：{', '.join(sorted(item.source_ids))}"
            ).encode("utf-8")).hexdigest()
            for item in prefix_sections
        }
        prefix_history = previous.get("prefix_history_by_role") or {}
        if not isinstance(prefix_history, dict):
            prefix_history = {}
        old_hashes = prefix_history.get(role, {})
        changed_keys = [key for key, value in prefix_hashes.items() if old_hashes.get(key) != value]
        changed_keys.extend(key for key in old_hashes if key not in prefix_hashes)
        prefix_history[role] = prefix_hashes
        payload = {
            "project_id": self.project.project_id,
            "source_revision": project_source_revision(self.project.root, self.project.internal),
            "updated_at": updated_at,
            "chapter_no": packet.chapter_no,
            "actor": role,
            "task": packet.task,
            "estimated_tokens": packet.estimated_tokens,
            "input_budget_scope": "to_model_prompt编译资料及调用者protected_input；Provider另核系统契约、Schema、追加指令和输出预算。",
            "protected_input_tokens": estimate_tokens(protected_input) if protected_input else 0,
            "protected_input_hash": content_hash(protected_input) if protected_input else None,
            "before_compression_tokens": before_compression,
            "previous_updated_at": previous_updated_at,
            "previous_estimated_tokens": previous_estimated if previous_updated_at else None,
            "change_since_previous_tokens": (packet.estimated_tokens - previous_estimated) if previous_updated_at else None,
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
                    "estimated_tokens": estimate_tokens(item.content),
                    "hard": item.hard,
                    "source_count": len(item.source_ids),
                }
                for item in packet.sections
            ],
            "retrieval_diagnostics": self.retriever.last_diagnostics,
            "cache_prefix": {
                "role": role,
                "changed_sections": changed_keys,
                "first_changed_section": changed_keys[0] if changed_keys else None,
                "reason": "首次记录该角色书籍段" if not old_hashes else ("书籍段内容、标题或来源已变化" if changed_keys else "书籍段未变化；还须对照调用记录中的模型、系统契约与前段输入指纹"),
                "estimated_prefix_tokens": estimate_tokens("\n".join(item.content for item in prefix_sections)),
            },
            "prefix_history_by_role": prefix_history,
            "warnings": packet.warnings,
        }
        atomic_write_text(status_path, json_dumps(payload))


def _fact_for_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "事实编号": item["fact_id"],
        "主体": item["subject"],
        "关系": item["predicate"],
        "内容": item["value"],
        "生效章节": item["valid_from_chapter"],
        "正文证据": item.get("evidence", ""),
        "关联证据": item.get("evidence_refs", []),
        "认识类型": item.get("epistemic_kind", "objective"),
        "事件时间": item.get("event_time"),
        "叙述时间": item.get("narrative_time"),
        "来源章节": item.get("source_chapter"),
        "来源版本": item.get("source_version"),
    }


def _state_for_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "主体": item["subject"],
        "状态或认知": item["predicate"],
        "当前内容": item["value"],
        "事实编号": item["fact_id"],
        "认识类型": item.get("epistemic_kind", "objective"),
        "来源章节": item.get("source_chapter"),
        "来源版本": item.get("source_version"),
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
            "决定": item.decision,
            "后果": item.consequence,
            "不可逆变化": item.irreversible_delta,
            "本章释放信息": item.information_release,
            "场景边界": item.scenes,
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
        "相邻章节提醒": {
            "边界规则": (
                "相邻卡用于防止提前消费和重复演出。当前正文不得把下一章的核心决定、"
                "信息释放或首次会面完整演完；若前章已经实际发生某个动作，本章必须承接其结果，"
                "不能再把它写成首次发生。"
            ),
            "章节": neighbours,
        },
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


def _recent_ending_patterns(project_root: Path, chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """提取可解释的结尾形态，不保存或复述章节原文。"""

    patterns: list[dict[str, Any]] = []
    for item in chapters:
        if "content" in item:
            content = str(item["content"]).strip()
        else:
            path = project_root / str(item["path"])
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8").strip()
        tail = content[-800:]
        final_paragraph = next((part.strip() for part in reversed(re.split(r"\n\s*\n", tail)) if part.strip()), tail)
        signals: list[str] = []
        if re.search(r"[？?]", final_paragraph):
            signals.append("悬问")
        if re.search(r"[“\"].{0,80}[”\"]\s*[。！？!?]?$", final_paragraph, re.S):
            signals.append("对话截停")
        if re.search(r"忽然|突然|竟然|发现|看见|听见|原来|真相|秘密", tail):
            signals.append("新信息揭示")
        if re.search(r"决定|必须|不能再|选择|答应|拒绝|转身|出发", tail):
            signals.append("决定或行动")
        if re.search(r"危险|警报|血|死|杀|追|逼近|崩塌|爆炸|失踪", tail):
            signals.append("迫近危险")
        if not signals:
            signals.append("余波收束")
        patterns.append(
            {
                "章节": int(item["chapter_no"]),
                "来源": str(item.get("source_kind") or "已接受章节"),
                "结尾形态": signals[:3],
                "末段长度": len(final_paragraph),
            }
        )
    return patterns


def _hook_strategy_instruction(value: str) -> str:
    return {
        "most_chapters": "大多数章节都应形成自然的前向期待；普通章可使用低强度余波、决定或信息差，禁止机械反转。",
        "key_chapters": "重点章使用明确钩子；普通章只要保持未完成的行动、关系或信息即可，不强求强悬念。",
        "natural_afterglow": "优先自然余味；只有章节卡或剧情本身需要时才加强悬念，但结尾仍要留下可继续阅读的期待。",
    }.get(value, "大多数章节都应形成自然的前向期待。")


def _review_detail_instruction(value: str) -> str:
    return {
        "concise": "阅读体验建议保持精简，最多指出 1 个最有价值的非阻断改进点。",
        "standard": "阅读体验建议保持适量，优先指出最影响吸引力的 1 至 3 个非阻断改进点。",
        "detailed": "可较详细说明阅读体验，但仍须区分硬门禁与可选建议，不得用建议阻止通过。",
    }.get(value, "阅读体验建议保持适量，并与硬门禁分开。")


def _output_contract(
    mode: str,
    card: dict[str, Any],
    *,
    hook_strategy: str = "most_chapters",
    review_experience_detail: str = "standard",
) -> str:
    if mode == "review":
        return (
            "只输出审查报告结构，并单独填写 hook_assessment；有意留白与表达混乱必须区分。"
            "钩子的未知项、延后回应和开放问题本身不算表达不清；只有锚点缺失、与正史冲突或正文无法理解时才按证据报告。"
            "没有可直接证明的硬问题时必须通过；不续写、不重写正文。"
            + _review_detail_instruction(review_experience_detail)
        )
    return (
        "只输出章节草稿结构。正文约 "
        f"{card['target_words']} 字，完成目标—阻力—决定—后果与不可逆变化即可；"
        "表达、场景调度与修辞可自由发挥；正文外填写 hook_note，说明实际锚点、留白边界和预计回应，"
        "不得把说明混入小说正文。"
        + _hook_strategy_instruction(hook_strategy)
    )
