from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .role_protocol import AgentRole, CollaborationMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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


class SuggestedPrompt(StrictModel):
    """一条按当前对话与项目状态预测的用户下一步提示词。"""

    label: str = Field(min_length=1, max_length=24)
    prompt: str = Field(min_length=1, max_length=200)


class SuggestedPrompts(StrictModel):
    """对话输入区的动态提示词预测结果；失败时由本地规则兜底。"""

    suggestions: list[SuggestedPrompt] = Field(min_length=1, max_length=5)


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


class BookPlan(StrictModel):
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


class VolumePlan(StrictModel):
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


class ChapterCard(StrictModel):
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


class ArcPlan(StrictModel):
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


class ArcPlanningBrief(StrictModel):
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


class OutlineChapter(StrictModel):
    """一章大纲的可执行摘要；不等同于章节卡，也不写入正史。"""

    chapter_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    purpose: str = Field(min_length=4, max_length=500)
    conflict: str = Field(min_length=4, max_length=500)
    turn: str = Field(min_length=4, max_length=500)
    hook: str = Field(min_length=4, max_length=500)
    scenes: list[str] = Field(default_factory=list, max_length=12)
    consequence: str = Field(default="", max_length=800)


class OutlineOutput(StrictModel):
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


class StoryDetailOutput(StrictModel):
    """剧情细纲：按故事阶段展开，不按章节分配内容。"""
    title: str = Field(min_length=1, max_length=160)
    scope: str = Field(min_length=4, max_length=600)
    segments: list[StoryDetailSegment] = Field(min_length=1, max_length=60)
    ending: str = Field(min_length=4, max_length=2000)


class PlanningVolumeDirection(StrictModel):
    volume_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    central_conflict: str = Field(min_length=20, max_length=1500)
    outcome: str = Field(min_length=20, max_length=1500)


class BookOutlineV2(StrictModel):
    """Whole-book narrative contract; chapter details belong downstream."""

    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=5000, max_length=25000)
    volumes: list[PlanningVolumeDirection] = Field(min_length=1, max_length=20)

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


class VolumeDetailV2(StrictModel):
    volume_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    chapter_start: int = Field(ge=1)
    chapter_end: int = Field(ge=1)
    body: str = Field(min_length=5000, max_length=25000)
    rough_chapter_beats: list[str] = Field(min_length=5, max_length=80)


class RollingChapterV2(StrictModel):
    chapter_no: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=200, max_length=700)


class RollingPlanV2(StrictModel):
    anchor_chapter: int = Field(ge=1)
    anchor_summary: str = Field(min_length=100, max_length=700)
    chapters: list[RollingChapterV2] = Field(min_length=1, max_length=50)


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


class HookNote(StrictModel):
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


class DraftOutput(StrictModel):
    title: str
    content: str = Field(min_length=100)
    decision_summary: list[str] = Field(
        default_factory=lambda: ["模型未提供结构化摘要；正文将由后续审查确认。"],
        min_length=1,
        max_length=12,
    )
    new_fact_candidates: list[str] = Field(default_factory=list)
    thread_changes: list[str] = Field(default_factory=list)
    hook_note: HookNote | None = None
    scene_blueprint: list[SceneBlueprintItem] = Field(default_factory=list, max_length=8)

    @field_validator("content")
    @classmethod
    def validate_novel_punctuation(cls, value: str) -> str:
        count = value.count("、")
        if count:
            raise ValueError(
                f"小说正文禁止使用中文顿号，当前仍有 {count} 个；请改写包含顿号的完整句子，"
                "用自然句法或‘和’‘跟’‘与’连接，不要机械替换成其他标点"
            )
        return value


class SceneDraftOutput(StrictModel):
    """Writer prose for an isolated scene draft; no review or canon authority."""

    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=20_000)
    decision_summary: list[str] = Field(min_length=1, max_length=8)

    @field_validator("content")
    @classmethod
    def validate_novel_punctuation(cls, value: str) -> str:
        return DraftOutput.validate_novel_punctuation(value)


class ParagraphInsertion(StrictModel):
    before_paragraph: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=12_000)


class ParagraphExpansionPlan(StrictModel):
    insertions: list[ParagraphInsertion] = Field(min_length=1, max_length=12)
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
    replacement: str = Field(min_length=1, max_length=800)


class AcceptedContinuityPatch(StrictModel):
    """Writer returns a handful of exact local edits, never a rewritten chapter."""

    edits: list[AcceptedContinuityLocalEdit] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=1_000)


class AcceptedContinuityVerification(StrictModel):
    verdict: Literal["resolved", "not_resolved", "needs_user"]
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1_000)
    evidence: str = Field(min_length=1, max_length=1_000)
    anchor_excerpt: str = Field(min_length=8, max_length=500)
    checked_state_ids: list[int] = Field(default_factory=list, max_length=80)


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


class PlanConflictAnchor(StrictModel):
    """Two exact quotes for a reported plan/canon conflict, not a new review."""

    plan_excerpt: str = Field(min_length=8, max_length=240)
    canon_ref: str = Field(min_length=1, max_length=100)
    canon_excerpt: str = Field(min_length=8, max_length=240)
    reason: str = Field(min_length=1, max_length=500)


ReviewScoreDimensionName = Literal[
    "剧情因果",
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


class ContextUseAudit(StrictModel):
    """Reviewer 对 Writer 是否正确使用上下文的公开核对结果。"""

    used_source_ids: list[str] = Field(default_factory=list)
    missing_required_source_ids: list[str] = Field(default_factory=list)
    conflicting_source_ids: list[str] = Field(default_factory=list)
    summary: str = ""


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


class ReviewEvidenceAnchor(StrictModel):
    criterion: str
    chapter_span: str = Field(pattern=r"^(?:body\.\d+)?$")
    source_span: str = Field(default="", pattern=r"^(?:source\d+\.\d+)?$")


class ReviewComparisonAnchor(StrictModel):
    chapter_span: str = Field(pattern=r"^(?:body\.\d+)?$")
    source_span: str = Field(pattern=r"^(?:source\d+\.\d+)?$")
    relation: Literal["aligned", "adapted", "tension", "conflict"]
    reason: str = Field(min_length=1, max_length=500)


class ReviewEvidenceRepair(StrictModel):
    """Select immutable source spans instead of asking models to transcribe quotes."""

    goal_span: str = Field(pattern=r"^(?:body\.\d+)?$")
    change_span: str = Field(pattern=r"^(?:body\.\d+)?$")
    assessments: list[ReviewEvidenceAnchor] = Field(default_factory=list, max_length=6)
    comparisons: list[ReviewComparisonAnchor] = Field(default_factory=list, max_length=8)


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


class ReviewReport(StrictModel):
    verdict: Literal["pass", "patch", "replan", "unknown"]
    confidence: float = Field(ge=0, le=1)
    model_self_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_basis: list[str] = Field(default_factory=list)
    scoring_version: str = ""
    assessments: list[ReviewAssessment] = Field(default_factory=list)
    missing_source_ids: list[str] = Field(default_factory=list)
    summary: str
    strengths: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    scorecard: list[ReviewScoreDimension] = Field(default_factory=list)
    source_hash: str = ""
    context_fingerprint: str = ""
    instruction_hash: str = ""
    hook_assessment: HookAssessment | None = None
    context_use_audit: ContextUseAudit = Field(default_factory=ContextUseAudit)
    focus_observation: ReviewFocusObservation = Field(default_factory=ReviewFocusObservation)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list)
    memory_patch: MemoryPatch | None = None


class ReviewModelOutput(StrictModel):
    """Only fields the Reviewer must generate; provenance and scores are deterministic."""

    verdict: Literal["pass", "patch", "replan", "unknown"]
    confidence: float = Field(ge=0, le=1)
    assessments: list[ReviewAssessment] = Field(default_factory=list, max_length=6)
    missing_source_ids: list[str] = Field(default_factory=list, max_length=6)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list, max_length=8)
    summary: str = Field(min_length=1, max_length=2_000)
    strengths: list[str] = Field(default_factory=list, max_length=8)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=24)
    hook_assessment: HookAssessment | None = None
    context_use_audit: ContextUseAudit = Field(default_factory=ContextUseAudit)
    focus_observation: ReviewFocusObservation = Field(default_factory=ReviewFocusObservation)
    memory_patch: MemoryPatch | None = None

    @model_validator(mode="after")
    def passed_review_must_handoff_memory(self) -> "ReviewModelOutput":
        """Keep a successful review and its memory handoff in one cacheable call."""

        if self.verdict == "pass" and self.memory_patch is None:
            raise ValueError("Reviewer 通过章节时必须同时填写 memory_patch，不能另起低命中率的补提取调用")
        return self


class ModeCheckOutput(StrictModel):
    """V2 role-scoped model output; the engine owns role and source identity."""

    verdict: Literal["pass", "patch", "unknown"]
    confidence: float = Field(ge=0, le=1)
    assessments: list[ReviewAssessment] = Field(default_factory=list, max_length=6)
    missing_source_ids: list[str] = Field(default_factory=list, max_length=6)
    summary: str = Field(min_length=1, max_length=2_000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=24)
    focus_observation: ReviewFocusObservation = Field(default_factory=ReviewFocusObservation)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list, max_length=8)
    memory_patch: MemoryPatch | None = None


class ReviewFindingBatch(StrictModel):
    findings: list[ReviewFinding] = Field(default_factory=list)


class ReviewClaimDecision(StrictModel):
    finding_index: int = Field(ge=0)
    verdict: Literal["supported", "contradicted", "not_blocking", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    resolution_evidence: str = ""


class ReviewClaimDecisionBatch(StrictModel):
    decisions: list[ReviewClaimDecision] = Field(default_factory=list)


class ArcAuditReport(StrictModel):
    verdict: Literal["aligned", "needs_replan", "blocked", "unknown"]
    confidence: float = Field(ge=0, le=1)
    model_self_confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_basis: list[str] = Field(default_factory=list)
    scoring_version: str = ""
    assessments: list[ReviewAssessment] = Field(default_factory=list, max_length=6)
    missing_source_ids: list[str] = Field(default_factory=list, max_length=6)
    summary: str
    fulfilled_commitments: list[str] = Field(default_factory=list)
    deviations: list[ReviewFinding] = Field(default_factory=list)
    source_comparisons: list[ReviewSourceComparison] = Field(default_factory=list, max_length=8)
    future_impact: list[str] = Field(default_factory=list)
    body_repair_recommended: bool = False
    body_repair_scope: list[int] = Field(default_factory=list)
    replan_recommended: bool = False
    proposed_future_changes: list[str] = Field(default_factory=list)
    source_hash: str = ""


class FactMutation(StrictModel):
    fact_id: str
    subject: str
    predicate: str
    value: Any
    valid_from_chapter: int = Field(ge=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    evidence: str = Field(min_length=2)
    epistemic_kind: Literal["objective", "belief", "rumor"] = "objective"
    event_time: str | None = None
    narrative_time: str | None = None


class ThreadMutation(StrictModel):
    thread_id: str
    kind: Literal["plot", "promise", "foreshadow", "relationship", "mystery"]
    title: str
    status: Literal["open", "advanced", "paid", "delayed", "abandoned"]
    description: str
    planted_chapter: int | None = Field(default=None, ge=1)
    due_chapter: int | None = Field(default=None, ge=1)


class MemoryPatch(StrictModel):
    chapter_no: int = Field(ge=1)
    chapter_summary: str
    scene_summaries: list[str] = Field(default_factory=list)
    facts: list[FactMutation] = Field(default_factory=list)
    threads: list[ThreadMutation] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)

    @field_validator("facts")
    @classmethod
    def unique_fact_ids(cls, value: list[FactMutation]) -> list[FactMutation]:
        ids = [item.fact_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("memory patch 中 fact_id 重复")
        return value


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


class ContextPacket(StrictModel):
    project_id: str
    chapter_no: int
    task: str
    sections: list[ContextSection]
    estimated_tokens: int
    warnings: list[str] = Field(default_factory=list)

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
        """Compile reusable source scopes before current chapter/request material."""

        scopes = {"global": 0, "book": 1, "canon": 2, "chapter": 3, "request": 4}

        def priority(indexed: tuple[int, ContextSection]) -> tuple[int, int, int]:
            index, section = indexed
            # Book sections have a fixed order. Sorting by their changing length
            # invalidated DeepSeek's exact-prefix cache on unrelated edits.
            # The book contract is a longer-lived prefix than a revisable
            # outline boundary. Keep it ahead of O0 so a new outline draft
            # does not invalidate the book-level DeepSeek cache.
            book_order = {"J": 0, "B": 1, "O0": 2, "O1": 3}
            order = book_order.get(section.key, 10 + index) if section.cache_scope == "book" else index
            return (scopes[section.cache_scope], order, index)

        output = ["# 墨流编译上下文", ""]
        for _, section in sorted(enumerate(self.sections), key=priority):
            output.extend([f"## {section.key}. {section.title}", "", section.content or "（无）", ""])
            if section.source_ids:
                output.extend([f"来源：{', '.join(sorted(section.source_ids))}", ""])
        output.extend([
            "## 本次执行信息",
            "",
            f"章节：{self.chapter_no}",
            f"估算输入：{self.estimated_tokens} tokens",
        ])
        if self.warnings:
            output.extend(["", "警告：", *[f"- {item}" for item in self.warnings]])
        return "\n".join(output).rstrip() + "\n"

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


class TaskTicket(StrictModel):
    # Missing version means the historical combined reviewer, not a specialist.
    role_protocol_version: Literal[1, 2] = 1
    collaboration_mode: CollaborationMode = "everyday"
    task_snapshot_hash: str | None = None
    ticket_id: str
    objective: str
    user_message: str = ""
    task_revision: int = Field(default=1, ge=1)
    related_task_id: str | None = None
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


class DispatchPlan(StrictModel):
    role_protocol_version: Literal[1, 2] = 1
    collaboration_mode: CollaborationMode = "everyday"
    task_snapshot_hash: str | None = None
    required_checks: list[str] = Field(default_factory=list)
    check_owners: dict[str, Literal["editor", "reviewer", "memory_keeper"]] = Field(default_factory=dict)
    memory_owner: Literal["editor", "memory_keeper"] | None = None
    return_to_base: bool = False
    workflow: str
    steps: list[DispatchStep] = Field(default_factory=list, max_length=100)
    parallel: bool = False
    stop_conditions: list[str] = Field(default_factory=list)


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


class ConflictRecord(StrictModel):
    """One focused, versioned disagreement; not permission to bypass a gate."""

    role_protocol_version: Literal[1, 2]
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


class ReviewResultBase(StrictModel):
    """Validated envelope, not a direct model output or permission to accept prose."""

    role_protocol_version: Literal[2] = 2
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


class MemoryResult(StrictModel):
    role_protocol_version: Literal[2] = 2
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
    "chat",
    "ideate",
    "discuss",
    "status",
    "plan",
    "plan_preview",
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


class TerminalIntent(StrictModel):
    """Coordinator 的受限路由输出；不能发明工作流、写正文或强制验收。"""

    action: TerminalAction
    outline_level: Literal["story", "detail"] = "story"
    requested_outcome: str = Field(default="", max_length=1_000)
    user_message: str = Field(default="", max_length=4_000)
    task_revision: int = Field(default=1, ge=1)
    related_task_id: str | None = Field(default=None, max_length=180)
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
    chapter_no: int | None = Field(default=None, ge=1)
    end_chapter_no: int | None = Field(default=None, ge=1)
    checkpoint_id: str | None = Field(default=None, max_length=120)
    confirmation_token: str | None = Field(default=None, max_length=120)
    batch_id: str | None = Field(default=None, max_length=180)
    plan_change_confirmed: bool = False
    target_characters: int | None = Field(default=None, ge=1_000, le=5_000_000)
    max_revision_rounds: int = Field(default=2, ge=0, le=6)
    operation_instruction: str = Field(default="", max_length=4_000)
    settings_patch: dict[str, Any] = Field(default_factory=dict, max_length=16)
    visible_reason: str = Field(min_length=1, max_length=240)
    conversation_reply: str = Field(default="", max_length=1_500)


class ClarificationOption(StrictModel):
    """One user-visible answer choice; the host appends a free-text option."""

    label: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    recommended: bool = False


class ClarificationQuestion(StrictModel):
    """A compact, inspectable human-in-the-loop question."""

    header: str = Field(default="需要确认", max_length=24)
    question: str = Field(min_length=1, max_length=500)
    why_it_matters: str = Field(default="", max_length=300)
    selection: Literal["single", "multiple"] = "single"
    options: list[ClarificationOption] = Field(default_factory=list, max_length=5)


ReviewReport.model_rebuild()
ModeCheckOutput.model_rebuild()
