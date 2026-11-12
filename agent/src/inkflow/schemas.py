from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from typing_extensions import Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema

from .role_protocol import AgentRole, CollaborationMode, ROLE_PROTOCOL_VERSION, adapt_role, new_task_mode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PromiseAwareModel(StrictModel):
    @model_serializer(mode="wrap")
    def serialize_promises(self, handler):
        data = handler(self)
        # Empty additions must not change saved legacy planning and memory hashes.
        for key in ("promises", "promise_beats", "promise_work", "plan", "plan_source_id",
                    "plan_source_hash", "evidence", "reason", "condition_checks"):
            if data.get(key) in (None, "", [], {}):
                data.pop(key, None)
        return data


class PromisePlan(StrictModel):
    thread_id: str = Field(min_length=1, max_length=100)
    kind: Literal["plot", "promise", "foreshadow", "relationship", "mystery", "hook"] = "foreshadow"
    horizon: Literal["short", "arc", "volume", "book", "open"] = "arc"
    title: str = Field(min_length=2, max_length=160)
    question: str = Field(min_length=2, max_length=600)
    purpose: str = Field(min_length=2, max_length=800)
    causal_basis: str = Field(min_length=2, max_length=1000)
    trigger_conditions: list[str] = Field(default_factory=list, max_length=8)
    completion_criteria: list[str] = Field(default_factory=list, max_length=8)
    milestones: list[str] = Field(default_factory=list, max_length=8)
    window_start: int | None = Field(default=None, ge=1)
    window_end: int | None = Field(default=None, ge=1)
    open_reason: str = Field(default="", max_length=600)
    parent_thread_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def grounded_contract(self):
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("预计章节窗口须同时填写起止，或都留空")
        if self.window_start is not None and self.window_end < self.window_start:
            raise ValueError("预计窗口结束不能早于开始")
        if not self.open_reason and (not self.trigger_conditions or not self.completion_criteria):
            raise ValueError("伏笔须有触发条件与完成判据；少量开放留白须明确理由")
        for values in (self.trigger_conditions, self.completion_criteria, self.milestones, self.parent_thread_ids):
            if any(not value.strip() for value in values) or len(set(values)) != len(values):
                raise ValueError("伏笔条件、过程和关联编号不得为空或重复")
        if self.thread_id in self.parent_thread_ids:
            raise ValueError("伏笔不能依赖自身")
        return self


class PromiseBeat(StrictModel):
    thread_id: str = Field(min_length=1, max_length=100)
    action: Literal["plant", "advance", "payoff", "delay"]
    expected_change: str = Field(min_length=2, max_length=800)


class PromiseConditionCheck(StrictModel):
    condition: str = Field(min_length=2, max_length=1000)
    status: Literal["met", "unmet", "unknown"]
    source_chapter: int = Field(ge=1)
    quote: str = Field(default="", max_length=2000)
    source_version: int | None = Field(default=None, ge=1)
    source_hash: str = ""


class PromiseWork(StrictModel):
    thread_id: str = Field(min_length=1, max_length=100)
    action: Literal["plant", "advance", "payoff", "delay"]
    quote: str = Field(default="", max_length=2000)
    rationale: str = Field(min_length=2, max_length=1000)


class ConversationReply(StrictModel):
    reply: str = Field(min_length=1, max_length=1500)


class RunningMessageReply(ConversationReply):
    kind: Literal["chat", "query", "task_revision", "new_task", "clarify"]


class UnifiedRoleModel(StrictModel):
    """Consume old role tags at input; serialized contracts contain only canonical roles."""

    @property
    def role_protocol_version(self) -> int:
        return ROLE_PROTOCOL_VERSION

    @model_validator(mode="before")
    @classmethod
    def adapt_role_input(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        result = dict(value)
        label = result.pop("role_protocol_version", None)
        if label is not None and (type(label) is not int or label not in {1, 2}):
            raise ValueError("导入结构中的旧角色标签无效。")
        legacy = label == 1
        if "collaboration_mode" in result:
            result["collaboration_mode"] = new_task_mode(result["collaboration_mode"])

        def canonical_role(role: str) -> str:
            return role if isinstance(role, str) and role in {"engine", "user"} else adapt_role(role, legacy=legacy)

        for key in ("role", "memory_owner"):
            if result.get(key) is not None:
                result[key] = canonical_role(result[key])
        if isinstance(result.get("check_owners"), dict):
            result["check_owners"] = {check: canonical_role(role) for check, role in result["check_owners"].items()}
        # Only schema-owned role structures are adapted; user setting_change/content remain verbatim.
        for key, field in (("steps", "role"), ("coverage", "owner")):
            if isinstance(result.get(key), list):
                items = []
                for raw in result[key]:
                    item = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw) if isinstance(raw, dict) else raw
                    if isinstance(item, dict) and item.get(field) is not None:
                        item[field] = canonical_role(item[field])
                    items.append(item)
                result[key] = items
        for key in ("first", "second"):
            raw = result.get(key)
            item = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw) if isinstance(raw, dict) else raw
            if isinstance(item, dict) and item.get("role") is not None:
                item["role"] = canonical_role(item["role"])
                result[key] = item
        return result


class BookBrief(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    genre: str = Field(min_length=1, max_length=100)
    premise: str = Field(min_length=10)
    protagonist: str = Field(min_length=1, max_length=100)
    target_audience: str = "中文网文读者"
    core_selling_point: str = ""
    target_chapter_words: int = Field(default=3000, ge=500, le=20_000)
    estimated_chapters: int = Field(default=200, ge=10, le=5000)
    estimated_volumes: int = Field(default=6, ge=1, le=100)
    user_rules: list[str] = Field(default_factory=list)


class NovelIdeaCandidate(BookBrief):
    """Writer 在建项前提供的可选开书方案。"""

    concept_id: str = Field(min_length=1, max_length=40)
    opening_hook: str = Field(min_length=1)
    long_term_engine: str = Field(min_length=1)
    choice_note: str = Field(min_length=1)


class NovelIdeaBundle(StrictModel):
    """一次零想法构思返回一个快速方向或三个对比方向。"""

    candidates: list[NovelIdeaCandidate] = Field(min_length=1, max_length=3)
    public_reasoning_summary: list[str] = Field(min_length=2, max_length=6)


class ProviderProbe(StrictModel):
    """低成本连通性探针，只验证模型是否能返回受约束 JSON。"""

    status: Literal["ok"]
    reply: str = Field(min_length=1, max_length=200)
    public_reasoning_summary: str = Field(min_length=1, max_length=300)


class PromptOptimization(StrictModel):
    """面向用户输入框的可撤回提示词优化结果。"""

    optimized_prompt: str = Field(min_length=1, max_length=8_000)
    change_summary: list[str] = Field(min_length=1, max_length=6)
    preserved_constraints: list[str] = Field(default_factory=list, max_length=12)


class VoiceCloneReadingScript(StrictModel):
    """Writer 生成的本地克隆参考朗读稿；不属于小说正文或正史。"""

    reading_text: str = Field(min_length=180, max_length=400)
    coverage_summary: list[str] = Field(min_length=3, max_length=6)


class CreativeBrainstorm(StrictModel):
    """Writer 创意分身的输出：无依据可查时的纯创意提案，不写正文、不入正史。"""

    reply: str = Field(min_length=1, max_length=6_000)


class SuggestedNavigation(StrictModel):
    """建议附带的只读页面入口；不接收模型提供的地址、命令或写入操作。"""

    target: Literal["pending_work", "book_settings", "threads", "outline", "story_detail", "recent_plan", "questions", "model_settings"]
    chapter_no: int | None = Field(default=None, ge=1)


class SuggestedPrompt(StrictModel):
    """一条按当前对话与项目状态预测的用户下一步提示词。"""

    label: str = Field(min_length=1, max_length=24)
    prompt: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=180)
    navigation: SuggestedNavigation | None = None


class SuggestedPrompts(StrictModel):
    """Coordinator 根据实际结果提出的下一步选项，不代表执行授权。"""

    suggestions: list[SuggestedPrompt] = Field(min_length=2, max_length=4)


class VolumeCompass(StrictModel):
    volume_no: int = Field(ge=1)
    title: str
    promise: str
    start_state: str
    end_state: str
    estimated_chapters: int = Field(ge=1)


class ArcSummary(StrictModel):
    arc_id: str
    title: str
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    promise: str
    end_state: str

    @model_validator(mode="after")
    def validate_range(self) -> "ArcSummary":
        if self.chapter_end < self.chapter_start:
            raise ValueError("篇章结束章节不能早于开始章节")
        return self


class BookPlan(PromiseAwareModel):
    title: str
    premise: str
    reader_promise: str
    narrative_engine: str
    main_conflict: str
    protagonist_start: str
    protagonist_end: str
    theme_question: str
    ending_direction: str
    estimated_chapters: int = Field(ge=10)
    estimated_volumes: int = Field(ge=1)
    volume_compass: list[VolumeCompass] = Field(min_length=1)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)


class VolumePlan(PromiseAwareModel):
    volume_no: int = Field(ge=1)
    title: str
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    promise: str
    start_state: str
    end_state: str
    antagonist_pressure: str
    midpoint_turn: str
    climax: str
    cost_and_result: str
    next_volume_bridge: str
    arcs: list[ArcSummary] = Field(min_length=1)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_ranges(self) -> "VolumePlan":
        if self.chapter_end < self.chapter_start:
            raise ValueError("卷结束章节不能早于开始章节")
        ordered = sorted(self.arcs, key=lambda item: item.chapter_start)
        for index, arc in enumerate(ordered):
            if arc.chapter_start < self.chapter_start or arc.chapter_end > self.chapter_end:
                raise ValueError(f"篇章 {arc.arc_id} 超出当前卷章节范围")
            if index and arc.chapter_start <= ordered[index - 1].chapter_end:
                raise ValueError("篇章章节范围发生重叠")
        return self


HOOK_TYPES = Literal[
    "问题",
    "危险",
    "揭示",
    "决定",
    "逆转",
    "代价",
    "倒计时",
    "关系破裂",
    "错误胜利",
    "新目标",
    "余韵",
]


class ChapterCard(PromiseAwareModel):
    chapter_no: int = Field(ge=1)
    title_working: str
    status: Literal["planned", "drafted", "accepted"] = "planned"
    pov: str
    time_location: str
    function: str
    goal: str
    obstacle: str
    decision: str
    consequence: str
    irreversible_delta: str
    scenes: list[str] = Field(min_length=1, max_length=8)
    information_release: str
    foreshadow_advance: list[str] = Field(default_factory=list)
    payoff: list[str] = Field(default_factory=list)
    hook_type: HOOK_TYPES
    hook_question: str
    hook_strength: Literal["light", "medium", "strong"] = "medium"
    hook_anchor: str = ""
    withholding_boundary: str = ""
    payoff_window: str = "下一章或当前篇章内"
    target_words: int = Field(ge=500, le=20_000)
    dependencies: list[str] = Field(default_factory=list)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=16)
    promise_beats: list[PromiseBeat] = Field(default_factory=list, max_length=12)


class ArcPlan(PromiseAwareModel):
    arc_id: str
    volume_no: int = Field(ge=1)
    title: str
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    promise: str
    central_conflict: str
    start_state: str
    end_state: str
    escalation: list[str] = Field(min_length=2)
    revelations: list[str] = Field(default_factory=list)
    relationship_movement: list[str] = Field(default_factory=list)
    planted_threads: list[str] = Field(default_factory=list)
    advanced_threads: list[str] = Field(default_factory=list)
    payoff_threads: list[str] = Field(default_factory=list)
    midpoint_turn: str
    climax: str
    aftermath: str
    exit_bridge: str
    replan_triggers: list[str] = Field(default_factory=list)
    public_reasoning_summary: list[str] = Field(default_factory=list, max_length=8)
    public_risks_to_verify: list[str] = Field(default_factory=list, max_length=6)
    chapter_cards: list[ChapterCard] = Field(min_length=1)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_cards(self) -> "ArcPlan":
        if self.chapter_end < self.chapter_start:
            raise ValueError("篇章结束章节不能早于开始章节")
        numbers = [item.chapter_no for item in self.chapter_cards]
        expected = list(range(self.chapter_start, self.chapter_end + 1))
        if numbers != expected:
            raise ValueError(f"当前篇章章节卡必须连续覆盖 {expected[0]}～{expected[-1]}")
        return self


class ArcPlanningBeat(StrictModel):
    chapter_no: int = Field(ge=1)
    intended_turn: str = Field(min_length=4, max_length=500)
    hook: str = Field(min_length=4, max_length=500)
    basis_refs: list[str] = Field(min_length=1, max_length=4)


class ArcPlanningGroundedItem(StrictModel):
    """A public planning statement with exact canon references."""

    text: str = Field(min_length=4, max_length=700)
    canon_refs: list[str] = Field(min_length=1, max_length=4)


class ArcPlanningNewElement(StrictModel):
    """A deliberately limited new ingredient in a future arc proposal."""

    name: str = Field(min_length=2, max_length=80)
    kind: Literal["person", "place", "object", "organization", "event"]
    introduced_chapter: int = Field(ge=1)
    purpose: str = Field(min_length=4, max_length=300)
    verification_needed: str = Field(min_length=4, max_length=300)


class ArcPlanningBrief(PromiseAwareModel):
    """A user-visible planning rationale, never a replacement for ArcPlan."""

    title: str = Field(min_length=1, max_length=160)
    central_question: str = Field(min_length=8, max_length=800)
    start_state: str = Field(min_length=8, max_length=1_200)
    desired_end_state: str = Field(min_length=8, max_length=1_200)
    constraints_checked: list[ArcPlanningGroundedItem] = Field(min_length=1, max_length=12)
    chosen_direction: list[ArcPlanningGroundedItem] = Field(min_length=2, max_length=8)
    beats: list[ArcPlanningBeat] = Field(min_length=1, max_length=16)
    new_elements: list[ArcPlanningNewElement] = Field(default_factory=list, max_length=2)
    risks_to_verify: list[str] = Field(default_factory=list, max_length=8)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=24)


class OutlineChapter(PromiseAwareModel):
    """一章大纲的可执行摘要；不等同于章节卡，也不写入正史。"""

    chapter_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=4, max_length=500)
    conflict: str = Field(min_length=4, max_length=500)
    turn: str = Field(min_length=4, max_length=500)
    hook: str = Field(min_length=4, max_length=500)
    scenes: list[str] = Field(default_factory=list, max_length=12)
    consequence: str = Field(default="", max_length=800)
    promise_beats: list[PromiseBeat] = Field(default_factory=list, max_length=12)


class OutlineOutput(PromiseAwareModel):
    """独立大纲生成器的输出，和正式 PLAN/草稿保持文件边界。"""

    title: str = Field(min_length=1, max_length=160)
    start_chapter: int = Field(ge=1)
    end_chapter: int = Field(ge=1)
    premise: str = Field(min_length=8, max_length=1_500)
    main_story: str = Field(default="", max_length=3000)
    character_arc: str = Field(default="", max_length=2000)
    ending: str = Field(default="", max_length=1500)
    chapters: list[OutlineChapter] = Field(min_length=1, max_length=500)
    public_reasoning_summary: list[str] = Field(default_factory=list, max_length=8)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_range(self) -> "OutlineOutput":
        if self.end_chapter < self.start_chapter:
            raise ValueError("大纲结束章节不能早于开始章节")
        expected = list(range(self.start_chapter, self.end_chapter + 1))
        actual = [item.chapter_no for item in self.chapters]
        if actual != expected:
            raise ValueError(f"大纲章节必须连续覆盖 {expected[0]}～{expected[-1]}")
        return self


class StoryDetailSegment(StrictModel):
    title: str = Field(min_length=1, max_length=160)
    motivation: str = Field(min_length=4, max_length=1200)
    events: list[str] = Field(min_length=1, max_length=20)
    conflict: str = Field(min_length=4, max_length=1200)
    choice: str = Field(min_length=4, max_length=1200)
    consequence: str = Field(min_length=4, max_length=1200)
    setup_and_payoff: str = Field(min_length=4, max_length=1200)


class StoryDetailOutput(PromiseAwareModel):
    """剧情细纲：按故事阶段展开，不按章节分配内容。"""
    title: str = Field(min_length=1, max_length=160)
    scope: str = Field(min_length=4, max_length=600)
    segments: list[StoryDetailSegment] = Field(min_length=1, max_length=60)
    ending: str = Field(min_length=4, max_length=2000)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)


class PlanningVolumeDirection(StrictModel):
    volume_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    central_conflict: str = Field(min_length=20, max_length=1500)
    outcome: str = Field(min_length=20, max_length=1500)


class BookOutlineV2(PromiseAwareModel):
    """Whole-book narrative contract; chapter details belong downstream."""

    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=5000, max_length=25000)
    volumes: list[PlanningVolumeDirection] = Field(min_length=1, max_length=20)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_volumes(self) -> "BookOutlineV2":
        ordered = sorted(self.volumes, key=lambda item: item.volume_no)
        if [item.volume_no for item in ordered] != list(range(1, len(ordered) + 1)):
            raise ValueError("大纲的卷号必须从第一卷连续排列")
        if ordered[0].chapter_start != 1:
            raise ValueError("全书大纲必须从第一章开始")
        for index, item in enumerate(ordered):
            if item.chapter_end - item.chapter_start + 1 < 10:
                raise ValueError(f"第 {item.volume_no} 卷不足十章")
            if index and item.chapter_start != ordered[index - 1].chapter_end + 1:
                raise ValueError("卷章节范围必须连续且不重叠")
        return self


class VolumeDetailV2(PromiseAwareModel):
    volume_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    body: str = Field(min_length=5000, max_length=25000)
    rough_chapter_beats: list[str] = Field(min_length=5, max_length=80)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=32)


class RollingChapterV2(PromiseAwareModel):
    chapter_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=200, max_length=700)
    time_location: str = Field(default="", max_length=300)
    goal: str = Field(default="", max_length=400)
    obstacle: str = Field(default="", max_length=400)
    decision: str = Field(default="", max_length=400)
    consequence: str = Field(default="", max_length=400)
    scenes: list[str] = Field(default_factory=list, max_length=8)
    hook_question: str = Field(default="", max_length=300)
    promise_beats: list[PromiseBeat] = Field(default_factory=list, max_length=12)


class RollingPlanV2(PromiseAwareModel):
    anchor_chapter: int = Field(ge=1)
    anchor_summary: str = Field(min_length=100, max_length=700)
    chapters: list[RollingChapterV2] = Field(min_length=1, max_length=50)
    promises: list[PromisePlan] = Field(default_factory=list, max_length=24)


class PlanningReviewEvidence(StrictModel):
    candidate_excerpt: str = Field(min_length=4, max_length=300)
    source_excerpt: str = Field(min_length=4, max_length=300)
    finding: str = Field(min_length=4, max_length=500)


class PlanningReviewV2(StrictModel):
    verdict: Literal["pass", "revise", "insufficient_context"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=8, max_length=1000)
    evidence: list[PlanningReviewEvidence] = Field(default_factory=list, max_length=8)
    repair_instruction: str = Field(default="", max_length=1200)


class PromiseAlignmentBeatChange(StrictModel):
    """承诺对齐只可修现有节拍说明，章号、索引、承诺身份与动作均由原候选核对。"""
    chapter_no: int = Field(ge=1)
    beat_index: int = Field(ge=0)
    thread_id: str = Field(min_length=1)
    action: Literal["plant", "advance", "payoff", "delay"]
    expected_change: str = Field(min_length=1, max_length=1200)


class PromiseAlignmentPatch(StrictModel):
    """Writer仅返回指定冲突定义、实际缺失的父定义及必要节拍说明，不返回章节卡。"""
    plans: list[PromisePlan] = Field(min_length=1, max_length=32)
    beat_changes: list[PromiseAlignmentBeatChange] = Field(default_factory=list, max_length=128)


class PromiseAlignmentReviewV2(StrictModel):
    """附加差异审核绑定原候选、窄补丁和最终对象，旧层审核仅覆盖未改变部分。"""
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    patch_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_thread_ids: list[str] = Field(min_length=1, max_length=32)
    review: PlanningReviewV2


class PlanBundle(StrictModel):
    book: BookPlan
    current_volume: VolumePlan
    current_arc: ArcPlan

    @model_validator(mode="after")
    def validate_hierarchy(self) -> "PlanBundle":
        if self.current_arc.volume_no != self.current_volume.volume_no:
            raise ValueError("当前篇章不属于当前卷")
        matching = [item for item in self.current_volume.arcs if item.arc_id == self.current_arc.arc_id]
        if not matching:
            raise ValueError("当前卷纲中缺少当前篇章")
        summary = matching[0]
        if (summary.chapter_start, summary.chapter_end) != (
            self.current_arc.chapter_start,
            self.current_arc.chapter_end,
        ):
            raise ValueError("篇章摘要与篇章细纲的章节范围不一致")
        from .story_promises import planning_promises
        planning_promises(self)
        return self


class VolumeArcPlan(StrictModel):
    """The newly opened volume plus only its first detailed arc.

    The book compass is deliberately absent: advancing the rolling planning
    window must not silently rewrite the already approved whole-book promise.
    """

    current_volume: VolumePlan
    current_arc: ArcPlan

    @model_validator(mode="after")
    def validate_hierarchy(self) -> "VolumeArcPlan":
        if self.current_arc.volume_no != self.current_volume.volume_no:
            raise ValueError("当前篇章不属于新打开的卷")
        matching = [item for item in self.current_volume.arcs if item.arc_id == self.current_arc.arc_id]
        if not matching:
            raise ValueError("新卷纲中缺少当前篇章")
        summary = matching[0]
        if (summary.chapter_start, summary.chapter_end) != (
            self.current_arc.chapter_start,
            self.current_arc.chapter_end,
        ):
            raise ValueError("新卷篇章摘要与详细篇章的章节范围不一致")
        return self


class WriterNoteClaim(StrictModel):
    """公开创作说明；原文是定位线索，未来设想没有事实权威。"""

    kind: Literal["new_fact", "interpretation", "future", "unknown"]
    statement: str = Field(min_length=1, max_length=400)
    quote: str = Field(default="", max_length=200)
    prerequisites: list[str] = Field(default_factory=list, max_length=3)
    causal_steps: list[str] = Field(default_factory=list, max_length=3)
    followup_window: str = Field(default="", max_length=120)


class HookNote(PromiseAwareModel):
    """Writer 对当前正文版本的公开钩子交付说明，不属于小说正文或正史。"""

    hook_type: str = ""
    strength: Literal["light", "medium", "strong"] = "medium"
    actual_anchor: str = ""
    reader_expectation: str = ""
    why_keep: str = ""
    intentionally_withheld: str = ""
    must_be_clear: str = ""
    planned_followup: str = ""
    # 兼容模型偶尔使用的更明确字段名；落盘时统一归并为 planned_followup。
    planned_followup_window: str = ""
    annotations: list[WriterNoteClaim] = Field(default_factory=list, max_length=8)
    promise_work: list[PromiseWork] = Field(default_factory=list, max_length=12)


class WriterNoteClarification(StrictModel):
    answers: list[str] = Field(min_length=1, max_length=3)
    corrected_hook_note: HookNote | None = None

    @field_validator("answers")
    @classmethod
    def short_public_answers(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 600 for value in values):
            raise ValueError("逐题说明须为1～600字的公开结论，不返回正文或隐藏推理")
        return values


class HookAssessment(StrictModel):
    """Reviewer 对版本钩子的阅读体验判断；不替代正史安全门禁。"""

    clarity: Literal["clear", "intentional_ambiguity", "confusing", "absent"] = "absent"
    actual_anchor: str = ""
    reader_expectation: str = ""
    repetition_risk: str = ""
    payoff_risk: str = ""
    suggestion: str = ""


class SceneBlueprintItem(StrictModel):
    scene: str
    entry_state: str
    character_goal: str
    new_pressure: str
    key_choice: str
    exit_change: str
    reading_promise: str = ""


class SettingEvidenceRef(TypedDict, total=False):
    source_kind: Required[Literal["canonical", "draft", "planning", "writer_hook"]]
    chapter_no: int | None
    path: str | None
    artifact_id: str | None
    quote: Required[str]
    relation: Literal["support", "counter", "belief", "reference"]
    source_hash: str | None
    source_version: int | None


class SettingRecordProposal(StrictModel):
    collection_id: str = Field(min_length=1, max_length=180)
    record_id: str | None = Field(default=None, max_length=180)
    expected_revision: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=160)
    values: dict[str, Any] = Field(min_length=1, max_length=40)
    evidence_refs: list[SettingEvidenceRef] = Field(default_factory=list, max_length=12)
    epistemic_status: Literal["hypothesis", "objective", "belief", "rumor", "user_constraint"] = "hypothesis"

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def normalize_evidence_identity(cls, value):
        if not isinstance(value, list):
            return value
        result = []
        for item in value:
            if not isinstance(item, dict):
                result.append(item)
                continue
            ref = dict(item)
            source = ref.pop("source_chapter", None)
            source_id = ref.pop("source_id", None)
            if source is not None and source_id is not None and source != source_id:
                raise ValueError("设定引用包含不同的来源身份，不能猜测采用哪一个")
            source = source if source is not None else source_id
            explicit_kind = ref.get("source_kind")
            if "source_kind" not in ref:
                ref["source_kind"] = "canonical"
            if ref.get("chapter_no") is not None and source is not None:
                chapter = source if type(source) is int else int(source[8:]) if isinstance(source, str) and source.startswith("chapter:") and source[8:].isdigit() else None
                if chapter != ref["chapter_no"]:
                    raise ValueError("设定引用的新旧章号不一致，不能串用来源")
            if ref.get("chapter_no") is None and source is not None:
                if type(source) is int:
                    ref["chapter_no"] = source
                elif isinstance(source, str) and source.startswith("chapter:") and source[8:].isdigit():
                    ref["chapter_no"] = int(source[8:])
                elif source in {"BOOK.md", "PLAN.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"}:
                    if explicit_kind not in {None, "planning"} or ref.get("path") not in {None, source}:
                        raise ValueError("设定引用的文件与来源类型不一致")
                    ref.update(source_kind="planning", path=source)
                else:
                    raise ValueError("设定来源必须是明确章号或当前文档名称，不能把未知文字当章号")
            result.append(ref)
        return result

    @field_validator("evidence_refs")
    @classmethod
    def require_evidence_location(cls, values):
        for ref in values:
            if not ref["quote"].strip():
                raise ValueError("设定依据须填写连续原句")
            if ref["source_kind"] in {"canonical", "draft"} and (type(ref.get("chapter_no")) is not int or ref["chapter_no"] < 1):
                raise ValueError("正文设定依据须填写chapter_no正整数章号；文档引用用planning及path")
            if ref["source_kind"] == "planning" and ref.get("path") not in {"BOOK.md", "PLAN.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"}:
                raise ValueError("规划设定依据须填写当前文档path")
        return values


class SettingCandidateVerification(StrictModel):
    """原责任角色仅核已返回候选，不开放重写字段；双哈希绑定引用回源前后的完整记录。"""

    original_candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verdict: Literal["pass", "needs_evidence"]
    reason: str = Field(min_length=1, max_length=1200)


class SettingRecordSet(StrictModel):
    records: list[SettingRecordProposal] = Field(default_factory=list, max_length=8)


class SettingRuleCorrection(StrictModel):
    old_rule: str = Field(min_length=1)
    new_rule: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_quote: str = Field(min_length=4)


class StorySettingsAudit(StrictModel):
    summary: str = Field(min_length=1, max_length=2400)
    findings: list[str] = Field(default_factory=list, max_length=12)
    rule_corrections: list[SettingRuleCorrection] = Field(default_factory=list, max_length=8)
    needs_choice: list[str] = Field(default_factory=list, max_length=3)
    setting_updates: list[SettingRecordProposal] = Field(default_factory=list, max_length=8)
    questions: list["ClarificationQuestion"] = Field(default_factory=list, max_length=3)


class DraftOutput(StrictModel):
    setting_updates: list[SettingRecordProposal] = Field(default_factory=list, max_length=8)
    title: str = Field(description="独立的章节标题，必须单独返回；不能只把标题写进content。")
    content: str = Field(min_length=100, description="完整小说正文，不含Markdown章节标题；公开创作说明放在其他专用字段。")
    decision_summary: list[str] = Field(
        default_factory=lambda: ["模型未提供结构化摘要；正文将由后续审查确认。"],
        min_length=1,
        max_length=12,
    )
    new_fact_candidates: list[str] = Field(default_factory=list)
    thread_changes: list[str] = Field(default_factory=list)
    hook_note: HookNote | None = None
    scene_blueprint: list[SceneBlueprintItem] = Field(default_factory=list, max_length=8)

class SceneDraftOutput(StrictModel):
    """Writer prose for an isolated scene draft; no review or canon authority."""

    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20_000)
    decision_summary: list[str] = Field(min_length=1, max_length=8)

class ParagraphInsertion(StrictModel):
    before_paragraph: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=12_000)


class ParagraphExpansionPlan(StrictModel):
    insertions: list[ParagraphInsertion] = Field(min_length=1)
    decision_summary: list[str] = Field(min_length=1, max_length=8)


class ParagraphCutPlan(StrictModel):
    paragraph_ids: list[int] = Field(min_length=1, max_length=80)
    decision_summary: list[str] = Field(min_length=1, max_length=8)


class SelectionRevisionOutput(StrictModel):
    """Writer 对一个已锁定正文选区给出的最小替换结果。"""

    replacement: str = Field(min_length=1, max_length=20_000)
    decision_summary: list[str] = Field(min_length=1, max_length=6)


class AcceptedContinuityDiagnosis(StrictModel):
    """An Editor's evidence-bound decision about an accepted-chapter hold."""

    verdict: Literal["already_explained", "needs_local_repair", "needs_user"]
    reason: str = Field(min_length=1, max_length=1_000)
    bridge_evidence: str = Field(default="", max_length=1_000)
    repair_instruction: str = Field(default="", max_length=1_000)
    key_objects: list[str] = Field(default_factory=list, max_length=4)


class AcceptedContinuityLocalEdit(StrictModel):
    target_excerpt: str = Field(min_length=1, max_length=500)
    replacement: str = Field(max_length=800)


class ReviewCopyEdit(AcceptedContinuityLocalEdit):
    """An optional, source-bound micro edit proposed during the existing review."""

    kind: Literal["typo", "punctuation", "duplicate", "expression"]
    reason: str = Field(min_length=1, max_length=400)
    meaning_preserved: bool
    author_role: Literal["editor", "reviewer"] | None = None


class AcceptedContinuityPatch(StrictModel):
    """Writer returns a handful of exact local edits, never a rewritten chapter."""

    edits: list[AcceptedContinuityLocalEdit] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=1_000)


class DraftRevisionEdit(AcceptedContinuityLocalEdit):
    """草稿场景修订允许定位完整问题段；正史微改仍使用原短片段合同。"""

    target_excerpt: str = Field(min_length=1, max_length=16_000)
    replacement: str = Field(max_length=16_000)


class DraftRevisionPatch(AcceptedContinuityPatch):
    """沿用逐字定位补丁，容纳问题场景；引擎仍拒绝重叠、错版和整章替换。"""

    edits: list[DraftRevisionEdit] = Field(min_length=1, max_length=4)


class AcceptedContinuityVerification(StrictModel):
    """独立核对局部候选；旧章工作引用清理另须明确确认含义与事实均保持。"""
    verdict: Literal["resolved", "not_resolved", "needs_user"]
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1_000)
    evidence: str = Field(min_length=1, max_length=1_000)
    anchor_excerpt: str = Field(min_length=8, max_length=500)
    checked_state_ids: list[int] = Field(default_factory=list, max_length=80)
    meaning_preserved: bool = False


FindingCategory = Literal[
    "timeline",
    "character",
    "knowledge",
    "world",
    "causality",
    "planning",
    "pacing",
    "style",
    "originality",
    "format",
]


EvidenceRelation = Literal["unchecked", "direct", "state_change", "perspective", "plan_adaptation", "new_information", "exclusive_conflict", "irrelevant", "insufficient"]


class ReviewFinding(StrictModel):
    category: FindingCategory
    severity: Literal["info", "minor", "major", "blocking"]
    evidence: str
    canon_refs: list[str] = Field(default_factory=list)
    explanation: str
    repair_instruction: str
    rule_id: str = ""
    reference_evidence: str = ""
    verification_note: str = ""
    claim: str = ""
    verification_status: Literal["unchecked", "anchored", "unsupported", "uncertain"] = "unchecked"
    semantic_status: Literal["unchecked", "supported", "contradicted", "not_blocking", "uncertain"] = "unchecked"
    verification_confidence: float = Field(default=0.0, ge=0, le=1)
    proposed_severity: Literal["info", "minor", "major", "blocking"] | None = None
    conflict_type: EvidenceRelation = "unchecked"

    @field_validator("category", mode="before")
    @classmethod
    def normalize_continuity_category(cls, value: Any) -> Any:
        return "knowledge" if value == "continuity" else value


class PlanConflictAnchor(StrictModel):
    """Two exact quotes for a reported plan/canon conflict, not a new review."""

    plan_excerpt: str = Field(min_length=8, max_length=240)
    canon_ref: str = Field(min_length=1, max_length=100)
    canon_excerpt: str = Field(min_length=8, max_length=240)
    reason: str = Field(min_length=1, max_length=500)


ReviewScoreDimensionName = Literal[
    "剧情因果",
    "剧情推进、伏笔与钩子",  # 专项审查报告的剧情推进维度，旧报告继续沿用原评分名称。
    "人物动机与认知",
    "线索来源与世界规则",
    "章节职责与承接",
    "文本完整性与阅读",
    "正史与认知",
    "因果与人物",
    "章节卡履约",
    "完整性",
    "表达与节奏",
]


class ReviewScoreDimension(StrictModel):
    """A transparent editorial score derived only from evidence-backed findings."""

    dimension: ReviewScoreDimensionName
    maximum_score: int = Field(ge=1, le=100)
    score: float = Field(ge=0, le=100)
    deductions: list[ReviewFinding] = Field(default_factory=list)


class SettingUseCheck(StrictModel):
    record_id: str = Field(min_length=1)
    record_revision: int | None = Field(default=None, ge=1)
    disposition: Literal["compatible", "conflict", "not_applicable", "needs_evidence"]
    setting_quote: str = Field(min_length=1, max_length=400)
    chapter_quote: str = Field(default="", max_length=400)
    reason: str = Field(min_length=1, max_length=500)


class ContextUseAudit(StrictModel):
    """Reviewer 对 Writer 是否正确使用上下文的公开核对结果。"""

    used_source_ids: list[str] = Field(default_factory=list)
    missing_required_source_ids: list[str] = Field(default_factory=list)
    conflicting_source_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    setting_checks: list[SettingUseCheck] = Field(default_factory=list)


class ReviewFocusObservation(StrictModel):
    """Short, quote-grounded readback of the actual chapter before judgment."""

    observed_goal: str = ""
    goal_evidence: str = ""
    observed_change: str = ""
    change_evidence: str = ""
    plan_alignment: Literal["aligned", "adapted", "diverged", "unclear"] = "unclear"
    alignment_reason: str = ""


class ReviewSourceComparison(StrictModel):
    """A visible, quote-checkable comparison to one actual context source."""

    source_id: str = Field(min_length=1, max_length=160)
    source_evidence: str = Field(min_length=4, max_length=400)
    chapter_evidence: str = Field(min_length=4, max_length=400)
    relation: Literal["aligned", "adapted", "tension", "conflict"]
    reason: str = Field(min_length=1, max_length=500)
    evidence_relation: EvidenceRelation = "unchecked"


class ReviewAssessment(StrictModel):
    """One public rubric judgement, not a hidden reasoning trace or probability."""

    criterion: Literal["continuity", "causality", "requirements", "motivation", "progression", "readability"]
    status: Literal["met", "partial", "failed", "data_missing"]
    grade: int = Field(ge=0, le=4)
    chapter_evidence: str = Field(default="", max_length=400)
    source_id: str = Field(default="", max_length=160)
    source_evidence: str = Field(default="", max_length=400)
    reason: str = Field(min_length=1, max_length=600)
    alternative: str = Field(min_length=1, max_length=400)
    evidence_relation: EvidenceRelation = "unchecked"


class EndingEvidence(StrictModel):
    """提供终局结果所在的实际正文及逐字引文。"""

    source_id: str = Field(min_length=1, max_length=160)
    quote: str = Field(min_length=8, max_length=1200)


class EndingCheck(StrictModel):
    """终章逐条核对全书主线；证据只能来自当前正文或已接受前章。"""

    thread_id: str = Field(min_length=1, max_length=100)
    status: Literal["resolved", "unresolved", "needs_evidence"]
    evidence: list[EndingEvidence] = Field(default_factory=list, max_length=6)
    reason: str = Field(min_length=1, max_length=600)


class PreferenceConflictSource(StrictModel):
    preference_id: str = Field(min_length=1, max_length=180)
    revision: int = Field(ge=0)
    quote: str = Field(min_length=2, max_length=1000)


class PreferenceConflict(StrictModel):
    sources: list[PreferenceConflictSource] = Field(min_length=1, max_length=2)
    current_task_quote: str = Field(default="", max_length=2000)
    suggested_id: str = Field(default="", max_length=180)
    reason: str = Field(min_length=4, max_length=600)


class ReviewReferenceObservation(StrictModel):
    """Optional evidence-bound reading notes; no independent score or release gate."""

    aspect: str = Field(min_length=1, max_length=80)
    observation: str = Field(min_length=1, max_length=800)
    chapter_evidence: str = Field(default="", max_length=400)
    source_id: str = Field(default="", max_length=160)
    source_evidence: str = Field(default="", max_length=400)
    alternative: str = Field(min_length=1, max_length=400)
    reference_use: str = Field(min_length=1, max_length=400)


class MemorySummaryRebuild(StrictModel):
    """A same-call proposal to rebuild one stale summary from accepted full text."""

    chapter_no: int = Field(ge=1)
    source_version: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_summary: str = Field(min_length=1, max_length=6_000)
    evidence_quotes: list[Annotated[str, Field(min_length=2, max_length=1_200)]] = Field(min_length=1, max_length=6)


class ReviewReport(UnifiedRoleModel):
    preference_decisions: list[dict[str, Any]] = Field(default_factory=list, max_length=6)
    verdict: Literal["pass", "patch", "replan", "unknown"]
    confidence: float = Field(ge=0, le=1)
    model_self_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_basis: list[str] = Field(default_factory=list)
    scoring_version: str = ""
    evidence_policy_version: str = ""
    evidence_recovery: dict[str, Any] = Field(default_factory=dict)
    assessments: list[ReviewAssessment] = Field(default_factory=list)
    missing_source_ids: list[str] = Field(default_factory=list)
    source_queries: list[str] = Field(default_factory=list)
    summary: str
    strengths: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    copyedits: list[ReviewCopyEdit] = Field(default_factory=list, max_length=4)
    scorecard: list[ReviewScoreDimension] = Field(default_factory=list)
    source_hash: str = ""
    writer_notes_hash: str = ""
    approved_setting_proposals: list[int] = Field(default_factory=list, max_length=8)
    context_fingerprint: str = ""
    instruction_hash: str = ""
    hook_assessment: HookAssessment | None = None
    context_use_audit: ContextUseAudit = Field(default_factory=ContextUseAudit)
    focus_observation: ReviewFocusObservation = Field(default_factory=ReviewFocusObservation)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list)
    ending_checks: list[EndingCheck] = Field(default_factory=list, max_length=34, exclude_if=lambda checks: not checks)
    reference_observations: list[ReviewReferenceObservation] = Field(default_factory=list, max_length=8)
    memory_patch: MemoryPatch | None = None


class ModeCheckOutput(UnifiedRoleModel):
    """Unified role-scoped output; source identity and memory ownership belong to the engine."""

    verdict: Literal["pass", "patch", "unknown"]
    preference_conflicts: list[PreferenceConflict] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0, le=1)
    assessments: list[ReviewAssessment] = Field(default_factory=list, max_length=6)
    missing_source_ids: list[str] = Field(default_factory=list, max_length=6)
    source_queries: list[str] = Field(default_factory=list, max_length=4)
    writer_note_questions: list[str] = Field(default_factory=list, max_length=3)
    setting_updates: list[SettingRecordProposal] = Field(default_factory=list, max_length=8)
    summary: str = Field(min_length=1, max_length=2_000)
    strengths: list[str] = Field(default_factory=list, max_length=8)
    approved_setting_proposals: list[int] = Field(default_factory=list, max_length=8)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=24)
    copyedits: list[ReviewCopyEdit] = Field(default_factory=list, max_length=4)
    focus_observation: ReviewFocusObservation = Field(default_factory=ReviewFocusObservation)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list, max_length=8)
    # 只有终章主审的动态合同对模型公开该字段，旧任务的请求结构仍可精确复用。
    ending_checks: SkipJsonSchema[list[EndingCheck]] = Field(default_factory=list, max_length=34, exclude_if=lambda checks: not checks)
    reference_observations: list[ReviewReferenceObservation] = Field(default_factory=list, max_length=8)
    source_summary_rebuilds: list[MemorySummaryRebuild] = Field(default_factory=list, max_length=6)
    source_event_rebuilds: list[NarrativeEventRebuild] = Field(default_factory=list, max_length=6)
    memory_patch: MemoryPatch | None = None
    hook_assessment: HookAssessment | None = None
    context_use_audit: ContextUseAudit = Field(default_factory=ContextUseAudit)

    @model_serializer(mode="wrap")
    def serialize_event_rebuilds(self, handler):
        data = handler(self)
        if not self.source_event_rebuilds:
            data.pop("source_event_rebuilds", None)
        return data

    @model_validator(mode="after")
    def consistent_generation_verdict(self, info: ValidationInfo) -> "ModeCheckOutput":
        if (info.context and info.context.get("model_generation") and self.verdict == "patch"
                and not self.findings and not (self.memory_patch and self.memory_patch.unresolved_conflicts)):
            raise ValueError("审核标为需要修订，却没有列出问题。请核对本次结论：存在问题须给有据findings；资料不足用unknown；必要检查完成且没有问题用pass。返回memory_patch本身不表示正文需要修订。")
        if info.context and info.context.get("model_generation"):
            failed_required = [item.criterion for item in self.assessments
                if item.criterion in {"continuity", "causality", "requirements"}
                and item.status in {"partial", "failed"}]
            if failed_required and not any(item.severity in {"major", "blocking"} and item.rule_id
                    for item in self.findings):
                raise ValueError("必需项判为未满足，却只给出普通表达问题或未标明规则的指控。请由当前审核角色纠正结论：已证实硬矛盾须在findings给适用rule_id及双方原句；资料不足用data_missing；仅有普通表达弱点不能把已完成的核心结果判为partial或failed，应在质量项说明。不得编造硬问题来凑结论。")
        return self


# Import-name compatibility only; no separate output contract or execution path.
ReviewModelOutput = ModeCheckOutput


class ReviewFindingBatch(StrictModel):
    findings: list[ReviewFinding] = Field(default_factory=list)


class ReviewClaimDecision(StrictModel):
    finding_index: int = Field(ge=0)
    verdict: Literal["supported", "contradicted", "not_blocking", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    resolution_evidence: str = ""
    conflict_type: EvidenceRelation = "unchecked"


class ReviewClaimDecisionBatch(StrictModel):
    decisions: list[ReviewClaimDecision] = Field(default_factory=list)


class ArcAuditReport(StrictModel):
    verdict: Literal["aligned", "needs_replan", "blocked", "unknown"]
    confidence: float = Field(ge=0, le=1)
    model_self_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_basis: list[str] = Field(default_factory=list)
    scoring_version: str = ""
    evidence_policy_version: str = ""
    evidence_recovery: dict[str, Any] = Field(default_factory=dict)
    assessments: list[ReviewAssessment] = Field(default_factory=list, max_length=6)
    missing_source_ids: list[str] = Field(default_factory=list, max_length=6)
    source_queries: list[str] = Field(default_factory=list, max_length=4)
    summary: str
    fulfilled_commitments: list[str] = Field(default_factory=list)
    deviations: list[ReviewFinding] = Field(default_factory=list)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list, max_length=8)
    reference_observations: list[ReviewReferenceObservation] = Field(default_factory=list, max_length=8)
    future_impact: list[str] = Field(default_factory=list)
    body_repair_recommended: bool = False
    body_repair_scope: list[int] = Field(default_factory=list)
    replan_recommended: bool = False
    proposed_future_changes: list[str] = Field(default_factory=list)
    source_hash: str = ""


class MemoryEvidence(StrictModel):
    source_chapter: int = Field(ge=1)
    quote: str = Field(min_length=2, max_length=2000)
    relation: Literal["support", "counter", "belief", "reference"] = "support"


class MemoryOperation(StrictModel):
    action: Literal["supplement", "retract", "replace_evidence", "retire_evidence"]
    target_fact_id: str = Field(min_length=1)
    target_evidence_id: str = ""
    reason: str = Field(min_length=2, max_length=1000)
    evidence: list[MemoryEvidence] = Field(min_length=1, max_length=12)


class FactMutation(StrictModel):
    fact_id: str
    subject: str
    predicate: str
    value: Any
    valid_from_chapter: int = Field(ge=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: str = Field(min_length=2)
    evidence_refs: list[MemoryEvidence] = Field(default_factory=list, max_length=12)
    epistemic_kind: Literal["objective", "belief", "rumor"] = "objective"
    event_time: str | None = None
    narrative_time: str | None = None


class ThreadMutation(PromiseAwareModel):
    thread_id: str
    kind: Literal["plot", "promise", "foreshadow", "relationship", "mystery", "hook"]
    title: str
    status: Literal["open", "advanced", "paid", "delayed", "abandoned"]
    description: str
    planted_chapter: int | None = Field(default=None, ge=1)
    due_chapter: int | None = Field(default=None, ge=1)
    plan: PromisePlan | None = None
    plan_source_id: str = ""
    plan_source_hash: str = ""
    evidence: str = Field(default="", max_length=2000)
    reason: str = Field(default="", max_length=1000)
    condition_checks: list[PromiseConditionCheck] = Field(default_factory=list, max_length=16)


class EventTimeEvidence(MemoryEvidence):
    source_version: int | None = Field(default=None, ge=1)
    source_hash: str = ""


class NarrativeEventRelation(StrictModel):
    target_chapter: int = Field(ge=1)
    target_event_id: str = Field(min_length=1, max_length=100)
    relation: Literal["before", "after", "overlaps", "same_event"]
    evidence: str = Field(min_length=2, max_length=2000)
    target_source_version: int | None = Field(default=None, ge=1)
    target_source_hash: str = ""


class NarrativeEvent(StrictModel):
    event_id: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=2, max_length=800)
    participants: list[str] = Field(min_length=1, max_length=12)
    location: str = Field(default="", max_length=200)
    evidence: str = Field(min_length=2, max_length=2000)
    scene_start: str = Field(min_length=2, max_length=200)
    scene_end: str = Field(min_length=2, max_length=200)
    narrative_mode: Literal["current", "flashback", "dream", "hypothetical", "belief", "rumor"] = "current"
    event_time: str | None = Field(default=None, max_length=200)
    time_status: Literal["explicit", "inferred", "unknown"] = "unknown"
    time_evidence: list[EventTimeEvidence] = Field(default_factory=list, max_length=12)
    relations: list[NarrativeEventRelation] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def grounded_time(self) -> "NarrativeEvent":
        if self.time_status == "unknown":
            if self.event_time is not None:
                raise ValueError("事件时间未知时不得补造时间标签")
        elif not self.event_time or not self.time_evidence:
            raise ValueError("明确或推断的事件时间必须提供时间标签和原文依据")
        if any(not name.strip() for name in self.participants):
            raise ValueError("事件参与者不得为空")
        return self


class MemoryPatch(StrictModel):
    setting_updates: list[SettingRecordProposal] = Field(default_factory=list, max_length=8)
    chapter_no: int = Field(ge=1)
    chapter_summary: str
    scene_summaries: list[str] = Field(default_factory=list)
    narrative_events: list[NarrativeEvent] = Field(default_factory=list, max_length=32)
    facts: list[FactMutation] = Field(default_factory=list)
    threads: list[ThreadMutation] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)
    operations: list[MemoryOperation] = Field(default_factory=list, max_length=20)

    @model_serializer(mode="wrap")
    def serialize_events(self, handler):
        data = handler(self)
        # Empty legacy patches keep their original serialized hashes during recovery.
        if not self.narrative_events:
            data.pop("narrative_events", None)
        return data

    @field_validator("facts")
    @classmethod
    def unique_fact_ids(cls, value: list[FactMutation]) -> list[FactMutation]:
        ids = [item.fact_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("memory patch 中 fact_id 重复")
        return value

    @field_validator("narrative_events")
    @classmethod
    def unique_event_ids(cls, value: list[NarrativeEvent]) -> list[NarrativeEvent]:
        if len({item.event_id for item in value}) != len(value):
            raise ValueError("同章事件编号不得重复")
        return value


class NarrativeEventRebuild(StrictModel):
    chapter_no: int = Field(ge=1)
    source_version: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    narrative_events: list[NarrativeEvent] = Field(min_length=1, max_length=32)

    def as_patch(self) -> MemoryPatch:
        return MemoryPatch(chapter_no=self.chapter_no, chapter_summary="", narrative_events=self.narrative_events)


class FactEvidenceRepair(StrictModel):
    fact_id: str
    evidence: str | None = None
    drop: bool = False

    @model_validator(mode="after")
    def evidence_or_drop(self) -> "FactEvidenceRepair":
        if self.drop:
            return self
        if not self.evidence or len(self.evidence.strip()) < 2:
            raise ValueError("未删除的事实必须提供正文连续原文 evidence")
        return self


class EvidenceRepairBatch(StrictModel):
    repairs: list[FactEvidenceRepair] = Field(default_factory=list)

    @field_validator("repairs")
    @classmethod
    def unique_fact_ids(cls, value: list[FactEvidenceRepair]) -> list[FactEvidenceRepair]:
        ids = [item.fact_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence repair 中 fact_id 重复")
        return value


class FactEvidenceSelection(StrictModel):
    fact_id: str
    candidate_id: str | None = Field(default=None, max_length=40)
    drop: bool = False

    @model_validator(mode="after")
    def candidate_or_drop(self) -> "FactEvidenceSelection":
        if self.drop:
            return self
        if not self.candidate_id:
            raise ValueError("未删除的事实必须选择一个候选 evidence ID")
        return self


class EvidenceSelectionBatch(StrictModel):
    selections: list[FactEvidenceSelection] = Field(default_factory=list)

    @field_validator("selections")
    @classmethod
    def unique_fact_ids(cls, value: list[FactEvidenceSelection]) -> list[FactEvidenceSelection]:
        ids = [item.fact_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence selection 中 fact_id 重复")
        return value


class ContextSection(StrictModel):
    key: str
    title: str
    content: str
    source_ids: list[str] = Field(default_factory=list)
    hard: bool = False
    # Scope describes how long the source is expected to remain unchanged.
    # Unknown/new sources default to the dynamic tail, never the cache prefix.
    cache_scope: Literal["global", "book", "canon", "chapter", "request"] = "request"
    # 仅控制请求里的既有来源顺序；不增加取材、不改原文、版本、权限或接受条件。
    # B使用原公共事实集合，F/CSET填写已选条目顺序；SET仅由v3 Coordinator填写完整已选编号，
    # 用于无损拆分定义与记录。旧SET未填此提示，原始块仍供恢复索引及来源核对使用。
    cache_prefix_source_ids: list[str] | None = None

    def model_content(self, *, canonical: bool = False) -> str:
        """Keep source text intact while excluding duplicated operational diagnostics."""
        try:
            value = json.loads(self.content)
        except (ValueError, TypeError):
            return self.content or "（无）"
        if self.key in {"B", "B-current"} and isinstance(value, list):
            for fact in value:
                if not isinstance(fact, dict) or not fact.get("事实编号") or not isinstance(fact.get("关联证据"), list):
                    continue
                for reference in fact["关联证据"]:
                    if isinstance(reference, dict) and all(reference.get(key) for key in
                            ("evidence_id", "source_chapter", "source_version", "quote", "relation")):
                        # Integrity and quote offsets are checked against the untouched source packet.
                        reference.pop("source_hash", None)
                        reference.pop("offset", None)
        if self.key == "MEMORY" and isinstance(value, dict):
            value.pop("query_input", None)
            value.pop("canonical_batch_scope", None)
            retrieval = value.get("retrieval")
            if isinstance(retrieval, dict):
                for key in ("query", "discarded", "discarded_display_limit", "branches", "overlaps", "selected", "source_reads", "writer_reference_omissions", "candidate_count", "maintenance"):
                    retrieval.pop(key, None)
        if self.key == "WORK" and isinstance(value, list):
            pending = value
        elif self.key in {"C", "C-work"} and isinstance(value, dict) and isinstance(value.get("pending_work"), list):
            pending = value["pending_work"]
        else:
            pending = []
        if pending:
            for work in pending:
                if not isinstance(work, dict):
                    continue
                if isinstance(work.get("source"), dict):
                    work["source"].pop("failure_case", None)
                if isinstance(work.get("progress"), dict):
                    work["progress"].pop("original_node_receipts", None)
                if isinstance(work.get("intent"), dict) and work["intent"]:
                    try:
                        # Omit repeated defaults only on the wire; original tasks and permissions stay intact.
                        work["intent"] = TerminalIntent.model_validate(work["intent"]).model_dump(mode="json", exclude_defaults=True)
                    except (ValueError, TypeError):
                        pass
        if self.key in {"F", "F0"} and isinstance(value, (dict, list)):
            hits = value.get("自适应结果", []) if isinstance(value, dict) else value
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                # 仅容量预检查显式标记的新包使用精简表示；旧付费请求保持原投影和恢复哈希。
                wire_version = hit.pop("_wire_metadata_version", None)
                compact = (type(wire_version) is int and wire_version == 1
                    and hit.get("source_type") in {"canon_fact", "canon_source", "chapter_summary"}
                    and isinstance(hit.get("body"), str) and bool(hit.get("source_id")))
                if self.key == "F" or compact:
                    for key in ("score", "reranker_score", "retrieval_reasons", "entities"):
                        hit.pop(key, None)
                if (self.key == "F" or compact) and hit.get("body_kind") == "accepted_prose" and hit.get("resolved_source"):
                    for key in ("summary_body", "summary_content_hash", "summary_origin"):
                        hit.pop(key, None)
                if compact:
                    # 完整原句不动；只有能逐字重建的重复事实字段才省略，不能靠近似文本去重。
                    if (hit.get("source_type") == "canon_fact" and "value" in hit and "evidence" in hit
                            and hit["body"] == json.dumps(hit["value"], ensure_ascii=False) + "\n" + str(hit["evidence"] or "")):
                        hit.pop("value")
                        hit.pop("evidence")
                        if hit.get("title") == f"{hit.get('subject')} / {hit.get('predicate')}":
                            hit.pop("title")
                    # 程序仍用原packet核验这些哈希；模型引用依据是原句、编号、章节及版本。
                    for key in ("content_hash", "source_fingerprint", "source_hash", "authority_rank"):
                        hit.pop(key, None)
                    if isinstance(hit.get("resolved_source"), dict):
                        hit["resolved_source"].pop("source_hash", None)
        # JSON whitespace is not source prose; strings, quotes and source identities remain exact.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=canonical) if isinstance(value, (dict, list)) else self.content


def _compact_context_tables(value: Any) -> Any:
    """把重复字段名改成带说明的对象表；每个值、原句、缺字段与 null 均保持原义。"""
    if isinstance(value, dict):
        return {key: _compact_context_tables(item) for key, item in value.items()}
    if not isinstance(value, list):
        return value
    records = [_compact_context_tables(item) for item in value]
    if len(records) < 3 or not all(isinstance(item, dict) for item in records):
        return records
    columns = sorted(set.intersection(*(set(item) for item in records)))
    if len(columns) < 4:
        return records
    column_set = set(columns)
    table = {"对象表读取": "每行前面的值依次对应字段名，末项对象保存该行其余原字段；原句和来源不变。",
        "字段": columns, "行": [[*[item[key] for key in columns],
            {key: item[key] for key in item if key not in column_set}] for item in records]}
    # 小表不抵说明开销时保持原表示；只压重复键，不摘要、截断或舍弃任何条目。
    dump = lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    return table if len(dump(records)) - len(dump(table)) >= 256 else records


def _compact_context_repetitions(value: Any) -> Any:
    """同一节的长原文或完整对象只发送一份，其余以可还原的本节路径引用。"""
    seen: dict[str, list[str | int]] = {}
    reserved = False

    def visit(item: Any, path: list[str | int]) -> Any:
        """只合并逐字相同的JSON值；不同版本、性质或来源的对象不能合并。"""
        nonlocal reserved
        if isinstance(item, dict) and set(item) == {"本段原文"}:
            reserved = True  # 原资料已使用保留形状时整节不转换，不能误读作者自己的字段。
        wire = json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(wire) >= 256:
            if wire in seen:
                return {"本段原文": seen[wire]}
            seen[wire] = path
        if isinstance(item, dict):
            return {key: visit(item[key], [*path, key]) for key in sorted(item)}
        if isinstance(item, list):
            return [visit(child, [*path, index]) for index, child in enumerate(item)]
        return item

    compact = {"原文复用规则": "本段原文是本节内容中完整原值的路径，逐字相同；引用不改变来源、性质、版本或适用范围。",
        "内容": visit(value, [])}
    dump = lambda item: json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return compact if not reserved and len(dump(value)) - len(dump(compact)) >= 256 else value


def _expand_context_tables(value: Any) -> Any:
    """还原本节同文引用及对象表供来源核对；拒绝缺失或循环引用，不把引用当新证据。"""
    if isinstance(value, dict) and set(value) == {"原文复用规则", "内容"}:
        root = value["内容"]

        def restore(item: Any, ancestors: set[tuple]) -> Any:
            """引用只定位同节真实路径；损坏数据不能跳到其他文件或无限递归。"""
            if isinstance(item, dict) and set(item) == {"本段原文"}:
                path = item["本段原文"]
                if not isinstance(path, list) or any(type(part) not in (str, int) for part in path):
                    raise ValueError("同文引用路径无效。")
                identity = tuple(path)
                if identity in ancestors:
                    raise ValueError("同文引用存在循环。")
                target = root
                for part in path:
                    if (isinstance(target, dict) and type(part) is str and part in target
                            or isinstance(target, list) and type(part) is int and 0 <= part < len(target)):
                        target = target[part]
                    else:
                        raise ValueError("同文引用缺少完整原值。")
                return restore(target, ancestors | {identity})
            if isinstance(item, dict):
                return {key: restore(child, ancestors) for key, child in item.items()}
            if isinstance(item, list):
                return [restore(child, ancestors) for child in item]
            return item

        return _expand_context_tables(restore(root, set()))
    if isinstance(value, list):
        return [_expand_context_tables(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"对象表读取", "字段", "行"}:
        columns, rows = value["字段"], value["行"]
        if (not isinstance(columns, list) or not columns or not all(isinstance(key, str) for key in columns)
                or len(set(columns)) != len(columns) or not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != len(columns) + 1
                    or not isinstance(row[-1], dict) or set(row[-1]) & set(columns) for row in rows)):
            raise ValueError("上下文对象表结构不完整，不能核对原来源索引。")
        return [{**{key: _expand_context_tables(row[index]) for index, key in enumerate(columns)},
                 **_expand_context_tables(row[-1])} for row in rows]
    return {key: _expand_context_tables(item) for key, item in value.items()}


class ContextPacket(StrictModel):
    project_id: str
    chapter_no: int
    task: str
    sections: list[ContextSection]
    estimated_tokens: int
    warnings: list[str] = Field(default_factory=list)
    cache_policy: Literal["standard", "deepseek", "mimo", "openai"] = "standard"
    cache_layout_version: int = 1
    # 已授权的当次审查要求已在请求正文中。单独保留用于引用核验，不改变原资料包的发送格式。
    review_instruction: str = Field(default="", exclude=True)

    def to_markdown(self) -> str:
        output = [
            f"# Context Packet · 第 {self.chapter_no} 章",
            "",
            f"> 任务：{self.task}",
            f"> 估算输入：{self.estimated_tokens} tokens",
            "",
        ]
        for section in self.sections:
            output.extend([f"## {section.key}. {section.title}", "", section.content or "（无）", ""])
            if section.source_ids:
                output.extend([f"来源：{', '.join(section.source_ids)}", ""])
        if self.warnings:
            output.extend(["## Warnings", "", *[f"- {item}" for item in self.warnings], ""])
        return "\n".join(output).rstrip() + "\n"

    def to_model_prompt(self) -> str:
        return "".join(self.model_prompt_parts())

    def _settings_catalog_sections(self, section: ContextSection) -> list[ContextSection] | None:
        """无损拆分已选设定的固定定义、作者约定和任务记录；不能完整绑定时保留原块。"""
        key = section.key
        if (sum(item.key == key for item in self.sections) != 1
                or any(item.key in {f"{key}-definitions", f"{key}-rules", f"{key}-records"} for item in self.sections)):
            return None
        try:
            value = json.loads(section.content)
        except (ValueError, TypeError):
            return None
        if not isinstance(value, dict) or not isinstance(value.get("collections"), list):
            return None
        collections = value["collections"]
        if any(not isinstance(item, dict) or not isinstance(item.get("collection_id"), str)
               or not item["collection_id"] or not isinstance(item.get("records"), list) for item in collections):
            return None
        collection_ids = [item["collection_id"] for item in collections]
        if len(set(collection_ids)) != len(collection_ids):
            return None
        # 提示只能绑定本次已选的完整集合；不能借旧顺序补回未选条目，也不能猜同名对象。
        record_ids = []
        for collection in collections:
            for record in collection["records"]:
                if (not isinstance(record, dict) or not isinstance(record.get("record_id"), str)
                        or not record["record_id"]
                        or ("collection_id" in record and record["collection_id"] != collection["collection_id"])):
                    return None
                record_ids.append(record["record_id"])
        if key == "CSET" and (len(section.source_ids) != len(record_ids) or set(section.source_ids) != set(record_ids)):
            return None  # 来源身份不完整时保留原块，不能靠拆分重建或猜补编号。
        hint = section.cache_prefix_source_ids
        if key == "CSET" and hint is None:
            hint = record_ids
        if (not isinstance(hint, list) or any(not isinstance(identity, str) for identity in hint)
                or len(hint) != len(set(hint)) or len(record_ids) != len(set(record_ids))
                or set(hint) != set(record_ids)):
            return None
        # 先复用原规范化路径，避免换key后绕过投影、恢复已省略的审计材料。
        # SET清空提示防递归；CSET降布局版本，仍保留本轮已经核实的条目排列。
        projected = self.model_copy(update={
            "sections": [section.model_copy(update={"cache_prefix_source_ids": None}) if key == "SET" else section],
            # CSET沿旧投影完成排序、覆盖范围分离和审计省略；避免在新版本递归拆分。
            "cache_layout_version": 11 if key == "CSET" else self.cache_layout_version,
        }).model_sections()
        original = next((item for item in projected if item.key == key), None)
        if original is None:
            return None
        try:
            normalized = _expand_context_tables(json.loads(original.content)) if key == "CSET" else json.loads(original.content)
        except (ValueError, TypeError):
            return None
        catalog = {key: item for key, item in normalized.items() if key != "collections"}
        catalog["collections"] = [{key: item for key, item in collection.items() if key != "records"}
                                  for collection in normalized["collections"]]
        if self.cache_policy == "deepseek" and self.cache_layout_version >= 13:
            # 定义的顺序不跟随本轮选中记录；记录仍按完整身份关联，不增删任何集合。
            catalog["collections"].sort(key=lambda item: item["collection_id"])
        # collection_id把两处一一关联；版本、权限、性质、来源及未知扩展字段均原样留在其所属层。
        selected = {"collections": [{"collection_id": collection["collection_id"], "records": collection["records"]}
                                    for collection in normalized["collections"]]}
        common = {"cache_prefix_source_ids": None}
        rules = []
        if key == "CSET":
            # 用户硬约定不随相关性记录的增减换位置；只搬已有完整记录，不推断新规则。
            constraints = {"collections": [{"collection_id": collection["collection_id"],
                "records": [record for record in collection["records"] if record.get("epistemic_status") == "user_constraint"]}
                for collection in selected["collections"]]}
            rule_ids = [record["record_id"] for collection in constraints["collections"] for record in collection["records"]]
            if rule_ids:
                if self.cache_policy == "deepseek" and self.cache_layout_version >= 13:
                    constraints["collections"].sort(key=lambda item: item["collection_id"])
                    for collection in constraints["collections"]:
                        collection["records"].sort(key=lambda item: item["record_id"])
                rules.append(original.model_copy(update={**common, "key": f"{key}-rules", "title": "本书作者硬约定（非已发生事实）",
                    "content": json.dumps(constraints, ensure_ascii=False, separators=(",", ":")),
                    "source_ids": rule_ids, "cache_scope": "book"}))
                for collection in selected["collections"]:
                    collection["records"] = [record for record in collection["records"] if record.get("epistemic_status") != "user_constraint"]
        selected_ids = [record["record_id"] for collection in selected["collections"] for record in collection["records"]]
        return [
            original.model_copy(update={**common, "key": f"{key}-definitions", "title": "设定合集定义与使用边界（非正史）",
                "content": json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), "source_ids": [], "cache_scope": "book"}),
            *rules,
            original.model_copy(update={**common, "key": f"{key}-records", "title": "本次设定记录与来源（非正史）",
                "content": json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
                "source_ids": selected_ids if key == "CSET" else original.source_ids, "cache_scope": "canon"}),
            *[item for item in projected if item.key != key],
        ]

    def model_sections(self) -> list[ContextSection]:
        """按供应商和冻结布局投影发送顺序，保持原始来源与门禁材料不变。"""
        # 新DeepSeek包沿用已核实的来源排列；旧包缺省版本1，保持原输入。
        openai_layout = self.cache_policy == "openai" and self.cache_layout_version >= 28
        stable_layout = openai_layout or self.cache_policy == "mimo" or self.cache_policy == "deepseek" and self.cache_layout_version >= 9
        sections = []
        for section in self.sections:
            if (self.cache_policy == "deepseek" and self.cache_layout_version >= 13
                    and section.key == "C" and section.title == "当前项目状态"):
                try:
                    state = json.loads(section.content)
                except (ValueError, TypeError):
                    state = None
                if (isinstance(state, dict) and isinstance(state.get("pending_work"), list)
                        and not any(item.key == "C-work" for item in self.sections)):
                    # 变化频繁的当前状态不能排在整份待办之前；原包仍供恢复、权限和来源核验。
                    sections.append(section.model_copy(update={"key": "C-work",
                        "title": "本会话待办（按编号引用，不是新授权）",
                        "content": json.dumps({"pending_work": state.pop("pending_work")}, ensure_ascii=False)}))
                    section = section.model_copy(update={"content": json.dumps(state, ensure_ascii=False)})
            if (stable_layout and section.key == "SET" and section.cache_prefix_source_ids is not None
                    or (openai_layout or self.cache_policy == "deepseek" and self.cache_layout_version >= 12) and section.key == "CSET"):
                # 旧SET仍需Coordinator的完整绑定提示；CSET仅新DeepSeek布局启用，原packet不改。
                split = self._settings_catalog_sections(section)
                if split is not None and (openai_layout or self.cache_policy == "deepseek" and self.cache_layout_version >= 16):
                    # 普通记录随选材与人物状态变化；放在稳定正史之后，硬约定及定义仍在固定前段。
                    split = [item.model_copy(update={"cache_scope": "chapter"})
                             if item.key == "CSET-records" else item for item in split]
                sections.extend(split if split is not None else [section])
                continue
            if openai_layout and section.key in {"O2", "C0"} and section.cache_scope == "book":
                # 滚动章纲与组合卡经常换版，放到大纲、细纲和固定设定之后；正文和原编号照常发送。
                section = section.model_copy(update={"cache_scope": "chapter"})
            if stable_layout and section.key == "B" and section.cache_prefix_source_ids is not None:
                try:
                    facts = json.loads(section.content)
                except (ValueError, TypeError):
                    facts = None
                if (isinstance(facts, list) and all(isinstance(item, dict) and isinstance(item.get("事实编号"), str) for item in facts)
                        and len({item["事实编号"] for item in facts}) == len(facts)
                        and {item["事实编号"] for item in facts} == set(section.source_ids)):
                    stable_ids = set(section.cache_prefix_source_ids)
                    for stable in (True, False):
                        selected = [item for item in facts if (item["事实编号"] in stable_ids) == stable]
                        if stable:
                            # These facts form an identity-bound set; relevance rank must not churn its prefix.
                            selected.sort(key=lambda item: item["事实编号"])
                        if selected:
                            sections.append(section.model_copy(update={"key": "B" if stable else "B-current",
                                "content": json.dumps(selected, ensure_ascii=False),
                                "source_ids": [item["事实编号"] for item in selected],
                                # 相关性选入的补充事实也已属正史；新DeepSeek只把它们排到旧稳定层之后，
                                # 不新增事实、不改变本轮选择，内容或来源变化仍自然使该处缓存失效。
                                "cache_scope": "canon" if stable or self.cache_policy == "deepseek"
                                    and self.cache_layout_version >= 10 else "chapter"}))
                    continue
            if stable_layout and section.key == "J":
                try:
                    direction = json.loads(section.content)
                except (ValueError, TypeError):
                    direction = None
                if isinstance(direction, dict) and isinstance(direction.get("强制记忆"), list):
                    forced = direction.pop("强制记忆")
                    sections.append(ContextSection(key="J-forced", title="本章适用的强制记忆",
                        content=json.dumps({"强制记忆": forced}, ensure_ascii=False), hard=section.hard,
                        source_ids=[source_id for source_id in section.source_ids if source_id != "book:requirements"],
                        cache_scope="chapter"))
                    section = section.model_copy(update={"content": json.dumps(direction, ensure_ascii=False),
                        "source_ids": [source_id for source_id in section.source_ids if source_id == "book:requirements"]})
            if stable_layout and section.key == "F" and section.cache_prefix_source_ids is not None:
                try:
                    value = json.loads(section.content)
                except (ValueError, TypeError):
                    value = None
                hits = value.get("自适应结果") if isinstance(value, dict) else None
                order = section.cache_prefix_source_ids
                if (isinstance(hits, list) and all(isinstance(item, dict) and isinstance(item.get("source_id"), str) for item in hits)
                        and len({item["source_id"] for item in hits}) == len(hits)
                        and len(set(order)) == len(order) and set(order) == {item["source_id"] for item in hits}
                        and set(order) == set(section.source_ids)):
                    # 提示只改变同一集合的排列；有缺失、重复或外部来源时完全沿用原顺序。
                    rank = {identity: index for index, identity in enumerate(order)}
                    value["自适应结果"] = sorted(hits, key=lambda item: rank[item["source_id"]])
                    section = section.model_copy(update={"content": json.dumps(value, ensure_ascii=False)})
            if self.cache_policy in {"deepseek", "mimo"} and section.key in {"CSET", "SET"}:
                try:
                    value = json.loads(section.content)
                except (ValueError, TypeError):
                    value = None
                if (isinstance(value, dict) and isinstance(value.get("collections"), list)
                        and all(isinstance(item, dict) and isinstance(item.get("records", []), list)
                                and all(isinstance(record, dict) for record in item.get("records", []))
                                for item in value["collections"])):
                    coverage = value.pop("coverage", None)
                    if stable_layout:
                        record_ids = [str(record.get("record_id", "")) for collection in value["collections"] for record in collection["records"]]
                        hint = section.cache_prefix_source_ids
                        stable_rank = ({identity: index for index, identity in enumerate(hint)}
                            if hint is not None and len(hint) == len(set(hint)) == len(record_ids)
                            and set(hint) == set(record_ids) and len(record_ids) == len(set(record_ids)) else {})
                        # 新布局可复用上一份同版条目顺序；旧快照/无合法hint继续原record_id排序。
                        value["collections"].sort(key=lambda item: (
                            min((stable_rank.get(str(record.get("record_id", "")), len(stable_rank)) for record in item["records"]), default=len(stable_rank)),
                            str(item.get("collection_id", ""))))
                        for collection in value["collections"]:
                            if isinstance(collection.get("records"), list):
                                collection["records"].sort(key=lambda item: (
                                    item.get("epistemic_status") != "user_constraint",
                                    stable_rank.get(str(item.get("record_id", "")), len(stable_rank)), str(item.get("record_id", ""))))
                    section = section.model_copy(update={"content": json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                        "cache_scope": "canon" if stable_layout else section.cache_scope})
                    if coverage is not None:
                        sections.append(ContextSection(key=f"{section.key}-coverage", title="设定选择与未装入范围",
                            content=json.dumps({"coverage": coverage}, ensure_ascii=False, separators=(",", ":")),
                            hard=section.hard, cache_scope="request"))
            if stable_layout and section.key in {"D", "G"} and section.cache_scope == "canon":
                section = section.model_copy(update={"cache_scope": "chapter"})
            sections.append(section)
        if stable_layout:
            # Object insertion order is not source meaning; lists and source strings stay exact.
            sections = [section.model_copy(update={"content": section.model_content(canonical=True)})
                        for section in sections]
        if self.cache_policy == "deepseek" and self.cache_layout_version >= 11:
            # 原packet、审计及门禁仍用未改内容；发送投影只省重复JSON字段，硬规则正文不改。
            tabular = []
            for section in sections:
                if section.key in {"B", "B-current", "CSET", "CSET-rules", "CSET-records", "C0", "C", "C-work", "O0", "O1", "O2", "P0", "F", "F0"}:
                    try:
                        value = json.loads(section.content)
                        value = _compact_context_tables(value)
                        if self.cache_layout_version >= 13 and section.key in {"C0", "C", "C-work", "P0"}:
                            # 全书/卷/篇反复携带的同一合同可引用；原packet与接受门禁仍读取完整原值。
                            value = _compact_context_repetitions(value)
                        section = section.model_copy(update={"content": json.dumps(value,
                            ensure_ascii=False, separators=(",", ":"), sort_keys=True)})
                    except (ValueError, TypeError):
                        pass  # 未知结构保留原文，不能把解析失败当作无资料。
                tabular.append(section)
            sections = tabular
        scopes = {"global": 0, "book": 1, "canon": 2, "chapter": 3, "request": 4}

        def priority(indexed: tuple[int, ContextSection]) -> tuple[int, int, int]:
            index, section = indexed
            book_order = {"J": 0, "B": 1, "O0": 2, "O1": 3, "O2": 4, "C0": 5}
            if self.cache_policy == "deepseek" and self.cache_layout_version >= 12:
                book_order.update({"CSET-definitions": 6, "CSET-rules": 7})
            if openai_layout:
                book_order.update({"CSET-definitions": 5, "CSET-rules": 6})
            canon_order = ({"B": 0, "F": 1, "CSET": 2, "SET": 2} if stable_layout else
                           {"B": 0, "CSET": 1, "D": 2, "G": 3} if self.cache_policy == "deepseek" else
                           {"B": 0, "D": 1, "CSET": 2, "G": 3})
            if self.cache_policy == "deepseek" and self.cache_layout_version >= 10:
                # 放在原B/F/CSET之后，避免相关事实集合改变时破坏更长、已稳定的前段。
                canon_order["B-current"] = 3
            if self.cache_policy == "deepseek" and self.cache_layout_version >= 12:
                canon_order["CSET-records"] = -1
            # Append-only batch memory remains reusable ahead of the changing chapter card.
            order = (book_order.get(section.key, 10 + index) if section.cache_scope == "book"
                     else canon_order.get(section.key, 10 + index) if section.cache_scope == "canon"
                     else -1 if section.cache_scope == "chapter" and section.key == "E0" else index)
            if (self.cache_policy == "deepseek" and self.cache_layout_version >= 25
                    and section.cache_scope == "chapter" and section.key in {"F", "F1", "MEMORY", "WORK"}):
                # 同章补核时检索、诊断与待办容易变化，排到其余章节资料后；正文和来源逐字保留。
                # 已回源完整正史若属于canon层仍沿原顺序，不把稳定全文移到动态章节卡后。
                order = len(sections) + index
            return (scopes[section.cache_scope], order, index)

        return [section for _, section in sorted(enumerate(sections), key=priority)]

    def model_prompt_parts(self) -> tuple[str, str]:
        """Compile reusable source scopes before current chapter/request material."""

        output = ["# 墨流编译上下文", ""]
        prefix = ""
        for section in self.model_sections():
            if not prefix and section.cache_scope in {"chapter", "request"}:
                prefix = "\n".join(output) + "\n"
                output = []
            output.extend([f"## {section.key}. {section.title}", "", section.model_content(), ""])
            if section.source_ids:
                output.extend([f"来源：{', '.join(sorted(section.source_ids))}", ""])
        if not prefix:
            prefix = "\n".join(output) + "\n"
            output = []
        output.extend([
            "## 本次执行信息",
            "",
            f"章节：{self.chapter_no}",
            f"估算输入：{self.estimated_tokens} tokens",
        ])
        if self.warnings:
            output.extend(["", "警告：", *[f"- {item}" for item in self.warnings]])
        return prefix, "\n".join(output).rstrip() + "\n"

    def gate_material(self) -> str:
        """Return only acceptance-critical context for stale-review detection."""

        return "\n".join(
            [
                f"task={self.task}",
                *[
                    f"{section.key}\n{section.content}\n{','.join(sorted(section.source_ids))}"
                    for section in sorted(self.sections, key=lambda item: item.key)
                    if section.hard
                ],
            ]
        )


class PrefillSuggestion(StrictModel):
    insertion: str = Field(default="", max_length=4_000)
    confidence: Literal["high", "medium", "low"] = "medium"
    constraint_notes: list[str] = Field(default_factory=list, max_length=5)


class WriterDirection(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    scene_goal: str = Field(min_length=1, max_length=500)
    turning_point: str = Field(min_length=1, max_length=500)
    ending_hook: str = Field(min_length=1, max_length=500)
    risks: list[str] = Field(default_factory=list, max_length=6)


class WriterDirectionSet(StrictModel):
    directions: list[WriterDirection] = Field(min_length=2, max_length=5)


class CollaborationReply(StrictModel):
    answer: str = Field(min_length=1, max_length=2_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    resolved: bool = False
    remaining_question: str = Field(default="", max_length=500)


class RoleCapability(StrictModel):
    role: AgentRole
    formal_ai_agent: bool = True
    novel_production_agent: bool
    can: list[str]
    cannot: list[str]


class TaskTicket(UnifiedRoleModel):
    collaboration_mode: CollaborationMode = "everyday"
    task_snapshot_hash: str | None = None
    ticket_id: str
    objective: str
    user_message: str = ""
    task_revision: int = Field(default=1, ge=1)
    related_task_id: str | None = None
    pending_work_id: str | None = None
    pending_question_id: str | None = None
    response_kind: Literal["new_task", "task_revision", "question_answer"] = "new_task"
    narrative_scope: Literal["none", "scene", "chapter", "batch"] = "none"
    edit_scope: Literal["none", "selection", "chapter", "document"] = "none"
    preserve_constraints: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    target_excerpt: str = ""
    document_kind: Literal["none", "book", "outline", "story_detail"] = "none"
    setting_change: dict[str, Any] = Field(default_factory=dict)
    chapter_no: int | None = None
    end_chapter_no: int | None = None
    chapter_version: int | None = None
    hard_constraints: list[str] = Field(default_factory=list)
    authorized_operations: list[str] = Field(default_factory=list)
    input_sources: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    max_model_calls: int = Field(ge=0, le=100)
    max_tokens: int = Field(ge=0, le=1_000_000)
    max_discussion_rounds: int = Field(default=2, ge=0, le=4)
    authorization_source: Literal[
        "none", "current_request", "per_chapter_click", "batch_preapproval", "settings_auto_accept"
    ] = "none"
    acceptance_confirmation_mode: Literal[
        "per_chapter", "batch_once", "auto_after_review"
    ] = "per_chapter"


class DispatchStep(StrictModel):
    step_id: str
    role: Literal["writer", "editor", "reviewer", "memory_keeper", "engine"]
    operation: str
    depends_on: list[str] = Field(default_factory=list)
    required_output: str
    gate: str = ""
    chapter_no: int | None = Field(default=None, ge=1)
    instruction: str = Field(default="", max_length=1200)
    when: Literal["always", "draft_missing", "revision_needed", "review_needed", "ready_to_accept"] = "always"


class DispatchPlan(UnifiedRoleModel):
    collaboration_mode: CollaborationMode = "everyday"
    task_snapshot_hash: str | None = None
    required_checks: list[str] = Field(default_factory=list)
    check_owners: dict[str, Literal["editor", "reviewer", "memory_keeper"]] = Field(default_factory=dict)
    memory_owner: Literal["editor", "memory_keeper"] | None = None
    return_to_base: bool = False
    workflow: str
    selection_source: Literal["reference", "agent"] = "reference"
    steps: list[DispatchStep] = Field(default_factory=list, max_length=100)
    parallel: bool = False
    stop_conditions: list[str] = Field(default_factory=list)


class AgentToolPlan(StrictModel):
    """读取工具返回后的下一段选择；不能修改原目标、授权、角色配置或预算。"""
    summary: str = Field(min_length=1, max_length=500)
    steps: list[DispatchStep] = Field(default_factory=list, max_length=100)
    clarification_questions: list["ClarificationQuestion"] = Field(default_factory=list, max_length=3)


class ResultSources(StrictModel):
    """Engine-supplied identities; a model must not choose its own evidence version."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
    project_id: str = Field(min_length=1)
    chapter_no: int = Field(ge=1)
    chapter_version: int = Field(ge=1)
    source_hash: str = Field(min_length=1)
    context_fingerprint: str = Field(min_length=1)
    # Fingerprints bind settings, outline, story detail, plans and user rules.
    foundation_fingerprint: str = Field(min_length=1)
    canon_revision: str = Field(min_length=1)
    rules_version: str = Field(min_length=1)


class CheckCoverage(StrictModel):
    check_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    owner: Literal["editor", "reviewer", "memory_keeper", "engine"]
    status: Literal["passed", "needs_revision", "insufficient_context", "not_run", "stale"]
    evidence_refs: list[str] = Field(default_factory=list)


class ConflictClaim(StrictModel):
    role: Literal["coordinator", "writer", "editor", "reviewer", "memory_keeper", "engine", "user"]
    statement: str = Field(min_length=1, max_length=2_000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    source_excerpt: str = Field(default="", max_length=1_000)


class ConflictAttempt(StrictModel):
    strategy: str = Field(min_length=1, max_length=80)
    input_fingerprint: str = Field(min_length=1)
    outcome: str = Field(min_length=1, max_length=1_000)


class ConflictRecord(UnifiedRoleModel):
    """One focused, versioned disagreement; not permission to bypass a gate."""

    conflict_id: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    task_revision: int = Field(ge=1)
    category: Literal[
        "style", "compatible_creative", "fact_timeline", "character_perspective",
        "goal_mismatch", "review_disagreement", "memory_conflict", "service_interruption", "other",
    ]
    source: ResultSources | None = None
    first: ConflictClaim
    second: ConflictClaim | None = None
    user_request: str = Field(min_length=1, max_length=4_000)
    hard_constraints: list[str] = Field(default_factory=list)
    attempts: list[ConflictAttempt] = Field(default_factory=list)
    remaining_model_calls: int = Field(ge=0)
    remaining_raw_tokens: int = Field(ge=0)
    remaining_cost_yuan: float | None = Field(default=None, ge=0)
    next_action: Literal[
        "check_evidence", "fetch_context", "local_revision", "specialist_review",
        "replan", "offer_alternatives", "ask_user", "wait_for_condition", "continue_task", "other",
    ]
    status: Literal["recovering", "waiting_user", "waiting_condition", "resolved"]
    public_summary: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def action_matches_conflict(self) -> "ConflictRecord":
        if self.status == "waiting_user" and self.next_action != "ask_user":
            raise ValueError("等待用户决定的冲突必须标明提问动作。")
        if self.status == "waiting_condition" and self.next_action != "wait_for_condition":
            raise ValueError("等待外部条件的冲突必须标明等待动作。")
        return self


class ReviewResultBase(UnifiedRoleModel):
    """Validated envelope, not a direct model output or permission to accept prose."""

    role: Literal["editor", "reviewer"]
    sources: ResultSources
    verdict: Literal["pass", "revise", "insufficient_context"]
    summary: str = Field(min_length=1)
    coverage: list[CheckCoverage] = Field(min_length=1)
    findings: list[ReviewFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_coverage(self) -> "ReviewResultBase":
        identifiers = [check.check_id for check in self.coverage]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("同一结果不能重复记录检查项")
        if any(check.owner != self.role for check in self.coverage):
            raise ValueError("审查角色不能代填其他角色的检查覆盖")
        if self.verdict == "pass":
            if any(check.status != "passed" for check in self.coverage):
                raise ValueError("通过结论不能包含未完成、过期或需修订的检查")
            if any(finding.severity == "blocking" for finding in self.findings):
                raise ValueError("通过结论不能包含未解决的阻断问题")
        return self


class EditorResult(ReviewResultBase):
    role: Literal["editor"] = "editor"
    memory_owner: Literal["editor", "memory_keeper"]
    memory_patch: MemoryPatch | None = None

    @model_validator(mode="after")
    def memory_handoff_matches_owner(self) -> "EditorResult":
        if self.memory_owner != "editor" and self.memory_patch is not None:
            raise ValueError("记忆已分派给 Memory Keeper，Editor 不重复提交记忆候选")
        if self.verdict == "pass" and self.memory_owner == "editor" and self.memory_patch is None:
            raise ValueError("日常综合编辑通过时必须交付同版本记忆候选")
        if self.memory_patch and self.memory_patch.chapter_no != self.sources.chapter_no:
            raise ValueError("记忆候选与审查正文不属于同一章节")
        return self


class ReviewerResult(ReviewResultBase):
    role: Literal["reviewer"] = "reviewer"
    # No memory_patch: the specialist must not inherit the old Editor contract.


class MemoryResult(UnifiedRoleModel):
    """Validated envelope; actual role model calls use the common ModeCheckOutput."""
    role: Literal["memory_keeper"] = "memory_keeper"
    sources: ResultSources
    status: Literal["ready", "conflict", "insufficient_context"]
    summary: str = Field(min_length=1)
    memory_patch: MemoryPatch | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ready_has_evidence(self) -> "MemoryResult":
        if self.memory_patch and self.memory_patch.chapter_no != self.sources.chapter_no:
            raise ValueError("记忆候选与来源正文不属于同一章节")
        if self.status == "ready" and (
            self.memory_patch is None or not self.evidence_refs or self.memory_patch.unresolved_conflicts
        ):
            raise ValueError("可提交的记忆候选须有来源且没有未解决冲突")
        return self


TerminalAction = Literal[
    "pending_work",
    "chat",
    "ideate",
    "discuss",
    "knowledge_query",
    "status",
    "plan",
    "plan_preview",
    "planning_history_view",
    "planning_history_restore",
    "planning_publish_reviewed",
    "outline",
    "redesign_story",
    "plan_brief",
    "plan_next_arc",
    "arc_audit",
    "continue_run",
    "batch_draft",
    "batch_draft_accept",
    "batch_repair",
    "batch_accept",
    "checkpoint_list",
    "checkpoint_create",
    "rollback_preview",
    "rollback_restore",
    "write_draft",
    "scene_draft",
    "story_setting_edit",
    "story_settings_manage",
    "revise_selection",
    "write_review",
    "write_review_accept",
    "review",
    "revise_draft",
    "repair_accepted",
    "revise_review",
    "review_accept",
    "revise_review_accept",
    "accept",
    "help",
    "voice_clone_script",
    "settings_update",
    "exit",
]


class PreferenceObservation(StrictModel):
    text: str = Field(min_length=1, max_length=1000)
    source_quote: str = Field(min_length=1, max_length=2000)
    level: Literal["author", "project", "task"] = "task"
    scope: str = "project"
    topic: str = ""
    explicit: bool = False
    preference_id: str = Field(default="", max_length=180)
    expected_revision: int | None = Field(default=None, ge=0)
    feedback_kind: Literal["none", "liked", "corrective", "disliked"] = "none"


class CoordinatorRecoveryStep(StrictModel):
    """原任务的补救动作；需要作者决定时附上可直接回答的具体问题。"""
    action: Literal["diagnose_dependencies", "review", "arc_audit", "batch_review", "ask_user", "development_fix"]
    chapter_no: int | None = Field(default=None, ge=1)
    end_chapter_no: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=4, max_length=500)
    evidence: str = Field(min_length=4, max_length=1000)
    instruction: str = Field(default="", max_length=2000)
    question: ClarificationQuestion | None = Field(default=None,
        description="ask_user时提供具体争议、目的、答后安排和可行选项；不让作者承担程序诊断。")


class CoordinatorRecoveryPlan(StrictModel):
    summary: str = Field(min_length=4, max_length=500)
    steps: list[CoordinatorRecoveryStep] = Field(min_length=1, max_length=3)


class PendingWorkProposal(StrictModel):
    reason: str = Field(min_length=4, max_length=500)
    evidence: str = Field(min_length=4, max_length=1000)
    action: TerminalAction
    chapter_no: int | None = Field(default=None, ge=1)
    end_chapter_no: int | None = Field(default=None, ge=1)
    outline_level: Literal["story", "detail"] = "story"
    instruction: str = Field(min_length=4, max_length=4000)


class QualityStage(StrictModel):
    """One explicitly requested, continuous chapter window and its quality strategy."""

    chapter_no: int = Field(ge=1)
    end_chapter_no: int = Field(ge=1)
    quality_preset: Literal["economy", "balanced", "quality"]

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end_chapter_no < self.chapter_no:
            raise ValueError("阶段结束章不得早于起始章")
        return self


def setting_record_evidence_mode(change: dict[str, Any], *, user_message: str | None = None) -> str:
    """校验单条引用整理的显式交接；执行时再对照原请求，普通补充始终保留并集语义。"""
    mode = change.get("evidence_mode", "append")
    if type(mode) is not str or mode not in {"append", "replace"}:
        raise ValueError("引用处理方式须为append或replace，不能用布尔值或其他值代替。")
    if "evidence_mode" in change and change.get("operation") != "record_supplement":
        raise ValueError("整理现用引用仅适用于已指定的单条设定补证，不能扩展为整套设定操作。")
    if mode == "replace":
        if (not isinstance(change.get("record_id"), str) or not change["record_id"].strip()
                or type(change.get("expected_revision")) is not int or change["expected_revision"] < 1):
            raise ValueError("整理引用须指定同一条设定及当前修订号，不能替换未定位的记录。")
        if user_message is not None:
            import re
            # 此处只核对明确的引用整理授权，不由错误上限、补充或保存意图自动升级为替换。
            clauses = re.split(r"[，,。；;！？!?\n]", user_message)
            allowed = any(re.search(r"(?:整理|精简|替换|重选|重新筛选).{0,24}(?:引用|证据)|(?:引用|证据).{0,16}(?:整理|精简|替换|重选|重新筛选)", clause)
                and not re.search(r"不要|无需|暂不|先不|别|禁止|只(?:查看|讨论|解释)|仅(?:查看|讨论|解释)", clause)
                for clause in clauses)
            if not allowed:
                raise ValueError("本次未明确要求整理这条设定的现用引用；普通补证保留原引用，不自动替换。")
    return mode


def setting_audit_record_limit(*, populate_records: bool | None, new_record_limit: Any) -> int | None:
    """统一审计的新增范围：显式关闭时仅维护原条目，旧调用未声明时保持既有行为。"""
    if populate_records is not None and type(populate_records) is not bool:
        raise ValueError("是否自动新增记录须为明确的true或false布尔值，不能从字符串或数字推断")
    if populate_records is False:
        return 0
    if populate_records is True:
        if type(new_record_limit) is not int or not 1 <= new_record_limit <= 3:
            raise ValueError("仅在启用自动新增记录时，上限须为一至三条；仅核对或补充原条目无需开启新增。")
    elif new_record_limit is not None and (type(new_record_limit) is not int or not 0 <= new_record_limit <= 3):
        raise ValueError("新增记录上限须为0至3的整数；0表示只核对和补充原条目，不创建新记录。")
    return new_record_limit


class TerminalIntent(StrictModel):
    """Coordinator解释目标并选择工具；生产权限与完成条件由引擎核验。"""

    action: TerminalAction
    tool_steps: list[DispatchStep] = Field(default_factory=list, max_length=100,
        description="按当前产物自主组合章节工具，流程仅作参考；无需为了填满流程重复已完成工作。")
    specialist_role: Literal["reviewer", "memory_keeper"] | None = None
    specialist_reason: str = Field(default="", max_length=300)
    planning_discussion: str = Field(default="", max_length=400)
    # 独立保存规划前置；正文待答不应丢失它或触发重复发布。
    planning_instruction: str = Field(default="", max_length=4000)
    preference_conflicts: list[PreferenceConflict] = Field(default_factory=list, max_length=3)
    preference_observations: list[PreferenceObservation] = Field(default_factory=list, max_length=3)
    outline_level: Literal["story", "detail"] = "story"
    requested_outcome: str = Field(default="", max_length=1_000)
    user_message: str = Field(default="", max_length=4_000)
    task_revision: int = Field(default=1, ge=1)
    related_task_id: str | None = Field(default=None, max_length=180)
    pending_work_id: str | None = Field(default=None, max_length=180)
    pending_work_decision: Literal["inspect", "process", "process_all", "defer"] = "inspect"
    pending_work_proposals: list["PendingWorkProposal"] = Field(default_factory=list, max_length=3)
    pending_question_id: str | None = Field(default=None, max_length=180)
    response_kind: Literal["new_task", "task_revision", "question_answer"] = "new_task"
    narrative_scope: Literal["none", "scene", "chapter", "batch"] = "none"
    edit_scope: Literal["none", "selection", "chapter", "document"] = "none"
    target_excerpt: str = Field(default="", max_length=4_000)
    preserve_constraints: list[str] = Field(default_factory=list, max_length=16)
    forbidden_actions: list[str] = Field(default_factory=list, max_length=16)
    document_kind: Literal["none", "book", "outline", "story_detail"] = "none"
    setting_change: dict[str, Any] = Field(default_factory=dict, max_length=16)
    alternative_action: TerminalAction | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    authorization: Literal["none", "proposed", "approved"] = "none"
    authorization_source: Literal[
        "none", "current_request", "per_chapter_click", "batch_preapproval", "settings_auto_accept"
    ] = "none"
    acceptance_confirmation_mode: Literal[
        "per_chapter", "batch_once", "auto_after_review"
    ] = "per_chapter"
    missing_fields: list[str] = Field(default_factory=list, max_length=8)
    clarification_question: str = Field(default="", max_length=500)
    clarification_questions: list["ClarificationQuestion"] = Field(default_factory=list, max_length=3)
    question_strategy: Literal["clarify", "grill"] = "clarify"
    grill_round: int = Field(default=0, ge=0)
    grill_summary: str = Field(default="", max_length=2_000)
    grill_ready: bool = False
    chapter_no: int | None = Field(default=None, ge=1)
    end_chapter_no: int | None = Field(default=None, ge=1)
    checkpoint_id: str | None = Field(default=None, max_length=120)
    confirmation_token: str | None = Field(default=None, max_length=120)
    batch_id: str | None = Field(default=None, max_length=180)
    batch_review_only: bool = False
    review_before_revision: bool = False
    plan_change_confirmed: bool = False
    planning_revision_no: int | None = Field(default=None, ge=0)
    planning_part: Literal["none", "outline", "detail", "recent"] = "none"
    planning_total_chapters: int | None = Field(default=None, ge=1)
    planning_volume_count: int | None = Field(default=None, ge=1, le=20)
    planning_reference_chapter_no: int | None = Field(default=None, ge=1)
    planning_reference_volume_no: int | None = Field(default=None, ge=1)
    target_characters: int | None = Field(default=None, ge=1_000, le=5_000_000)
    max_revision_rounds: int = Field(default=10, ge=0, le=10,
        description="同一正文修复步骤累计上限，续接不清零；0表示只核对，作者指定更少时遵从")
    operation_instruction: str = Field(default="", max_length=4_000)
    settings_patch: dict[str, Any] = Field(default_factory=dict, max_length=16)
    settings_scope: Literal["task", "defaults"] = "task"
    quality_stages: list[QualityStage] = Field(default_factory=list, max_length=8)
    visible_reason: str = Field(min_length=1, max_length=240)
    conversation_reply: str = Field(default="", max_length=1_500)

    def checked_setting_change(self) -> dict[str, Any]:
        """核对设定操作的结构化交接；只校验显式参数，不从自由说明推断操作或写入授权。"""
        if self.action != "story_settings_manage":
            return self.setting_change
        operations = {"list", "audit", "collection_create", "collection_update", "collection_archive",
                      "collection_create_many", "record_generate", "record_supplement", "record_save",
                      "record_archive", "record_merge", "manual_decide"}
        operation = self.setting_change.get("operation")
        if not isinstance(operation, str) or operation not in operations:
            raise ValueError("story_settings_manage须在setting_change.operation填写实际操作；"
                "查看用list，跨合集核对用audit，不能缺省为list或只写在operation_instruction。"
                "保留原任务理解，仅补齐结构参数，不新增写入授权。")
        for key in ("apply_corrections", "apply_rule_corrections", "populate_records"):
            if key in self.setting_change and type(self.setting_change[key]) is not bool:
                raise ValueError(f"setting_change.{key}须为明确的true或false布尔值，不能用字符串或数字替代授权")
        setting_record_evidence_mode(self.setting_change)
        if operation == "audit":
            setting_audit_record_limit(populate_records=self.setting_change.get("populate_records"),
                                      new_record_limit=self.setting_change.get("new_record_limit"))
        return self.setting_change

    @model_validator(mode="after")
    def generated_settings_contract(self, info: ValidationInfo) -> "TerminalIntent":
        """新Coordinator结果进入既有格式补正；旧存档仍可读取，执行时另行核对交接。"""
        if info.context and info.context.get("model_generation"):
            self.checked_setting_change()
        return self


class ClarificationOption(StrictModel):
    """One user-visible answer choice; the host appends a free-text option."""

    label: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    recommended: bool = False


class ClarificationQuestion(StrictModel):
    """作者的待决事项，说明选择目的以及回答后原任务怎样继续。"""

    header: str = Field(default="需要确认", max_length=24)
    question: str = Field(min_length=1, max_length=500)
    why_it_matters: str = Field(default="", max_length=300)
    after_answer: str = Field(default="", max_length=500,
        description="回答后先做什么、交付什么以及如何接回原目标；只描述已有权限内的安排。")
    selection: Literal["single", "multiple"] = "single"
    options: list[ClarificationOption] = Field(default_factory=list, max_length=5)


ReviewReport.model_rebuild()
ModeCheckOutput.model_rebuild()
StorySettingsAudit.model_rebuild()
CoordinatorRecoveryPlan.model_rebuild()
AgentToolPlan.model_rebuild()
