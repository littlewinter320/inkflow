from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .checkpoints import CheckpointService
from .config import Settings
from .context import ContextBuilder
from .errors import ProjectError, ProviderError, ValidationGateError
from .project import InkFlowProject
from .project_lock import project_mutation_locked, project_mutation_locked_sync
from .prompts import (
    ARC_AUDIT_SYSTEM,
    MEMORY_EVIDENCE_SYSTEM,
    MEMORY_SYSTEM,
    PLANNER_SYSTEM,
    REVIEW_CLAIM_CHECK_SYSTEM,
    REVIEW_CORRECTION_SYSTEM,
    REVIEW_DISPUTE_SYSTEM,
    REVIEWER_SYSTEM,
    REVISER_SYSTEM,
    SELECTION_REVISER_SYSTEM,
    WRITER_IDEATE_SYSTEM,
    WRITER_SYSTEM,
)
from .provider import JsonModelProvider
from .render import render_memory_conflict, render_plan, render_review, render_state
from .review_verifier import (
    apply_dispute_decisions,
    apply_semantic_decisions,
    findings_for_semantic_check,
    local_nli_decisions,
    verify_review,
)
from .schemas import (
    ArcAuditReport,
    ArcPlan,
    ArcPlanningBrief,
    ArcSummary,
    BookBrief,
    ContextPacket,
    ContextSection,
    CreativeBrainstorm,
    DraftOutput,
    EvidenceRepairBatch,
    EvidenceSelectionBatch,
    FactMutation,
    MemoryPatch,
    PlanBundle,
    ReviewFinding,
    ReviewFindingBatch,
    ReviewClaimDecision,
    ReviewClaimDecisionBatch,
    ReviewReport,
    ReviewScoreDimension,
    SelectionRevisionOutput,
    VolumeArcPlan,
    VolumeCompass,
    VolumePlan,
)
from .studio import StudioService
from .trace import TraceRecorder
from .utils import atomic_write_text, content_hash, estimate_tokens, json_dumps, utc_now


class InkFlowEngine:
    def __init__(self, provider: JsonModelProvider, settings: Settings | None = None):
        self.provider = provider
        self.settings = settings or Settings.from_env()

    def _context_builder(self, project: InkFlowProject) -> ContextBuilder:
        return ContextBuilder(
            project,
            self.settings.context_soft_tokens,
            hard_token_limit=self.settings.context_hard_tokens,
            embedding_model=self.settings.retrieval_embedding_model,
            reranker_model=self.settings.retrieval_reranker_model,
        )

    async def brainstorm(self, root: str | Path, prompt: str, packet: ContextPacket) -> dict[str, Any]:
        """Writer 灵感分身：零依据构思只出创意提案，不做证据核验、不写正文、不入正史。"""

        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, "writer-brainstorm", self.settings.trace_level)
        try:
            # 灵感分身只把最近对话当作背景参考，不做证据核验，也不引用正史结论。
            context = packet.to_markdown()
            if len(context) > 6_000:
                context = context[-6_000:]
            result = await self.provider.generate_json(
                system_prompt=WRITER_IDEATE_SYSTEM,
                user_prompt=(
                    f"用户当前的想法：\n{prompt}\n\n"
                    f"最近对话与项目状态（背景参考，不需要核验）：\n{context}"
                ),
                output_model=CreativeBrainstorm,
                effort="low",
                max_tokens=2_400,
                thinking=False,
                agent_role="writer",
                timeout_seconds=min(self.settings.request_timeout_seconds, 120.0),
            )
            trace.record_model(
                "writer.brainstorm",
                result,
                "Writer 灵感分身完成零依据创意提案",
            )
            trace.finish(summary="创意提案已生成，未写入任何正文或正史")
            return {
                "reply": result.data.reply,
                "model": result.model,
                "next_action": "选中哪个方向告诉我；确认后再进入正式规划流程。",
                "trace_id": trace.run_id,
            }
        except Exception as exc:
            trace.record("brainstorm", "failed", "创意提案生成失败", str(exc))
            trace.finish(status="failed", summary="灵感分身未产出提案")
            raise

    def create_project(self, root: str | Path, brief: BookBrief) -> dict[str, Any]:
        project = InkFlowProject.create(root, brief)
        checkpoint = CheckpointService(project).create(
            label="项目创建",
            reason="project_created",
        )
        return {
            "project_id": project.project_id,
            "root": str(project.root),
            "files": ["BOOK.md", "PLAN.md", "STATE.md"],
            "checkpoint": checkpoint,
            "next_action": "生成四级规划",
        }

    @project_mutation_locked
    async def generate_plan(self, root: str | Path) -> dict[str, Any]:
        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, "plan", self.settings.trace_level)
        try:
            brief = project.db.get_brief()
            trace.record("plan.prepare", "completed", "读取书籍契约并确定当前规划范围")
            task = (
                "生成初始四级规划：全书所有卷给卷级罗盘，完整规划第一卷的篇章摘要，"
                "只细化第一篇章的全部连续章节卡。"
            )
            sections = [
                ContextSection(
                    key="A",
                    title="当前规划任务",
                    content=task,
                    hard=True,
                ),
                ContextSection(
                    key="B",
                    title="书籍契约与用户硬规则",
                    content=json_dumps({"book_brief": brief.model_dump(mode="json")}),
                    hard=True,
                ),
                ContextSection(
                    key="J",
                    title="输出契约",
                    content=(
                        "只输出 PlanBundle JSON。每张章节卡目标字数接近 target_chapter_words；"
                        "第一卷篇章摘要应覆盖该卷，但只为第一篇章生成详细章节卡。"
                    ),
                    hard=True,
                ),
            ]
            packet = ContextPacket(
                project_id=project.project_id,
                chapter_no=1,
                task=task,
                sections=sections,
                estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
            )
            atomic_write_text(trace.run_dir / "context-packet.md", packet.to_markdown())
            trace.record(
                "context.build",
                "completed",
                f"构建唯一规划 Context Packet，估算 {packet.estimated_tokens} tokens",
            )
            result = await self.provider.generate_json(
                system_prompt=PLANNER_SYSTEM,
                user_prompt=packet.to_markdown(),
                output_model=PlanBundle,
                effort="max",
                max_tokens=16_000,
                agent_role="writer",
            )
            bundle = result.data
            trace.record_model(
                "plan.model",
                result,
                f"生成 {len(bundle.current_arc.chapter_cards)} 张连续章节卡",
            )
            project.db.save_plan_bundle(bundle)
            atomic_write_text(project.root / "PLAN.md", render_plan(bundle))
            trace.record("plan.commit", "completed", "规划通过 Schema 与层级门禁并写入 SQLite/PLAN.md")
            checkpoint = CheckpointService(project).create(
                label=f"四级规划 · {bundle.current_arc.title}",
                reason="plan_committed",
            )
            trace.record(
                "checkpoint.create",
                "completed",
                "规划提交后已创建恢复点",
                metadata={"checkpoint_id": checkpoint["checkpoint_id"]},
            )
            trace.finish(summary="四级规划完成")
            return {
                "project_id": project.project_id,
                "current_volume": bundle.current_volume.volume_no,
                "current_arc": bundle.current_arc.arc_id,
                "chapter_range": [bundle.current_arc.chapter_start, bundle.current_arc.chapter_end],
                "plan_path": str(project.root / "PLAN.md"),
                "checkpoint": checkpoint,
                "trace_id": trace.run_id,
            }
        except Exception as exc:
            trace.record("plan", "failed", "规划失败", str(exc))
            trace.finish(status="failed", summary="四级规划未提交")
            raise

    @project_mutation_locked
    async def preview_next_arc(self, root: str | Path, *, instruction: str = "") -> dict[str, Any]:
        """Create a small, user-visible rationale before committing a new arc plan."""

        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, "plan-brief", self.settings.trace_level)
        try:
            previous = project.db.get_current_plan_bundle()
            if not previous:
                raise ValidationGateError("尚未生成初始四级规划，不能生成下一篇章判断单。")
            current = previous.current_arc
            expected = list(range(current.chapter_start, current.chapter_end + 1))
            accepted = project.db.accepted_chapter_numbers(current.chapter_start, current.chapter_end)
            missing = [number for number in expected if number not in set(accepted)]
            if missing:
                display = "、".join(str(number) for number in missing[:12])
                raise ValidationGateError(f"当前篇章尚未全部进入正史，缺少第 {display} 章。")
            next_start = current.chapter_end + 1
            if next_start > previous.book.estimated_chapters:
                raise ValidationGateError("全书规划章节已全部完成，没有下一篇章。")
            target_summary = _next_arc_summary(previous, next_start)
            if target_summary is None:
                raise ValidationGateError("下一篇章尚无卷内摘要，暂不能生成可核对的判断单。")

            brief = project.db.get_brief()
            facts = project.db.current_facts()
            threads = project.db.open_threads()
            recent = [
                {"chapter_no": item["chapter_no"], "title": item["title"], "summary": item.get("summary") or ""}
                for item in project.db.accepted_chapters()[-6:]
            ]
            compact_facts = [
                {
                    "fact_id": item["fact_id"],
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    "value": item["value"],
                    "source_chapter": item["source_chapter"],
                }
                for item in facts
            ]
            compact_threads = [
                {
                    "thread_id": item["thread_id"],
                    "status": item["status"],
                    "description": item["description"],
                    "due_chapter": item.get("due_chapter"),
                }
                for item in threads
            ]
            task = (
                f"为第 {target_summary.chapter_start}～{target_summary.chapter_end} 章先写一份公开篇章判断单。"
                "它供用户检查和讨论，不生成 ArcPlan、不改 PLAN.md、不提交 SQLite。"
            )
            if instruction.strip():
                task += f"\n用户补充：{instruction.strip()}"
            sections = [
                ContextSection(key="A", title="公开判断单任务", content=task, hard=True),
                ContextSection(key="B", title="书籍契约", content=json_dumps(brief.model_dump(mode="json")), hard=True),
                ContextSection(
                    key="C",
                    title="当前卷与刚完成篇章（紧凑视图）",
                    content=json_dumps(
                        {
                            "current_volume": previous.current_volume.model_dump(mode="json"),
                            "completed_arc": current.model_dump(mode="json", exclude={"chapter_cards"}),
                            "next_arc_summary": target_summary.model_dump(mode="json"),
                        }
                    ),
                    hard=True,
                ),
                ContextSection(key="D", title="最近已接受章节结果", content=json_dumps(recent), hard=True),
                ContextSection(
                    key="E",
                    title="当前正史事实（紧凑语义视图）",
                    content=json_dumps(compact_facts),
                    source_ids=[str(item["fact_id"]) for item in facts],
                    hard=True,
                ),
                ContextSection(
                    key="F",
                    title="可用来源编号（只能逐字复制）",
                    content=json_dumps(
                        {
                            "fact_ids": [str(item["fact_id"]) for item in facts],
                            "thread_ids": [str(item["thread_id"]) for item in threads],
                        }
                    ),
                    hard=True,
                ),
                ContextSection(
                    key="G",
                    title="未结线索与兑现义务",
                    content=json_dumps(compact_threads),
                    source_ids=[str(item["thread_id"]) for item in threads],
                    hard=True,
                ),
                ContextSection(
                    key="J",
                    title="输出契约",
                    content=(
                        "只输出 ArcPlanningBrief JSON。beats 必须恰好覆盖第 "
                        f"{target_summary.chapter_start}～{target_summary.chapter_end} 章；"
                        f"本篇不可漂移的承诺是：{target_summary.promise}；"
                        f"本篇不可漂移的结束状态是：{target_summary.end_state}；"
                        "必须围绕这两项展开，而不是用大量新谜团、专名或设定替换它们。"
                        "constraints_checked、chosen_direction 与每个 beat 都必须给出来自本包“当前正史事实”或“未结线索与兑现义务”的精确编号；"
                        "引用时只能从“可用来源编号”逐字复制，不能自己改写、缩写或新造编号；"
                        "若一个元素只是未来提案，必须写成建议、可能或待验证，不得把它说成已发生的正史事实。"
                        "全篇最多引入两个新元素，且必须登记到 new_elements；未登记的新元素不得使用专有名称、不得直接给出它的真实身份或终局答案。"
                        "说明判断依据与风险，但不输出逐步自言自语或隐藏思维链。"
                    ),
                    hard=True,
                ),
            ]
            packet = ContextPacket(
                project_id=project.project_id,
                chapter_no=next_start,
                task=task,
                sections=sections,
                estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
            )
            atomic_write_text(trace.run_dir / "context-packet.md", packet.to_markdown())
            trace.record(
                "context.build",
                "completed",
                f"构建公开判断单 Context Packet，估算 {packet.estimated_tokens} tokens",
                metadata={"next_start": next_start, "next_end": target_summary.chapter_end},
            )
            result = await self.provider.generate_json(
                system_prompt=PLANNER_SYSTEM,
                user_prompt=packet.to_markdown(),
                output_model=ArcPlanningBrief,
                effort="high",
                max_tokens=6_000,
                timeout_seconds=self.settings.request_timeout_seconds,
                agent_role="writer",
            )
            rationale = result.data
            expected_beats = list(range(target_summary.chapter_start, target_summary.chapter_end + 1))
            actual_beats = [item.chapter_no for item in rationale.beats]
            if actual_beats != expected_beats:
                raise ValidationGateError(
                    f"公开判断单章节节拍必须连续覆盖 {expected_beats[0]}～{expected_beats[-1]} 章。"
                )
            rationale, source_corrections = _normalize_planning_brief_references(
                rationale,
                fact_ids={str(item["fact_id"]) for item in facts},
                thread_ids={str(item["thread_id"]) for item in threads},
            )
            _validate_planning_brief_grounding(
                rationale,
                fact_ids={str(item["fact_id"]) for item in facts},
                thread_ids={str(item["thread_id"]) for item in threads},
            )
            brief_id = f"brief-{trace.run_id}"
            payload = {
                "brief_id": brief_id,
                "chapter_range": [target_summary.chapter_start, target_summary.chapter_end],
                "target_summary": target_summary.model_dump(mode="json"),
                "created_at": utc_now(),
                "instruction": instruction,
                "source_corrections": source_corrections,
                "rationale": rationale.model_dump(mode="json"),
            }
            internal_path = project.internal / "planning-briefs" / f"{brief_id}.json"
            visible_path = project.root / "planning" / f"arc_{next_start:05d}_{brief_id}.md"
            atomic_write_text(internal_path, json_dumps(payload) + "\n")
            atomic_write_text(visible_path, _render_planning_brief(payload))
            trace.record_model("plan.brief.model", result, f"生成第 {next_start}～{target_summary.chapter_end} 章公开判断单")
            trace.record("plan.brief.write", "completed", "公开判断单已写入，尚未改动正式规划", metadata={"path": str(visible_path)})
            trace.finish(summary="公开判断单已生成，等待用户确认后再展开正式章节卡")
            return {
                "brief_id": brief_id,
                "chapter_range": payload["chapter_range"],
                "brief_path": str(visible_path),
                "rationale": rationale.model_dump(mode="json"),
                "trace_id": trace.run_id,
                "next_action": "阅读判断单后，可用自然语言说“按刚才的判断单展开第二篇章节卡”。",
            }
        except Exception as exc:
            trace.record("plan.brief", "failed", "公开判断单未生成", str(exc))
            trace.finish(status="failed", summary="公开判断单未改变规划或正史")
            raise

    @project_mutation_locked
    async def advance_plan(self, root: str | Path, *, instruction: str = "") -> dict[str, Any]:
        """Open the next rolling planning window after the current arc is canon.

        Only the next arc is detailed.  The approved book compass is carried
        forward verbatim, while SQLite keeps earlier arc/card versions.
        """

        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, "plan-advance", self.settings.trace_level)
        try:
            previous = project.db.get_current_plan_bundle()
            if not previous:
                raise ValidationGateError("尚未生成初始四级规划，不能推进下一篇章。")
            current = previous.current_arc
            expected = list(range(current.chapter_start, current.chapter_end + 1))
            accepted = project.db.accepted_chapter_numbers(current.chapter_start, current.chapter_end)
            missing = [number for number in expected if number not in set(accepted)]
            if missing:
                display = "、".join(str(number) for number in missing[:12])
                suffix = "……" if len(missing) > 12 else ""
                raise ValidationGateError(
                    f"当前篇章尚未全部进入正史，缺少第 {display}{suffix} 章；滚动规划不会越级。"
                )

            next_start = current.chapter_end + 1
            if next_start > previous.book.estimated_chapters:
                raise ValidationGateError("全书规划章节已全部完成，没有下一篇章。")

            brief = project.db.get_brief()
            facts = project.db.current_facts()
            threads = project.db.open_threads()
            planning_history = {
                "全书长期契约": previous.book.model_dump(mode="json", exclude={"volume_compass"}),
                "当前卷": previous.current_volume.model_dump(mode="json"),
                "刚完成篇章（不重复携带旧章节卡）": current.model_dump(
                    mode="json", exclude={"chapter_cards"}
                ),
            }
            planning_facts = [
                {
                    "fact_id": item["fact_id"],
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    "value": item["value"],
                    "source_chapter": item["source_chapter"],
                }
                for item in facts
            ]
            planning_threads = [
                {
                    "thread_id": item["thread_id"],
                    "status": item["status"],
                    "description": item["description"],
                    "due_chapter": item.get("due_chapter"),
                }
                for item in threads
            ]
            accepted_summaries = [
                {
                    "chapter_no": item["chapter_no"],
                    "title": item["title"],
                    "summary": item.get("summary") or "",
                }
                for item in project.db.accepted_chapters()[-12:]
            ]
            same_volume = next_start <= previous.current_volume.chapter_end
            target_summary = _next_arc_summary(previous, next_start) if same_volume else None
            target = {
                "mode": "same_volume" if same_volume else "next_volume",
                "next_chapter_start": next_start,
                "known_arc_summary": target_summary.model_dump(mode="json") if target_summary else None,
            }
            if not same_volume:
                next_volume_no = previous.current_volume.volume_no + 1
                compass = _volume_compass(previous, next_volume_no)
                if compass is None:
                    raise ValidationGateError(f"全书罗盘中缺少第 {next_volume_no} 卷，必须先重规划。")
                target["volume_compass"] = compass.model_dump(mode="json")

            approved_replan = instruction.strip()
            latest_audit = self._latest_arc_audit(project, current.chapter_start, current.chapter_end)
            latest_planning_brief = self._latest_planning_brief(project, next_start)
            task = (
                f"在第 {current.chapter_start}～{current.chapter_end} 章全部进入正史后，"
                f"细化从第 {next_start} 章开始的下一个篇章。"
            )
            if approved_replan:
                task += f"\n用户已经明确确认可调整未来规划，补充要求：{approved_replan}"
            sections = [
                ContextSection(key="A", title="当前滚动规划任务", content=task, hard=True),
                ContextSection(
                    key="B",
                    title="不可改写的书籍契约",
                    content=json_dumps(brief.model_dump(mode="json")),
                    hard=True,
                ),
                ContextSection(
                    key="C",
                    title="已批准的全书/当前卷/刚完成篇章（紧凑视图）",
                    content=json_dumps(planning_history),
                    hard=True,
                ),
                ContextSection(
                    key="D",
                    title="最近已接受章节结果",
                    content=json_dumps(accepted_summaries),
                    hard=True,
                ),
                ContextSection(
                    key="E",
                    title="当前正史事实（紧凑语义视图）",
                    content=json_dumps(planning_facts),
                    source_ids=[str(item["fact_id"]) for item in facts],
                    hard=True,
                ),
                ContextSection(
                    key="G",
                    title="未结线索与兑现义务",
                    content=json_dumps(planning_threads),
                    source_ids=[str(item["thread_id"]) for item in threads],
                    hard=True,
                ),
                ContextSection(
                    key="I",
                    title="本次不可漂移的目标边界",
                    content=json_dumps(
                        {
                            **target,
                            "用户已确认的未来规划调整": approved_replan or "无；保持既有未来规划承诺。",
                            "最近篇章复审": latest_audit or "无；不要猜测不存在的复审结论。",
                            "最近公开篇章判断单": latest_planning_brief or "无；直接依据正史与篇章摘要展开。",
                        }
                    ),
                    hard=True,
                ),
                ContextSection(
                    key="J",
                    title="输出契约",
                    content=(
                        "同卷时只输出 ArcPlan JSON；若打开下一卷则输出 VolumeArcPlan JSON。"
                        "只细化一个篇章，章节卡必须连续并逐章包含目标、阻力、决定、后果、"
                        "不可逆变化和有轮换的章末钩子。"
                    ),
                    hard=True,
                ),
            ]
            packet = ContextPacket(
                project_id=project.project_id,
                chapter_no=next_start,
                task=task,
                sections=sections,
                estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
            )
            atomic_write_text(trace.run_dir / "context-packet.md", packet.to_markdown())
            trace.record(
                "context.build",
                "completed",
                f"构建唯一滚动规划 Context Packet，估算 {packet.estimated_tokens} tokens",
                metadata={"next_start": next_start, "mode": target["mode"]},
            )

            if same_volume:
                result = await self.provider.generate_json(
                    system_prompt=PLANNER_SYSTEM,
                    user_prompt=packet.to_markdown(),
                    output_model=ArcPlan,
                    # A rolling window only needs one arc and a handful of
                    # chapter cards.  Flash timed out on max/28k before
                    # returning JSON, so retain thinking but cap it to a
                    # budget that fits six detailed cards.
                    effort="high",
                    max_tokens=16_000,
                    timeout_seconds=self.settings.planning_timeout_seconds,
                    agent_role="writer",
                )
                arc = _lock_next_arc(
                    result.data,
                    previous.current_volume,
                    next_start,
                    target_summary,
                    preserve_summary=not bool(approved_replan),
                )
                volume = previous.current_volume
                if target_summary is None:
                    volume_data = volume.model_dump(mode="json")
                    volume_data["arcs"].append(
                        ArcSummary(
                            arc_id=arc.arc_id,
                            title=arc.title,
                            chapter_start=arc.chapter_start,
                            chapter_end=arc.chapter_end,
                            promise=arc.promise,
                            end_state=arc.end_state,
                        ).model_dump(mode="json")
                    )
                    volume = VolumePlan.model_validate(volume_data)
                elif approved_replan:
                    volume = _replace_arc_summary(volume, arc)
            else:
                result = await self.provider.generate_json(
                    system_prompt=PLANNER_SYSTEM,
                    user_prompt=packet.to_markdown(),
                    output_model=VolumeArcPlan,
                    effort="max",
                    max_tokens=16_000,
                    timeout_seconds=self.settings.planning_timeout_seconds,
                    agent_role="writer",
                )
                proposal = result.data
                compass = _volume_compass(previous, previous.current_volume.volume_no + 1)
                assert compass is not None
                volume, arc = _lock_next_volume(proposal, previous, compass, next_start)

            _assert_new_plan_cards(project, arc)
            bundle = PlanBundle(book=previous.book, current_volume=volume, current_arc=arc)
            trace.record_model(
                "plan.advance.model",
                result,
                f"生成下一篇章 {arc.arc_id} 的 {len(arc.chapter_cards)} 张连续章节卡",
            )
            project.db.save_plan_bundle(bundle)
            atomic_write_text(project.root / "PLAN.md", render_plan(bundle))
            checkpoint = CheckpointService(project).create(
                label=f"滚动规划 · {arc.title}",
                reason="plan_advanced",
            )
            trace.record(
                "plan.advance.commit",
                "completed",
                "下一规划窗口已提交；旧篇章仍保留在 SQLite 与检查点历史中",
                metadata={"checkpoint_id": checkpoint["checkpoint_id"]},
            )
            trace.finish(summary=f"滚动规划已推进到第 {arc.chapter_start}～{arc.chapter_end} 章")
            return {
                "project_id": project.project_id,
                "current_volume": volume.volume_no,
                "current_arc": arc.arc_id,
                "chapter_range": [arc.chapter_start, arc.chapter_end],
                "plan_path": str(project.root / "PLAN.md"),
                "future_plan_adjusted": bool(approved_replan),
                "checkpoint": checkpoint,
                "trace_id": trace.run_id,
            }
        except Exception as exc:
            trace.record("plan.advance", "failed", "滚动篇章规划未提交", str(exc))
            trace.finish(status="failed", summary="规划窗口保持不变")
            raise

    def build_context(self, root: str | Path, chapter_no: int, task: str | None = None) -> dict[str, Any]:
        project = InkFlowProject(root)
        packet = self._context_builder(project).build(
            chapter_no,
            task or f"按照已批准章节卡创作第 {chapter_no} 章",
        )
        return packet.model_dump(mode="json") | {"markdown": packet.to_markdown()}

    def preview_plan_range(
        self,
        root: str | Path,
        start_chapter: int,
        end_chapter: int,
    ) -> dict[str, Any]:
        """Return several existing chapter cards as one read-only review batch."""

        project = InkFlowProject(root)
        if end_chapter < start_chapter:
            raise ValidationGateError("结束章节不能早于起始章节。")
        if end_chapter - start_chapter + 1 > 20:
            raise ValidationGateError("一次最多预览 20 张章节卡；请缩小范围后分批查看。")

        cards: list[dict[str, Any]] = []
        missing: list[int] = []
        for chapter_no in range(start_chapter, end_chapter + 1):
            card = project.db.get_chapter_card(chapter_no)
            if card is None:
                missing.append(chapter_no)
                continue
            cards.append(
                {
                    "章节": chapter_no,
                    "暂定标题": card["title_working"],
                    "章节功能": card["function"],
                    "目标": card["goal"],
                    "阻力": card["obstacle"],
                    "决定": card["decision"],
                    "后果": card["consequence"],
                    "不可逆变化": card["irreversible_delta"],
                    "信息释放": card["information_release"],
                    "推进伏笔": card["foreshadow_advance"],
                    "兑现事项": card["payoff"],
                    "钩子": {"类型": card["hook_type"], "问题": card["hook_question"]},
                    "目标字数": card["target_words"],
                }
            )
        if missing:
            display = "、".join(str(number) for number in missing)
            raise ValidationGateError(f"第 {display} 章尚无章节卡；请先生成或推进对应篇章规划。")
        return {
            "mode": "批次规划只读预览",
            "chapter_range": [start_chapter, end_chapter],
            "count": len(cards),
            "cards": cards,
            "plan_path": str(project.root / "PLAN.md"),
            "changed": False,
            "confirmation": "可一次确认全部，或按章节号提出局部修改。",
        }

    @project_mutation_locked
    async def write_chapter(
        self,
        root: str | Path,
        chapter_no: int,
        instruction: str = "",
        *,
        provisional_chapters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, f"write-{chapter_no:05d}", self.settings.trace_level)
        try:
            card = project.db.get_chapter_card(chapter_no)
            if not card:
                raise ValidationGateError(f"缺少第 {chapter_no} 章章节卡。")
            creative_lens = _creative_lens(chapter_no)
            task = (
                f"创作第 {chapter_no} 章。用户补充：{instruction or '无'}\n"
                f"本章创意镜头软建议：{creative_lens}。只有在不违背正史、章节卡和人物动机时采用；"
                "它用于改变信息呈现方式，不得凭空增加事件。"
            )
            packet = self._context_builder(project).build(
                chapter_no, task, mode="draft", provisional_chapters=provisional_chapters
            )
            packet_path = trace.run_dir / "context-packet.md"
            atomic_write_text(packet_path, packet.to_markdown())
            trace.record(
                "context.build",
                "completed",
                f"构建唯一 Context Packet，估算 {packet.estimated_tokens} tokens",
                metadata={
                    "sections": [section.key for section in packet.sections],
                    "warnings": packet.warnings,
                    "creative_lens": creative_lens,
                },
            )
            active_skills = ["章节卡履约", "场景动作落地", "自然中文正文"]
            trace.record(
                "writer.skills",
                "completed",
                "写作角色已装载三项轻量技能；创意镜头和用户文风优先",
                metadata={"skills": active_skills, "extra_model_calls": 0},
            )
            result = await self.provider.generate_json(
                system_prompt=WRITER_SYSTEM,
                user_prompt=packet.to_markdown(),
                output_model=DraftOutput,
                effort="high",
                max_tokens=min(16_000, max(8_000, int(card["target_words"] * 2.2))),
                agent_role="writer",
            )
            draft = result.data
            trace.record_model("writer.model", result, "完成章节草稿并给出可审计决策摘要")
            chapter_title = _normalise_chapter_title(chapter_no, draft.title)
            chapter_text = f"# 第 {chapter_no} 章 {chapter_title}\n\n{draft.content.strip()}\n"
            relative = Path("chapters") / f"chapter_{chapter_no:05d}.draft.md"
            atomic_write_text(project.root / relative, chapter_text)
            version = project.db.upsert_draft(chapter_no, chapter_title, relative.as_posix(), chapter_text)
            project.db.resolve_pending_collaboration(chapter_no=chapter_no, recipient_role="writer")
            project.db.append_collaboration_message(
                thread_id=f"chapter-{chapter_no:05d}-v{version}",
                run_id=trace.run_id,
                sender_role="writer",
                recipient_role="reviewer",
                message_type="handoff",
                chapter_no=chapter_no,
                chapter_version=version,
                context_packet_id=content_hash(packet.to_markdown()),
                claim=f"第 {chapter_no} 章草稿 v{version} 已完成，等待独立审查。",
                evidence_refs=[relative.as_posix(), f"plan:chapter:{chapter_no:05d}"],
                requested_response="按当前版本和 Context Packet 给出带证据审查；不直接修改正文。",
            )
            trace.record(
                "draft.write",
                "completed",
                f"写入草稿 v{version}",
                details="\n".join(f"- {item}" for item in draft.decision_summary),
                metadata={"path": relative.as_posix(), "characters": _content_char_count(draft.content)},
            )
            trace.finish(summary="章节草稿已生成，等待审查")
            return {
                "chapter_no": chapter_no,
                "version": version,
                "title": chapter_title,
                "draft_path": str(project.root / relative),
                "decision_summary": draft.decision_summary,
                "skills_used": active_skills,
                "trace_id": trace.run_id,
                "next_action": "审查章节",
            }
        except Exception as exc:
            trace.record("write", "failed", "章节生成失败", str(exc))
            trace.finish(status="failed", summary="草稿未完成")
            raise

    @project_mutation_locked
    async def review_chapter(
        self,
        root: str | Path,
        chapter_no: int,
        *,
        provisional_chapters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, f"review-{chapter_no:05d}", self.settings.trace_level)
        try:
            chapter = project.db.get_chapter(chapter_no)
            card = project.db.get_chapter_card(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ProjectError(f"第 {chapter_no} 章没有待审草稿。")
            if not card:
                raise ValidationGateError(f"第 {chapter_no} 章缺少章节卡。")
            draft_path = project.root / chapter["path"]
            content = draft_path.read_text(encoding="utf-8")
            brief = project.db.get_brief()
            metrics, code_findings = _deterministic_audit(
                content,
                int(card["target_words"]),
                brief.user_rules,
            )
            trace.record(
                "review.rules",
                "completed",
                f"确定性检查发现 {len(code_findings)} 个问题",
                metadata=metrics,
            )
            if any(item.severity in {"major", "blocking"} for item in code_findings):
                report = ReviewReport(
                    verdict="patch",
                    confidence=1.0,
                    summary="确定性检查已发现会阻止验收的问题；无需消耗模型审查调用。",
                    strengths=[],
                    findings=code_findings,
                    scorecard=_build_review_scorecard(code_findings),
                    source_hash=content_hash(content),
                )
                relative = Path("reviews") / f"chapter_{chapter_no:05d}.review.md"
                atomic_write_text(project.root / relative, render_review(chapter_no, report, metrics))
                project.db.save_review(chapter_no, int(chapter["version"]), report, relative.as_posix())
                self._record_review_collaboration(
                    project, chapter_no, int(chapter["version"]), report, trace.run_id, ""
                )
                trace.record(
                    "review.short_circuit",
                    "completed",
                    "确定性硬问题已生成审查报告，跳过大模型调用",
                    metadata={"path": relative.as_posix(), "finding_count": len(code_findings)},
                )
                trace.finish(summary="审查完成：patch（确定性短路）")
                return {
                    "chapter_no": chapter_no,
                    "verdict": report.verdict,
                    "confidence": report.confidence,
                    "summary": report.summary,
                    "finding_count": len(report.findings),
                    "findings": [item.model_dump(mode="json") for item in report.findings],
                    "score_total": _score_total(report.scorecard),
                    "scorecard": [item.model_dump(mode="json") for item in report.scorecard],
                    "review_path": str(project.root / relative),
                    "trace_id": trace.run_id,
                    "next_action": "按审查意见修改章节",
                    "model_skipped": True,
                }
            packet = self._context_builder(project).build(
                chapter_no,
                f"审查第 {chapter_no} 章草稿",
                mode="review",
                provisional_chapters=provisional_chapters,
                protected_input=content,
            )
            user_prompt = (
                packet.to_markdown()
                + "\n\n# 待审正文\n\n"
                + content
                + "\n\n# 代码层指标\n\n"
                + json_dumps(metrics)
            )
            result = await self.provider.generate_json(
                system_prompt=REVIEWER_SYSTEM,
                user_prompt=user_prompt,
                output_model=ReviewReport,
                effort="low",
                max_tokens=16_000,
                timeout_seconds=120,
                agent_role="reviewer",
            )
            model_report = result.data
            verified, verdict = await self._verify_review_output(
                project,
                model_report,
                content,
                packet,
                trace,
            )
            findings = [*code_findings, *verified]
            current = project.db.get_chapter(chapter_no)
            if not current or current["version"] != chapter["version"] or current["path"] != chapter["path"] or draft_path.read_text(encoding="utf-8") != content:
                raise ValidationGateError("审查期间正文已变化，请重新审查当前版本。")
            report = ReviewReport(
                verdict=verdict,
                confidence=model_report.confidence,
                summary=model_report.summary,
                strengths=model_report.strengths,
                findings=findings,
                scorecard=_build_review_scorecard(findings),
                source_hash=content_hash(content),
            )
            trace.record_model("review.model", result, f"综合审查结论：{report.verdict}")
            relative = Path("reviews") / f"chapter_{chapter_no:05d}.review.md"
            atomic_write_text(project.root / relative, render_review(chapter_no, report, metrics))
            project.db.save_review(chapter_no, int(chapter["version"]), report, relative.as_posix())
            self._record_review_collaboration(
                project,
                chapter_no,
                int(chapter["version"]),
                report,
                trace.run_id,
                content_hash(packet.to_markdown()),
            )
            trace.record("review.write", "completed", "审查报告已写入", metadata={"path": relative.as_posix()})
            trace.finish(summary=f"审查完成：{report.verdict}")
            return {
                "chapter_no": chapter_no,
                "verdict": report.verdict,
                "confidence": report.confidence,
                "summary": report.summary,
                "finding_count": len(report.findings),
                "findings": [item.model_dump(mode="json") for item in report.findings],
                "score_total": _score_total(report.scorecard),
                "scorecard": [item.model_dump(mode="json") for item in report.scorecard],
                "review_path": str(project.root / relative),
                "trace_id": trace.run_id,
                "next_action": "接受章节" if report.verdict == "pass" else "按审查意见修改章节",
            }
        except Exception as exc:
            trace.record("review", "failed", "章节审查失败", str(exc))
            trace.finish(status="failed", summary="审查未完成，章节不会放行")
            raise

    async def _verify_review_output(
        self,
        project: InkFlowProject,
        report: ReviewReport,
        content: str,
        packet: ContextPacket,
        trace: TraceRecorder,
    ) -> tuple[list[ReviewFinding], str]:
        findings, verdict = verify_review(report, content, packet)
        mode = self.settings.review_verification_mode
        if mode in {"assisted", "strict"} and verdict == "unknown":
            rejected = [
                item.model_dump(mode="json")
                for item in findings
                if item.verification_status == "unsupported"
                and item.proposed_severity in {"major", "blocking"}
            ]
            if rejected:
                correction = await self.provider.generate_json(
                    system_prompt=REVIEW_CORRECTION_SYSTEM,
                    user_prompt=(
                        "# 当前 Context Packet\n"
                        + packet.to_markdown()
                        + "\n\n# 当前正文\n"
                        + content
                        + "\n\n# 当前全部审查意见（请保留其中合格条目）\n"
                        + json_dumps([item.model_dump(mode="json") for item in findings])
                        + "\n\n# 被程序拒绝的问题\n"
                        + json_dumps(rejected)
                    ),
                    output_model=ReviewFindingBatch,
                    effort="low",
                    max_tokens=6_000,
                    thinking=False,
                    agent_role="reviewer",
                )
                corrected_report = report.model_copy(update={"findings": correction.data.findings})
                findings, verdict = verify_review(corrected_report, content, packet)
                trace.record_model(
                    "review.correction",
                    correction,
                    f"Reviewer 完成唯一一次受约束纠错，保留 {len(findings)} 条意见",
                )

        targets = findings_for_semantic_check(findings)
        decision_sets: list[list[ReviewClaimDecision]] = []
        if targets and mode in {"assisted", "strict"}:
            checked = await self.provider.generate_json(
                system_prompt=REVIEW_CLAIM_CHECK_SYSTEM,
                user_prompt=json_dumps({"findings": targets}),
                output_model=ReviewClaimDecisionBatch,
                effort="low",
                max_tokens=min(4_000, max(800, len(targets) * 320)),
                thinking=False,
                agent_role="reviewer_verifier",
            )
            decision_sets.append(checked.data.decisions)
            trace.record_model("review.semantic", checked, f"逐条语义核验 {len(targets)} 个硬问题")

        if targets and self.settings.review_local_nli_model:
            try:
                local = await asyncio.to_thread(
                    local_nli_decisions,
                    self.settings.review_local_nli_model,
                    findings,
                )
                decision_sets.append(local)
                trace.record(
                    "review.local_nli",
                    "completed",
                    f"本地 NLI 核验 {len(local)} 个硬问题",
                    metadata={"model": self.settings.review_local_nli_model},
                )
            except Exception as exc:
                trace.record("review.local_nli", "warning", "本地 NLI 不可用，保留其他核验结果", str(exc))

        if decision_sets:
            merged = _merge_claim_decisions(decision_sets, [item["finding_index"] for item in targets])
            findings, verdict = apply_semantic_decisions(findings, merged, source="逐条语义核验")

        if mode == "strict" and verdict == "unknown" and self.settings.review_judge_model:
            disputed = [
                {
                    "finding_index": index,
                    "finding": item.model_dump(mode="json"),
                }
                for index, item in enumerate(findings)
                if item.verification_status == "uncertain"
            ]
            if disputed:
                judged = await self.provider.generate_json(
                    system_prompt=REVIEW_DISPUTE_SYSTEM,
                    user_prompt=json_dumps({"disputed_findings": disputed}),
                    output_model=ReviewClaimDecisionBatch,
                    effort="low",
                    max_tokens=min(4_000, max(800, len(disputed) * 320)),
                    thinking=False,
                    agent_role="reviewer_judge",
                    model_override=self.settings.review_judge_model,
                )
                findings, verdict = apply_dispute_decisions(
                    findings,
                    judged.data.decisions,
                    source=f"争议裁判 {judged.model}",
                )
                trace.record_model("review.dispute_judge", judged, f"裁决 {len(disputed)} 个语义争议")
        return findings, verdict

    @staticmethod
    def _record_review_collaboration(
        project: InkFlowProject,
        chapter_no: int,
        chapter_version: int,
        report: ReviewReport,
        run_id: str,
        context_packet_id: str,
    ) -> None:
        project.db.resolve_pending_collaboration(chapter_no=chapter_no, recipient_role="reviewer")
        hard = [item for item in report.findings if item.severity in {"major", "blocking"}]
        if report.verdict == "pass":
            recipient, message_type, status = "memory_keeper", "handoff", "pending"
            claim = f"第 {chapter_no} 章 v{chapter_version} 已通过 Reviewer，可在用户授权后提取正史记忆。"
            requested = "验收时重新核对版本与正文哈希，只从已接受正文提取 MemoryPatch。"
        elif report.verdict == "patch":
            recipient, message_type, status = "writer", "revision_request", "pending"
            claim = "；".join(item.explanation for item in hard[:6]) or report.summary
            requested = "仅按已核验证据定点修订；生成新版本后必须重新审查。"
        else:
            recipient, message_type, status = "coordinator", "risk", "escalated"
            claim = report.summary
            requested = "材料不足或方向存在分歧，请向用户说明缺口，不要自动修改正文。"
        project.db.append_collaboration_message(
            thread_id=f"chapter-{chapter_no:05d}-v{chapter_version}",
            run_id=run_id,
            sender_role="reviewer",
            recipient_role=recipient,
            message_type=message_type,
            chapter_no=chapter_no,
            chapter_version=chapter_version,
            context_packet_id=context_packet_id,
            claim=claim,
            evidence_refs=sorted({ref for item in report.findings for ref in item.canon_refs}),
            requested_response=requested,
            status=status,
        )
        for finding in report.findings:
            for source_id in finding.canon_refs:
                project.db.record_retrieval_feedback(
                    query=f"第 {chapter_no} 章 v{chapter_version} Reviewer 审查",
                    source_id=source_id,
                    outcome="misleading" if finding.verification_status == "unsupported" else "cited",
                    role="reviewer",
                    chapter_no=chapter_no,
                    packet_id=context_packet_id or None,
                )
        if report.verdict != "pass":
            project.db.record_learning_event(
                "rejected",
                {
                    "verdict": report.verdict,
                    "finding_rules": [item.rule_id for item in report.findings if item.rule_id],
                    "source_hash": report.source_hash,
                },
                chapter_no=chapter_no,
                chapter_version=chapter_version,
            )

    @project_mutation_locked
    async def revise_chapter(
        self,
        root: str | Path,
        chapter_no: int,
        instruction: str = "",
        *,
        provisional_chapters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, f"revise-{chapter_no:05d}", self.settings.trace_level)
        try:
            chapter = project.db.get_chapter(chapter_no)
            card = project.db.get_chapter_card(chapter_no)
            review_record = project.db.latest_review_record(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ProjectError(f"第 {chapter_no} 章没有可修订草稿。")
            if not card:
                raise ValidationGateError(f"第 {chapter_no} 章缺少章节卡。")
            if not review_record:
                raise ValidationGateError("没有审查报告，不能进入证据化修订。")
            if review_record["chapter_version"] != int(chapter["version"]):
                raise ValidationGateError(
                    f"最近审查对应草稿 v{review_record['chapter_version']}，"
                    f"当前草稿是 v{chapter['version']}；必须先审查当前版本再修订。"
                )

            draft_path = project.root / chapter["path"]
            review_path = project.root / review_record["path"]
            current_draft = draft_path.read_text(encoding="utf-8")
            review_text = review_path.read_text(encoding="utf-8")
            task = f"修订第 {chapter_no} 章。用户补充：{instruction or '无'}"
            packet = self._context_builder(project).build(
                chapter_no,
                task,
                mode="revise",
                provisional_chapters=provisional_chapters,
                protected_input=current_draft + "\n" + review_text,
            )
            atomic_write_text(trace.run_dir / "context-packet.md", packet.to_markdown())
            trace.record(
                "context.build",
                "completed",
                f"构建唯一 Context Packet，估算 {packet.estimated_tokens} tokens",
                metadata={"sections": [section.key for section in packet.sections], "warnings": packet.warnings},
            )
            active_skills = ["证据定点修订", "回归连续性扫描", "自然中文正文"]
            trace.record(
                "writer.skills",
                "completed",
                "写作角色已装载修订技能；只修有依据的问题并保留有效声线",
                metadata={"skills": active_skills, "extra_model_calls": 0},
            )
            user_prompt = (
                packet.to_markdown()
                + "\n\n# 当前草稿\n\n"
                + current_draft
                + "\n\n# 当前版本 Reviewer 报告\n\n"
                + review_text
                + "\n\n# 修订要求\n\n"
                + (instruction or "逐项处理有证据的问题，保留审查确认有效的内容。")
            )
            result = await self.provider.generate_json(
                system_prompt=REVISER_SYSTEM,
                user_prompt=user_prompt,
                output_model=DraftOutput,
                effort="high",
                max_tokens=min(16_000, max(8_000, int(card["target_words"] * 2.2))),
                agent_role="writer",
            )
            draft = result.data
            trace.record_model("writer.revise", result, "Writer 读取旧稿与审查证据后完成定点修订")
            chapter_title = _normalise_chapter_title(chapter_no, draft.title)
            chapter_text = f"# 第 {chapter_no} 章 {chapter_title}\n\n{draft.content.strip()}\n"
            atomic_write_text(draft_path, chapter_text)
            previous_version = int(chapter["version"])
            version = project.db.upsert_draft(
                chapter_no,
                chapter_title,
                Path(chapter["path"]).as_posix(),
                chapter_text,
            )
            project.db.record_learning_event(
                "revised",
                {"previous_version": previous_version, "new_version": version, "source": "reviewer_or_user"},
                chapter_no=chapter_no,
                chapter_version=version,
            )
            trace.record(
                "draft.revise",
                "completed",
                f"草稿由 v{previous_version} 修订为 v{version}；旧审查自动失效",
                details="\n".join(f"- {item}" for item in draft.decision_summary),
                metadata={"path": chapter["path"], "characters": _content_char_count(draft.content)},
            )
            trace.finish(summary="章节修订完成，等待重新审查")
            return {
                "chapter_no": chapter_no,
                "previous_version": previous_version,
                "version": version,
                "title": chapter_title,
                "draft_path": str(draft_path),
                "decision_summary": draft.decision_summary,
                "skills_used": active_skills,
                "trace_id": trace.run_id,
                "next_action": "重新审查当前版本",
            }
        except Exception as exc:
            trace.record("revise", "failed", "章节修订失败", str(exc))
            trace.finish(status="failed", summary="草稿未修改")
            raise

    @project_mutation_locked
    async def revise_selection(
        self,
        root: str | Path,
        relative_path: str,
        start_offset: int,
        end_offset: int,
        instruction: str,
        *,
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        """让 Writer 只返回一个选区替换片段，再由编排器确定性落盘。"""

        project = InkFlowProject(root)
        relative = relative_path.replace("\\", "/")
        match = re.search(r"chapter_(\d+)", relative, flags=re.IGNORECASE)
        chapter_no = int(match.group(1)) if match else 0
        trace = TraceRecorder(project.root, f"selection-revise-{chapter_no:05d}", self.settings.trace_level)
        try:
            if not chapter_no or not relative.endswith(".draft.md"):
                raise ValidationGateError("局部修订只适用于尚未验收的章节草稿；正史正文需先预览影响并确认分支修订。")
            if not instruction.strip():
                raise ProjectError("局部修订要求不能为空。")

            chapter = project.db.get_chapter(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ValidationGateError(f"第 {chapter_no} 章当前不是可直接修改的草稿。")
            if Path(str(chapter["path"])).as_posix() != Path(relative).as_posix():
                raise ValidationGateError("选中的文件不是数据库记录的当前草稿版本，请重新打开当前章节。")

            studio = StudioService(project)
            document = studio.read_document(relative)
            current = str(document["content"])
            current_hash = str(document["content_hash"])
            if expected_hash and expected_hash != current_hash:
                raise ValidationGateError("正文在你选中后已经变化。请重新选择文字，墨流没有覆盖新内容。")
            if not (0 <= start_offset < end_offset <= len(current)):
                raise ProjectError("局部修订选区超出当前正文范围。")
            selected = current[start_offset:end_offset]
            if not selected.strip():
                raise ProjectError("不能修订空白选区。")
            if len(selected) > 8_000:
                raise ValidationGateError("一次局部修订最多处理 8000 个字符；更长范围请使用整章修订。")

            annotation = studio.create_annotation(relative, start_offset, end_offset, instruction.strip())
            before = current[max(0, start_offset - 1_200) : start_offset]
            after = current[end_offset : min(len(current), end_offset + 1_200)]
            task = (
                f"局部修订第 {chapter_no} 章的一处已锁定选区。"
                f"只处理这条用户意见：{instruction.strip()}"
            )
            packet = self._context_builder(project).build(
                chapter_no,
                task,
                mode="revise",
                protected_input=before + "\n" + selected + "\n" + after,
            )
            atomic_write_text(trace.run_dir / "context-packet.md", packet.to_markdown())
            trace.record(
                "context.build",
                "completed",
                f"为局部修订构建唯一 Context Packet，估算 {packet.estimated_tokens} tokens",
                metadata={"relative_path": relative, "selection_characters": len(selected)},
            )

            user_prompt = (
                packet.to_markdown()
                + "\n\n# 选区前文（只读，不得改写）\n\n"
                + before
                + "\n\n# 唯一允许替换的原文\n\n"
                + selected
                + "\n\n# 选区后文（只读，不得改写）\n\n"
                + after
                + "\n\n# 用户局部意见\n\n"
                + instruction.strip()
            )
            result = await self.provider.generate_json(
                system_prompt=SELECTION_REVISER_SYSTEM,
                user_prompt=user_prompt,
                output_model=SelectionRevisionOutput,
                effort="medium",
                max_tokens=min(8_000, max(2_000, len(selected) * 2)),
                agent_role="writer",
            )
            replacement_core = result.data.replacement.strip()
            leading = selected[: len(selected) - len(selected.lstrip())]
            trailing = selected[len(selected.rstrip()) :]
            replacement = f"{leading}{replacement_core}{trailing}"
            updated = current[:start_offset] + replacement + current[end_offset:]
            if content_hash(updated) == current_hash:
                raise ValidationGateError("Writer 返回的内容与原选区相同，草稿没有产生新版本。")

            trace.record_model(
                "writer.selection_revise",
                result,
                "Writer 已完成锁定选区的最小替换",
            )
            saved = studio.save_document(
                relative,
                updated,
                expected_hash=current_hash,
                source="writer_selection_revision",
            )
            if not saved.get("saved"):
                raise ValidationGateError(str(saved.get("gate") or "局部修订未能写入草稿。"))
            studio.db.set_annotation_status(str(annotation["annotation_id"]), "resolved")
            current_chapter = project.db.get_chapter(chapter_no) or {}
            project.db.record_learning_event(
                "revised",
                {
                    "previous_hash": current_hash,
                    "new_hash": str(saved.get("content_hash") or ""),
                    "source": "user_selection",
                    "annotation_id": annotation["annotation_id"],
                },
                chapter_no=chapter_no,
                chapter_version=int(current_chapter.get("version") or 0) or None,
            )
            trace.record(
                "draft.selection_revise",
                "completed",
                f"第 {chapter_no} 章局部修订已写入新草稿版本",
                details="\n".join(f"- {item}" for item in result.data.decision_summary),
                metadata={
                    "annotation_id": annotation["annotation_id"],
                    "version": current_chapter.get("version"),
                    "before_characters": len(selected),
                    "after_characters": len(replacement),
                },
            )
            trace.finish(summary="局部修订完成，等待 Reviewer 检查当前版本")
            return {
                "chapter_no": chapter_no,
                "version": current_chapter.get("version"),
                "annotation_id": annotation["annotation_id"],
                "relative_path": relative,
                "replaced_excerpt": selected[:240],
                "replacement_excerpt": replacement[:240],
                "decision_summary": result.data.decision_summary,
                "trace_id": trace.run_id,
                "next_action": "重新审查当前草稿；不会自动验收或写入正史",
            }
        except Exception as exc:
            trace.record("selection.revise", "failed", "局部修订失败", str(exc))
            trace.finish(status="failed", summary="原草稿未被局部修订覆盖")
            raise

    @project_mutation_locked
    async def accept_chapter(
        self,
        root: str | Path,
        chapter_no: int,
        *,
        force: bool = False,
        _prepared_patch: MemoryPatch | None = None,
        _provisional_batch_id: str | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, f"accept-{chapter_no:05d}", self.settings.trace_level)
        try:
            chapter = project.db.get_chapter(chapter_no)
            review_record = project.db.latest_review_record(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ProjectError(f"第 {chapter_no} 章没有待接受草稿。")
            if not review_record:
                raise ValidationGateError("没有审查报告，不能提交正史。")
            if review_record["chapter_version"] != int(chapter["version"]):
                raise ValidationGateError(
                    f"最近审查对应草稿 v{review_record['chapter_version']}，"
                    f"当前草稿是 v{chapter['version']}；正文修改后必须重新审查。"
                )
            review = review_record["report"]
            if review.verdict != "pass" and not force:
                raise ValidationGateError(f"审查结论为 {review.verdict}，需要修改或显式 force 接受。")
            draft_path = project.root / chapter["path"]
            content = draft_path.read_text(encoding="utf-8")
            if review.source_hash and review.source_hash != content_hash(content):
                raise ValidationGateError("正文与审查时的内容不一致，必须重新审查。")
            if _prepared_patch is None:
                patch, conflict_relative, conflict_path = await self._extract_memory_patch(
                    project,
                    chapter_no,
                    content,
                    trace,
                    source_status="accepted",
                    force=force,
                )
            else:
                patch = _validate_memory_patch_scope(_prepared_patch, chapter_no)
                patch, aligned_ids = _align_patch_evidence(patch, content)
                unsupported = [
                    fact.fact_id for fact in patch.facts if not _evidence_in_content(fact.evidence, content)
                ]
                if unsupported:
                    raise ValidationGateError(
                        f"批次临时记忆已失效，以下证据无法在当前正文定位：{', '.join(unsupported)}"
                    )
                if patch.unresolved_conflicts and not force:
                    raise ValidationGateError("批次临时记忆仍有未解决冲突，不能提升为正史。")
                conflict_relative = Path("reviews") / f"chapter_{chapter_no:05d}.memory-conflict.md"
                conflict_path = project.root / conflict_relative
                trace.record(
                    "memory.promote.prepare",
                    "completed",
                    "复用并校验 Reviewer 通过后生成的批次临时记忆",
                    metadata={
                        "batch_id": _provisional_batch_id,
                        "aligned_fact_ids": aligned_ids,
                    },
                )
            final_relative = Path("chapters") / f"chapter_{chapter_no:05d}.md"
            final_path = project.root / final_relative
            transaction = project.prepare_file_commit(final_relative, content)
            project.db.accept_chapter(
                chapter_no,
                str(chapter["title"]),
                final_relative.as_posix(),
                content,
                patch,
                provisional_batch_id=_provisional_batch_id,
            )
            project.mark_file_commit_database(transaction)
            project.finalize_file_commit(transaction)
            if conflict_path.exists():
                try:
                    project.delete_file(conflict_relative.as_posix(), permanent=False)
                    trace.record("memory.conflict_cleanup", "completed", "旧冲突投影已移入可恢复回收区")
                except Exception as cleanup_error:
                    trace.record(
                        "memory.conflict_cleanup",
                        "warning",
                        "正史已提交，但旧冲突投影清理失败",
                        str(cleanup_error),
                    )
            if draft_path != final_path and draft_path.exists():
                draft_path.unlink()
            atomic_write_text(
                project.root / "STATE.md",
                render_state(project.db.current_facts(), project.db.open_threads(), project.db.project_status()),
            )
            project.db.resolve_pending_collaboration(chapter_no=chapter_no, recipient_role="memory_keeper")
            project.db.append_collaboration_message(
                thread_id=f"chapter-{chapter_no:05d}-v{chapter['version']}",
                run_id=trace.run_id,
                sender_role="memory_keeper",
                recipient_role="coordinator",
                message_type="memory_sync",
                chapter_no=chapter_no,
                chapter_version=int(chapter["version"]),
                context_packet_id=review.source_hash,
                claim=f"第 {chapter_no} 章已进入正史，事实与伏笔同步完成。",
                evidence_refs=[
                    final_relative.as_posix(),
                    *[fact.fact_id for fact in patch.facts],
                    *[thread.thread_id for thread in patch.threads],
                ],
                requested_response="向用户汇报正史、事实和伏笔已同步。",
                status="resolved",
            )
            project.db.record_learning_event(
                "accepted",
                {
                    "source_hash": content_hash(content),
                    "facts": [fact.fact_id for fact in patch.facts],
                    "threads": [thread.thread_id for thread in patch.threads],
                },
                chapter_no=chapter_no,
                chapter_version=int(chapter["version"]),
            )
            trace.record(
                "memory.commit",
                "completed",
                "章节、事实、线索和状态视图已事务提交",
                metadata={"final_path": final_relative.as_posix()},
            )
            checkpoint = CheckpointService(project).create(
                label=f"第 {chapter_no} 章已接受",
                reason=f"chapter_accepted:{chapter_no}",
            )
            trace.record(
                "checkpoint.create",
                "completed",
                "正史提交后已创建恢复点",
                metadata={"checkpoint_id": checkpoint["checkpoint_id"]},
            )
            trace.finish(summary="章节已接受并进入正史")
            return {
                "chapter_no": chapter_no,
                "status": "accepted",
                "chapter_path": str(final_path),
                "facts_committed": len(patch.facts),
                "threads_updated": len(patch.threads),
                "checkpoint": checkpoint,
                "trace_id": trace.run_id,
                "next_action": f"写第 {chapter_no + 1} 章",
            }
        except Exception as exc:
            trace.record("accept", "failed", "章节接受失败", str(exc))
            trace.finish(status="failed", summary="正史未提交")
            raise

    async def _extract_memory_patch(
        self,
        project: InkFlowProject,
        chapter_no: int,
        content: str,
        trace: TraceRecorder,
        *,
        source_status: str,
        provisional_patches: list[dict[str, Any]] | None = None,
        force: bool = False,
    ) -> tuple[MemoryPatch, Path, Path]:
        """Extract and validate a patch without changing canonical memory."""

        if source_status not in {"accepted", "provisional"}:
            raise ValueError(f"不支持的记忆来源状态：{source_status}")
        conflict_relative = Path("reviews") / f"chapter_{chapter_no:05d}.memory-conflict.md"
        conflict_path = project.root / conflict_relative
        current_state = {
            "canonical_facts": project.db.current_facts(),
            "canonical_threads": project.db.open_threads(),
            "earlier_batch_memory": provisional_patches or [],
        }
        if source_status == "provisional":
            source_description = "已经通过 Reviewer、等待用户验收的批次临时正文"
            state_description = "上一正史与同一批次更早章节的临时记忆"
        else:
            source_description = "用户已经接受、即将提交正史的正文"
            state_description = "上一正史状态"
        user_prompt = (
            f"当前来源状态：{source_status}。请从第 {chapter_no} 章{source_description}中提取 MemoryPatch JSON。\n\n"
            f"# {state_description}\n{json_dumps(current_state)}\n\n# 当前正文\n{content}"
        )
        result = await self.provider.generate_json(
            system_prompt=MEMORY_SYSTEM,
            user_prompt=user_prompt,
            output_model=MemoryPatch,
            effort="low",
            max_tokens=10_000,
            agent_role="memory_keeper",
        )
        known_facts = list(current_state["canonical_facts"])
        for staged_patch in current_state["earlier_batch_memory"]:
            if isinstance(staged_patch, dict):
                known_facts.extend(staged_patch.get("facts") or [])
        patch = _validate_memory_patch_scope(result.data, chapter_no, known_facts=known_facts)
        trace.record_model(
            "memory.model",
            result,
            f"提取 {len(patch.facts)} 条事实和 {len(patch.threads)} 条线索变化",
        )
        patch, aligned_ids = _align_patch_evidence(patch, content)
        if aligned_ids:
            trace.record(
                "memory.evidence.align",
                "completed",
                f"本地高置信对齐 {len(aligned_ids)} 条证据",
                metadata={"fact_ids": aligned_ids},
            )
        unsupported = [fact.fact_id for fact in patch.facts if not _evidence_in_content(fact.evidence, content)]
        if unsupported:
            trace.record(
                "memory.evidence",
                "retrying",
                f"有 {len(unsupported)} 条事实证据无法定位，执行一次受约束原文候选选择",
                metadata={"fact_ids": unsupported},
            )
            candidate_groups = {
                fact.fact_id: _build_evidence_candidates(fact, content)
                for fact in patch.facts
                if fact.fact_id in unsupported
            }
            repair_prompt = (
                "# 仅待修事实与程序截取的原文候选\n"
                + json_dumps(
                    [
                        {
                            "fact": fact.model_dump(mode="json"),
                            "candidates": candidate_groups[fact.fact_id],
                        }
                        for fact in patch.facts
                        if fact.fact_id in unsupported
                    ]
                )
                + "\n\n请为每个 fact_id 只选择一个 candidate_id，或在没有直接支持时 drop。"
            )
            repaired = await self.provider.generate_json(
                system_prompt=MEMORY_EVIDENCE_SYSTEM,
                user_prompt=repair_prompt,
                output_model=EvidenceSelectionBatch,
                effort="low",
                max_tokens=min(2_000, max(800, len(unsupported) * 180)),
                thinking=False,
                agent_role="memory_keeper",
            )
            patch, selected_evidence = _apply_evidence_selections(
                patch,
                repaired.data,
                unsupported,
                candidate_groups,
            )
            trace.record_model(
                "memory.evidence_select",
                repaired,
                f"受约束选择后保留 {len(patch.facts)} 条事实",
            )
            if selected_evidence:
                trace.record(
                    "memory.evidence.selected",
                    "completed",
                    f"按候选 ID 写回 {len(selected_evidence)} 段正文原文",
                    details="\n".join(
                        f"- `{item['fact_id']}` → `{item['candidate_id']}`：{item['evidence']}"
                        for item in selected_evidence
                    ),
                )
            unsupported = [
                fact.fact_id for fact in patch.facts if not _evidence_in_content(fact.evidence, content)
            ]
            if unsupported:
                raise ValidationGateError(f"以下事实证据修复后仍无法在正文中定位：{', '.join(unsupported)}")
        patch, open_questions = _separate_open_questions(patch)
        if open_questions:
            trace.record(
                "memory.open_questions",
                "completed",
                f"将 {len(open_questions)} 条未知信息识别为开放问题，不作为正史冲突",
                details="\n".join(f"- {item}" for item in open_questions),
            )
        if patch.unresolved_conflicts and not force:
            trace.record(
                "memory.conflict",
                "retrying",
                f"Memory Keeper 报告 {len(patch.unresolved_conflicts)} 个冲突，执行一次限次自解析",
                details="\n".join(f"- {item}" for item in patch.unresolved_conflicts),
            )
            conflict_prompt = (
                user_prompt
                + "\n\n# 上一次候选补丁\n"
                + json_dumps(patch.model_dump(mode="json"))
                + "\n\n# 冲突自解析要求\n"
                + "SQLite 正史尚未提交。请仅依据上一状态与正文逐字证据，重新输出完整 MemoryPatch。"
                + "能由明确原文消解的误报冲突应删除；真正存在两个无法同时成立的版本时必须保留，"
                + "不得猜测、补写或替用户选择。每条 fact evidence 仍须是正文连续原文。"
            )
            conflict_result = await self.provider.generate_json(
                system_prompt=MEMORY_SYSTEM,
                user_prompt=conflict_prompt,
                output_model=MemoryPatch,
                effort="low",
                max_tokens=12_000,
                agent_role="memory_keeper",
            )
            patch = _validate_memory_patch_scope(
                conflict_result.data,
                chapter_no,
                known_facts=known_facts,
            )
            patch, open_questions = _separate_open_questions(patch)
            trace.record_model(
                "memory.conflict_resolution",
                conflict_result,
                f"限次自解析后剩余 {len(patch.unresolved_conflicts)} 个冲突",
            )
            if open_questions:
                trace.record(
                    "memory.open_questions",
                    "completed",
                    f"自解析结果中 {len(open_questions)} 条属于开放问题，不阻塞提交",
                    details="\n".join(f"- {item}" for item in open_questions),
                )
            unsupported = [
                fact.fact_id for fact in patch.facts if not _evidence_in_content(fact.evidence, content)
            ]
            if unsupported:
                atomic_write_text(conflict_path, render_memory_conflict(chapter_no, patch))
                raise ValidationGateError(
                    "冲突自解析后仍有无法逐字定位的事实证据："
                    f"{', '.join(unsupported)}。候选补丁：{conflict_path}"
                )
            if patch.unresolved_conflicts:
                atomic_write_text(conflict_path, render_memory_conflict(chapter_no, patch))
                conflict_text = "；".join(patch.unresolved_conflicts)
                trace.record(
                    "memory.conflict_report",
                    "failed",
                    "冲突无法自动消解，已保存用户可见候选补丁",
                    details=conflict_text,
                    metadata={"path": conflict_relative.as_posix()},
                )
                raise ValidationGateError(
                    f"记忆补丁仍有未解决冲突：{conflict_text}。详情：{conflict_path}"
                )
        return patch, conflict_relative, conflict_path

    async def balance(self) -> dict[str, Any]:
        getter = getattr(self.provider, "get_balance", None)
        if getter is None:
            raise ProviderError("当前模型 Provider 不支持手动余额查询。")
        result = await getter()
        if result.get("currency") != "CNY" or "total_balance" not in result:
            raise ProviderError("余额结果缺少可验证的 CNY total_balance。")
        return result

    @project_mutation_locked
    async def draft_batch(
        self,
        root: str | Path,
        start_chapter_no: int,
        end_chapter_no: int,
        *,
        instruction: str = "",
        max_revision_rounds: int = 2,
    ) -> dict[str, Any]:
        """Create a provisional multi-chapter batch without committing canon."""

        if start_chapter_no < 1 or end_chapter_no < start_chapter_no:
            raise ValidationGateError("批次章节范围不合法。")
        if not 0 <= max_revision_rounds <= 2:
            raise ValidationGateError("批量草稿每章自动修订轮数必须在 0～2 之间。")
        project = InkFlowProject(root)
        expected_start = project.db.latest_accepted_chapter_no() + 1
        if start_chapter_no != expected_start:
            raise ValidationGateError(
                f"批量草稿必须从紧邻正史的第 {expected_start} 章开始，"
                "以避免临时内容跨越未确认章节。"
            )
        for chapter_no in range(start_chapter_no, end_chapter_no + 1):
            if not project.db.get_chapter_card(chapter_no):
                raise ValidationGateError(f"第 {chapter_no} 章缺少章节卡，不能进入批量草稿。")

        trace = TraceRecorder(project.root, "batch-draft", self.settings.trace_level)
        batch_id = f"batch-{trace.run_id}"
        manifest = {
            "batch_id": batch_id,
            "status": "drafting",
            "start_chapter_no": start_chapter_no,
            "end_chapter_no": end_chapter_no,
            "instruction": instruction,
            "max_revision_rounds": max_revision_rounds,
            "created_at": utc_now(),
            "chapters": [],
        }
        self._save_batch_manifest(project, manifest)
        provisional: list[dict[str, Any]] = []
        try:
            trace.record(
                "batch.start",
                "completed",
                f"创建第 {start_chapter_no}～{end_chapter_no} 章临时批次；不提交正史",
                metadata={"batch_id": batch_id, "max_revision_rounds": max_revision_rounds},
            )
            for chapter_no in range(start_chapter_no, end_chapter_no + 1):
                chapter = project.db.get_chapter(chapter_no)
                if chapter is None:
                    written = await self.write_chapter(
                        project.root,
                        chapter_no,
                        instruction,
                        provisional_chapters=provisional,
                    )
                    trace.record(
                        "batch.writer.draft",
                        "completed",
                        f"第 {chapter_no} 章已生成临时草稿 v{written['version']}",
                        metadata={"trace_id": written["trace_id"]},
                    )
                elif chapter["status"] != "draft":
                    raise ValidationGateError(f"第 {chapter_no} 章不是可复用的草稿，无法加入当前批次。")

                revision_round = 0
                while True:
                    reviewed = await self.review_chapter(
                        project.root,
                        chapter_no,
                        provisional_chapters=provisional,
                    )
                    trace.record(
                        "batch.reviewer.current",
                        "completed",
                        f"第 {chapter_no} 章即时审查：{reviewed['verdict']}",
                        metadata={"trace_id": reviewed["trace_id"], "revision_round": revision_round},
                    )
                    if reviewed["verdict"] == "pass":
                        current = project.db.get_chapter(chapter_no)
                        assert current is not None
                        content = (project.root / current["path"]).read_text(encoding="utf-8")
                        earlier_memory = [
                            item["memory_patch"]
                            for item in provisional
                            if isinstance(item.get("memory_patch"), dict)
                        ]
                        memory_patch, _, _ = await self._extract_memory_patch(
                            project,
                            chapter_no,
                            content,
                            trace,
                            source_status="provisional",
                            provisional_patches=earlier_memory,
                            force=False,
                        )
                        project.db.save_provisional_memory_patch(
                            batch_id,
                            chapter_no,
                            int(current["version"]),
                            content,
                            memory_patch,
                        )
                        project.db.append_collaboration_message(
                            thread_id=f"batch-{batch_id}-chapter-{chapter_no:05d}-v{current['version']}",
                            run_id=trace.run_id,
                            sender_role="memory_keeper",
                            recipient_role="coordinator",
                            message_type="memory_sync",
                            chapter_no=chapter_no,
                            chapter_version=int(current["version"]),
                            context_packet_id=content_hash(content),
                            claim=f"第 {chapter_no} 章 Reviewer 已通过；临时事实与伏笔已写入批次记忆，但尚非正史。",
                            evidence_refs=[
                                *[fact.fact_id for fact in memory_patch.facts],
                                *[thread.thread_id for thread in memory_patch.threads],
                            ],
                            requested_response="后续章节只作为当前批次临时连续性使用；集中验收时再逐章提升。",
                            status="resolved",
                        )
                        trace.record(
                            "batch.memory.provisional",
                            "completed",
                            f"第 {chapter_no} 章临时记忆已同步，供本批次下一章读取",
                            metadata={
                                "facts": len(memory_patch.facts),
                                "threads": len(memory_patch.threads),
                                "chapter_version": int(current["version"]),
                            },
                        )
                        entry = {
                            "chapter_no": chapter_no,
                            "version": int(current["version"]),
                            "title": current["title"],
                            "draft_path": current["path"],
                            "review_path": str(Path(reviewed["review_path"]).relative_to(project.root)),
                            "verdict": "pass",
                            "revision_rounds": revision_round,
                            "review_summary": reviewed.get("summary", ""),
                            "finding_count": int(reviewed.get("finding_count", 0)),
                            "score_total": reviewed.get("score_total"),
                            "findings": list(reviewed.get("findings") or []),
                            "memory_status": "provisional",
                            "memory_facts": len(memory_patch.facts),
                            "memory_threads": len(memory_patch.threads),
                        }
                        manifest["chapters"].append(entry)
                        provisional.append(
                            {
                                "batch_id": batch_id,
                                "chapter_no": chapter_no,
                                "content": content,
                                "memory_patch": memory_patch.model_dump(mode="json"),
                            }
                        )
                        self._save_batch_manifest(project, manifest)
                        break
                    if reviewed["verdict"] == "patch" and revision_round < max_revision_rounds:
                        revision_round += 1
                        revised = await self.revise_chapter(
                            project.root,
                            chapter_no,
                            instruction
                            or "只修复当前审查报告有明确证据的硬问题，保留有效剧情和叙事留白。",
                            provisional_chapters=provisional,
                        )
                        trace.record(
                            "batch.writer.revise",
                            "completed",
                            f"第 {chapter_no} 章完成第 {revision_round} 轮独立 Writer 修订",
                            metadata={"trace_id": revised["trace_id"]},
                        )
                        continue
                    manifest["status"] = "needs_revision"
                    manifest["stopped_at_chapter"] = chapter_no
                    manifest["stop_reason"] = (
                        f"Reviewer verdict={reviewed['verdict']}，已使用 {revision_round}/{max_revision_rounds} 轮修订。"
                    )
                    self._save_batch_manifest(project, manifest)
                    result = self._batch_result(project, manifest)
                    trace.finish(status="failed", summary="批量草稿停在待修订章节；未提交正史")
                    return result

            manifest["status"] = "ready_for_acceptance"
            manifest["ready_at"] = utc_now()
            self._save_batch_manifest(project, manifest)
            result = self._batch_result(project, manifest)
            trace.finish(summary="批量草稿已完成，等待用户集中验收")
            return {**result, "trace_id": trace.run_id}
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["stop_reason"] = str(exc)
            self._save_batch_manifest(project, manifest)
            trace.record("batch", "failed", "批量草稿失败，已有草稿保留", str(exc))
            trace.finish(status="failed", summary="批次未提交正史")
            raise

    @project_mutation_locked
    async def repair_batch(
        self,
        root: str | Path,
        batch_id: str,
        *,
        start_chapter_no: int | None = None,
        end_chapter_no: int | None = None,
        instruction: str = "",
        max_additional_revision_rounds: int = 1,
    ) -> dict[str, Any]:
        """Revise and re-review a contiguous portion of one provisional batch.

        This is deliberately distinct from creating a new batch: the visible
        batch manifest must never continue to claim that a superseded draft
        version passed review.  It never calls Memory Keeper or changes canon.
        """

        if not 0 <= max_additional_revision_rounds <= 2:
            raise ValidationGateError("批次修复的额外自动修订轮数必须在 0～2 之间。")
        project = InkFlowProject(root)
        manifest = self._load_batch_manifest(project, batch_id)
        repairable_status = manifest.get("status") == "ready_for_acceptance" or (
            manifest.get("status") == "needs_revision" and isinstance(manifest.get("last_repair"), dict)
        )
        if not repairable_status:
            raise ValidationGateError("只有已完成的临时批次，或此前修复中断且保留修复记录的批次可以继续修复。")
        entries = list(manifest.get("chapters") or [])
        if not entries:
            raise ValidationGateError("批次没有可修复的章节。")
        entries.sort(key=lambda item: int(item["chapter_no"]))
        batch_start = int(manifest["start_chapter_no"])
        batch_end = int(manifest["end_chapter_no"])
        start = start_chapter_no if start_chapter_no is not None else batch_start
        requested_end = end_chapter_no if end_chapter_no is not None else batch_end
        if start < batch_start or requested_end > batch_end or requested_end < start:
            raise ValidationGateError(f"修复范围必须位于该批次的第 {batch_start}～{batch_end} 章内。")
        # Once an earlier chapter changes, every later draft was written from
        # stale continuity.  Rebuild the remaining suffix instead of allowing
        # stale provisional memory to reach the acceptance gate.
        end = batch_end

        entry_by_chapter = {int(item["chapter_no"]): item for item in entries}
        selected = list(range(start, end + 1))
        missing = [chapter_no for chapter_no in selected if chapter_no not in entry_by_chapter]
        if missing:
            display = "、".join(str(item) for item in missing)
            raise ValidationGateError(f"批次清单缺少第 {display} 章，不能安全修复。")

        trace = TraceRecorder(project.root, "batch-repair", self.settings.trace_level)
        previous_status = str(manifest.get("status"))
        manifest["status"] = "repairing"
        manifest["stop_reason"] = ""
        manifest["last_repair"] = {
            "started_at": utc_now(),
            "chapter_range": [start, end],
            "requested_chapter_range": [start, requested_end],
            "range_expanded_for_continuity": requested_end < batch_end,
            "instruction": instruction,
            "max_additional_revision_rounds": max_additional_revision_rounds,
            "trace_id": trace.run_id,
        }
        self._save_batch_manifest(project, manifest)

        # Preload only earlier temporary chapters.  They are the valid local
        # continuity context for the first repaired chapter; later chapters
        # must wait for the repaired predecessor.
        provisional: list[dict[str, Any]] = []
        for entry in entries:
            chapter_no = int(entry["chapter_no"])
            if chapter_no >= start:
                break
            current = project.db.get_chapter(chapter_no)
            if not current or current["status"] != "draft":
                continue
            content = (project.root / current["path"]).read_text(encoding="utf-8")
            staged_memory = project.db.get_provisional_memory_patch(batch_id, chapter_no)
            provisional.append(
                {
                    "batch_id": batch_id,
                    "chapter_no": chapter_no,
                    "content": content,
                    **(
                        {"memory_patch": staged_memory["patch"].model_dump(mode="json")}
                        if staged_memory and staged_memory["status"] == "active"
                        else {}
                    ),
                }
            )

        try:
            trace.record(
                "batch.repair.start",
                "completed",
                f"开始修复批次 {batch_id} 的第 {start}～{end} 章；不提交正史",
                metadata={
                    "previous_status": previous_status,
                    "max_additional_revision_rounds": max_additional_revision_rounds,
                },
            )
            for chapter_no in selected:
                current = project.db.get_chapter(chapter_no)
                if not current or current["status"] != "draft":
                    raise ValidationGateError(f"第 {chapter_no} 章已不是可修复的临时草稿。")

                invalidated = project.db.invalidate_provisional_memory_from(
                    batch_id,
                    chapter_no,
                    f"第 {chapter_no} 章开始修订，当前章及后续临时记忆需要重建",
                )
                if invalidated:
                    trace.record(
                        "batch.memory.invalidate",
                        "completed",
                        f"已使第 {chapter_no} 章起的 {invalidated} 份旧临时记忆失效",
                    )

                review_record = project.db.latest_review_record(chapter_no)
                if not review_record or review_record["chapter_version"] != int(current["version"]):
                    initial_review = await self.review_chapter(
                        project.root,
                        chapter_no,
                        provisional_chapters=provisional,
                    )
                    trace.record(
                        "batch.repair.reviewer.baseline",
                        "completed",
                        f"第 {chapter_no} 章补齐当前版本的基线审查：{initial_review['verdict']}",
                        metadata={"trace_id": initial_review["trace_id"]},
                    )

                revision_rounds = 0
                base_revision_rounds = int(entry_by_chapter[chapter_no].get("revision_rounds", 0))
                while True:
                    revised = await self.revise_chapter(
                        project.root,
                        chapter_no,
                        instruction
                        or "按已确认的篇章复审因果方向修正本章；只处理可证实的问题，保留有效剧情与叙事留白。",
                        provisional_chapters=provisional,
                    )
                    revision_rounds += 1
                    trace.record(
                        "batch.repair.writer.revise",
                        "completed",
                        f"第 {chapter_no} 章完成第 {revision_rounds} 轮批次修订",
                        metadata={"trace_id": revised["trace_id"]},
                    )
                    reviewed = await self.review_chapter(
                        project.root,
                        chapter_no,
                        provisional_chapters=provisional,
                    )
                    trace.record(
                        "batch.repair.reviewer.current",
                        "completed",
                        f"第 {chapter_no} 章修订后审查：{reviewed['verdict']}",
                        metadata={"trace_id": reviewed["trace_id"], "revision_round": revision_rounds},
                    )

                    current = project.db.get_chapter(chapter_no)
                    assert current is not None
                    entry_by_chapter[chapter_no] = self._batch_entry(
                        project,
                        current,
                        reviewed,
                        previous_rounds=base_revision_rounds,
                        added_rounds=revision_rounds,
                    )
                    manifest["chapters"] = [entry_by_chapter[int(item["chapter_no"])] for item in entries]
                    self._save_batch_manifest(project, manifest)

                    if reviewed["verdict"] == "pass":
                        content = (project.root / current["path"]).read_text(encoding="utf-8")
                        earlier_memory = [
                            item["memory_patch"]
                            for item in provisional
                            if isinstance(item.get("memory_patch"), dict)
                        ]
                        memory_patch, _, _ = await self._extract_memory_patch(
                            project,
                            chapter_no,
                            content,
                            trace,
                            source_status="provisional",
                            provisional_patches=earlier_memory,
                            force=False,
                        )
                        project.db.save_provisional_memory_patch(
                            batch_id,
                            chapter_no,
                            int(current["version"]),
                            content,
                            memory_patch,
                        )
                        project.db.append_collaboration_message(
                            thread_id=f"batch-{batch_id}-chapter-{chapter_no:05d}-v{current['version']}",
                            run_id=trace.run_id,
                            sender_role="memory_keeper",
                            recipient_role="coordinator",
                            message_type="memory_sync",
                            chapter_no=chapter_no,
                            chapter_version=int(current["version"]),
                            context_packet_id=content_hash(content),
                            claim=f"第 {chapter_no} 章修订版已通过；临时事实与伏笔已重新同步，仍未进入正史。",
                            evidence_refs=[
                                *[fact.fact_id for fact in memory_patch.facts],
                                *[thread.thread_id for thread in memory_patch.threads],
                            ],
                            requested_response="后续章节使用当前修订版临时记忆；集中验收时再逐章提升。",
                            status="resolved",
                        )
                        entry_by_chapter[chapter_no]["memory_status"] = "provisional"
                        entry_by_chapter[chapter_no]["memory_facts"] = len(memory_patch.facts)
                        entry_by_chapter[chapter_no]["memory_threads"] = len(memory_patch.threads)
                        provisional = [item for item in provisional if int(item["chapter_no"]) != chapter_no]
                        provisional.append(
                            {
                                "batch_id": batch_id,
                                "chapter_no": chapter_no,
                                "content": content,
                                "memory_patch": memory_patch.model_dump(mode="json"),
                            }
                        )
                        break
                    if reviewed["verdict"] == "patch" and revision_rounds <= max_additional_revision_rounds:
                        continue

                    manifest["status"] = "needs_revision"
                    manifest["stopped_at_chapter"] = chapter_no
                    manifest["stop_reason"] = (
                        f"第 {chapter_no} 章修订后 Reviewer verdict={reviewed['verdict']}；"
                        f"已完成 {revision_rounds} 轮修订并保留当前草稿。"
                    )
                    manifest["last_repair"]["finished_at"] = utc_now()
                    manifest["last_repair"]["status"] = "needs_revision"
                    self._save_batch_manifest(project, manifest)
                    trace.finish(status="failed", summary="批次修复停在待修订章节；未提交正史")
                    return {**self._batch_result(project, manifest), "trace_id": trace.run_id}

            manifest["status"] = "ready_for_acceptance"
            manifest["ready_at"] = utc_now()
            manifest["last_repair"]["finished_at"] = utc_now()
            manifest["last_repair"]["status"] = "ready_for_acceptance"
            self._save_batch_manifest(project, manifest)
            trace.finish(summary="批次修复、逐章重审和清单同步已完成，等待集中验收")
            return {**self._batch_result(project, manifest), "trace_id": trace.run_id}
        except Exception as exc:
            manifest["status"] = "needs_revision"
            manifest["stop_reason"] = str(exc)
            manifest["last_repair"]["finished_at"] = utc_now()
            manifest["last_repair"]["status"] = "failed"
            self._save_batch_manifest(project, manifest)
            trace.record("batch.repair", "failed", "批次修复失败，现有草稿均保留", str(exc))
            trace.finish(status="failed", summary="批次修复失败；未提交正史")
            raise

    def _batch_entry(
        self,
        project: InkFlowProject,
        chapter: dict[str, Any],
        reviewed: dict[str, Any],
        *,
        previous_rounds: int,
        added_rounds: int,
    ) -> dict[str, Any]:
        """Make the manifest point at exactly the reviewed current version."""

        review_path = Path(str(reviewed["review_path"])).relative_to(project.root)
        return {
            "chapter_no": int(chapter["chapter_no"]),
            "version": int(chapter["version"]),
            "title": chapter["title"],
            "draft_path": chapter["path"],
            "review_path": review_path.as_posix(),
            "verdict": reviewed["verdict"],
            "revision_rounds": previous_rounds + added_rounds,
            "review_summary": reviewed.get("summary", ""),
            "finding_count": int(reviewed.get("finding_count", 0)),
            "score_total": reviewed.get("score_total"),
            "findings": list(reviewed.get("findings") or []),
        }

    @project_mutation_locked
    async def accept_batch(self, root: str | Path, batch_id: str) -> dict[str, Any]:
        """Commit one reviewed provisional batch as a continuous canonical prefix."""

        project = InkFlowProject(root)
        manifest = self._load_batch_manifest(project, batch_id)
        if manifest.get("status") not in {"ready_for_acceptance", "accepting"}:
            raise ValidationGateError("该批次尚未全部通过审查，不能集中接收。")
        chapters = list(manifest.get("chapters") or [])
        if not chapters:
            raise ValidationGateError("批次没有可接收章节。")
        expected_start = project.db.latest_accepted_chapter_no() + 1
        if int(chapters[0]["chapter_no"]) != expected_start:
            raise ValidationGateError(
                f"当前正史下一章是第 {expected_start} 章，批次无法形成连续前缀，拒绝接收。"
            )
        trace = TraceRecorder(project.root, "batch-accept", self.settings.trace_level)
        accepted: list[dict[str, Any]] = []
        manifest["status"] = "accepting"
        self._save_batch_manifest(project, manifest)
        try:
            for entry in chapters:
                chapter_no = int(entry["chapter_no"])
                current = project.db.get_chapter(chapter_no)
                if current and current["status"] == "accepted":
                    accepted.append({"chapter_no": chapter_no, "status": "already_accepted"})
                    continue
                if not current or current["status"] != "draft":
                    raise ValidationGateError(f"第 {chapter_no} 章已不是待接收草稿。")
                review_record = project.db.latest_review_record(chapter_no)
                if (
                    not review_record
                    or review_record["report"].verdict != "pass"
                    or review_record["chapter_version"] != int(current["version"])
                    or int(current["version"]) != int(entry["version"])
                ):
                    raise ValidationGateError(f"第 {chapter_no} 章的通过审查已失效，不能集中接收。")
                staged_memory = project.db.get_provisional_memory_patch(batch_id, chapter_no)
                prepared_patch = None
                provisional_batch_id = None
                if staged_memory and staged_memory["status"] == "active":
                    prepared_patch = staged_memory["patch"]
                    provisional_batch_id = batch_id
                result = await self.accept_chapter(
                    project.root,
                    chapter_no,
                    force=False,
                    _prepared_patch=prepared_patch,
                    _provisional_batch_id=provisional_batch_id,
                )
                accepted.append(result)
                entry["memory_status"] = "canon"
                trace.record(
                    "batch.memory.accept",
                    "completed",
                    f"第 {chapter_no} 章已从临时批次进入正史",
                    metadata={"trace_id": result["trace_id"]},
                )

            manifest["status"] = "accepted"
            manifest["accepted_at"] = utc_now()
            manifest["accepted_chapters"] = [item["chapter_no"] for item in accepted]
            self._save_batch_manifest(project, manifest)
            trace.finish(summary="批次已按连续前缀提交正史")
            return {**self._batch_result(project, manifest), "accepted": accepted, "trace_id": trace.run_id}
        except Exception as exc:
            manifest["status"] = "accepting"
            manifest["stop_reason"] = str(exc)
            self._save_batch_manifest(project, manifest)
            trace.record("batch.accept", "failed", "批次接收停在当前连续前缀", str(exc))
            trace.finish(status="failed", summary="未越过当前接收边界")
            raise

    @project_mutation_locked
    async def audit_range(
        self,
        root: str | Path,
        start_chapter_no: int,
        end_chapter_no: int,
        *,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """Review an arc/range against its plan without changing canon or plans."""

        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, "arc-audit", self.settings.trace_level)
        try:
            provisional, source_batch_id = self._provisional_for_audit(
                project,
                start_chapter_no,
                end_chapter_no,
                batch_id=batch_id,
            )
            packet = self._context_builder(project).build_arc_audit(
                start_chapter_no,
                end_chapter_no,
                provisional_chapters=provisional,
            )
            atomic_write_text(trace.run_dir / "context-packet.md", packet.to_markdown())
            trace.record(
                "arc_audit.context",
                "completed",
                f"构建第 {start_chapter_no}～{end_chapter_no} 章唯一复审 Context Packet",
                metadata={
                    "estimated_tokens": packet.estimated_tokens,
                    "source_batch_id": source_batch_id,
                    "provisional_chapter_count": len(provisional),
                },
            )
            result = await self.provider.generate_json(
                system_prompt=ARC_AUDIT_SYSTEM,
                user_prompt=packet.to_markdown(),
                output_model=ArcAuditReport,
                effort="low",
                max_tokens=16_000,
                timeout_seconds=120,
                agent_role="reviewer",
            )
            report = result.data
            audit_content = next(
                (section.content for section in packet.sections if section.key == "E"),
                "",
            )
            verification_report = ReviewReport(
                verdict=(
                    "unknown"
                    if report.verdict == "unknown"
                    else "pass"
                    if report.verdict == "aligned"
                    else "patch"
                ),
                confidence=report.confidence,
                summary=report.summary,
                findings=report.deviations,
            )
            verified_deviations, verified_verdict = await self._verify_review_output(
                project,
                verification_report,
                audit_content,
                packet,
                trace,
            )
            if verified_verdict == "unknown":
                arc_verdict = "unknown"
            elif any(item.severity in {"major", "blocking"} for item in verified_deviations):
                arc_verdict = "needs_replan" if report.verdict == "needs_replan" else "blocked"
            else:
                arc_verdict = "aligned"
            report = report.model_copy(
                update={
                    "verdict": arc_verdict,
                    "deviations": verified_deviations,
                    "source_hash": content_hash(audit_content),
                }
            )
            scorecard = _build_review_scorecard(report.deviations)
            verified_hard = any(item.severity in {"major", "blocking"} for item in report.deviations)
            repair_scope = (
                sorted(
                    {
                        chapter_no
                        for chapter_no in report.body_repair_scope
                        if start_chapter_no <= chapter_no <= end_chapter_no
                    }
                )
                if verified_hard
                else []
            )
            if not repair_scope and verified_hard:
                # A conservative fallback for model outputs that identify a major
                # prose contradiction but omit the new scope field.
                repair_scope = list(range(start_chapter_no, end_chapter_no + 1))
            body_repair_recommended = verified_hard and (report.body_repair_recommended or bool(repair_scope))
            audit_id = f"audit-{trace.run_id}"
            relative = Path("reviews") / f"arc_audit_{start_chapter_no:05d}_{end_chapter_no:05d}_{trace.run_id}.md"
            visible_path = project.root / relative
            atomic_write_text(
                visible_path,
                _render_arc_audit(
                    audit_id,
                    start_chapter_no,
                    end_chapter_no,
                    report,
                    source_batch_id=source_batch_id,
                    body_repair_recommended=body_repair_recommended,
                    body_repair_scope=repair_scope,
                    scorecard=scorecard,
                ),
            )
            audit_manifest = {
                "audit_id": audit_id,
                "status": "completed",
                "start_chapter_no": start_chapter_no,
                "end_chapter_no": end_chapter_no,
                "source_batch_id": source_batch_id,
                "created_at": utc_now(),
                "review_path": relative.as_posix(),
                "report": report.model_dump(mode="json"),
                "body_repair_recommended": body_repair_recommended,
                "body_repair_scope": repair_scope,
                "scorecard": [item.model_dump(mode="json") for item in scorecard],
            }
            self._save_arc_audit(project, audit_manifest)
            trace.record_model(
                "arc_audit.reviewer",
                result,
                f"篇章复审结论：{report.verdict}",
            )
            trace.record(
                "arc_audit.write",
                "completed",
                "篇章复审报告已写入；没有修改规划或正史",
                metadata={"path": relative.as_posix(), "audit_id": audit_id},
            )
            trace.finish(summary=f"第 {start_chapter_no}～{end_chapter_no} 章篇章复审完成")
            needs_confirmation = (
                report.verdict != "aligned" or report.replan_recommended or body_repair_recommended
            )
            return {
                "audit_id": audit_id,
                "chapter_range": [start_chapter_no, end_chapter_no],
                "verdict": report.verdict,
                "confidence": report.confidence,
                "score_total": _score_total(scorecard),
                "summary": report.summary,
                "fulfilled_commitments": report.fulfilled_commitments,
                "deviations": [item.model_dump(mode="json") for item in report.deviations],
                "future_impact": report.future_impact,
                "body_repair_recommended": body_repair_recommended,
                "body_repair_scope": repair_scope,
                "scorecard": [item.model_dump(mode="json") for item in scorecard],
                "proposed_future_changes": report.proposed_future_changes,
                "source_batch_id": source_batch_id,
                "review_path": str(visible_path),
                "requires_user_confirmation": needs_confirmation,
                "next_action": (
                    "请先阅读报告；若同意修正文或调整未来规划，请明确说明确认的范围与方向。"
                    if needs_confirmation
                    else "复审认为可自然衔接；仍可由你决定是否按原规划继续。"
                ),
                "trace_id": trace.run_id,
            }
        except Exception as exc:
            trace.record("arc_audit", "failed", "篇章复审失败，规划和正史保持不变", str(exc))
            trace.finish(status="failed", summary="篇章复审未改变任何规划或正史")
            raise

    def _batch_paths(self, project: InkFlowProject, batch_id: str) -> tuple[Path, Path]:
        if not batch_id.startswith("batch-") or any(char in batch_id for char in "\\/"):
            raise ValidationGateError("批次编号不合法。")
        return (
            project.internal / "batches" / f"{batch_id}.json",
            project.root / "batches" / f"{batch_id}.md",
        )

    def _save_batch_manifest(self, project: InkFlowProject, manifest: dict[str, Any]) -> None:
        internal_path, visible_path = self._batch_paths(project, str(manifest["batch_id"]))
        atomic_write_text(internal_path, json_dumps(manifest) + "\n")
        atomic_write_text(visible_path, _render_batch_manifest(manifest))

    def _load_batch_manifest(self, project: InkFlowProject, batch_id: str) -> dict[str, Any]:
        internal_path, _ = self._batch_paths(project, batch_id)
        if not internal_path.is_file():
            raise ProjectError(f"找不到批次：{batch_id}")
        try:
            value = json.loads(internal_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProjectError(f"批次清单无法解析：{batch_id}") from exc
        if not isinstance(value, dict) or value.get("batch_id") != batch_id:
            raise ProjectError(f"批次清单不合法：{batch_id}")
        return value

    def _batch_result(self, project: InkFlowProject, manifest: dict[str, Any]) -> dict[str, Any]:
        _, visible_path = self._batch_paths(project, str(manifest["batch_id"]))
        return {
            "batch_id": manifest["batch_id"],
            "status": manifest["status"],
            "chapter_range": [manifest["start_chapter_no"], manifest["end_chapter_no"]],
            "chapters": list(manifest.get("chapters") or []),
            "batch_path": str(visible_path),
            "next_action": (
                f"集中阅读后说“接收批次 {manifest['batch_id']}”"
                if manifest["status"] == "ready_for_acceptance"
                else "根据批次清单修订停住的章节后，再重新生成批次。"
            ),
        }

    def _provisional_for_audit(
        self,
        project: InkFlowProject,
        start_chapter_no: int,
        end_chapter_no: int,
        *,
        batch_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Load only the temporary chapters needed by one range audit.

        Canonical text always wins.  A batch may supply only the still-draft
        suffix, preventing an accepted chapter from being shadowed by stale
        draft files.
        """

        accepted = set(project.db.accepted_chapter_numbers(start_chapter_no, end_chapter_no))
        missing = set(range(start_chapter_no, end_chapter_no + 1)) - accepted
        if not missing:
            return [], None

        manifests: list[dict[str, Any]] = []
        if batch_id:
            manifests = [self._load_batch_manifest(project, batch_id)]
        else:
            folder = project.internal / "batches"
            for path in folder.glob("batch-*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if value.get("status") in {"ready_for_acceptance", "accepting"}:
                    manifests.append(value)

        eligible: list[dict[str, Any]] = []
        for manifest in manifests:
            entries = {int(item["chapter_no"]): item for item in manifest.get("chapters") or []}
            if missing.issubset(entries):
                eligible.append(manifest)
        if not eligible:
            display = "、".join(str(item) for item in sorted(missing))
            raise ValidationGateError(
                f"第 {display} 章尚未进入正史，且没有同一临时批次可供复审；请先提供已完成批次。"
            )
        if len(eligible) > 1:
            raise ValidationGateError("存在多个可用临时批次，请在复审请求中明确 batch_id，避免猜测版本。")

        manifest = eligible[0]
        entries = {int(item["chapter_no"]): item for item in manifest.get("chapters") or []}
        provisional: list[dict[str, Any]] = []
        for chapter_no in sorted(missing):
            entry = entries[chapter_no]
            path = project.root / entry["draft_path"]
            if not path.is_file():
                raise ProjectError(f"批次第 {chapter_no} 章草稿文件缺失，无法复审。")
            provisional.append(
                {
                    "batch_id": manifest["batch_id"],
                    "chapter_no": chapter_no,
                    "content": path.read_text(encoding="utf-8"),
                }
            )
        return provisional, str(manifest["batch_id"])

    def _arc_audit_paths(self, project: InkFlowProject, audit_id: str) -> tuple[Path, Path]:
        if not audit_id.startswith("audit-") or any(char in audit_id for char in "\\\\/"):
            raise ValidationGateError("篇章复审编号不合法。")
        return (
            project.internal / "arc_audits" / f"{audit_id}.json",
            project.root / "reviews" / f"{audit_id}.md",
        )

    def _save_arc_audit(self, project: InkFlowProject, manifest: dict[str, Any]) -> None:
        internal_path, _ = self._arc_audit_paths(project, str(manifest["audit_id"]))
        atomic_write_text(internal_path, json_dumps(manifest) + "\n")

    def _latest_arc_audit(
        self,
        project: InkFlowProject,
        start_chapter_no: int,
        end_chapter_no: int,
    ) -> dict[str, Any] | None:
        """Return the newest matching completed audit as planning evidence only."""

        folder = project.internal / "arc_audits"
        matches: list[dict[str, Any]] = []
        for path in folder.glob("audit-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                value.get("status") == "completed"
                and value.get("start_chapter_no") == start_chapter_no
                and value.get("end_chapter_no") == end_chapter_no
            ):
                matches.append(value)
        if not matches:
            return None
        latest = max(matches, key=lambda item: str(item.get("created_at", "")))
        return {
            "复审编号": latest["audit_id"],
            "报告路径": latest["review_path"],
            "结论": latest["report"],
        }

    def _latest_planning_brief(self, project: InkFlowProject, next_start: int) -> dict[str, Any] | None:
        """Return the newest public planning brief for the immediate next arc.

        The file is a user-visible discussion artifact, not canon. Supplying
        it to the later planning call makes the user's reviewed direction an
        explicit input instead of a detached explanation.
        """

        folder = project.internal / "planning-briefs"
        matches: list[dict[str, Any]] = []
        fact_ids = {str(item["fact_id"]) for item in project.db.current_facts()}
        thread_ids = {str(item["thread_id"]) for item in project.db.open_threads()}
        for path in folder.glob("brief-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if "target_summary" not in value:
                    continue
                rationale = ArcPlanningBrief.model_validate(value.get("rationale") or {})
                _validate_planning_brief_grounding(rationale, fact_ids=fact_ids, thread_ids=thread_ids)
            except (OSError, json.JSONDecodeError):
                continue
            except (ValidationGateError, ValueError):
                continue
            chapter_range = value.get("chapter_range")
            if isinstance(chapter_range, list) and chapter_range and chapter_range[0] == next_start:
                matches.append(value)
        if not matches:
            return None
        latest = max(matches, key=lambda item: str(item.get("created_at", "")))
        return {
            "判断单编号": latest.get("brief_id", ""),
            "章节范围": latest.get("chapter_range"),
            "用户补充": latest.get("instruction", ""),
            "公开判断": latest.get("rationale", {}),
        }

    def accepted_character_count(self, root: str | Path) -> dict[str, Any]:
        project = InkFlowProject(root)
        total = 0
        chapters: list[dict[str, Any]] = []
        for item in project.db.accepted_chapters():
            path = project.root / item["path"]
            if not path.is_file():
                raise ProjectError(f"正史章节文件缺失：{path}")
            characters = _content_char_count(path.read_text(encoding="utf-8"))
            total += characters
            chapters.append({"chapter_no": item["chapter_no"], "characters": characters})
        return {"total_characters": total, "chapter_count": len(chapters), "chapters": chapters}

    @project_mutation_locked
    async def continue_until(
        self,
        root: str | Path,
        target_characters: int | None = None,
        *,
        instruction: str = "",
        target_chapter_no: int | None = None,
        max_revision_rounds: int = 1,
    ) -> dict[str, Any]:
        """Run the complete gated chapter workflow until a canon character target.

        This is an orchestrator, not a fourth creative role: every content
        mutation still passes through Writer, Reviewer and Memory Keeper.
        """

        if target_characters is None and target_chapter_no is None:
            raise ValidationGateError("长跑必须指定正史字符目标或结束章节号。")
        if target_characters is not None and target_chapter_no is not None:
            raise ValidationGateError("长跑只能指定一种终点：正史字符目标或结束章节号。")
        if target_characters is not None and target_characters < 1_000:
            raise ValidationGateError("长跑目标至少为 1,000 个已接受有效字符。")
        if target_chapter_no is not None and target_chapter_no < 1:
            raise ValidationGateError("长跑结束章节号必须不小于 1。")
        if not 0 <= max_revision_rounds <= 6:
            raise ValidationGateError("每章自动修订轮数必须在 0～6 之间。")

        project = InkFlowProject(root)
        trace = TraceRecorder(project.root, "continue-until", self.settings.trace_level)
        progress_path = trace.run_dir / "progress.md"
        events: list[dict[str, Any]] = []
        target_label = (
            f"已接受正文达到 {target_characters} 个有效字符"
            if target_characters is not None
            else f"第 {target_chapter_no} 章进入正史"
        )

        def snapshot(run_status: str, detail: str = "") -> dict[str, Any]:
            counts = self.accepted_character_count(project.root)
            current = {
                "status": run_status,
                "detail": detail,
                "target_label": target_label,
                "accepted_characters": counts["total_characters"],
                "accepted_chapters": counts["chapter_count"],
                "latest_accepted_chapter": project.db.latest_accepted_chapter_no(),
                "events": list(events),
                "progress_path": str(progress_path),
                "trace_id": trace.run_id,
            }
            atomic_write_text(progress_path, _render_long_run_progress(project.project_id, current))
            return current

        trace.record(
            "long_run.start",
            "completed",
            f"目标为{target_label}",
            metadata={"max_revision_rounds": max_revision_rounds},
        )
        snapshot("running", "长跑协调器已启动")
        try:
            while True:
                counts = self.accepted_character_count(project.root)
                latest_accepted = project.db.latest_accepted_chapter_no()
                reached_characters = (
                    target_characters is not None and counts["total_characters"] >= target_characters
                )
                reached_chapter = target_chapter_no is not None and latest_accepted >= target_chapter_no
                if reached_characters or reached_chapter:
                    result = snapshot("target_reached", f"已达到终点：{target_label}")
                    trace.finish(summary="长跑目标已达到")
                    return result

                chapter_no = latest_accepted + 1
                card = project.db.get_chapter_card(chapter_no)
                if card is None:
                    bundle = project.db.get_current_plan_bundle()
                    if not bundle:
                        raise ValidationGateError("缺少初始规划，长跑无法确定下一章。")
                    if chapter_no <= bundle.current_arc.chapter_end:
                        raise ValidationGateError(f"第 {chapter_no} 章章节卡缺失，拒绝猜写。")
                    planned = await self.advance_plan(project.root)
                    events.append(
                        {
                            "event": "plan_advanced",
                            "chapter_range": planned["chapter_range"],
                            "arc_id": planned["current_arc"],
                            "trace_id": planned["trace_id"],
                        }
                    )
                    snapshot("running", f"已推进规划到第 {planned['chapter_range'][0]} 章")
                    continue

                chapter = project.db.get_chapter(chapter_no)
                if chapter is None:
                    written = await self.write_chapter(project.root, chapter_no, instruction)
                    events.append(
                        {
                            "event": "draft_written",
                            "chapter_no": chapter_no,
                            "version": written["version"],
                            "trace_id": written["trace_id"],
                        }
                    )
                    snapshot("running", f"第 {chapter_no} 章草稿已生成")

                revision_round = 0
                while True:
                    chapter = project.db.get_chapter(chapter_no)
                    if not chapter or chapter["status"] != "draft":
                        raise ValidationGateError(f"第 {chapter_no} 章不处于可审查草稿状态。")
                    review_record = project.db.latest_review_record(chapter_no)
                    if not review_record or review_record["chapter_version"] != int(chapter["version"]):
                        reviewed = await self.review_chapter(project.root, chapter_no)
                        events.append(
                            {
                                "event": "reviewed",
                                "chapter_no": chapter_no,
                                "version": chapter["version"],
                                "verdict": reviewed["verdict"],
                                "trace_id": reviewed["trace_id"],
                            }
                        )
                        snapshot("running", f"第 {chapter_no} 章审查结论：{reviewed['verdict']}")
                        review_record = project.db.latest_review_record(chapter_no)
                        assert review_record is not None

                    verdict = review_record["report"].verdict
                    if verdict == "pass":
                        accepted = await self.accept_chapter(project.root, chapter_no, force=False)
                        current_counts = self.accepted_character_count(project.root)
                        events.append(
                            {
                                "event": "chapter_accepted",
                                "chapter_no": chapter_no,
                                "accepted_characters": current_counts["total_characters"],
                                "trace_id": accepted["trace_id"],
                                "checkpoint_id": accepted["checkpoint"]["checkpoint_id"],
                            }
                        )
                        snapshot("running", f"第 {chapter_no} 章已进入正史")
                        break

                    if verdict == "patch" and revision_round < max_revision_rounds:
                        revision_round += 1
                        revised = await self.revise_chapter(
                            project.root,
                            chapter_no,
                            instruction or "逐项修复当前版本审查报告中的有证据问题，保留有效内容。",
                        )
                        events.append(
                            {
                                "event": "draft_revised",
                                "chapter_no": chapter_no,
                                "version": revised["version"],
                                "revision_round": revision_round,
                                "trace_id": revised["trace_id"],
                            }
                        )
                        snapshot("running", f"第 {chapter_no} 章完成第 {revision_round} 轮定点修订")
                        continue

                    reason = (
                        f"第 {chapter_no} 章审查结论为 {verdict}；"
                        + (
                            f"已达到自动修订上限 {max_revision_rounds} 轮。"
                            if verdict == "patch"
                            else "该结论需要规划或人工边界判断。"
                        )
                    )
                    result = snapshot("gate_stop", reason)
                    trace.finish(status="failed", summary=reason)
                    return result
        except Exception as exc:
            result = snapshot("gate_stop", str(exc))
            trace.record("long_run", "failed", "长跑被门禁或错误停止", str(exc))
            trace.finish(status="failed", summary="已保存进度；未绕过门禁")
            if isinstance(exc, (ProjectError, ProviderError, ValidationGateError)):
                return result
            raise

    def status(self, root: str | Path) -> dict[str, Any]:
        project = InkFlowProject(root)
        return {
            "project_id": project.project_id,
            "root": str(project.root),
            "recovery_warnings": project.recovery_warnings,
            **project.db.project_status(),
        }

    @project_mutation_locked_sync
    def checkpoint_create(self, root: str | Path, label: str = "用户手动检查点") -> dict[str, Any]:
        project = InkFlowProject(root)
        return CheckpointService(project).create(label=label, reason="manual")

    def checkpoint_list(self, root: str | Path, limit: int = 50) -> dict[str, Any]:
        project = InkFlowProject(root)
        service = CheckpointService(project)
        checkpoints = service.list(limit)
        return {
            "checkpoints": checkpoints,
            "count": len(checkpoints),
            "pending_recovery": service.pending_recovery(),
        }

    def rollback_preview(
        self,
        root: str | Path,
        *,
        checkpoint_id: str | None = None,
        boundary_chapter: int | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        service = CheckpointService(project)
        resolved = service.resolve(checkpoint_id, boundary_chapter)
        return service.preview_restore(resolved)

    @project_mutation_locked_sync
    def rollback_restore(
        self,
        root: str | Path,
        *,
        confirmation_token: str,
        checkpoint_id: str | None = None,
        boundary_chapter: int | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        service = CheckpointService(project)
        resolved = service.resolve(checkpoint_id, boundary_chapter)
        return service.restore(resolved, confirmation_token)

    @project_mutation_locked_sync
    def rollback_recover(self, root: str | Path) -> dict[str, Any]:
        """收拾中断的回退，回到回退前的安全检查点。"""

        project = InkFlowProject(root)
        return CheckpointService(project).recover_interrupted()


def _creative_lens(chapter_no: int) -> str:
    lenses = (
        "让误解先成立一小段，再用人物行动暴露代价",
        "用一个可触碰的物件承载信息变化，避免旁白解释",
        "让旁观者的现实压力迫使人物更早作出选择",
        "把关键消息延迟到人物已经付出小代价之后",
        "给予人物一次局部成功，但让成功改变下一步风险",
        "利用空间限制改变对话权力，而非单纯增加冲突",
        "让关系中的旧承诺与当前目标发生短暂拉扯",
        "从异常日常细节进入冲突，结尾保留可追踪余韵",
    )
    return lenses[(chapter_no - 1) % len(lenses)]


def _render_long_run_progress(project_id: str, state: dict[str, Any]) -> str:
    lines = [
        "# 墨流长跑进度",
        "",
        "> 只记录可展示的工作流决策与结果；不保存供应商原始隐藏推理。",
        "",
        f"- 项目：`{project_id}`",
        f"- 状态：`{state['status']}`",
        f"- 说明：{state.get('detail') or '无'}",
        f"- 终点：{state['target_label']}",
        f"- 已接受正文：{state['accepted_characters']} 有效字符",
        f"- 已接受章节：{state['accepted_chapters']}",
        f"- 最新正史章节：第 {state['latest_accepted_chapter']} 章",
        "",
        "## 工作流事件",
        "",
    ]
    events = state.get("events") or []
    if not events:
        lines.append("- 尚无章节事件。")
    for item in events:
        kind = item.get("event", "event")
        chapter = f"第 {item['chapter_no']} 章" if "chapter_no" in item else ""
        verdict = f" / {item['verdict']}" if "verdict" in item else ""
        version = f" / v{item['version']}" if "version" in item else ""
        chapter_range = (
            f"第 {item['chapter_range'][0]}～{item['chapter_range'][1]} 章"
            if "chapter_range" in item
            else ""
        )
        trace_id = f" / trace `{item['trace_id']}`" if item.get("trace_id") else ""
        lines.append(f"- `{kind}` {chapter or chapter_range}{version}{verdict}{trace_id}".rstrip())
    lines.extend(
        [
            "",
            "> 若状态为 `gate_stop`，修复原因后可再次用同一自然语言目标继续；"
            "协调器会从 SQLite 正史与现有同版本草稿恢复，不会从第一章重写。",
            "",
        ]
    )
    return "\n".join(lines)


def _normalize_planning_brief_references(
    rationale: ArcPlanningBrief,
    *,
    fact_ids: set[str],
    thread_ids: set[str],
) -> tuple[ArcPlanningBrief, list[dict[str, str]]]:
    """Correct only a uniquely obvious one-off reference typo.

    This is deliberately narrower than semantic matching. It helps a weak
    model copy an identifier such as ``confirms`` without dropping the factual
    source gate; ambiguous or low-similarity strings remain invalid.
    """

    valid_refs = fact_ids | thread_ids
    corrections: list[dict[str, str]] = []

    def normalize(reference: str) -> str:
        if reference in valid_refs:
            return reference
        scored = sorted(
            ((SequenceMatcher(None, reference, candidate, autojunk=False).ratio(), candidate) for candidate in valid_refs),
            reverse=True,
        )
        if not scored:
            return reference
        best_score, best_candidate = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= 0.90 and best_score - second_score >= 0.06:
            corrections.append({"from": reference, "to": best_candidate})
            return best_candidate
        return reference

    constraints = [item.model_copy(update={"canon_refs": [normalize(ref) for ref in item.canon_refs]}) for item in rationale.constraints_checked]
    directions = [item.model_copy(update={"canon_refs": [normalize(ref) for ref in item.canon_refs]}) for item in rationale.chosen_direction]
    beats = [item.model_copy(update={"basis_refs": [normalize(ref) for ref in item.basis_refs]}) for item in rationale.beats]
    return rationale.model_copy(update={"constraints_checked": constraints, "chosen_direction": directions, "beats": beats}), corrections


def _validate_planning_brief_grounding(
    rationale: ArcPlanningBrief,
    *,
    fact_ids: set[str],
    thread_ids: set[str],
) -> None:
    """Require public claims and proposals to point back to current canon.

    A planning brief can propose a scene or a reveal, but it may not present
    unsupported material as a checked constraint or an established reason.
    Exact references also give the reader something concrete to inspect.
    """

    valid_refs = fact_ids | thread_ids
    items: list[tuple[str, list[str]]] = [
        *(('已核对约束', item.canon_refs) for item in rationale.constraints_checked),
        *(('推进路径', item.canon_refs) for item in rationale.chosen_direction),
        *(('章节节拍', item.basis_refs) for item in rationale.beats),
    ]
    missing: list[str] = []
    for label, refs in items:
        for reference in refs:
            if reference not in valid_refs:
                missing.append(f"{label}引用了不存在的正史/线索编号 `{reference}`")
    if missing:
        raise ValidationGateError("公开判断单不能把无来源内容写成已核对依据：" + "；".join(missing[:6]))


def _render_planning_brief(payload: dict[str, Any]) -> str:
    """Render a reviewable planning rationale without exposing hidden CoT."""

    rationale = ArcPlanningBrief.model_validate(payload["rationale"])
    chapter_range = payload["chapter_range"]
    start_chapter, end_chapter = int(chapter_range[0]), int(chapter_range[1])
    target_summary = ArcSummary.model_validate(payload["target_summary"])
    lines = [
        f"# 第 {start_chapter}～{end_chapter} 章公开篇章判断单",
        "",
        f"- 判断单编号：`{payload['brief_id']}`",
        f"- 生成时间：{payload.get('created_at', '未知')}",
        f"- 章节范围：第 {start_chapter}～{end_chapter} 章",
        "",
        "> 这是 Writer 交给读者核对的规划说明：列出已核对的依据、选择的推进方向与风险。它不是模型的逐步隐藏思维，也不会修改正文、PLAN.md 或 SQLite 正史。",
        "",
        "## 本篇不可漂移的原定承诺",
        "",
        f"- 原定篇章：{target_summary.title}",
        f"- 必须完成：{target_summary.promise}",
        f"- 原定出口：{target_summary.end_state}",
        "",
        "## 中心问题",
        "",
        rationale.central_question,
        "",
        "## 起点与目标状态",
        "",
        f"- 起点：{rationale.start_state}",
        f"- 本篇完成时：{rationale.desired_end_state}",
        "",
        "## 已核对的约束",
        "",
        *(
            [f"- {item.text}（依据：{', '.join(item.canon_refs)}）" for item in rationale.constraints_checked]
            or ["- 暂无单独列出的约束；展开正式规划前仍会读取正史。"]
        ),
        "",
        "## 选择这条推进路径的理由",
        "",
        *(
            [f"- 建议：{item.text}（出发依据：{', '.join(item.canon_refs)}）" for item in rationale.chosen_direction]
            or ["- 暂无；需要重新生成判断单。"]
        ),
        "",
        "## 章节节拍与钩子",
        "",
        "| 章节 | 预期转折（提案） | 章末钩子（提案） | 出发依据 |",
        "| --- | --- | --- | --- |",
        *[
            f"| 第 {item.chapter_no} 章 | {item.intended_turn} | {item.hook} | {', '.join(item.basis_refs)} |"
            for item in rationale.beats
        ],
        "",
        "## 本篇新增元素账本（最多两项）",
        "",
    ]
    if rationale.new_elements:
        for item in rationale.new_elements:
            lines.extend(
                [
                f"### {item.name}（{item.kind}，第 {item.introduced_chapter} 章）",
                "",
                f"- 用途：{item.purpose}",
                f"- 仍需验证：{item.verification_needed}",
                "",
                ]
            )
    else:
        lines.extend(["- 本篇不新增具名人、地点、物件、组织或事件；优先推进已有正史与线索。", ""])
    lines.extend(
        [
        "## 后续需要继续核对的风险",
        "",
        *([f"- {item}" for item in rationale.risks_to_verify] or ["- 暂无新增风险。"]),
        "",
        ]
    )
    corrections = list(payload.get("source_corrections") or [])
    if corrections:
        lines.extend(["## 来源编号自动更正", ""])
        for item in corrections:
            lines.append(f"- `{item['from']}` → `{item['to']}`（唯一高相似候选；已由程序复核）")
        lines.append("")
    instruction = str(payload.get("instruction") or "").strip()
    if instruction:
        lines.extend(["## 本次用户补充", "", instruction, ""])
    lines.extend(
        [
            "## 下一步",
            "",
            "- 若方向合适，可以自然地说：`按这份判断单展开下一篇章节卡。`",
            "- 若要改方向，直接说想保留、删去或替换哪一项；系统会生成新的判断单或在确认后改写未来章节卡。",
            "",
        ]
    )
    return "\n".join(lines)


def _render_batch_manifest(manifest: dict[str, Any]) -> str:
    lines = [
        "# 墨流批量草稿",
        "",
        f"- 批次：{manifest['batch_id']}",
        f"- 范围：第 {manifest['start_chapter_no']}～{manifest['end_chapter_no']} 章",
        f"- 状态：{manifest['status']}",
        f"- 创建时间：{manifest.get('created_at', '未知')}",
        "",
        "> 本文件是临时批次投影。每章通过审查后会同步临时记忆供后续章节使用；用户明确接收前，正文和记忆均不是 SQLite 正史。",
        "",
        "## 章节结果",
        "",
    ]
    chapters = list(manifest.get("chapters") or [])
    if not chapters:
        lines.append("- 尚无通过即时审查的章节。")
    for entry in chapters:
        lines.extend(
            [
                f"### 第 {entry['chapter_no']} 章 · {entry['title']}",
                "",
                f"- 草稿：[{Path(entry['draft_path']).name}](../{entry['draft_path']})",
                f"- 审查：[{Path(entry['review_path']).name}](../{entry['review_path']})",
                f"- 版本：v{entry['version']}；自动修订：{entry['revision_rounds']} 轮；即时审查：{entry['verdict']}",
                *(
                    [
                        f"- 临时记忆：已同步；事实 {entry.get('memory_facts', 0)} 条；"
                        f"线索 {entry.get('memory_threads', 0)} 条。"
                    ]
                    if entry.get("memory_status") == "provisional"
                    else (
                        ["- 记忆：已随正文提升为正式正史。"]
                        if entry.get("memory_status") == "canon"
                        else ["- 临时记忆：尚未同步或需要重建。"]
                    )
                ),
                *(
                    [f"- 评分：{entry['score_total']}/100；问题数：{entry.get('finding_count', 0)}。"]
                    if entry.get("score_total") is not None
                    else []
                ),
                *([f"- 审查摘要：{entry['review_summary']}"] if entry.get("review_summary") else []),
                "",
            ]
        )
        findings = list(entry.get("findings") or [])
        if findings:
            lines.append("- 即时审查问题：")
            for finding in findings:
                lines.extend(
                    [
                        f"  - [{finding['severity']}] {finding['category']}：{finding['evidence']}",
                        f"    - 原因：{finding['explanation']}",
                        f"    - 依据：{', '.join(finding.get('canon_refs') or []) or '本章正文'}",
                        f"    - 建议：{finding['repair_instruction']}",
                    ]
                )
            lines.append("")
    if manifest.get("stop_reason"):
        lines.extend(["## 停止原因", "", f"- {manifest['stop_reason']}", ""])
    if manifest.get("last_repair"):
        repair = manifest["last_repair"]
        range_value = repair.get("chapter_range") or []
        display_range = "～".join(str(item) for item in range_value) if len(range_value) == 2 else "未记录"
        lines.extend(
            [
                "## 最近一次批次修复",
                "",
                f"- 范围：第 {display_range} 章；结果：{repair.get('status', '进行中')}。",
                *(
                    ["- 为防止后续章节沿用旧连续性，本次修复范围已自动延伸到批次末章。"]
                    if repair.get("range_expanded_for_continuity")
                    else []
                ),
                f"- 开始：{repair.get('started_at', '未知')}；结束：{repair.get('finished_at', '进行中')}。",
                *([f"- 修订方向：{repair['instruction']}"] if repair.get("instruction") else []),
                "",
            ]
        )
    if manifest["status"] == "ready_for_acceptance":
        lines.extend(
            [
                "## 下一步",
                "",
                f"- 若确认本批次，可对 Agent 说：接收批次 {manifest['batch_id']}。",
                "- 若发现跨章问题，可说“按复审结论修复这批草稿”；系统会修订指定临时章节、逐章重审并同步本清单。",
                "",
            ]
        )
    elif manifest["status"] == "accepted":
        lines.extend(["## 已接收", "", "- 本批次已按连续章节顺序写入 SQLite 正史。", ""])
    return "\n".join(lines)


def _render_arc_audit(
    audit_id: str,
    start_chapter_no: int,
    end_chapter_no: int,
    report: ArcAuditReport,
    *,
    source_batch_id: str | None,
    body_repair_recommended: bool,
    body_repair_scope: list[int],
    scorecard: list[ReviewScoreDimension],
) -> str:
    score_total = _score_total(scorecard)
    max_total = sum(item.maximum_score for item in scorecard)
    lines = [
        f"# 第 {start_chapter_no}～{end_chapter_no} 章篇章复审",
        "",
        f"- 复审编号：`{audit_id}`",
        f"- 结论：`{report.verdict}`｜置信度：{report.confidence:.0%}",
        f"- 临时批次来源：`{source_batch_id}`" if source_batch_id else "- 正文来源：均为已接受正史",
        "",
        "> 本报告只由 Reviewer 生成，用于比较实际章节与原规划。它不修改章节、PLAN.md 或 SQLite；若建议调整未来规划，必须由用户明确确认后才会交给 Writer。",
        "",
        "## 总结",
        "",
        report.summary,
        "",
        "## 透明评分（只按下列偏离的证据扣分）",
        "",
        f"- 总分：{score_total}/{max_total}。分数解释篇章对齐度，不取代正文修订和重规划门禁。",
        "",
    ]
    for item in scorecard:
        lines.extend([f"### {item.dimension}：{item.score}/{item.maximum_score}", ""])
        if not item.deductions:
            lines.append("- 未见可引用的扣分证据，因此本项满分。")
        for deduction in item.deductions:
            loss = _REVIEW_SCORE_DEDUCTIONS[deduction.severity]
            lines.extend(
                [
                    f"- 扣 {loss} 分（[{deduction.severity}] {deduction.category}）：{deduction.evidence}",
                    f"  - 原因：{deduction.explanation}",
                    f"  - 依据：{', '.join(deduction.canon_refs) if deduction.canon_refs else '本次复审正文'}",
                ]
            )
        lines.append("")
    lines.extend(
        [
            "## 已兑现的篇章承诺",
            "",
            *([f"- {item}" for item in report.fulfilled_commitments] or ["- 暂无单独记录"]),
            "",
            "## 与规划的偏离",
            "",
        ]
    )
    if not report.deviations:
        lines.append("- 未发现需要影响后续规划的偏离。")
    for index, item in enumerate(report.deviations, 1):
        lines.extend(
            [
                f"### {index}. [{item.severity}] {item.category}",
                "",
                f"- 证据：{item.evidence}",
                f"- 正史引用：{', '.join(item.canon_refs) if item.canon_refs else '无'}",
                f"- 说明：{item.explanation}",
                f"- 证据核验：{item.verification_note or '未记录核验结果'}；语义状态：{item.semantic_status}",
                f"- 建议：{item.repair_instruction}",
                "",
            ]
        )
    lines.extend(
        [
            "## 对后续篇章的影响",
            "",
            *([f"- {item}" for item in report.future_impact] or ["- 后续可按当前规划自然延续。"]),
            "",
            "## 正文修订建议（未执行）",
            "",
            *(
                [
                    f"- 建议修订范围：第 {'、'.join(str(item) for item in body_repair_scope)} 章。",
                    "- 临时批次：修订后须逐章复审，再生成新的临时批次；通过后才可接收。",
                    "- 已接受正史：须先由用户确认分支修订，从最早受影响章重新走 Writer → Reviewer → Memory Keeper，同步后续事实和线索。",
                ]
                if body_repair_recommended
                else ["- 未发现必须修改正文的跨章问题。"]
            ),
            "",
            "## 未来规划建议（未执行）",
            "",
            *([f"- {item}" for item in report.proposed_future_changes] or ["- 无需调整未来规划。"]),
            "",
        ]
    )
    if report.verdict != "aligned" or report.replan_recommended or body_repair_recommended:
        lines.extend(
            [
                "## 等待你的决定",
                "",
                "- 若同意修正临时草稿，请明确说明要修订的章节和采用的因果方向；修订、复审和批次接收会依次执行。",
                "- 若同意修改已接受正文，请先确认“从第 N 章开启分支修订”；系统会预览影响范围，再从最早受影响章重新同步正史。",
                "- 若同意改变后续篇章，请说：`确认根据这次复审重新规划下一篇章`。",
                "",
            ]
        )
    return "\n".join(lines)


_REVIEW_SCORE_RULES: tuple[tuple[str, int, frozenset[str]], ...] = (
    ("正史与认知", 25, frozenset({"timeline", "knowledge", "world"})),
    ("因果与人物", 25, frozenset({"causality", "character"})),
    ("章节卡履约", 20, frozenset({"planning"})),
    ("完整性", 15, frozenset({"format"})),
    ("表达与节奏", 15, frozenset({"pacing", "style", "originality"})),
)

_REVIEW_SCORE_DEDUCTIONS = {"info": 0, "minor": 3, "major": 12, "blocking": 25}


def _merge_claim_decisions(
    decision_sets: list[list[ReviewClaimDecision]],
    expected_indexes: list[int],
) -> list[ReviewClaimDecision]:
    """Require every enabled verifier to support a hard finding."""

    merged: list[ReviewClaimDecision] = []
    for index in expected_indexes:
        decisions = [next((item for item in group if item.finding_index == index), None) for group in decision_sets]
        available = [item for item in decisions if item is not None]
        if len(available) != len(decision_sets) or any(item.verdict == "uncertain" for item in available):
            verdict = "uncertain"
        elif any(item.verdict == "contradicted" for item in available):
            verdict = "contradicted"
        else:
            verdict = "supported"
        merged.append(
            ReviewClaimDecision(
                finding_index=index,
                verdict=verdict,
                confidence=min((item.confidence for item in available), default=0.0),
                reason="；".join(item.reason for item in available) or "核验器未返回结果",
            )
        )
    return merged


def _build_review_scorecard(findings: list[ReviewFinding]) -> list[ReviewScoreDimension]:
    """Make score deductions reproducible from the report's cited findings only.

    The score never decides whether a chapter may become canon.  Gates are
    still the explicit major/blocking findings; scores explain non-blocking
    quality debt in a reader-visible way.
    """

    scorecard: list[ReviewScoreDimension] = []
    for dimension, maximum_score, categories in _REVIEW_SCORE_RULES:
        deductions = [item for item in findings if item.category in categories and item.severity != "info"]
        loss = sum(_REVIEW_SCORE_DEDUCTIONS[item.severity] for item in deductions)
        scorecard.append(
            ReviewScoreDimension(
                dimension=dimension,
                maximum_score=maximum_score,
                score=max(0, maximum_score - loss),
                deductions=deductions,
            )
        )
    return scorecard


def _score_total(scorecard: list[ReviewScoreDimension]) -> int:
    return sum(item.score for item in scorecard)


def _next_arc_summary(bundle: PlanBundle, next_start: int) -> ArcSummary | None:
    for summary in sorted(bundle.current_volume.arcs, key=lambda item: item.chapter_start):
        if summary.chapter_start == next_start:
            return summary
    return None


def _volume_compass(bundle: PlanBundle, volume_no: int) -> VolumeCompass | None:
    return next((item for item in bundle.book.volume_compass if item.volume_no == volume_no), None)


def _lock_next_arc(
    candidate: ArcPlan,
    volume: VolumePlan,
    next_start: int,
    summary: ArcSummary | None,
    *,
    preserve_summary: bool = True,
) -> ArcPlan:
    """Apply deterministic plan boundaries after creative arc generation."""

    data = candidate.model_dump(mode="json")
    if summary is not None and preserve_summary:
        data.update(
            {
                "arc_id": summary.arc_id,
                "volume_no": volume.volume_no,
                "title": summary.title,
                "chapter_start": summary.chapter_start,
                "chapter_end": summary.chapter_end,
                "promise": summary.promise,
                "end_state": summary.end_state,
            }
        )
    else:
        if candidate.chapter_start != next_start:
            raise ValidationGateError(
                f"下一篇章必须从第 {next_start} 章开始，模型给出第 {candidate.chapter_start} 章。"
            )
        if candidate.chapter_end > volume.chapter_end:
            raise ValidationGateError("下一篇章超出当前卷边界。")
        if not 2 <= len(candidate.chapter_cards) <= 16:
            raise ValidationGateError("动态补出的篇章应包含 2～16 张连续章节卡。")
        data["volume_no"] = volume.volume_no
        if summary is not None:
            data.update(
                {
                    "arc_id": summary.arc_id,
                    "chapter_start": summary.chapter_start,
                    "chapter_end": summary.chapter_end,
                }
            )
    try:
        locked = ArcPlan.model_validate(data)
    except Exception as exc:
        raise ValidationGateError(f"下一篇章未遵守已锁定的章节范围：{exc}") from exc
    if locked.chapter_start != next_start:
        raise ValidationGateError(f"下一篇章必须从第 {next_start} 章开始。")
    if locked.arc_id == "" or locked.arc_id == getattr(volume, "arc_id", None):
        raise ValidationGateError("下一篇章缺少稳定且唯一的 arc_id。")
    return locked


def _replace_arc_summary(volume: VolumePlan, arc: ArcPlan) -> VolumePlan:
    """Replace only one not-yet-written arc summary after user-approved replanning."""

    summaries = []
    found = False
    for item in volume.arcs:
        if item.arc_id == arc.arc_id:
            summaries.append(
                ArcSummary(
                    arc_id=arc.arc_id,
                    title=arc.title,
                    chapter_start=arc.chapter_start,
                    chapter_end=arc.chapter_end,
                    promise=arc.promise,
                    end_state=arc.end_state,
                )
            )
            found = True
        else:
            summaries.append(item)
    if not found:
        raise ValidationGateError("未来规划未能定位需要更新的篇章摘要。")
    return VolumePlan.model_validate(volume.model_dump(mode="json") | {"arcs": [item.model_dump(mode="json") for item in summaries]})


def _lock_next_volume(
    proposal: VolumeArcPlan,
    previous: PlanBundle,
    compass: VolumeCompass,
    next_start: int,
) -> tuple[VolumePlan, ArcPlan]:
    expected_end = min(
        previous.book.estimated_chapters,
        next_start + compass.estimated_chapters - 1,
    )
    volume_data = proposal.current_volume.model_dump(mode="json")
    volume_data.update(
        {
            "volume_no": compass.volume_no,
            "title": compass.title,
            "chapter_start": next_start,
            "chapter_end": expected_end,
            "promise": compass.promise,
            "start_state": compass.start_state,
            "end_state": compass.end_state,
        }
    )
    try:
        volume = VolumePlan.model_validate(volume_data)
    except Exception as exc:
        raise ValidationGateError(f"新卷规划未遵守全书罗盘边界：{exc}") from exc

    arcs = sorted(volume.arcs, key=lambda item: item.chapter_start)
    if not arcs or arcs[0].chapter_start != next_start or arcs[-1].chapter_end != expected_end:
        raise ValidationGateError("新卷篇章摘要必须从卷首连续覆盖到卷末。")
    for left, right in zip(arcs, arcs[1:]):
        if right.chapter_start != left.chapter_end + 1:
            raise ValidationGateError("新卷篇章摘要之间存在章节空洞。")
    first_summary = arcs[0]
    arc = _lock_next_arc(proposal.current_arc, volume, next_start, first_summary)
    return volume, arc


def _assert_new_plan_cards(project: InkFlowProject, arc: ArcPlan) -> None:
    collisions = [
        card.chapter_no
        for card in arc.chapter_cards
        if project.db.get_chapter_card(card.chapter_no) is not None
    ]
    if collisions:
        display = "、".join(str(number) for number in collisions[:12])
        raise ValidationGateError(f"滚动规划将覆盖已有章节卡：第 {display} 章；请先回退或显式重规划。")


def _content_char_count(content: str) -> int:
    return len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", content))


def _normalise_chapter_title(chapter_no: int, title: str) -> str:
    """Prevent a model-supplied chapter prefix from being emitted twice."""

    trimmed = title.strip()
    prefix = re.compile(
        rf"^(?:第\s*{chapter_no}\s*章|chapter\s*{chapter_no})\s*[:：—-]?\s*",
        flags=re.IGNORECASE,
    )
    return prefix.sub("", trimmed).strip() or trimmed


def _evidence_in_content(evidence: str, content: str) -> bool:
    """证据必须是连续原文；仅容忍 Unicode、空白与标点表现差异。"""

    if evidence in content:
        return True

    def normalize(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", value)

    normalized_evidence = normalize(evidence)
    return len(normalized_evidence) >= 4 and normalized_evidence in normalize(content)


def _validate_memory_patch_scope(
    patch: MemoryPatch,
    chapter_no: int,
    *,
    known_facts: list[dict[str, Any]] | None = None,
) -> MemoryPatch:
    """Lock a model-produced delta to the current chapter and stable identities."""

    if patch.chapter_no != chapter_no:
        raise ValidationGateError("记忆补丁章节号与当前章节不一致。")
    identities: dict[str, tuple[str, str]] = {}
    for item in known_facts or []:
        fact_id = str(item.get("fact_id") or "")
        if fact_id:
            identities[fact_id] = (str(item.get("subject") or ""), str(item.get("predicate") or ""))
    seen_relations: set[tuple[str, str]] = set()
    normalized_facts: list[FactMutation] = []
    for fact in patch.facts:
        relation = (fact.subject, fact.predicate)
        if relation in seen_relations:
            raise ValidationGateError(
                f"记忆补丁重复修改同一关系：{fact.subject}/{fact.predicate}。"
            )
        seen_relations.add(relation)
        known_identity = identities.get(fact.fact_id)
        if known_identity and known_identity != relation:
            raise ValidationGateError(
                f"fact_id {fact.fact_id} 已属于其他主体关系，不能覆盖。"
            )
        normalized_facts.append(fact.model_copy(update={"valid_from_chapter": chapter_no}))
    return patch.model_copy(update={"facts": normalized_facts})


def _align_patch_evidence(patch: MemoryPatch, content: str) -> tuple[MemoryPatch, list[str]]:
    """把极小抄录偏差锚回正文原片段；歧义或低相似候选一律拒绝。"""

    aligned_ids: list[str] = []
    facts = []
    for fact in patch.facts:
        aligned = _align_evidence_to_content(fact.evidence, content)
        if aligned and aligned != fact.evidence:
            facts.append(fact.model_copy(update={"evidence": aligned}))
            aligned_ids.append(fact.fact_id)
        else:
            facts.append(fact)
    return patch.model_copy(update={"facts": facts}), aligned_ids


def _apply_evidence_repairs(
    patch: MemoryPatch,
    batch: EvidenceRepairBatch,
    requested_ids: list[str],
) -> MemoryPatch:
    requested = set(requested_ids)
    returned = {item.fact_id: item for item in batch.repairs if item.fact_id in requested}
    facts = []
    for fact in patch.facts:
        if fact.fact_id not in requested:
            facts.append(fact)
            continue
        repair = returned.get(fact.fact_id)
        if repair is None:
            facts.append(fact)
        elif repair.drop:
            continue
        else:
            facts.append(fact.model_copy(update={"evidence": repair.evidence}))
    return patch.model_copy(update={"facts": facts})


def _build_evidence_candidates(
    fact: FactMutation,
    content: str,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Retrieve exact source spans; the model may select IDs but cannot copy text."""

    spans: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"[^。！？!?\r\n]+[。！？!?]?", content):
        span = match.group(0).strip()
        if not 4 <= len(span) <= 260 or span in seen:
            continue
        seen.add(span)
        spans.append(span)

    query = " ".join(
        [
            fact.subject,
            fact.predicate,
            json_dumps(fact.value, indent=None),
            fact.evidence,
        ]
    )
    normalized_query = _normalize_for_retrieval(query)
    evidence_norm = _normalize_for_retrieval(fact.evidence)
    subject_norm = _normalize_for_retrieval(fact.subject)
    value_norm = _normalize_for_retrieval(json_dumps(fact.value, indent=None))
    query_bigrams = _character_ngrams(normalized_query, 2)

    ranked: list[tuple[float, int, str]] = []
    for index, span in enumerate(spans):
        normalized_span = _normalize_for_retrieval(span)
        span_bigrams = _character_ngrams(normalized_span, 2)
        overlap = len(query_bigrams & span_bigrams) / max(1, min(len(query_bigrams), len(span_bigrams)))
        score = 0.55 * overlap
        if evidence_norm and normalized_span:
            score += 0.35 * SequenceMatcher(
                None,
                evidence_norm,
                normalized_span,
                autojunk=False,
            ).ratio()
        if subject_norm and subject_norm in normalized_span:
            score += 0.25
        if value_norm and (value_norm in normalized_span or normalized_span in value_norm):
            score += 0.25
        ranked.append((score, -index, span))

    ranked.sort(reverse=True)
    return [
        {"candidate_id": f"c{index:02d}", "exact_text": span}
        for index, (_, _, span) in enumerate(ranked[:limit], 1)
    ]


def _apply_evidence_selections(
    patch: MemoryPatch,
    batch: EvidenceSelectionBatch,
    requested_ids: list[str],
    candidate_groups: dict[str, list[dict[str, str]]],
) -> tuple[MemoryPatch, list[dict[str, str]]]:
    requested = set(requested_ids)
    selections = {item.fact_id: item for item in batch.selections if item.fact_id in requested}
    facts = []
    selected_evidence: list[dict[str, str]] = []
    for fact in patch.facts:
        if fact.fact_id not in requested:
            facts.append(fact)
            continue
        selection = selections.get(fact.fact_id)
        if selection is None:
            facts.append(fact)
            continue
        if selection.drop:
            continue
        candidates = {
            item["candidate_id"]: item["exact_text"]
            for item in candidate_groups.get(fact.fact_id, [])
        }
        evidence = candidates.get(selection.candidate_id or "")
        if evidence is None:
            facts.append(fact)
            continue
        facts.append(fact.model_copy(update={"evidence": evidence}))
        selected_evidence.append(
            {
                "fact_id": fact.fact_id,
                "candidate_id": selection.candidate_id or "",
                "evidence": evidence,
            }
        )
    return patch.model_copy(update={"facts": facts}), selected_evidence


def _normalize_for_retrieval(value: str) -> str:
    return re.sub(
        r"[^\u3400-\u9fffA-Za-z0-9]",
        "",
        unicodedata.normalize("NFKC", value).casefold(),
    )


def _character_ngrams(value: str, width: int) -> set[str]:
    if len(value) < width:
        return {value} if value else set()
    return {value[index : index + width] for index in range(len(value) - width + 1)}


def _align_evidence_to_content(evidence: str, content: str) -> str | None:
    """返回正文中的精确连续片段，只容忍很小且唯一的抄录偏差。"""

    evidence_normalized, _ = _normalize_with_offsets(evidence)
    content_normalized, offsets = _normalize_with_offsets(content)
    if len(evidence_normalized) < 4 or not offsets:
        return None

    exact_at = content_normalized.find(evidence_normalized)
    if exact_at >= 0:
        return content[offsets[exact_at] : offsets[exact_at + len(evidence_normalized) - 1] + 1]
    if len(evidence_normalized) < 8:
        return None

    width = len(evidence_normalized)
    max_edits = min(6, max(1, round(width * 0.08)))
    anchor_width = min(10, max(4, width // 5))
    anchor_offsets = sorted({0, max(0, (width - anchor_width) // 2), width - anchor_width})
    candidate_starts: set[int] = set()
    for anchor_offset in anchor_offsets:
        anchor = evidence_normalized[anchor_offset : anchor_offset + anchor_width]
        found_at = content_normalized.find(anchor)
        while found_at >= 0:
            base = found_at - anchor_offset
            for shift in range(-max_edits, max_edits + 1):
                candidate_starts.add(max(0, base + shift))
            found_at = content_normalized.find(anchor, found_at + 1)

    scored: list[tuple[float, int, int]] = []
    for start in candidate_starts:
        for delta in range(-max_edits, max_edits + 1):
            end = start + width + delta
            if end <= start or end > len(content_normalized):
                continue
            candidate = content_normalized[start:end]
            score = SequenceMatcher(None, evidence_normalized, candidate, autojunk=False).ratio()
            if score >= 0.94:
                scored.append((score, start, end))
    if not scored:
        return None

    scored.sort(reverse=True)
    best_score, best_start, best_end = scored[0]
    for other_score, other_start, _ in scored[1:]:
        if other_score < best_score - 0.01:
            break
        if abs(other_start - best_start) > max_edits:
            return None
    return content[offsets[best_start] : offsets[best_end - 1] + 1]


def _normalize_with_offsets(value: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value):
        for normalized in unicodedata.normalize("NFKC", char).casefold():
            if re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9]", normalized):
                chars.append(normalized)
                offsets.append(index)
    return "".join(chars), offsets


def _separate_open_questions(patch: MemoryPatch) -> tuple[MemoryPatch, list[str]]:
    """未知信息遵循开放世界语义；只让真正互斥的正史版本阻塞提交。"""

    conflicts: list[str] = []
    open_questions: list[str] = []
    for item in patch.unresolved_conflicts:
        if _is_true_memory_conflict(item):
            conflicts.append(item)
        else:
            open_questions.append(item)
    return patch.model_copy(update={"unresolved_conflicts": conflicts}), open_questions


def _is_true_memory_conflict(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    explicit_conflict = (
        "无法同时成立",
        "不能同时成立",
        "互相排斥",
        "互斥版本",
        "两个版本",
        "两种版本",
        "与正史冲突",
        "前文与本章",
        "前文和本章",
    )
    if any(marker in compact for marker in explicit_conflict) or _asserts_hard_conflict(compact):
        return True
    uncertainty = (
        "未知",
        "不知",
        "无法确认",
        "无法知晓",
        "尚未确认",
        "尚未证实",
        "未证实",
        "是否",
        "究竟",
        "可能",
        "推测",
        "猜测",
    )
    if any(marker in compact for marker in uncertainty):
        return False
    # 无法明确分类时保守阻塞，避免静默吞掉真正的正史冲突。
    return True


_TEMPLATE_PHRASES = (
    "恐怖如斯",
    "震惊",
    "下一刻",
    "然而他不知道的是",
    "然而她不知道的是",
    "他不知道的是",
    "她不知道的是",
    "他还没有意识到",
    "她还没有意识到",
    "他并不知道",
    "她并不知道",
)


def _enforce_review_severity(finding: ReviewFinding) -> ReviewFinding:
    """把 Reviewer 已经承认的硬冲突从 minor 提升为不可放行级别。"""

    if finding.severity != "minor":
        return finding
    explanation = f"{finding.explanation}\n{finding.repair_instruction}"
    hard_fact_category = finding.category in {"planning", "world", "knowledge", "timeline", "causality"}
    explicit_user_rule = "用户规则" in explanation or "硬规则" in explanation
    if (hard_fact_category or explicit_user_rule) and _asserts_direct_hard_conflict(explanation):
        return finding.model_copy(update={"severity": "major"})
    return finding


def _asserts_direct_hard_conflict(text: str) -> bool:
    """Only promote a model finding when it names a non-negotiable contradiction.

    A reviewer may reasonably flag an unshown transition, a weak scene location
    or a vague hook.  Those are editorial notes, not automatic gate failures.
    """

    compact = re.sub(r"\s+", "", text)
    direct_markers = (
        "无法同时成立",
        "直接违反",
        "明确违反",
        "人物不可能知道",
        "核心目标未完成",
        "核心决定未发生",
        "不可逆变化缺失",
        "数字前后不一致",
        "日期前后不一致",
        "物件状态前后相反",
    )
    return any(marker in compact for marker in direct_markers)


def _asserts_hard_conflict(text: str) -> bool:
    """Distinguish a claimed contradiction from phrases such as '不矛盾'."""

    compact = re.sub(r"\s+", "", text)
    markers = ("不一致", "冲突", "矛盾", "违反", "违背", "越界", "不符合")
    negated_by_marker = {
        "不一致": ("无不一致", "没有不一致", "未见不一致", "不存在不一致"),
        "冲突": ("不冲突", "无冲突", "没有冲突", "未冲突", "不存在冲突"),
        "矛盾": ("不矛盾", "无矛盾", "没有矛盾", "未见矛盾", "不存在矛盾"),
        "违反": ("不违反", "无违反", "没有违反", "未违反", "不存在违反"),
        "违背": ("不违背", "无违背", "没有违背", "未违背", "不存在违背"),
        "越界": ("不越界", "无越界", "没有越界", "未越界", "不存在越界"),
        "不符合": ("并非不符合", "不是不符合", "并不不符合"),
    }
    for marker in markers:
        if marker not in compact:
            continue
        if any(token in compact for token in negated_by_marker[marker]):
            continue
        return True
    return False


def _deterministic_audit(
    content: str,
    target_words: int,
    user_rules: list[str] | None = None,
) -> tuple[dict[str, Any], list[ReviewFinding]]:
    char_count = _content_char_count(content)
    paragraphs = [item for item in re.split(r"\n\s*\n", content) if item.strip()]
    normalized = [re.sub(r"\s+", "", item) for item in paragraphs]
    duplicate_count = len(normalized) - len(set(normalized))
    template_hits = {phrase: content.count(phrase) for phrase in _TEMPLATE_PHRASES if phrase in content}
    metrics = {
        "content_characters": char_count,
        "target_words": target_words,
        "paragraph_count": len(paragraphs),
        "duplicate_paragraphs": duplicate_count,
        "placeholder_count": len(re.findall(r"TODO|TBD|待补|占位", content, flags=re.IGNORECASE)),
        "template_phrase_hits": template_hits,
    }
    findings: list[ReviewFinding] = []
    if char_count < target_words * 0.6:
        findings.append(
            ReviewFinding(
                category="format",
                severity="blocking",
                evidence=f"有效字符约 {char_count}，目标 {target_words}",
                explanation="正文显著短于章节卡目标，可能是截断或只生成了提纲。",
                repair_instruction="补足完整场景、决定和后果后重新审查。",
            )
        )
    if char_count > target_words * 1.6:
        findings.append(
            ReviewFinding(
                category="pacing",
                severity="major",
                evidence=f"有效字符约 {char_count}，目标 {target_words}",
                explanation="篇幅显著超出目标，可能同时塞入了多章功能。",
                repair_instruction="检查是否应拆章，或删除重复解释和无效过场。",
            )
        )
    if duplicate_count:
        findings.append(
            ReviewFinding(
                category="style",
                severity="major",
                evidence=f"检测到 {duplicate_count} 个完全重复段落",
                explanation="重复段通常来自生成或拼接故障。",
                repair_instruction="删除重复段并检查相邻转场。",
            )
        )
    if metrics["placeholder_count"]:
        findings.append(
            ReviewFinding(
                category="format",
                severity="blocking",
                evidence=f"检测到 {metrics['placeholder_count']} 个占位标记",
                explanation="正式草稿中仍有未完成内容。",
                repair_instruction="补写占位内容后重新审查。",
            )
        )
    rules = user_rules or []
    explicit_template_ban = any(
        any(marker in rule for marker in ("禁止", "不得", "避免"))
        and ("模板" in rule or any(phrase in rule for phrase in _TEMPLATE_PHRASES))
        for rule in rules
    )
    if template_hits:
        evidence = "；".join(f"{phrase}×{count}" for phrase, count in template_hits.items())
        findings.append(
            ReviewFinding(
                category="style",
                severity="major" if explicit_template_ban else "minor",
                evidence=evidence,
                explanation=(
                    "正文命中了用户明确禁用或 Writer 默认避免的模板化上帝视角表达。"
                    if explicit_template_ban
                    else "正文出现常见模板化提示语，需要结合语境判断是否削弱代入感。"
                ),
                repair_instruction="改用当下可见动作、感官或物件变化承载信息，不用旁白提前替角色和读者揭示。",
            )
        )
    return metrics, findings
