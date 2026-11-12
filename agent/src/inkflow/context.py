from __future__ import annotations

import hashlib
import json
import math
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from .craft import select_craft_guides
from .errors import ValidationGateError
from .project import InkFlowProject
from .planning_pipeline import load_active_planning, synchronize_v2_projection
from .project_lock import project_write_lock_sync
from .outline_context import outline_sections
from .retrieval import HybridRetriever, load_reference_cards
from .narrative_memory import NARRATIVE_COMPARISON_CONTRACT
from .story_promises import chapter_ending_contract, packet_ending_contract, planning_promises, thread_catalog
from .schemas import ArcPlan, ContextPacket, ContextSection, PlanBundle, VolumePlan
from .studio import StudioDatabase
from .utils import SOURCE_RECOVERY_TOKEN_LIMIT, atomic_write_text, content_hash, estimate_tokens, json_dumps, project_source_revision, utc_now
from .writer_notes import recent_approved_notes


_COMPRESSED_NOTE = "（已按完整条目压缩；完整资料仍保存在本地）"
_RECENT_PROSE_SOFT_LIMIT = 24_000
active_batch_context: ContextVar[dict[str, Any] | None] = ContextVar("inkflow_batch_operation", default=None)


def mandatory_rules_context(packet: ContextPacket) -> str:
    """短步骤也带齐本任务硬约定；普通设定的详细证据仍交既有分类检索，不重复整包前史。"""
    sections = []
    for section in packet.sections:
        if section.key in {"A", "A0", "J"} or section.key == "D2" and section.hard:
            sections.append(section)
        elif section.key == "B":
            try:
                facts = json.loads(section.content)
                rules = [item for item in facts if item["主体"] in {"世界", "世界观", "全书"}
                    or item["关系"].startswith(("rule.", "world.", "constraint."))]
                if rules:
                    sections.append(section.model_copy(update={"content": json_dumps(rules)}))
            except (ValueError, KeyError, TypeError, AttributeError):
                # 规则分类不可靠时保留原硬段；不凭猜测剔除潜在的世界约束。
                sections.append(section)
        elif section.key == "CSET":
            try:
                value = json.loads(section.content)
                collections = []
                for collection in value["collections"]:
                    records = [record for record in collection["records"]
                               if record.get("epistemic_status") == "user_constraint"]
                    if records:
                        collections.append({**collection, "records": records})
                sections.append(section.model_copy(update={"content": json_dumps({
                    "authority": value.get("authority"), "instructions": value.get("instructions"),
                    "collections": collections})}))
            except (ValueError, KeyError, TypeError, AttributeError):
                # 不认识的结构保留原块，不能把解析失败误当作本书没有硬约定。
                sections.append(section)
    if packet_ending_contract(packet).get("remaining") == 1:
        sections.extend(section for section in packet.sections if section.key == "ENDING")
    return "\n\n".join(f"## {item.title}\n{item.content}" for item in sections)


def named_prior_chapters(task: str, before_chapter: int, loaded: set[int]) -> list[int]:
    """识别实际回读的前章；保护范围不要求装入全文，旧任务按冻结版本重建。"""
    from .task_settings import active_task_settings
    scope = active_task_settings.get()
    distinguish_protection = scope is None or scope.settings.prompt_cache_layout_version >= 15
    numbers = []
    for match in re.finditer(r"第\s*(\d+)\s*(?:章)?\s*(?:[~～—–-]|至|到)\s*(?:第\s*)?(\d+)\s*章|第\s*(\d+)\s*章", task):
        if distinguish_protection:
            # “正史不动”是写入边界，仍留在任务硬约定中；只有另有明确回读才整章加载。
            left = re.split(r"[。！？；;\n，,]", task[:match.start()])[-1]
            right = re.split(r"[。！？；;\n，,]", task[match.end():], maxsplit=1)[0]
            clause = left + match.group(0) + right
            reads = list(re.finditer(r"回读|读取|阅读|查看|对照|核对|比对|参考|检查|审查|审核|复审|只读", clause))
            positive_read = any(not re.search(r"(?:不|别|勿|无需|不要|不得|禁止|不需要)(?:再)?\s*$",
                clause[:item.start()]) for item in reads)
            protected = re.search(
                r"不动|不改|不要改|不得改|禁止改|勿改|保持(?:原样|不变)|(?:不|无需|不要|不得|禁止|不需要)(?:再)?(?:读取|阅读|回读|参考)", clause)
            # “保留第13章的线索”仍可能需要回源；仅整章保留的表述当作保护范围。
            preserve_whole = re.fullmatch(r"\s*保留\s*" + re.escape(match.group(0)) + r"\s*(?:正史|正文|原文|现稿)?\s*", clause)
            if not positive_read and (protected or preserve_whole):
                continue
        first, last = (int(match[1]), int(match[2])) if match[1] else (int(match[3]), int(match[3]))
        first, last = max(1, first), min(last, before_chapter - 1)
        if last - first > 1000:
            raise ValidationGateError("点名前文范围过大，请缩小到本次实际需要回读的章节。")
        numbers.extend(number for number in range(first, last + 1) if number not in loaded)
    return list(dict.fromkeys(numbers))


def _chapter_history_query(task: str, card: dict[str, Any], mode: str, protected_input: str) -> str:
    query = "\n".join([task, *[str(card.get(key) or "") for key in
        ("title_working", "function", "goal", "obstacle", "information_release")],
        *[" ".join(str(value) for value in card.get(key) or []) for key in ("foreshadow_advance", "payoff")]])
    if mode in {"review", "revise"} and protected_input:
        # This material is supplied by the current task caller, including prose,
        # selection surroundings or its same-version review. It is never canon.
        query += "\n" + protected_input
    return query


def _batch_canonical_query(project: Any, chapter_no: int, history_query: str) -> tuple[str, dict | None]:
    binding = active_batch_context.get()
    if not binding or binding["root"] != project.root.resolve():
        return history_query, None
    manifest = binding["manifest"]
    start, end = int(manifest["start_chapter_no"]), int(manifest["end_chapter_no"])
    if not start <= chapter_no <= end:
        return history_query, None
    window_start = start + (chapter_no - start) // 10 * 10
    window_end = min(end, window_start + 9)
    cards = [project.db.get_chapter_card(number) for number in range(window_start, window_end + 1)]
    if not all(cards):
        return history_query, None
    query = str(manifest.get("instruction") or "") + "\n" + "\n".join(
        _chapter_history_query("", item, "draft", "") for item in cards)
    return query, {"start": window_start, "end": window_end, "query_hash": content_hash(query)}


def _packet_input_tokens(packet: ContextPacket, protected_input: str = "") -> int:
    return estimate_tokens(packet.to_model_prompt() + ("\n\n" + protected_input if protected_input else ""))


def planning_bundle_for_chapter(project: InkFlowProject, chapter_no: int) -> PlanBundle | None:
    """Resolve the card's persisted plan, not whichever window was opened last."""
    from .manual_edits import ManualEditsService
    ManualEditsService(project).assert_planning_ready(chapter_no)
    current = project.db.get_current_plan_bundle()
    active_v2 = load_active_planning(project)
    from .chapter_goals import temporary_goal_bundle
    temporary = temporary_goal_bundle(project, chapter_no)
    if temporary is not None:
        return temporary
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


def _covered_recent_source(hit: dict[str, Any], recent: dict[int, dict[str, Any]],
                           retained_prose: str) -> str | None:
    """Reuse only a verified canonical excerpt whose whole chapter survives E compression."""
    resolved = hit.get("resolved_source")
    if hit.get("body_kind") != "accepted_prose" or not isinstance(resolved, dict):
        return None
    number, version = resolved.get("chapter_no"), resolved.get("version")
    if type(number) is not int or number < 1 or type(version) is not int or version < 1:
        return None
    loaded = recent.get(number)
    if not loaded or loaded.get("version") != version:
        return None
    canonical, source_hash = loaded.get("content"), resolved.get("source_hash")
    identity = f"chapter:{number:05d}"
    if (not isinstance(canonical, str) or not source_hash
            or resolved.get("source_id") != identity
            or loaded.get("content_hash") != source_hash or content_hash(canonical) != source_hash
            or hit.get("version") != version or hit.get("source_hash") != source_hash):
        return None
    body, start, end = hit.get("body"), resolved.get("start"), resolved.get("end")
    if (not isinstance(body, str) or not body.strip() or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(canonical) or canonical[start:end] != body):
        return None
    # Keep the chapter heading too: evidence lookup must still scope this source to its own prose.
    block = f"### 第 {number} 章\n\n{canonical}".strip()
    return identity if block in retained_prose else None


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
    preferences: list[dict[str, Any]], task: str, card: dict[str, Any], limit: int | None = None,
    feedback_scores: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Budget the exact JSON for upstream scope-filtered preferences and candidates."""
    ranked = []
    for index, item in enumerate(preferences):
        candidate = item.get("status") == "candidate"
        if item.get("strength") != "weak" and not candidate:
            continue
        targeted = str(item.get("scope") or "project").partition(":")[0] != "project"
        ranked.append((not candidate, targeted, (feedback_scores or {}).get(item["preference_id"], 0.0), -index, item))
    ranked.sort(key=lambda row: row[:4], reverse=True)
    selected = []
    for _, _, _, _, item in ranked:
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
        setting_context_max_chars: int | None = None,
        embedding_model: str = "",
        reranker_model: str = "",
        hook_strategy: str = "most_chapters",
        review_experience_detail: str = "standard",
        quality_preset: str = "custom",
        retrieval_policy_version: int = 1,
        prompt_cache_layout_version: int = 1,
        actor: str = "writer",
        output_reserve_tokens: int = 0,
        cache_policy: Literal["standard", "deepseek", "mimo", "openai"] = "standard",
        input_token_factor: float = 1.0,
    ):
        self.project = project
        self.actor = actor
        self.soft_token_limit = soft_token_limit
        self.hard_token_limit = hard_token_limit or max(soft_token_limit, 512_000)
        self.configured_soft_token_limit = self.soft_token_limit
        self.configured_hard_token_limit = self.hard_token_limit
        self.output_reserve_tokens = max(0, int(output_reserve_tokens))
        self.input_token_factor = max(1.0, float(input_token_factor))
        # Packet measurements remain in the shared raw-estimate units. Only
        # convert available INPUT capacity; the output reserve is not scaled.
        if self.input_token_factor != 1.0:
            self.hard_token_limit = int((self.configured_hard_token_limit - self.output_reserve_tokens) / self.input_token_factor)
            self.soft_token_limit = min(int(self.configured_soft_token_limit / self.input_token_factor), self.hard_token_limit)
        # 设定取材沿原任务规模，发送线提高不应把未读过的设定塞进旧审核输入。
        self.setting_context_max_chars = (setting_context_max_chars if setting_context_max_chars is not None
            else min(64000, max(1000, self.hard_token_limit // 8)))
        self.retriever = HybridRetriever(
            project,
            embedding_model=embedding_model,
            reranker_model=reranker_model,
        )
        self.hook_strategy = hook_strategy
        self.review_experience_detail = review_experience_detail
        self.quality_preset = quality_preset
        self.retrieval_policy_version = retrieval_policy_version
        self.prompt_cache_layout_version = prompt_cache_layout_version
        self.cache_policy = cache_policy

    def build(
        self,
        chapter_no: int,
        task: str,
        *,
        mode: Literal["draft", "review", "revise"] = "draft",
        recent_limit: int | None = None,
        provisional_chapters: list[dict[str, Any]] | None = None,
        protected_input: str = "",
        checks: tuple[str, ...] = (),
    ) -> ContextPacket:
        """为当前角色编译同一任务、同一正文版本可用的资料和输出要求。"""
        self.hard_token_limit = int((self.configured_hard_token_limit - self.output_reserve_tokens) / self.input_token_factor)
        if self.hard_token_limit <= 0:
            raise ValidationGateError("当前角色的输入资料预算不可用，请核对资料容量设置。")
        self.soft_token_limit = min(int(self.configured_soft_token_limit / self.input_token_factor), self.hard_token_limit)
        database = self.project.db
        brief = database.get_brief()
        bundle = planning_bundle_for_chapter(self.project, chapter_no)
        card = database.get_chapter_card(chapter_no)
        temporary_goal = bool(bundle and bundle.current_arc.arc_id.startswith("task-goal:"))
        if not bundle:
            raise ValidationGateError("尚未生成全书、卷和篇章规划。")
        if not card:
            raise ValidationGateError(f"缺少第 {chapter_no} 章章节卡，写作门禁拒绝继续。")

        facts = database.facts_as_of(chapter_no - 1)
        thread_state = database.threads_state_as_of(chapter_no - 1)
        threads = thread_state["threads"]
        promise_plans = planning_promises(bundle)
        promise_ids = {plan["thread_id"] for plan in promise_plans}
        promise_prior = [{key: value for key, value in item.items() if key != "history"}
                         for item in thread_catalog(self.project, bundle, before_chapter=chapter_no)
                         if item.get("status") not in {"paid", "abandoned"}
                         or item["thread_id"] in promise_ids]
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
        from .learning import LearningService
        voice_preferences = _voice_preferences_for_context([*preferences, *candidates], task, preference_scope,
            feedback_scores=LearningService(self.project).preference_scores(applicable))
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
        prior_provisional = [item for item in provisional_chapters or [] if int(item["chapter_no"]) < chapter_no]
        recent_provisional = [item for index, item in enumerate(prior_provisional)
                              if index >= len(prior_provisional) - (4 if mode == "review" else 2)
                              or int(item["chapter_no"]) in named_numbers]
        loaded_numbers = {int(item["chapter_no"]) for item in [*recent, *recent_provisional]}
        named_source_tokens = {int(item["chapter_no"]): estimate_tokens(item["content"])
                               for item in [*recent, *recent_provisional] if int(item["chapter_no"]) in named_numbers}
        for number in named_prior_chapters(task, chapter_no, loaded_numbers):
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
        for item in recent_provisional:
            provisional_no = int(item["chapter_no"])
            provisional_content = str(item["content"])
            reference_title = (f"临时前章草稿 v{item['version']}" if item.get("source_kind") == "explicit_draft_reference"
                else "批次临时草稿")
            recent_parts.append(
                f"### {reference_title} · 第 {provisional_no} 章（尚未进入正史）\n\n"
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
                    "narrative_events": memory_patch.get("narrative_events", []),
                    "facts": memory_patch.get("facts", []),
                    "operations": memory_patch.get("operations", []),
                    "threads": memory_patch.get("threads", []),
                }
            )
            provisional_memory_ids.append(
                f"batch-memory:{item.get('batch_id', 'current')}:chapter:{provisional_no:05d}"
            )

        history_query = _chapter_history_query(task, card, mode, protected_input)
        canonical_query = history_query
        batch_scope = None
        if mode == "draft" and self.actor == "writer":
            canonical_query, batch_scope = _batch_canonical_query(self.project, chapter_no, history_query)
        batch_binding = active_batch_context.get()
        stable_batch_order = bool(batch_binding and batch_binding["root"] == self.project.root.resolve()
            and int(batch_binding["manifest"]["start_chapter_no"]) <= chapter_no
            <= int(batch_binding["manifest"]["end_chapter_no"]))
        reference_cards = self._load_reference_cards(limit=6)
        chapter = database.get_chapter(chapter_no)
        # 只提升当前卡明确要求推进/兑现的线索，未来窗口不构成提前揭谜的授权。
        foreshadow_targets = [str(value) for key in ("foreshadow_advance", "payoff") for value in card.get(key) or []]
        foreshadow_targets.extend(str(beat["thread_id"]) + " " + str(beat["expected_change"])
            for beat in card.get("promise_beats") or [] if beat.get("action") in {"advance", "payoff"})
        foreshadow_ids = {beat["thread_id"] for beat in card.get("promise_beats") or []
                         if beat.get("action") in {"advance", "payoff"}}
        foreshadow_targets.extend(plan["title"] + " " + plan["causal_basis"]
                                  for plan in promise_plans if plan["thread_id"] in foreshadow_ids)
        retrieval_hits = self.retriever.retrieve(
            history_query,
            role=self.actor,
            chapter_no=chapter_no,
            chapter_version=int(chapter["version"]) if chapter else None,
            quality_preset=self.quality_preset if self.retrieval_policy_version >= 2 else None,
            context_token_budget=max(1800, self.soft_token_limit // 5)
                if self.retrieval_policy_version >= 3 else None,
            foreshadow_query="\n".join(foreshadow_targets) if self.prompt_cache_layout_version >= 16 else "",
        )
        reading_candidates = list(retrieval_hits)
        retrieved_candidates = list(retrieval_hits)
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
                or bool(subject and subject.casefold() in canonical_query.casefold())
            )

        # Accepted facts arrive in subject order; chapter order keeps unchanged canon
        # ahead of newly accepted facts in consecutive Writer cache prefixes.
        ordered_facts = sorted(facts, key=lambda item: (int(item["source_chapter"]), str(item["fact_id"])))
        # Writer already selects from the whole batch window; chapter-only review hits stay in the tail.
        common_fact_ids = [str(item["fact_id"]) for item in (ordered_facts if batch_scope else ordered_facts[:256])]
        mandatory_facts = ordered_facts[:256] + [item for item in ordered_facts[256:] if required_now(item)]
        if self.retrieval_policy_version >= 3:
            # 历史事实不是全局规则：只读当前需要的原记录；数据库保留其余全文与证据供回查。
            # 全局硬约定、最近承接事实、命中事实以及当前人物的最新状态完整保留。
            retrieved_fact_ids = {str(item["source_id"]) for item in retrieval_hits
                                  if item.get("source_type") == "canon_fact"}
            latest_states = {}
            for item in ordered_facts:
                if required_now(item) and str(item["predicate"]).startswith(("state.", "knows.", "believes.")):
                    latest_states[(item["subject"], item["predicate"], item.get("epistemic_kind"))] = item["fact_id"]
            latest_state_ids = set(latest_states.values())
            mandatory_facts = [item for item in ordered_facts if
                str(item["subject"]).strip() in {"世界", "世界观", "全书"}
                or str(item["predicate"]).startswith(("rule.", "world.", "constraint."))
                or str(item["fact_id"]) in retrieved_fact_ids
                or item["fact_id"] in latest_state_ids
                or int(item["source_chapter"]) >= max(1, chapter_no - 3)]
            common_fact_ids = [str(item["fact_id"]) for item in mandatory_facts
                if self.cache_policy != "openai" or str(item["subject"]).strip() in {"世界", "世界观", "全书"}
                or str(item["predicate"]).startswith(("rule.", "world.", "constraint."))]
            self.retriever.last_diagnostics["fact_selection"] = {
                "available": len(ordered_facts), "loaded": len(mandatory_facts),
                "deferred": len(ordered_facts) - len(mandatory_facts),
                "policy": "未装入不代表不存在；涉及更早经历或状态变动须按人物、物件和事件定点回源。必要原记录及全局硬规则不摘要。"}
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
        # 新写作包保留已选近期正文完整原句；后续标硬不能弥补此前已经按字符截掉的承接。
        preserve_recent = self.prompt_cache_layout_version >= 20 and self.actor == "writer" and mode == "draft"
        retained_recent = "\n\n".join(recent_parts) if preserve_recent else _fit_soft_content("\n\n".join(recent_parts), _RECENT_PROSE_SOFT_LIMIT, keep_tail=True)
        recent_sources = {int(item["chapter_no"]): item for item in recent}
        prose_coverage = [(str(item["source_id"]),
                           _covered_recent_source(item, recent_sources, retained_recent)
                           if str(item["source_id"]) not in pinned_sources else None)
                          for item in retrieval_hits]
        covered_prose = {identity: source for identity, source in prose_coverage if source}
        for identity, source in prose_coverage:
            if source != covered_prose.get(identity):
                covered_prose.pop(identity, None)
        if self.retrieval_policy_version < 2:
            already_loaded.update(covered_prose)
        else:
            # 新策略稍后对实际模型投影核实完整载荷；相同 ID 或状态索引不等于证据已提供。
            already_loaded.clear()
        duplicate_retrieval_ids = sorted(
            str(item["source_id"]) for item in retrieval_hits
            if str(item["source_id"]) in already_loaded
        )
        if covered_prose and self.retrieval_policy_version < 2:
            discarded = self.retriever.last_diagnostics.setdefault("discarded", [])
            discarded[:0] = [{"source_id": item["source_id"], "source_type": item.get("source_type"),
                              "chapter_no": item["resolved_source"]["chapter_no"],
                              "resolved_source": dict(item["resolved_source"]),
                              "retrieval_uses": item.get("retrieval_uses", []),
                              "reason": f"已由本次 {covered_prose[str(item['source_id'])]} 同版正史全文覆盖，压缩后仍保留。"}
                             for item in retrieval_hits if str(item["source_id"]) in covered_prose]
            del discarded[self.retriever.last_diagnostics.get("discarded_display_limit", 80):]
        if covered_prose:
            # 回源读取可复用同版前章，但新策略的最终装入仍在完整编译后单独核实。
            reading_candidates = [item for item in reading_candidates if str(item["source_id"]) not in covered_prose]
        eligible_preference_ids = {str(item["preference_id"]) for item in applicable}
        preference_retrieval_omissions = []
        selected_hits = []
        writer_reference_tokens = 0
        writer_reference_budget = min(12_000, max(4_000, self.soft_token_limit // 8))
        if self.quality_preset == "economy":
            writer_reference_budget = min(writer_reference_budget, 6_000)
        writer_reference_omissions = []
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
            if (self.retrieval_policy_version < 2 and mode == "draft" and self.actor == "writer" and identity not in pinned_sources
                    and item.get("source_type") not in {"canon_fact", "user_preference", "story_setting"}):
                tokens = estimate_tokens(str(item.get("body") or ""))
                if writer_reference_tokens + tokens > writer_reference_budget:
                    writer_reference_omissions.append({"source_id": identity, "reason": "本章软参考预算已满；必要点名原文、正史、设定与锁定资料另计"})
                    continue
                writer_reference_tokens += tokens
            selected_hits.append(item)
        retrieval_hits = selected_hits
        retrieved_canon = [item for item in retrieval_hits if item.get("source_type") == "canon_fact"]
        supplemental_hits = [item for item in retrieval_hits if item.get("source_type") != "canon_fact"]
        self.retriever.last_diagnostics["selected"] = [{key: item.get(key) for key in
            ("source_id", "source_type", "score", "reasons", "retrieval_reasons", "body_kind", "identity", "resolved_source", "source_fingerprint")}
            for item in retrieval_hits]
        if duplicate_retrieval_ids or preference_retrieval_omissions:
            self.retriever.last_diagnostics["deduplicated_source_ids"] = duplicate_retrieval_ids
            self.retriever.last_diagnostics["preference_omissions"] = preference_retrieval_omissions
        self.retriever.last_diagnostics["already_in_context_count"] = len(duplicate_retrieval_ids)
        self.retriever.last_diagnostics["additional_selected_count"] = len(retrieval_hits)
        self.retriever.last_diagnostics["writer_reference_omissions"] = writer_reference_omissions
        self.retriever.last_diagnostics["canonical_batch_scope"] = batch_scope

        craft_guides = select_craft_guides(task=task, genre=brief.genre, card=card, limit=1)
        if self.prompt_cache_layout_version >= 22:
            # 新技能由实际请求编译回执证明装入；旧craft只保留本章可选建议，不能冒充固定执行。
            craft_guides = [{("建议编号" if key == "技能编号" else "写法建议" if key == "技能" else key): value
                for key, value in item.items()} for item in craft_guides if item.get("技能编号") != "author-sample-voice"]
        plan_view = _plan_for_model(bundle, card)
        ending_role = self.actor in {"writer", "editor", "reviewer", "memory_keeper"}
        ending_enabled = self.prompt_cache_layout_version >= 28 and ending_role
        ending = chapter_ending_contract(self.project, chapter_no, task, prior=promise_prior,
            temporary_goal=card["goal"] if temporary_goal else "", planned=ending_enabled) if ending_role else {}
        final_chapter = ending.get("remaining") == 1
        if final_chapter:
            # 旧章节卡的连载钩子仍保存在规划中，终章角色只采用当前收束要求。
            plan_view["本章卡"].update({"钩子类型": "余韵", "钩子问题": ""})
            plan_view["相邻章节提醒"] = {"边界规则": "这是全书终章。承接有效前文，旧规划中排在终点之后的事件仅供参考。"}
        if ending:
            remaining = ending["remaining"]
            ending["focus"] = (
                "逐条从实际前文梳理读者等待的结果。让人物完成关键行动，写明结果、代价和后来状态；最后一幕给余韵，不另开或延期主线。"
                if remaining == 1 else
                "把关键答案写进场景，补足结果与代价，减少新的大悬念。已在前章兑现的线索不必重复揭秘。"
                if remaining <= 3 else
                "让人物开始验证已有线索，推进关键因果和阶段结果，为最后的选择与代价做好铺垫。"
                if remaining <= 6 else
                "梳理未结主线，找到前文实际埋下的证据，安排回收次序；不要再扩张全书主矛盾。"
            )
            ending["evidence_rule"] = (
                "从实际正文识别读者在等待的问题。规划的未写场景只作定位参考；前文有据的另一种兑现方式有效。"
                "已经写出的结果可引用原文。证据只能支持有限结论时，明确最终处置和影响。有意留白限作者明确约定的范围。"
                "带 @ending/ 的项目只用于审读结果，不写成记忆里的新伏笔。"
                if final_chapter else "本表是待兑现的规划。已发生的结果以正式正文为据；有意留白只覆盖表内明确留白部分。")
        if ending_enabled:
            previous = next((item for item in recent if int(item["chapter_no"]) == chapter_no - 1), None)
            previous_card = database.get_chapter_card(chapter_no - 1) if previous else None
            previous_review = database.latest_review_record(chapter_no - 1) if previous_card and previous_card.get("hook_type") != "余韵" else None
            hook = previous_review["report"].hook_assessment if previous_review else None
            if (hook and previous_review["chapter_version"] == previous["version"]
                    and previous_review["report"].verdict == "pass" and previous_review["report"].source_hash == previous["content_hash"]
                    and hook.actual_anchor and hook.actual_anchor in previous["content"] and hook.reader_expectation):
                ending["previous_hook"] = {"source": f"chapter:{chapter_no - 1:05d}", "anchor": hook.actual_anchor,
                    "expectation": hook.reader_expectation}
            if foreshadow_targets or ending.get("previous_hook"):
                payoff = card.get("payoff") or any(beat.get("action") == "payoff" for beat in card.get("promise_beats", []))
                ending["progression_weight"] = max(ending.get("progression_weight", 35), 45 if payoff else 41 if foreshadow_targets else 39)
                ending["follow_through"] = ("检查前章悬念和当前伏笔，给出可核实的结果或最终处置。" if final_chapter else
                    "检查前章留下的悬念和本章计划的伏笔：用可见行动推进，或给出可核实的兑现结果。"
                    "需延后时给出剧情原因和下一步变化；重复提问、只提线索名字或换一个更大的问题都不算推进。")
        from .story_settings import StorySettingsService, book_requirement_collection
        setting_payload = StorySettingsService(self.project).context_payload(chapter_no=chapter_no, query=task, actor=self.actor,
            max_chars=self.setting_context_max_chars)
        requirements = book_requirement_collection(brief, (self.project.root / "BOOK.md").read_text(encoding="utf-8"))
        if requirements["records"]:
            setting_payload["collections"].insert(0, requirements)
        setting_context = json.dumps(setting_payload, ensure_ascii=False)
        sections = [
            ContextSection(key="A", title="当前任务与用户要求", content=task + (
                "\n本章没有已生效的近期小纲。C是本次任务的临时创作限制，不是正式规划，也不是已发生事实。"
                "已有大纲、设定和规划作为参考，没有的资料不假装存在；作者明确要求与已接受正史仍须遵守，合理偏离参考时说明原因及后续影响，不改写正式规划。"
                "Writer在同次正文交稿中安排具体场景、动机和结果，并通过scene_blueprint与创作说明交代；"
                "审查须用正文和前文证据验证，不能因缺小纲直接判错，也不能把Writer自述当通过证明。"
                if temporary_goal else ""), hard=True),
            ContextSection(key="CSET", title="本书必读要求与设定参考（非正史）",
                           content=setting_context, hard=True, cache_scope="canon" if stable_batch_order else "chapter",
                           source_ids=[record["record_id"] for collection in setting_payload["collections"] for record in collection["records"]]),
            *[section for section in outline_sections(self.project.root, chapter_no, chapter_no)
              if not temporary_goal or "RECENT_PLAN.md" not in section.source_ids],
            *([ContextSection(key="ENDING", title="伏笔、钩子与全书收尾专项", content=json_dumps(ending),
                              hard=True, cache_scope="chapter")] if ending else []),
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
                content=json.dumps([_fact_for_model(item, compact=self.actor == "writer" and self.quality_preset == "economy")
                                    for item in general_facts], ensure_ascii=False, indent=2)
                if general_facts
                else "当前尚无已提交事实。",
                source_ids=[str(item["fact_id"]) for item in general_facts],
                hard=True,
                cache_scope="canon",
                cache_prefix_source_ids=common_fact_ids if (self.cache_policy in {"mimo", "openai"} or self.cache_policy == "deepseek" and self.prompt_cache_layout_version >= 9) else None,
            ),
            ContextSection(
                key="C0", title="本书要求与本章临时范围" if temporary_goal else "书/卷/篇章规划",
                content=json.dumps({key: value for key, value in plan_view.items()
                                    if key not in {"本章卡", "相邻章节提醒"}}, ensure_ascii=False, indent=2),
                source_ids=(["plan:book"] if self.project.db.get_current_plan_bundle() else []) if temporary_goal else ["plan:book", f"plan:volume:{bundle.current_volume.volume_no}", bundle.current_arc.arc_id],
                # 新任务把随章改变的临时范围放到动态尾部，避免提前打断本书公共前缀；旧快照顺序不动。
                hard=True, cache_scope="chapter" if temporary_goal and self.retrieval_policy_version >= 2 else "book",
            ),
            ContextSection(
                key="C", title="本章临时创作目标（不是正式规划，具体安排随正文交审）" if temporary_goal else "本章卡与相邻章节提醒",
                content=json.dumps({key: value for key, value in plan_view.items()
                                    if key in {"本章卡", "相邻章节提醒"}}, ensure_ascii=False, indent=2),
                source_ids=[bundle.current_arc.arc_id if temporary_goal else f"plan:chapter:{chapter_no:05d}"], hard=True, cache_scope="chapter",
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
                key="WN", title="近期章节的重要决定与伏笔交接（候选与意图不等于正史）",
                content=json_dumps(recent_approved_notes(self.project, chapter_no, provisional_chapters)),
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
                hard=preserve_recent or bool(set(named_numbers) & loaded_numbers) or (self.quality_preset == "quality" and mode == "draft"),
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
                content=json.dumps(retrieved_canon, ensure_ascii=False, indent=2)
                    if retrieved_canon or self.retrieval_policy_version >= 2 else "无额外正史事实命中。",
                source_ids=[str(item["source_id"]) for item in retrieved_canon],
                # 新策略的额外命中共享软预算；B/D、本书要求和点名原文仍按原硬门禁保留。
                hard=self.retrieval_policy_version < 2, cache_scope="chapter",
            ),
            ContextSection(
                key="G",
                title="未结伏笔、故事承诺与钩子义务",
                content=json.dumps([_thread_for_model(item) for item in threads], ensure_ascii=False, indent=2)
                if threads
                else "当前无已提交未结线索。",
                source_ids=[str(item["thread_id"]) for item in threads],
                hard=True,
                cache_scope="canon" if stable_batch_order else "chapter",
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
                title="本章可选写法建议" if self.prompt_cache_layout_version >= 22 else "默认基础写作技能与本章表达引导",
                content=json.dumps(craft_guides, ensure_ascii=False, indent=2),
                source_ids=["craft:" + item.get("建议编号", item.get("技能编号", "")) for item in craft_guides],
                cache_scope="chapter",
            ),
            ContextSection(
                key="J",
                title="用户强制记忆与作品方向",
                content=json.dumps(
                    {
                        "题材": brief.genre,
                        "读者": brief.target_audience,
                        "单章目标字数": brief.target_chapter_words,
                        "全书预计章节": brief.estimated_chapters,
                        "全书预计卷数": brief.estimated_volumes,
                        "核心卖点": brief.core_selling_point,
                        "本书硬规则": brief.user_rules,
                        "强制记忆": forced_texts,
                        "使用要求": "Writer 必须逐条遵守；发现互相冲突时不得自行猜测，由 Reviewer 标出冲突并交给用户处理。",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                source_ids=["book:requirements", *[str(item["preference_id"]) for item in forced_preferences]],
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
                    final_chapter=final_chapter,
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
        if (promise_plans or card.get("promise_beats") or any(item.get("plan") for item in promise_prior)
                or self.prompt_cache_layout_version >= 16 and foreshadow_targets):
            sections.append(ContextSection(key="P0", title="规划承诺与伏笔钩子合同",
                content=json_dumps({"plans": promise_plans, "beats": card.get("promise_beats", []),
                    "prior_threads": promise_prior,
                    **({"chapter_priority": foreshadow_targets,
                        "priority_instruction": "优先让本章推进与兑现目标参与人物行动、冲突或选择，并在现有说明与审核中核对实际落点；已有编号用promise_work逐字引证。未推进应说明原因，不能仅重复清单或自称已兑现；触发条件不足保留未结，预计章窗不构成提前揭谜要求。"}
                       if self.prompt_cache_layout_version >= 16 else {}),
                    "instructions": "以剧情触发和完成判据为准；预计章范围只作提醒。大纲与卷细纲固定，未写章节规划的调整须先提出候选并经审核发布。规划索引、Writer分身建议不作正文证据；过窗不自动兑现。"}),
                source_ids=[item["source_id"] for item in promise_plans], hard=True, cache_scope="chapter"))
        sections.extend(self._recovery_guidance(chapter_no, query=task))
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
        if mode == "draft" and self.actor == "writer" and (self.quality_preset == "quality" or self.prompt_cache_layout_version >= 20):
            # 复用已有六来源/补读额度，本地优先展开相关原文，不为写前取材固定新增模型请求。
            self.retriever.last_diagnostics["quality_reading"] = self._quality_reading(
                reading_candidates, sections, chapter_no=chapter_no, protected_input=protected_input)
        if mode in {"draft", "review", "revise"}:
            supplement_tokens = sum(estimate_tokens(section.content) for section in sections
                if section.key == "F" or section.key.startswith(("TH-source-", "QR-")))
            source_slots = max(0, 6 - sum(section.key.startswith(("TH-source-", "QR-")) for section in sections))
            remaining = min(int(SOURCE_RECOVERY_TOKEN_LIMIT / self.input_token_factor) - supplement_tokens,
                self.hard_token_limit - estimate_tokens("\n".join([protected_input,
                    *[section.content for section in sections]])) - 4096)
            contract_tokens = estimate_tokens(NARRATIVE_COMPARISON_CONTRACT) + 64
            comparison = self.retriever.narrative_context(history_query, chapter_no=chapter_no,
                token_limit=max(0, remaining - contract_tokens),
                source_limit=source_slots,
                provisional_chapters=provisional_chapters,
                seed_chapters=list({int(item.get("chapter_no") or 0) for item in reading_candidates
                                    if item.get("source_type") in {"canon_fact", "canon_source", "chapter_summary"}})
                ) if remaining >= contract_tokens + 1024 else {"sources": [], "timeline": [],
                    "coverage": {"complete": False, "reason": "本轮没有足够的共享补读预算，未执行事件对照"}}
            sections.append(ContextSection(key="F1", title="事件时间关系与多场景原文对照",
                content=json.dumps({"instructions": NARRATIVE_COMPARISON_CONTRACT, **comparison}, ensure_ascii=False),
                source_ids=list(dict.fromkeys(identity for item in comparison["sources"]
                    for identity in (item["source_id"], item.get("chapter_source_id", f"chapter:{item['chapter_no']:05d}")))),
                hard=True, cache_scope="chapter"))
            self.retriever.last_diagnostics["narrative_comparison"] = comparison["coverage"]
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
                "canonical_batch_scope": batch_scope,
                "thread_history": thread_recovery,
                "retrieval": {**{key: self.retriever.last_diagnostics.get(key) for key in
                    ("query", "candidate_count", "retrieved_count", "already_in_context_count",
                     "additional_selected_count", "discarded", "discarded_display_limit", "source_reads", "preference_omissions", "writer_reference_omissions", "narrative_comparison")},
                    "branches": self.retriever.last_diagnostics.get("branches", {}),
                    "overlaps": self.retriever.last_diagnostics.get("overlaps", []),
                    "source_conflicts": self.retriever.last_diagnostics.get("source_conflicts", []),
                    "duplicates": self.retriever.last_diagnostics.get("duplicates", [])[:16],
                    "ambiguities": self.retriever.last_diagnostics.get("ambiguities", []),
                    "ambiguity_group_count": self.retriever.last_diagnostics.get("ambiguity_group_count", 0),
                    "maintenance": self.retriever.last_diagnostics.get("maintenance", {}),
                    "feedback_adjustments": self.retriever.last_diagnostics.get("feedback_adjustments", {}),
                    "policy": "相似度和反馈仅用于排序，不证明事实正确。多义项须区分主体、剧情时点、认识类型与原文；对照未装齐不能擅选一个当真。失效项退出当前候选，历史正文保留。",
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
        if self._quality_guidance():
            sections.append(ContextSection(key="Q", title="本次创作策略", content=self._quality_guidance(),
                hard=True, cache_scope="chapter", source_ids=[f"quality:{self.quality_preset}"]))
        if self.prompt_cache_layout_version >= 20:
            # 只去掉职责明确不涉及的软表达指南；作者硬规则、当前正文及事实证据始终保留。
            if self.actor == "memory_keeper" or checks == ("logic_continuity",):
                sections = [section for section in sections if section.hard or section.key not in {"H", "I"}]
            duties = {
                "writer": "先核当前目标、近期完整正文、人物认知、物件状态和相关原文；不把旧稿或未来规划当成已发生事实。缺证保留未知，不凭空补历史。",
                "editor": "独立阅读当前完整正文，只核分派项；正文问题、缺少历史依据和可选润色分别判断。只有分派记忆职责时才交付记忆变化。",
                "reviewer": "对照当前正文与有效历史原句核时间、因果、人物认知及物件变化。每个指控检查合理其他解释，缺证先请求补读，不凭Writer自述放行。",
                "memory_keeper": "仅整理当前正文带来的变化；客观事实、人物信念和传闻分别保留。每条变化指向原句与旧状态，候选错误修候选，不要求正文迁就候选。",
            }
            sections.append(ContextSection(key="ROLE", title="本次职责与关键资料清单",
                content=json_dumps({"职责": duties.get(self.actor, "按当前授权协调任务，不代写、代审或提交正史。"),
                    "分派检查": list(checks), "必要资料": [
                        {"类别": section.title, "来源": section.source_ids}
                        for section in sections if section.hard and section.source_ids],
                    "边界": "资料在场不表示已正确采用；历史模型输出为候选，正式接受只由引擎核权限及版本后提交。"}),
                hard=True, cache_scope="chapter"))
        packet = ContextPacket(
            project_id=self.project.project_id,
            cache_policy=self.cache_policy,
            cache_layout_version=self.prompt_cache_layout_version,
            chapter_no=chapter_no,
            task=task,
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join([*[item.content for item in sections], protected_input])),
            warnings=warnings,
        )
        if self.retrieval_policy_version >= 2:
            # 硬段均已建好后先去掉有完整实证的重复，避免它们挤掉尚未提供的整条证据。
            self._record_retrieval_inclusion(packet, retrieved_candidates,
                recent_sources=recent_sources, hard_only=True)
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
        if self.retrieval_policy_version >= 2:
            self._record_retrieval_inclusion(packet, retrieved_candidates,
                recent_sources=recent_sources)
            packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
        # 已按相关性完成取材、硬资料去重和软预算后，才安排同一集合的请求顺序。
        # 不让缓存偏好影响召回、保留数量或整条证据的去留；旧任务此处完全不变。
        # 供应商/版本门禁集中在共享方法，避免调用方仍把DeepSeek新布局挡在外面。
        self._stabilize_selected_cache_sources(packet)
        packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
        if self.retrieval_policy_version >= 2 and (packet.estimated_tokens > self.hard_token_limit
                or self.cache_policy == "deepseek" and self.prompt_cache_layout_version >= 13):
            # 新布局正常请求也复用已有无损精简，不等超容量才删除重复表示；原句和来源仍完整。
            # 标记随原packet落盘，旧已付费包没有该标记，恢复时仍使用旧投影。
            for section in packet.sections:
                if section.key not in {"F", "F0"}:
                    continue
                value = json.loads(section.content)
                hits = value.get("自适应结果", []) if isinstance(value, dict) else value
                if not isinstance(hits, list):
                    continue
                for hit in hits:
                    if (isinstance(hit, dict) and "_wire_metadata_version" not in hit
                            and hit.get("source_type") in {"canon_fact", "canon_source", "chapter_summary"}
                            and isinstance(hit.get("body"), str) and hit.get("source_id")):
                        hit["_wire_metadata_version"] = 1
                section.content = json_dumps(value)
            before_metadata_compaction = packet.estimated_tokens
            packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
            if packet.estimated_tokens < before_metadata_compaction:
                packet.warnings.append("检索资料无损精简了重复内容及机器审计字段；来源条目、原句、编号、章节和版本保留，完整审计仍在本地。")
                packet.estimated_tokens = _packet_input_tokens(packet, protected_input)
        self._publish_context_status(packet, before_compression, has_protected_input=bool(protected_input),
                                     protected_input=protected_input, role=self.actor)
        if packet.estimated_tokens > self.hard_token_limit:
            if named_numbers:
                required = [{"chapter_no": number,
                             "estimated_tokens": named_source_tokens[number]} for number in named_numbers]
                reason = (f"本轮完整硬材料与阈值内软资料合计 {packet.estimated_tokens} token，"
                          f"超过当前输入上限 {self.hard_token_limit}；点名来源："
                          + "、".join(f"第{item['chapter_no']}章（约{item['estimated_tokens']} token）" for item in required))
                self.project.record_pending_work(kind="context_source", reason=reason,
                    source={"required_chapters": required, "boundary_chapter": chapter_no - 1},
                    next_action="缩小本次必要点名范围，使完整材料符合模型窗口与软件上限后续接资料节点；不得静默只读前三章。",
                    status="waiting_condition")
                raise ValidationGateError(reason)
            raise ValidationGateError(
                f"保留完整硬规则及软资料阈值后，必要输入约{packet.estimated_tokens} token，"
                f"仍超过当前上限{self.hard_token_limit}；未静默删除规则或证据。"
                "请查看上下文占用，从缺口定向补读或缩小本步资料范围。"
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
            "writing_guides": [{"name": item.get("写法建议", item.get("技能", "")), "source_id": "craft:" + item.get("建议编号", item.get("技能编号", ""))}
                for section in packet.sections if section.key == "I" and section.source_ids
                for item in json.loads(section.content)
                if isinstance(item, dict) and item.get("建议编号", item.get("技能编号"))
                and "craft:" + item.get("建议编号", item.get("技能编号", "")) in section.source_ids],
            "meaning": "已完整装入最终编译上下文，不表示模型已经正确采用；candidate仍是未确认参考。"}
        database.set_metadata("preference.selection", selection)
        database.set_metadata(f"preference.selection:{chapter_no}:{mode}:{self.actor}", selection)
        from .retrieval_lifecycle import packet_feedback_manifest
        manifest = packet_feedback_manifest(packet, self.retriever.last_diagnostics, role=self.actor)
        if manifest["query_hash"] and manifest["sources"]:
            database.set_metadata("retrieval.packet:" + content_hash(packet.to_model_prompt()), manifest)
        return packet

    def _quality_guidance(self) -> str:
        strategy = {
            "economy": "节省：优先已有明确方向和本章直接依据，以场景行动推进，避免无任务需要的多案扩写与重复总结。资料缺口只定位补读，不因省调用猜事实；不缩短用户要求的篇幅。",
            "quality": "高质量：更细致地对照相关前史、人物动机与因果后果，检查叙述视角、指代和信息揭示时机；在同次交稿中完善自然对白与场景转折。增加精度不等于堆形容词、拉长篇幅或固定再写多稿。",
        }.get(self.quality_preset)
        return ("\n\n本次创作策略：" + strategy + "所有档位遵循当前明确要求、本书设定和已确认基础写法；不以档位名称代替证据，不改变审查底线、责任角色或修订次数。") if strategy else ""

    def _quality_reading(self, hits: list[dict], sections: list[ContextSection], *,
                         chapter_no: int, protected_input: str = "") -> list[dict]:
        """Expand ranked relevant prose using the existing shared supplement allowance."""
        loaded = {source for section in sections if section.key == "E" or section.key.startswith(("E-cited-", "TH-source-"))
                  for source in section.source_ids}
        supplemental = [section for section in sections if section.key in {"F", "F1"} or section.key.startswith("TH-source-")]
        budget = min(max(0, int(SOURCE_RECOVERY_TOKEN_LIMIT / self.input_token_factor) - sum(estimate_tokens(s.content) for s in supplemental)),
                     max(0, self.hard_token_limit - estimate_tokens(protected_input)
                         - sum(estimate_tokens(s.content) for s in sections) - 4096))
        slots = max(0, 6 - sum(s.key.startswith("TH-source-") for s in sections))
        reads, seen = [], set()
        for hit in hits:
            if hit.get("source_type") not in {"canon_source", "chapter_summary", "canon_fact", "canon_thread"}:
                continue
            number = hit.get("chapter_no")
            if not isinstance(number, int) or not 0 < number < chapter_no or number in seen:
                continue
            seen.add(number)
            source_id = f"chapter:{number:05d}"
            record = {"chapter_no": number, "source_id": source_id, "via": hit["source_id"]}
            if source_id in loaded:
                reads.append({**record, "state": "already_in_context"})
                continue
            if not slots:
                reads.append({**record, "state": "omitted", "reason": "本次补读来源已满"})
                continue
            try:
                source = self.retriever._accepted_source(number, before_chapter=chapter_no)
                identity = hit.get("resolved_source") or hit
                if hit["source_type"] in {"canon_source", "chapter_summary"} and (
                    identity.get("version") != source["version"] or identity.get("source_hash") != source["content_hash"]
                ):
                    raise ValueError("关联来源版本发生变化，请重新读取当前依据。")
            except (ValueError, OSError, UnicodeError) as exc:
                # Selected canonical evidence must not silently turn into invented history.
                raise ValidationGateError(f"写前读取第 {number} 章未完成：{exc}") from exc
            tokens = estimate_tokens(source["content"])
            if tokens > budget:
                reads.append({**record, "state": "excerpt_only", "reason": "完整原文超过剩余补读容量"})
                continue
            sections.append(ContextSection(key=f"QR-{number}", title=f"情节关联原文：第 {number} 章",
                content=source["content"], source_ids=[source_id], hard=True, cache_scope="chapter"))
            slots -= 1
            budget -= tokens
            reads.append({**record, "state": "canonical_source_loaded", "version": source["version"],
                          "source_hash": source["content_hash"], "estimated_tokens": tokens})
        return reads

    def _recovery_guidance(self, chapter_no: int, *, query: str = "") -> list[ContextSection]:
        """各角色读取少量本地工作经验，旧快照维持原协调经验输入。"""
        if self.retrieval_policy_version >= 3:
            from .learning import LearningService
            guidance = LearningService(self.project).workflow_guidance(self.actor,
                query=query if self.prompt_cache_layout_version >= 21 else "")
            cases = guidance.get("cases", [])
            if not cases:
                return []
            return [ContextSection(key="RECOVERY", title="工作经验与反思依据（不是小说记忆）",
                content=json_dumps(guidance), hard=False, cache_scope="request")]
        if self.actor != "coordinator":
            return []
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
        budget = min(int(SOURCE_RECOVERY_TOKEN_LIMIT / self.input_token_factor), max(0, self.hard_token_limit - hard_tokens - 4096))
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
        sections.extend(self._recovery_guidance(end_chapter_no, query=task))
        packet = ContextPacket(
            project_id=self.project.project_id,
            cache_policy=self.cache_policy,
            cache_layout_version=self.prompt_cache_layout_version,
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

    def _fit_retrieval_sections(self, packet: ContextPacket, *, protected_input: str, token_limit: int,
                                minimum_tokens: int = 0) -> list[str]:
        """让额外检索共享剩余输入预算，按原排序保留完整条目及其真实来源身份。"""
        sections = {item.key: item for item in packet.sections if item.key in {"F", "F0"} and not item.hard}
        payloads, originals, records = {}, {}, {}
        for key, section in sections.items():
            value = json.loads(section.content)
            hits = value.get("自适应结果") if key == "F" and isinstance(value, dict) else value
            if not isinstance(hits, list) or any(not isinstance(item, dict) or not item.get("source_id") for item in hits):
                raise ValidationGateError("检索条目结构无法可靠读取，未截断或伪造来源。")
            payloads[key], originals[key] = value, section.content
            records.update({str(item["source_id"]): (key, item) for item in hits})
            section.content = json.dumps({**value, "自适应结果": []} if key == "F" else [], ensure_ascii=False, indent=2)
            section.source_ids = []
        # 先扣除全部其他资料、正文和固定包装；新增事实也不能挤占硬正史或本书要求。
        available = max(0, token_limit - _packet_input_tokens(packet, protected_input))
        order = list(dict.fromkeys([str(item["source_id"]) for item in self.retriever.last_diagnostics.get("selected", [])
                                   if item.get("source_id") in records] + list(records)))
        kept: list[str] = []
        for identity in order:
            key, item = records[identity]
            section = sections[key]
            single = section.model_copy(update={"content": json.dumps(
                {**payloads[key], "自适应结果": [item]} if key == "F" else [item], ensure_ascii=False)})
            # 按模型投影计算增量，额外计入来源行；宁可少装整条，也不裁断证据原句。
            cost = max(1, estimate_tokens(single.model_content()) - estimate_tokens(section.model_content()))
            cost += estimate_tokens(identity) + 4
            if cost <= available:
                kept.append(identity)
                available -= cost

        def apply_kept() -> None:
            """同时提交完整 JSON 与对应来源清单，防止已删条目继续宣称装入。"""
            for key, section in sections.items():
                hits = [records[identity][1] for identity in kept if records[identity][0] == key]
                section.content = json.dumps({**payloads[key], "自适应结果": hits} if key == "F" else hits,
                    ensure_ascii=False, indent=2)
                section.source_ids = [str(item["source_id"]) for item in hits]
            # 请求阶段可以再次缩减备料；保留原编译数，单列本次实际装入数，不能拿旧计数拦恢复。
            for section in packet.sections:
                if section.key == "MEMORY":
                    value = json.loads(section.content)
                    if isinstance(value, dict) and isinstance(value.get("retrieval"), dict):
                        value["retrieval"]["request_additional_count"] = len(kept)
                        section.content = json.dumps(value, ensure_ascii=False, indent=2)
            self.retriever.last_diagnostics["request_additional_count"] = len(kept)

        apply_kept()
        # 增量估算涉及分词取整；最后以完整模型输入复核，仍超量时只移除末尾整条。
        while kept and _packet_input_tokens(packet, protected_input) > token_limit:
            kept.pop()
            apply_kept()
        # 整条来源不能切开凑阈值；必要时多留一条，若仍超容量由调用方明确报缺口。
        for identity in order:
            if sum(estimate_tokens(section.model_content()) for section in sections.values()) >= minimum_tokens:
                break
            if identity not in kept:
                kept.append(identity)
                apply_kept()
        catalogs = [section for section in packet.sections if section.key == "review-source-catalog"]
        if catalogs:
            from .review_verifier import packet_sources
            # 目录在请求压缩前建立；删去整条资料后必须同步，不让目录冒充仍已读的证据。
            loaded = packet_sources(packet.model_copy(update={"sections": [section for section in packet.sections
                if section.key != "review-source-catalog"]}))
            for catalog in catalogs:
                value = json.loads(catalog.content)
                value["已装入"] = sorted(loaded)
                statuses = value.get("补读状态与预算", {}).get("sources", {})
                for identity, status in list(statuses.items()):
                    if identity not in loaded and any(word in status for word in ("已装入", "已补读")):
                        statuses[identity] = "请求阶段资料缩减后未装入，必要依据仍须回源核对"
                catalog.content = json.dumps(value, ensure_ascii=False, indent=2)
        return [sections[key].title for key in sections if sections[key].content != originals[key]]

    def _record_retrieval_inclusion(self, packet: ContextPacket, retrieved: list[dict[str, Any]], *,
                                    recent_sources: dict[int, dict[str, Any]], hard_only: bool = False) -> None:
        """核记编译备料的完整载荷；请求层若再缩软资料，实际输入仍以对应请求记录为准。"""
        diagnostics = self.retriever.last_diagnostics
        final_items = []
        hard_payloads: dict[str, list[tuple[str, Any]]] = {}
        for section in packet.sections:
            if section.hard:
                content = section.model_content()
                try:
                    value = json.loads(content)
                except (ValueError, TypeError):
                    value = None
                for identity in section.source_ids:
                    hard_payloads.setdefault(identity, []).append((content, value))

        def complete_value(value: Any, item: dict[str, Any], parsed_body: Any) -> bool:
            """核对同源模型载荷中的整条内容；事实值、证据与版本须同时存在，索引不能代替原句。"""
            if isinstance(value, dict):
                if item.get("source_type") == "canon_fact" and value.get("事实编号") == item["source_id"]:
                    evidence = str(item.get("evidence") or "")
                    quote_present = value.get("正文证据") == evidence or any(
                        isinstance(reference, dict) and reference.get("quote") == evidence
                        and reference.get("source_chapter") == item.get("chapter_no")
                        and reference.get("source_version") == item.get("version")
                        and reference.get("evidence_id") == value.get("正文证据引用")
                        for reference in value.get("关联证据", []) or [])
                    return ("内容" in value and value["内容"] == item.get("value") and bool(evidence)
                        and quote_present and value.get("来源章节") == item.get("chapter_no")
                        and value.get("来源版本") == item.get("version"))
                if isinstance(parsed_body, dict) and parsed_body and all(
                        key in value and value[key] == expected for key, expected in parsed_body.items()):
                    return True
                return any(complete_value(child, item, parsed_body) for child in value.values())
            if isinstance(value, list):
                return value == parsed_body or any(complete_value(child, item, parsed_body) for child in value)
            return isinstance(value, str) and value == item.get("body")

        voice_records, reference_records = {}, []
        recent_prose = "\n\n".join(section.content for section in packet.sections if section.key == "E")
        for section in packet.sections:
            if section.key not in {"F", "F0", "J2", "H"}:
                continue
            try:
                value = json.loads(section.content)
            except (ValueError, TypeError):
                continue
            if section.key in {"F", "F0"}:
                hits = value.get("自适应结果", []) if isinstance(value, dict) else value
                if isinstance(hits, list):
                    final_items.extend(item for item in hits if isinstance(item, dict) and item.get("source_id") in section.source_ids)
            elif not hard_only and section.key == "J2" and isinstance(value, dict):
                voice_records.update({str(item["编号"]): item for item in value.get("本章可用偏好", [])
                    if isinstance(item, dict) and item.get("编号") in section.source_ids})
            elif not hard_only and section.key == "H" and isinstance(value, list):
                reference_records = value
        final_ids = {str(item["source_id"]) for item in final_items}
        already = set()
        for item in retrieved:
            identity = str(item["source_id"])
            body = str(item.get("body") or "")
            origin = (item.get("resolved_source") or {}).get("source_id") or identity
            try:
                parsed_body = json.loads(body)
            except (ValueError, TypeError):
                parsed_body = None
            # F1 的 chapter id、D 的事实 ID 均非完整性证明，须核实同源原文或等价结构。
            payloads = [*hard_payloads.get(identity, []), *hard_payloads.get(origin, [])] if origin != identity else hard_payloads.get(identity, [])
            if body and any(body in content or complete_value(value, item, parsed_body) for content, value in payloads):
                already.add(identity)
            elif identity in voice_records and all(voice_records[identity].get(key) == expected for key, expected in {
                    "范围": item.get("scope"), "层级": item.get("level", "project"),
                    "状态": item.get("status", "active"), "要求": item.get("text"),
                    "修订号": item.get("revision", 0)}.items()):
                already.add(identity)
            elif item.get("source_type") == "expression_reference":
                # 旧 H 压缩可能只保留半份字段；只有完整记录相等才能算已覆盖。
                try:
                    if json.loads(item["body"]) in reference_records:
                        already.add(identity)
                except (ValueError, TypeError):
                    pass
            elif not hard_only and _covered_recent_source(item, recent_sources, recent_prose):
                already.add(identity)
            elif not hard_only and item.get("source_type") == "canon_source":
                number = item.get("chapter_no")
                source = recent_sources.get(number, {})
                body = str(item.get("body") or "")
                if (body and source.get("version") == item.get("version")
                        and source.get("content_hash") == item.get("source_hash")
                        and f"### 第 {number} 章\n\n{body}".strip() in recent_prose):
                    already.add(identity)
        # QR/线索回源等后加硬段也可能覆盖原命中；最终去重后再发布来源及新增数量。
        for section in packet.sections:
            if section.key not in {"F", "F0"}:
                continue
            value = json.loads(section.content)
            hits = value.get("自适应结果", []) if isinstance(value, dict) else value
            kept = [item for item in hits if str(item["source_id"]) not in already]
            section.content = json.dumps({**value, "自适应结果": kept} if section.key == "F" else kept,
                ensure_ascii=False, indent=2)
            section.source_ids = [str(item["source_id"]) for item in kept]
        final_items = [item for item in final_items if str(item["source_id"]) not in already]
        # F/F0 分段不能改掉原始召回优先级，预算装入仍沿取回顺序选择完整记录。
        order = {str(item["source_id"]): index for index, item in enumerate(retrieved)}
        final_items.sort(key=lambda item: order.get(str(item["source_id"]), len(order)))
        final_ids.difference_update(already)
        included = final_ids | already
        relevant = int(diagnostics.get("relevant_candidate_count", 0))
        coverage = diagnostics.get("coverage_policy", {})
        target = int(coverage.get("target_count", 0))
        reason_by_id = {str(item["source_id"]): item["reason"] for item in diagnostics.get("preference_omissions", [])}
        omissions = [{"source_id": item["source_id"], "reason": reason_by_id.get(str(item["source_id"]),
            "已取回，但共享输入预算不足或原备料被压缩，未在最终上下文保留完整条目。")}
            for item in retrieved if str(item["source_id"]) not in included]
        budget_omitted = sum(item["source_id"] not in reason_by_id
            or "预算" in reason_by_id[item["source_id"]] for item in omissions)
        # 完整 body 只留在 F/F0，诊断沿用原白名单；该投影减少本地记录体积，不另称节省模型 token。
        selected_metadata = [{key: item.get(key) for key in
            ("source_id", "source_type", "score", "reasons", "retrieval_reasons", "body_kind", "identity", "resolved_source", "source_fingerprint")}
            for item in final_items]
        diagnostics.update(selected=selected_metadata, already_in_context_count=len(already),
            deduplicated_source_ids=sorted(already), additional_selected_count=len(final_ids),
            request_additional_count=len(final_ids),
            final_additional_count=len(final_ids), final_included_count=len(included),
            final_included_source_ids=sorted(included), budget_omitted_count=budget_omitted,
            post_retrieval_omissions=omissions[:80], post_retrieval_omission_count=len(omissions),
            source_reads_meaning="本地回源核验不代表模型已读；装入数仅为编译备料，请求层仍可能缩减软资料，实际输入以请求记录为准；正确引用还需审查。")
        diagnostics["coverage_policy"] = {**coverage, "final_included_count": len(included),
            "actual_fraction": len(included) / relevant if relevant else 0,
            "target_shortfall_count": max(0, target - len(included)), "budget_omitted_count": budget_omitted}
        for group in diagnostics.get("ambiguities", []):
            group.setdefault("retrieved_source_ids", group.get("selected_source_ids", []))
            group["selected_source_ids"] = [identity for identity in group["source_ids"] if identity in included]
            group["omitted_count"] = sum(identity not in included for identity in group["source_ids"])
        # MEMORY 中原有数量也随最终条目收缩；新增运营统计留在本地诊断，不复制进模型提示。
        for section in packet.sections:
            if section.key != "MEMORY":
                continue
            value = json.loads(section.content)
            value["retrieval"].update(already_in_context_count=len(already), additional_selected_count=len(final_ids),
                request_additional_count=len(final_ids),
                selected=[{key: item.get(key) for key in ("source_id", "source_type", "score", "reasons", "retrieval_reasons",
                    "body_kind", "identity", "resolved_source")} for item in selected_metadata],
                ambiguities=[{key: value for key, value in group.items() if key != "retrieved_source_ids"}
                    for group in diagnostics.get("ambiguities", [])])
            section.content = json_dumps(value)

    def _shrink_soft_sections(self, packet: ContextPacket, *, protected_input: str = "") -> list[str]:
        """只压缩低权威软资料，单次最多减少30%；硬资料完整保留，超预算由原门禁说明缺口。"""

        actions: list[str] = []
        soft_sections = [section for section in packet.sections if not section.hard]
        soft_floor = math.ceil(sum(estimate_tokens(section.model_content()) for section in soft_sections) * 0.70)

        def keep_trim(saved: list[tuple[ContextSection, str, list[str]]]) -> bool:
            """压缩不能越过保留阈值；整组恢复内容与来源，防止检索条目已删却仍宣称引用。"""
            if sum(estimate_tokens(section.model_content()) for section in soft_sections) >= soft_floor:
                return True
            for section, content, sources in saved:
                section.content, section.source_ids = content, sources
            warning = "软资料单次压缩已触及30%损失阈值，原资料与来源已保留；仍超预算时须缩小取材范围或补源。"
            if warning not in packet.warnings:
                packet.warnings.append(warning)
            return False

        targets = {"RECOVERY": 4_000, "H": 4_000, "I": 4_000, "J1": 2_000, "J2": 4_000, "E1": 4_000, "F": 10_000, "E": _RECENT_PROSE_SOFT_LIMIT, "D1": 8_000}
        for key in ("RECOVERY", "H", "J1", "I", "J2", "E1", "F", "E", "D1"):
            if _packet_input_tokens(packet, protected_input) <= int(self.soft_token_limit * 0.82):
                break
            section = next((item for item in packet.sections if item.key == key and not item.hard), None)
            if section is None:
                continue
            saved = [(item, item.content, list(item.source_ids)) for item in soft_sections]
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
                if self.retrieval_policy_version >= 2:
                    other_soft = sum(estimate_tokens(item.model_content()) for item in soft_sections
                        if item.key not in {"F", "F0"})
                    trimmed = self._fit_retrieval_sections(packet, protected_input=protected_input,
                        token_limit=int(self.soft_token_limit * 0.82),
                        minimum_tokens=max(0, soft_floor - other_soft))
                    if keep_trim(saved):
                        actions.extend(trimmed)
                    continue
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
            if section.content != original and keep_trim(saved):
                actions.append(section.title)
        return actions

    def _stabilize_selected_cache_sources(self, packet: ContextPacket) -> None:
        """新布局只排列本轮已选条目，复用同项目同角色的编译顺序，不声称供应商已经命中。"""
        # v15将同一算法用于专项与记忆；旧快照仍保留原角色集合，避免改变已付费输入证明。
        eligible = self.actor in {"writer", "editor"} or self.prompt_cache_layout_version >= 15 and self.actor in {"reviewer", "memory_keeper"}
        if self.prompt_cache_layout_version < 2 or not (self.cache_policy == "mimo" or self.cache_policy == "deepseek" and self.prompt_cache_layout_version >= 9) or not eligible:
            return
        previous: dict[str, Any] = {}
        try:
            value = json.loads((self.project.internal / f"context-status-{self.actor}.json").read_text(encoding="utf-8"))
            if (isinstance(value, dict) and value.get("project_id") == self.project.project_id
                    and value.get("actor") == self.actor and value.get("cache_policy") == self.cache_policy):
                previous = {item["key"]: item["content"] for item in value.get("material_contents", [])
                    if isinstance(item, dict) and isinstance(item.get("key"), str) and isinstance(item.get("content"), str)}
        except (OSError, ValueError, TypeError):
            # 不存在、损坏或其他项目的旧备料不是重排依据；本轮材料照常保留。
            previous = {}

        def indexed(value: Any, key: str) -> dict[str, Any]:
            """比较完整模型载荷及所属集合元数据；仅F原投影已排除的运营评分不参与。"""
            if not isinstance(value, dict):
                return {}
            if key == "F":
                items = value.get("自适应结果")
                pairs = [(item.get("source_id"), item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
                if not isinstance(items, list) or len(pairs) != len(items):
                    return {}
            else:
                collections = value.get("collections")
                if not isinstance(collections, list) or not all(isinstance(item, dict) and isinstance(item.get("records"), list)
                        and all(isinstance(record, dict) for record in item["records"]) for item in collections):
                    return {}
                collection_ids = [item.get("collection_id") for item in collections]
                if not all(isinstance(identity, str) and identity for identity in collection_ids) or len(set(collection_ids)) != len(collection_ids):
                    return {}
                pairs = [(record.get("record_id"), {"collection": {name: entry for name, entry in collection.items() if name != "records"},
                    "record": record}) for collection in collections for record in collection["records"]]
            if not all(isinstance(identity, str) and identity for identity, _ in pairs) or len({identity for identity, _ in pairs}) != len(pairs):
                return {}
            return dict(pairs)

        def common_length(left: str, right: str) -> int:
            """仅量化当前本地请求前缀，不将字符数换称已命中的token或节省费用。"""
            return next((index for index, (first, second) in enumerate(zip(left, right)) if first != second), min(len(left), len(right)))

        audit: dict[str, Any] = {"version": 2, "basis": "上一份同项目同角色的编译备料，不等于已发送或供应商缓存回执", "sections": {}}
        projected = {item.key: item for item in packet.model_sections()}
        for section in packet.sections:
            projected_key = ("CSET-records" if section.key == "CSET" and packet.cache_policy == "deepseek"
                and packet.cache_layout_version >= 13 and "CSET-records" in projected else section.key)
            if section.key not in {"F", "CSET"} or projected_key not in projected:
                continue
            from .schemas import _expand_context_tables
            current = _expand_context_tables(json.loads(projected[projected_key].model_content(canonical=True)))
            try:
                old_wire = json.loads(previous.get(projected_key, previous.get(section.key, "")))
                old = _expand_context_tables(old_wire)
                if projected_key == "CSET-records" and projected_key not in previous:
                    # 兼容拆分前的同角色备料：硬约定及定义已在独立前段，仅比较仍选中的普通记录。
                    old = {"collections": [{"collection_id": item["collection_id"],
                        "records": [record for record in item["records"] if record.get("epistemic_status") != "user_constraint"]}
                        for item in old["collections"]]}
                    from .schemas import _compact_context_tables
                    old_wire = _compact_context_tables(old)
            except (ValueError, TypeError, KeyError):
                old, old_wire = {}, {}
            current_records, old_records = indexed(current, section.key), indexed(old, section.key)
            collection_field = "自适应结果" if section.key == "F" else "collections"
            metadata_same = (isinstance(old, dict) and {key: value for key, value in current.items() if key != collection_field}
                == {key: value for key, value in old.items() if key != collection_field})
            stable = [identity for identity, item in old_records.items() if metadata_same and current_records.get(identity) == item]
            expected_ids = set(projected[projected_key].source_ids)
            if stable and set(current_records) == expected_ids and expected_ids <= set(section.source_ids):
                # hint必须仍绑定原块的全部编号；独立硬约定不因普通记录重排而被遗漏。
                remaining_ids = section.source_ids if projected_key == "CSET-records" else current_records
                order = [*stable, *[identity for identity in remaining_ids if identity not in set(stable)]]
                trial = packet.model_copy(update={"sections": [section.model_copy(update={"cache_prefix_source_ids": order})]})
                candidate = next(item for item in trial.model_sections() if item.key == projected_key)
                original_text = json.dumps(old_wire, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                before = common_length(original_text, projected[projected_key].model_content(canonical=True))
                after = common_length(original_text, candidate.model_content(canonical=True))
                if after > before:
                    section.cache_prefix_source_ids = order
                    audit["sections"][section.key] = {"unchanged_source_count": len(stable), "source_order": order,
                        "previous_common_characters": before, "arranged_common_characters": after}

            if section.key == "F" and current_records:
                def complete_accepted(hit: dict[str, Any]) -> bool:
                    """只把本轮已回源的完整已接受正文前置；摘要、片段及参考身份继续在动态段。"""
                    body, number, version = hit.get("body"), hit.get("chapter_no"), hit.get("version")
                    if (not isinstance(body, str) or not body or type(number) is not int or not 0 < number < packet.chapter_no
                            or type(version) is not int or version < 1 or hit.get("source_hash") != content_hash(body)):
                        return False
                    if hit.get("source_type") == "canon_source":
                        return hit.get("identity") == "accepted_prose" and hit.get("source_id") == f"chapter:{number:05d}"
                    resolved = hit.get("resolved_source")
                    return (hit.get("source_type") == "chapter_summary" and hit.get("body_kind") == "accepted_prose"
                        and isinstance(resolved, dict) and resolved == {"source_id": f"chapter:{number:05d}", "chapter_no": number,
                            "version": version, "source_hash": hit["source_hash"], "start": 0, "end": len(body)})
                if all(complete_accepted(hit) for hit in current_records.values()):
                    # 只改变同一来源集合的排列，不赋予新的事实身份；B硬正史仍排在前面。
                    section.cache_scope = "canon"
                    audit["complete_prose_before_settings"] = True
        # 审计只留本地诊断，不再把此次排序计数塞回模型材料、产生新的动态前缀。
        self.retriever.last_diagnostics["cache_layout"] = audit

    def _publish_context_status(
        self,
        packet: ContextPacket,
        before_compression: int,
        *,
        has_protected_input: bool = False,
        protected_input: str = "",
        role: str = "writer",
    ) -> None:
        """Save prepared materials with their originating task, independently of request outcomes."""
        from .runtime import active_runtime
        from .task_settings import active_task_settings
        runtime = active_runtime.get()
        scope = active_task_settings.get()
        soft_sections = [item for item in packet.sections if not item.hard]
        hard_sections = [item for item in packet.sections if item.hard]
        def scaled(tokens: int) -> int:
            return math.ceil(tokens * self.input_token_factor)
        ratio = packet.estimated_tokens / max(1, self.hard_token_limit)
        status = "safe" if ratio < 0.7 else "watch" if ratio < 0.9 else "near_limit"
        status_path = self.project.internal / "context-status.json"
        role_path = self.project.internal / f"context-status-{role}.json"
        previous: dict[str, Any] = {}
        try:
            raw_previous = json.loads((role_path if role_path.is_file() else status_path).read_text(encoding="utf-8"))
            if isinstance(raw_previous, dict) and raw_previous.get("project_id") in {None, "", self.project.project_id} and raw_previous.get("actor") == role:
                previous = raw_previous
        except (OSError, json.JSONDecodeError):
            previous = {}
        previous_updated_at = str(previous.get("updated_at") or "")
        comparable = previous_updated_at and previous.get("input_token_factor", 1.0) == self.input_token_factor
        previous_estimated = int(previous.get("estimated_tokens") or 0) if previous_updated_at else 0
        updated_at = utc_now()
        model_sections = packet.model_sections()
        prefix_sections = [item for item in model_sections if item.cache_scope in {"global", "book", "canon"}]
        prefix_hashes = {
            item.key: hashlib.sha256((
                f"## {item.key}. {item.title}\n\n{item.model_content()}\n\n"
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
            "packet_hash": content_hash(packet.to_model_prompt() + content_hash(protected_input)),
            "source_revision": project_source_revision(self.project.root, self.project.internal),
            "updated_at": updated_at,
            "chapter_no": None if role == "coordinator" else packet.chapter_no,
            "material_scope": "project" if role == "coordinator" else "chapter",
            "actor": role,
            "record_kind": "compiled_materials",
            "task_id": runtime.task_id if runtime else scope.task_id if scope else "",
            "run_id": runtime.run_id if runtime else "",
            "cache_policy": packet.cache_policy,
            "task": packet.task,
            "estimated_tokens": scaled(packet.estimated_tokens),
            "input_token_factor": self.input_token_factor,
            "estimation_method": (("MiMo" if self.cache_policy == "mimo" else
                "DeepSeek" if self.cache_policy == "deepseek" else "通用") + "本地保守估算，非官方分词器")
                if self.input_token_factor != 1 else "通用本地估算",
            "input_budget_scope": "to_model_prompt编译资料及调用者protected_input；Provider另核系统契约、Schema、追加指令和输出预算。",
            "protected_input_tokens": scaled(estimate_tokens(protected_input)) if protected_input else 0,
            "protected_input_hash": content_hash(protected_input) if protected_input else None,
            "before_compression_tokens": scaled(before_compression),
            "previous_updated_at": previous_updated_at,
            "previous_estimated_tokens": previous_estimated if comparable else None,
            "change_since_previous_tokens": (scaled(packet.estimated_tokens) - previous_estimated) if comparable else None,
            "soft_limit_tokens": scaled(self.soft_token_limit),
            "hard_limit_tokens": scaled(self.hard_token_limit),
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
                    "estimated_tokens": scaled(estimate_tokens(item.model_content())),
                    "hard": item.hard,
                    "source_count": len(item.source_ids),
                    "changed": next((old.get("content_hash") != content_hash(item.model_content()) for old in previous.get("budget_allocation", []) if old.get("key") == item.key), True),
                    "content_hash": content_hash(item.model_content()),
                }
                for item in model_sections
            ] + ([{"key": "INPUT", "title": "本次正文与交接说明", "estimated_tokens": scaled(estimate_tokens(protected_input)),
                "hard": True, "source_count": 1, "content_hash": content_hash(protected_input),
                "changed": previous.get("protected_input_hash") != content_hash(protected_input)}] if protected_input else []),
            "retrieval_diagnostics": self.retriever.last_diagnostics,
            "cache_prefix": {
                "role": role,
                "changed_sections": changed_keys,
                "first_changed_section": changed_keys[0] if changed_keys else None,
                "reason": "首次记录该角色参考资料" if not old_hashes else ("参考资料内容、标题或来源已变化" if changed_keys else "参考资料未变化；仍须核对模型、系统契约及供应商缓存是否可用"),
                "estimated_prefix_tokens": scaled(estimate_tokens(packet.model_prompt_parts()[0])),
                "reference_prefix_hash": content_hash(packet.model_prompt_parts()[0]),
            },
            "prefix_history_by_role": prefix_history,
            "warnings": packet.warnings,
        }
        from .retrieval_lifecycle import packet_feedback_manifest
        manifest = packet_feedback_manifest(packet, self.retriever.last_diagnostics, role=role)
        if manifest["query_hash"] and manifest["sources"]:
            self.project.db.set_metadata("retrieval.packet:" + payload["packet_hash"], manifest)
        atomic_write_text(status_path, json_dumps(payload))
        atomic_write_text(role_path, json_dumps({**payload, "material_contents": [
            {"key": item.key, "title": item.title, "content": item.model_content()} for item in model_sections]
            + ([{"key": "INPUT", "title": "本次正文与交接说明", "content": protected_input}] if protected_input else [])}))
        atomic_write_text(self.project.internal / f"context-status-{role}-summary.json", json_dumps({
            key: payload[key] for key in ("project_id", "actor", "updated_at", "packet_hash", "chapter_no", "material_scope", "task", "task_id", "run_id",
                "estimated_tokens", "change_since_previous_tokens", "budget_allocation", "record_kind",
                "configured_hard_limit_tokens", "hard_limit_tokens", "output_reserve_tokens", "input_token_factor", "estimation_method")}))


def _fact_for_model(item: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    projected = {
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
    if compact and projected["正文证据"] and isinstance(projected["关联证据"], list):
        matches = [reference for reference in projected["关联证据"] if isinstance(reference, dict)
            and reference.get("evidence_id") and reference.get("quote") == projected["正文证据"]
            and reference.get("source_chapter") == item.get("source_chapter")
            and reference.get("source_version") == item.get("source_version")
            and item.get("source_chapter") and item.get("source_version")]
        if len(matches) == 1:
            projected["正文证据引用"] = matches[0]["evidence_id"]
            projected.pop("正文证据")
    return projected


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
        **{key: item[key] for key in ("plan", "plan_source_id", "plan_source_hash", "evidence", "reason", "condition_checks") if key in item},
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
            "伏笔与钩子合同": [item.model_dump(mode="json") for item in book.promises],
        },
        "当前卷": {
            "卷序号": volume.volume_no,
            "卷名": volume.title,
            "章节范围": f"{volume.chapter_start}-{volume.chapter_end}",
            "阶段承诺": volume.promise,
            "中段转折": volume.midpoint_turn,
            "卷高潮": volume.climax,
            "伏笔与钩子合同": [item.model_dump(mode="json") for item in volume.promises],
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
            "伏笔与钩子合同": [item.model_dump(mode="json") for item in arc.promises],
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
            "承诺动作": card.get("promise_beats", []),
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
    final_chapter: bool = False,
) -> str:
    """终章覆盖普通章节的追读要求，保持各角色原有交付结构。"""
    if mode == "review":
        return (
            "只输出审查报告结构，并单独填写 hook_assessment；有意留白与表达混乱必须区分。"
            + ("全书终章以主线和终幕有据收束为通过条件。已收束时可填写 clarity=absent；待兑现的追更悬念用终章逐项核对处理。" if final_chapter else
               "钩子的未知项、延后回应和开放问题本身不算表达不清；只有锚点缺失、与正史冲突或正文无法理解时才按证据报告。")
            + "没有可直接证明的硬问题时必须通过；不续写、不重写正文。"
            + _review_detail_instruction(review_experience_detail)
        )
    return (
        "只输出完整章节草稿结构，title单独返回标题，content只放正文。正文约 "
        f"{card['target_words']} 字，完成目标—阻力—决定—后果与不可逆变化即可；"
        "表达、场景调度与修辞可自由发挥；正文外填写 hook_note，"
        + ("记录实际终幕锚点和留白边界，预计回应留空；全书终章用已兑现的结果、人物归宿和余韵收束，不另造追读理由。"
           if final_chapter else "说明实际锚点、留白边界和预计回应，")
        + "不得把说明混入小说正文。"
        + ("" if final_chapter else _hook_strategy_instruction(hook_strategy))
    )
