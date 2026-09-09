from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class SelectionRevisionOutput(StrictModel):
    """Writer 对一个已锁定正文选区给出的最小替换结果。"""

    replacement: str = Field(min_length=1, max_length=20_000)
    decision_summary: list[str] = Field(min_length=1, max_length=6)


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
    semantic_status: Literal["unchecked", "supported", "contradicted", "uncertain"] = "unchecked"
    verification_confidence: float = Field(default=0.0, ge=0, le=1)
    proposed_severity: Literal["info", "minor", "major", "blocking"] | None = None


ReviewScoreDimensionName = Literal[
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
    score: int = Field(ge=0, le=100)
    deductions: list[ReviewFinding] = Field(default_factory=list)


class ReviewReport(StrictModel):
    verdict: Literal["pass", "patch", "replan", "unknown"]
    confidence: float = Field(ge=0, le=1)
    summary: str
    strengths: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    scorecard: list[ReviewScoreDimension] = Field(default_factory=list)
    source_hash: str = ""


class ReviewFindingBatch(StrictModel):
    findings: list[ReviewFinding] = Field(default_factory=list)


class ReviewClaimDecision(StrictModel):
    finding_index: int = Field(ge=0)
    verdict: Literal["supported", "contradicted", "uncertain"]
    confidence: float = Field(ge=0, le=1)
    reason: str


class ReviewClaimDecisionBatch(StrictModel):
    decisions: list[ReviewClaimDecision] = Field(default_factory=list)


class ArcAuditReport(StrictModel):
    verdict: Literal["aligned", "needs_replan", "blocked", "unknown"]
    confidence: float = Field(ge=0, le=1)
    summary: str
    fulfilled_commitments: list[str] = Field(default_factory=list)
    deviations: list[ReviewFinding] = Field(default_factory=list)
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


class RoleCapability(StrictModel):
    role: Literal["coordinator", "writer", "reviewer", "memory_keeper"]
    formal_ai_agent: bool = True
    novel_production_agent: bool
    can: list[str]
    cannot: list[str]


class TaskTicket(StrictModel):
    ticket_id: str
    objective: str
    chapter_no: int | None = None
    end_chapter_no: int | None = None
    chapter_version: int | None = None
    hard_constraints: list[str] = Field(default_factory=list)
    input_sources: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    max_model_calls: int = Field(ge=0, le=100)
    max_tokens: int = Field(ge=0, le=1_000_000)
    max_discussion_rounds: int = Field(default=2, ge=0, le=4)


class DispatchStep(StrictModel):
    step_id: str
    role: Literal["writer", "reviewer", "memory_keeper", "engine"]
    operation: str
    depends_on: list[str] = Field(default_factory=list)
    required_output: str
    gate: str = ""


class DispatchPlan(StrictModel):
    workflow: str
    steps: list[DispatchStep] = Field(default_factory=list, max_length=100)
    parallel: bool = False
    stop_conditions: list[str] = Field(default_factory=list)


TerminalAction = Literal[
    "discuss",
    "status",
    "plan",
    "plan_preview",
    "plan_brief",
    "plan_next_arc",
    "arc_audit",
    "continue_run",
    "batch_draft",
    "batch_repair",
    "batch_accept",
    "checkpoint_list",
    "checkpoint_create",
    "rollback_preview",
    "rollback_restore",
    "write_draft",
    "write_review",
    "review",
    "revise_draft",
    "revise_review",
    "review_accept",
    "revise_review_accept",
    "accept",
    "help",
    "exit",
]


class TerminalIntent(StrictModel):
    """Coordinator 的受限路由输出；不能发明工作流、写正文或强制验收。"""

    action: TerminalAction
    requested_outcome: str = Field(default="", max_length=1_000)
    alternative_action: TerminalAction | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    authorization: Literal["none", "proposed", "approved"] = "none"
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
    max_revision_rounds: int = Field(default=1, ge=0, le=6)
    operation_instruction: str = Field(default="", max_length=4_000)
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
