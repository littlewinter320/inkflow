from __future__ import annotations

"""Natural-language terminal entry point with strictly bounded workflow routing."""

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Settings, save_user_settings
from .coordinator import Coordinator
from .engine import InkFlowEngine
from .errors import InkFlowError, ValidationGateError
from .project import InkFlowProject
from .prompts import MOBAO_PERSONA
from .project_lock import project_write_lock_sync
from .schemas import ContextPacket, ContextSection, TerminalIntent
from .trace import TraceRecorder
from .utils import atomic_write_text, content_hash, estimate_tokens, json_dumps


TERMINAL_ROUTER_SYSTEM = """
你是墨流的第四个 AI Agent：Coordinator（AI 产品经理与协作管家）。你不是 Writer、Reviewer 或 Memory Keeper。

你的工作是理解和维护用户需求、把自然语言整理成任务单，并映射到一个已存在、不可跳过门禁的工作流。你还要在执行结果中给出一条简短、可操作的下一步建议；建议只是用户可选择的入口，不代表自动授权。你绝不能：
- 写小说正文、续写任何段落、生成审查意见或记忆事实；
- 修改文件、数据库、计划、章节状态或调用工具；
- 选择 force、绕过审查、绕过 Memory Keeper，或把“直接通过/别审查”解释成许可；
- 输出隐藏推理、长篇分析或未被用户要求的行动。

理解原则：
- 下面的说法都是语义示例，不是触发口令。用户可以使用口语、同义词、委婉表达、错别字、省略、反问和最近对话中的指代。
- 先理解用户最终想得到什么，再选择当前可执行的第一步。不要因为用户没说“审查”“复审”“确认”等标准词就判定无法处理。
- 不要把“这个方案似乎可以”“如果这样改会怎样”当成立即执行；“行，就这么做”“照刚才说的改”“没问题，动手吧”都可以表示明确执行。
- “审查通过后就收进正史”“检查没问题的话就接收”是带安全条件的明确执行命令，应使用 review_accept 且 authorization=approved；“如果没问题是不是可以收进去”是在询问可能性，仍是 discuss/proposed。
- 一条消息有多个目标时，requested_outcome 保留完整目标，action 只选择安全的第一步；不要自行拼接任意工具链。

可选 action 的语义固定如下：
- chat：寒暄、闲聊、情绪表达、与创作无关的日常提问（如“晚上好”“我该怎么称呼你”），或用户明显只想聊天。conversation_reply 以墨宝口吻直接、完整地回应，不进入工作流、不说跑题、不催促干活。
- ideate：用户要从零构思、想要灵感、点子或天马行空的提案，且当前没有正史或规划依据可查（如“帮我想几个故事点子”“给我一个全新的方向”）。这会交给 Writer 的灵感分身产出创意提案，不写正文、不入正史、不做证据核验；用户明确要求出点子时 authorization=approved。
- status：只读取项目状态。
- plan：让 Writer 生成四级规划。
- settings_update：用户明确要求调整墨流设置时使用；只能修改白名单设置，执行后必须逐项告诉用户改了什么和当前值。仅询问“怎么设置”时不要执行。
- plan_preview：集中查看已经存在的连续章节卡，不调用 Writer、不改规划。chapter_no 是起始章，end_chapter_no 是结束章；范围完全按用户给出的章节号处理，不人为截断。用户说“把第7到30章规划一起给我看”时使用此动作。
- outline：让 Writer 生成指定范围的独立大纲。它只写入 planning/outlines/，不覆盖 PLAN.md、不生成草稿、不进入正史。
- plan_brief：当前篇章已全部进入正史后，让 Writer 先生成下一篇章的公开判断单。它只展示依据、约束、取舍、章节节拍和待核对风险，写入 planning/，不改 PLAN.md、正文或 SQLite；绝不要求或输出隐藏思维链。
- arc_audit：让 Reviewer 对 chapter_no 到 end_chapter_no 的实际正文与章节卡、篇章承诺作跨章复审；只写报告，不改正文、规划或正史。若范围包含尚未接收的临时批次，batch_id 可逐字复制用户提供的编号；未提供时由宿主只在唯一匹配批次存在时采用它。
- plan_next_arc：只有当前篇章全部进入正史后，才让 Writer 基于实际结果细化紧邻的下一篇章。operation_instruction 非空表示用户要调整未来规划；只要整句语义明确要求现在执行，authorization=approved 且 plan_change_confirmed=true，不限定必须出现“确认/同意”两个词。
- continue_run：按 Writer→Reviewer→必要时定点修订→重审→Memory Keeper 的门禁循环续写。必须提取“已接受正文目标字符数”或“结束章节号”之一；结束章节号写入 end_chapter_no，例如“连续完成第8到10章”应设为 10。不可同时填写两种终点。
- batch_draft：生成一段临时批次草稿。chapter_no 是起始章，end_chapter_no 是结束章；每章写完后立即由 Reviewer 审查，若有 patch 最多由独立 Writer 调用修订两轮。不得调用 Memory Keeper 或进入正史。
- batch_draft_accept：仅由已保存的“批次确认一次/自动验收”策略升级而来；先完整执行 batch_draft，只有整个连续批次都处于 ready_for_acceptance 时才调用 batch_accept。用户说“只要草稿/不要验收”时绝不能使用。
- batch_repair：按明确的因果方向修订一个已有临时批次中的一段章节。先让 Writer 修订每一章，再让 Reviewer 审查当前版本；若新审查仍有硬问题，最多再做 max_revision_rounds 轮修订。修订结果和审查分数必须回写同一批次清单；不得调用 Memory Keeper 或进入正史。batch_id 可逐字复制用户提供的编号；未提供时宿主只在唯一可修复批次存在时补齐。chapter_no/end_chapter_no 未提供时，宿主只在已确定批次时采用该批次的完整范围。
- batch_accept：用户明确接收一个已完成批次。batch_id 必须逐字复制用户输入；只能把通过审查、连续衔接的批次章节依次交给 Memory Keeper。
- checkpoint_list：列出可用检查点/回退点。
- checkpoint_create：为当前 SQLite 与托管 Markdown 创建手动检查点。
- rollback_preview：只预览回退影响并返回确认码，不改变项目。优先提取 checkpoint_id；若用户说“回到第 N 章生成前”，chapter_no 应设为 N-1，表示回到上一章已接受后的边界。
- rollback_restore：使用用户明确给出的 checkpoint_id/章节边界和 confirmation_token 执行分支式恢复；缺任一关键参数都不得猜测。
- write_review：让 Writer 写指定章节，然后让 Reviewer 审查；绝不接受。
- write_review_accept：仅由已保存的自动验收策略升级而来；Writer 写作后由 Reviewer 审查，只有当前版本 `pass` 才调用 Memory Keeper。用户要求先看草稿或不要验收时绝不能使用。
- revise_review：让 Writer 依同版本审查修订指定章节，然后让 Reviewer 重审；绝不接受。
- review_accept：先让 Reviewer 审查；仅当 verdict=pass 时才让 Memory Keeper 接受，且 force 永远为 false。
- revise_review_accept：修订→重审→仅 pass 时接受，途中任一门禁失败必须停止。
- accept：只尝试接受现有、同版本、已经 pass 的草稿；force 永远为 false。
- help：用户在询问如何使用。
- exit：用户明确结束会话。

多方案与第二意见规则：用户要求多个 Writer 或多个 Reviewer 时，不能增加正式 Agent、不能让多个角色自由改同一篇正文。将“多个 Writer”理解为同一 Writer 在同一 Context Packet 和硬约束下产出互不重叠的候选方向、章节卡或局部替换提案，等待用户选择后再进入单一正式草稿；将“多个 Reviewer”理解为对同一正文哈希的独立证据意见，任何分歧必须以结构化异议交给 Coordinator 汇总。若当前工作流没有相应的候选/复审入口，action=discuss，说明需要先确定范围与选择标准，不得假装已并行执行。

章节工作流 action 必须给出 chapter_no。若用户未明确章节号且不能从其文字可靠确定，仍返回最接近的 action，chapter_no 设为 null；宿主会安全地要求补充，不可猜测。
operation_instruction 用简洁中文保留用户对正文的硬约束，但不写正文；visible_reason 只给可展示的、简短的路由理由，不泄露逐步思考。
requested_outcome 用一句中文保留用户最终想看到的完整结果，即使本轮只能执行第一步。
alternative_action 只在两种理解确实都合理时填写；不要为了凑字段虚构候选。
confidence 表示语义识别把握：high=目标与动作清楚，medium=大意清楚但有轻微省略，low=两种以上理解都合理或关键指代不明。它不能作为绕过门禁的许可。
authorization 表示当前话语行为：none=询问/讨论，proposed=提出可能方案或条件句，approved=明确要求现在执行或明确接受最近待确认方案。
missing_fields 列出无法从本条消息、最近对话和项目状态可靠确定的必要字段；clarification_question 兼容只写一句最小澄清问题，无需让用户重述全部需求。
更适合用选项回答时填写 clarification_questions：每问都要有简短 header、question、why_it_matters、single/multiple 选择方式和 2～4 个差异明确的 options；每个选项说明会怎样影响成品，最多标一个 recommended。不要自行添加“其他”，宿主会固定把自由填写放在最后。
若用户明确要求“先问我”“向我提问”或“帮我补全想法”，action=discuss，clarification_questions 应主动提出 1～3 个最有价值、容易回答的问题，conversation_reply 只说明为什么此时值得问；不要执行小说工作流。
当存在可选但会明显改变成品的未知项时，可以填写 clarification_question。若当前消息是在回答上一轮问题，或用户明确说“直接开始/不用再问”，不要重复追问。
checkpoint_id 与 confirmation_token 必须逐字复制用户输入；用户未提供时设为 null。
batch_id 必须逐字复制用户输入；用户未提供时设为 null。
target_characters 只表示“已接受正文”的有效字符目标，不把草稿、审查或日志计入。end_chapter_no 对 plan_preview、arc_audit 表示结束章，对 continue_run 表示长跑结束章。plan_change_confirmed 根据整句与最近待确认事项的明确执行语义判断，不做关键词匹配。
轻量会话规则：
- 闲聊优先 chat：寒暄、情绪表达和与创作无关的日常问题用 action=chat 正常回应，像一个懂分寸的朋友；只有讨论创作话题（题材、人物、节奏、方案）才是 discuss；只有明确要具体点子或提案才是 ideate。不要把闲聊判定为跑题，也不要一味追求效率。
- 用户在讨论题材、人物、节奏、选择、方案或表达意见，而没有明确要求执行时，action 必须是 discuss。conversation_reply 用自然中文复述已理解的重点、给出一个建议和下一步确认问句；不调用 Writer、Reviewer 或 Memory Keeper。
- 用户用任何明确肯定表达接受最近方案时，可结合本次 Context Packet 中的最近对话，将其路由为相应既有 action；不要要求固定口令。
- 用户只要求“写草稿”“先写出来我看看”“不要审查”时，action=write_draft；只让 Writer 写草稿，绝不自动审查或接收。
- 用户只要求“审查第 N 章”时，action=review；只让 Reviewer 审查，绝不接收。只有明确说“审查通过后入正史/验收并接收”才可使用 review_accept。
- 用户说“复审第 X 到第 Y 章并和规划对比”时，action=arc_audit；它不能暗中触发重规划。用户说“根据复审重规划”但没有明确确认时，保持 plan_change_confirmed=false。
- 用户说“按复审结论修正这批草稿”“修第 9 到第 10 章并保留当前批次”时，action=batch_repair；它只修订临时批次并逐章重审，不等于接收，也不重写已接受正史。
- 用户只要求“按意见修改第 N 章，先给我看”时，action=revise_draft；只让 Writer 修订，不自动重审。
- 除非用户明确进入连续长跑或要求入正史，不能把普通写作升级为写作—审查—接收全链路。
""".strip()

# 墨宝口吻统一注入：路由与回复都由 Coordinator 负责，但对用户说话时始终以墨宝的身份。
TERMINAL_ROUTER_SYSTEM = TERMINAL_ROUTER_SYSTEM + "\n\n墨宝口吻（适用于你写出的所有 conversation_reply）：\n" + MOBAO_PERSONA


_SENSITIVE_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_NO_OPTIONAL_QUESTION_PATTERN = re.compile(r"(?:直接|马上|立刻)(?:开始|执行|写|做)|(?:不用|不要|别|不必)再?问")
_CHAPTER_RANGE_PATTERN = re.compile(
    # Accept both "第6章到第10章" and the shorter "第6到第10章".
    # The chapter marker before the separator is optional because both forms
    # are common in natural Chinese requests.
    r"第?\s*(\d+)\s*章?\s*(?:到|至|~|～|—|-)\s*第?\s*(\d+)\s*章"
)
_CHAPTER_NUMBER_PATTERN = re.compile(r"第\s*(\d+)\s*章")
_BEFORE_CHAPTER_PATTERN = re.compile(r"(?:生成|写|开始写)?\s*第\s*(\d+)\s*章\s*(?:之前|以前|前)")
_CHAPTER_ACTIONS = {
    "write_draft",
    "write_review",
    "write_review_accept",
    "review",
    "revise_draft",
    "revise_review",
    "review_accept",
    "revise_review_accept",
    "accept",
}
_READ_ONLY_ACTIONS = {
    "status",
    "help",
    "plan_preview",
    "outline",
    "plan_brief",
    "checkpoint_list",
    "rollback_preview",
    "exit",
}
_CANON_MUTATION_ACTIONS = {
    "plan_next_arc",
    "continue_run",
    "batch_accept",
    "batch_draft_accept",
    "write_review_accept",
    "review_accept",
    "revise_review_accept",
    "accept",
}
_SETTINGS_MUTATION_ACTIONS = {"settings_update"}
_RESTORE_ACTIONS = {"rollback_restore"}

_FIELD_LABELS = {
    "chapter_no": "章节号",
    "end_chapter_no": "结束章节号",
    "chapter_range": "起止章节",
    "batch_id": "批次编号",
    "checkpoint_id": "检查点编号或章节边界",
    "confirmation_token": "回退确认码",
    "target": "结束章节号或正史字符目标",
    "settings_patch": "要修改的设置项和值",
}

_SETTING_LABELS = {
    "model": "默认模型",
    "reasoning_effort": "思考强度",
    "inquiry_frequency": "主动询问频率",
    "hook_strategy": "章节结尾钩子",
    "chapter_length_tolerance": "章节长度容错",
    "acceptance_confirmation_mode": "验收确认方式",
    "context_budget_mode": "上下文预算方式",
    "context_soft_tokens": "常用上下文预算",
    "context_hard_tokens": "最大上下文上限",
    "voice_auto_read": "自动朗读",
    "voice_output_enabled": "语音输出",
    "voice_input_enabled": "语音输入",
    "agent_generation": "Agent 生成参数",
}

_SETTING_VALUE_LABELS = {
    "reasoning_effort": {"low": "低", "medium": "中", "high": "高", "max": "最高"},
    "inquiry_frequency": {"low": "少问", "medium": "适中", "high": "多问", "ultra": "频繁"},
    "hook_strategy": {
        "most_chapters": "大多数章节",
        "key_chapters": "重点章节",
        "natural_afterglow": "自然余味",
    },
    "acceptance_confirmation_mode": {
        "per_chapter": "逐章确认",
        "batch_once": "批次确认一次",
        "auto_after_review": "审查通过后自动验收",
    },
    "context_budget_mode": {"unified": "统一预算", "custom": "按 Agent 分开"},
}


class TerminalSession:
    """Turn one natural-language message into at most one safe engine workflow."""

    def __init__(self, engine: InkFlowEngine):
        self.engine = engine

    async def handle(
        self,
        root: str | Path,
        message: str,
        *,
        consume_steering: Callable[[], Awaitable[list[str]]] | None = None,
        emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        project = InkFlowProject(root)
        text = message.strip()
        if not text:
            return {
                "session": "墨流终端会话",
                "gate": "请输入一条自然语言请求；输入“帮助”可查看支持的工作流。",
            }
        if _SENSITIVE_PATTERN.search(text):
            return {
                "session": "墨流终端会话",
                "gate": "检测到疑似 API Key。为避免写入 Trace 或项目文件，已拒绝处理该输入。",
            }
        if text.lower() in {"/exit", "exit", "退出", "结束"}:
            return {"session": "墨流终端会话", "ended": True, "message": "会话已结束。"}

        shortcut = self._quick_response(project, text)
        if shortcut is not None:
            reply = (
                "已返回墨流使用说明（无需模型调用）。"
                if shortcut.get("help")
                else "已返回当前项目状态（无需模型调用）。"
            )
            self._append_dialogue_entry(project, text, reply, "本地快速回答")
            return shortcut

        trace = TraceRecorder(project.root, "terminal-session", self.engine.settings.trace_level)
        try:
            packet = self._build_packet(project, text)
            packet_path = trace.run_dir / "context-packet.md"
            atomic_write_text(packet_path, packet.to_markdown())
            trace.record(
                "session.context",
                "completed",
                "已构建唯一终端会话 Context Packet",
                metadata={"estimated_tokens": packet.estimated_tokens, "sections": [item.key for item in packet.sections]},
            )
            if emit:
                await emit({"type": "coordinator.model.started", "stage": "controller.routing", "role": "coordinator", "model": self.engine.settings.model, "summary": "Coordinator 正在理解请求并选择安全工作流"})
            trace.record_model_started(
                "controller.routing",
                model=self.engine.settings.model,
                agent_role="coordinator",
                max_tokens=900,
                timeout_seconds=self.engine.settings.request_timeout_seconds,
                thinking=False,
            )
            route_result = await self.engine.provider.generate_json(
                system_prompt=TERMINAL_ROUTER_SYSTEM,
                user_prompt=packet.to_model_prompt(),
                output_model=TerminalIntent,
                effort="low",
                max_tokens=900,
                thinking=False,
                agent_role="coordinator",
            )
            steering_messages = await consume_steering() if consume_steering else []
            if steering_messages:
                text = self._apply_steering(text, steering_messages)
                packet = self._build_packet(project, text)
                atomic_write_text(packet_path, packet.to_markdown())
                trace.record(
                    "session.steer",
                    "completed",
                    "已在安全节点接收用户引导，并重新判断后续处理。",
                    metadata={"guidance_count": len(steering_messages)},
                )
                if emit:
                    await emit({"type": "coordinator.model.started", "stage": "controller.routing.steer", "role": "coordinator", "model": self.engine.settings.model, "summary": "Coordinator 正在读取新增指导并重新选择工作流"})
                trace.record_model_started(
                    "controller.routing.steer",
                    model=self.engine.settings.model,
                    agent_role="coordinator",
                    max_tokens=900,
                    timeout_seconds=self.engine.settings.request_timeout_seconds,
                    thinking=False,
                )
                route_result = await self.engine.provider.generate_json(
                    system_prompt=TERMINAL_ROUTER_SYSTEM,
                    user_prompt=packet.to_model_prompt(),
                    output_model=TerminalIntent,
                    effort="low",
                    max_tokens=900,
                    thinking=False,
                    agent_role="coordinator",
                )
            raw_intent = self._coerce_settings_intent(text, route_result.data)
            intent, routing_response = self._resolve_intent(project, text, raw_intent)
            intent = self._apply_acceptance_policy(text, intent)
            ticket, dispatch_plan = Coordinator(project).compile(intent)
            trace.record(
                "session.route",
                "completed",
                f"自然语言请求已路由为 {intent.action}",
                details=intent.visible_reason,
                metadata={
                    "action": intent.action,
                    "alternative_action": intent.alternative_action,
                    "confidence": intent.confidence,
                    "authorization": intent.authorization,
                    "missing_fields": intent.missing_fields,
                    "chapter_no": intent.chapter_no,
                    "model": route_result.model,
                    "response_id": route_result.response_id,
                    "usage": route_result.usage,
                },
            )
            if intent.authorization == "approved" and dispatch_plan.steps:
                with project_write_lock_sync(project.root):
                    for step in dispatch_plan.steps:
                        if step.role == "engine":
                            continue
                        project.db.append_collaboration_message(
                            thread_id=ticket.ticket_id,
                            run_id=trace.run_id,
                            sender_role="coordinator",
                            recipient_role=step.role,
                            message_type="task_assignment",
                            chapter_no=ticket.chapter_no,
                            chapter_version=ticket.chapter_version,
                            context_packet_id=content_hash(packet.to_model_prompt()),
                            claim=ticket.objective,
                            evidence_refs=ticket.input_sources,
                            requested_response=step.required_output,
                        )
            if routing_response is not None:
                response = routing_response
            elif intent.action == "discuss":
                response: dict[str, Any] = {
                    "reply": intent.conversation_reply.strip()
                    or "我已理解你的想法。请确认要继续讨论，还是让我按这个方向执行下一步。"
                }
                if intent.clarification_questions or intent.clarification_question.strip():
                    response.update(
                        {
                            "needs_clarification": True,
                            "questions": self._question_cards(intent),
                        }
                    )
            elif intent.action == "chat":
                # 闲聊不进工作流：路由模型已按墨宝口吻写出完整回复，这里只兜底空回复。
                response: dict[str, Any] = {
                    "reply": intent.conversation_reply.strip()
                    or "我在呢。想聊点什么，或者继续你的故事都可以。"
                }
            elif intent.action == "ideate":
                # Writer 灵感分身：零依据构思不做证据核验，产出提案不入正史。
                if intent.authorization != "approved":
                    response = {
                        "gate": "想让我出点子的话，直接说就行，例如“帮我想几个故事点子”。"
                    }
                else:
                    response = await self.engine.brainstorm(project.root, text, packet)
            else:
                response = await self._dispatch(project.root, intent)
            if intent.authorization == "approved" and dispatch_plan.steps:
                with project_write_lock_sync(project.root):
                    project.db.resolve_collaboration_thread(ticket.ticket_id)
                    if response.get("gate") or response.get("needs_clarification"):
                        project.db.append_collaboration_message(
                            thread_id=ticket.ticket_id,
                            run_id=trace.run_id,
                            sender_role="coordinator",
                            recipient_role="user",
                            message_type="risk",
                            chapter_no=ticket.chapter_no,
                            chapter_version=ticket.chapter_version,
                            context_packet_id=content_hash(packet.to_model_prompt()),
                            claim=str(response.get("gate") or "任务仍需要用户补充信息。"),
                            evidence_refs=ticket.input_sources,
                            requested_response="请补充阻塞信息或确认新的处理方向。",
                            status="escalated",
                        )
            self._append_dialogue(project, text, intent, response)
            trace.record("session.dispatch", "completed", "受限工作流已返回结果")
            trace.finish(summary="终端自然语言请求处理完成")
            payload = {
                "session": {
                    "route": intent.action,
                    "requested_outcome": intent.requested_outcome,
                    "alternative_action": intent.alternative_action,
                    "confidence": intent.confidence,
                    "authorization": intent.authorization,
                    "chapter_no": intent.chapter_no,
                    "visible_reason": intent.visible_reason,
                    "task_ticket": ticket.model_dump(mode="json"),
                    "dispatch_plan": dispatch_plan.model_dump(mode="json"),
                    "role_capabilities": Coordinator.capabilities(),
                    "trace_id": trace.run_id,
                    "trace_path": str(trace.trace_path),
                },
                **response,
            }
            recommendation = self._recommend_next_step(intent, response)
            if recommendation:
                payload["next_step"] = recommendation
            return payload
        except asyncio.CancelledError:
            trace.record("session", "cancelled", "当前请求被停止；没有提交新的正文或正史")
            trace.finish(status="cancelled", summary="终端任务已停止，已有项目内容保留")
            raise
        except InkFlowError as exc:
            trace.record("session", "failed", "终端工作流被配置或门禁阻止", str(exc))
            trace.finish(status="failed", summary="终端自然语言请求未改变正史")
            return {
                "session": {"trace_id": trace.run_id, "trace_path": str(trace.trace_path)},
                "gate": str(exc),
            }
        except Exception as exc:
            trace.record("session", "failed", "终端会话发生未预期错误", str(exc))
            trace.finish(status="failed", summary="终端自然语言请求失败")
            raise

    def _quick_response(self, project: InkFlowProject, text: str) -> dict[str, Any] | None:
        """Avoid a paid model routing call for unambiguous read-only requests."""

        normalized = re.sub(r"\s+", "", text).lower()
        if normalized in {"状态", "项目状态", "当前状态", "当前进度", "进度"}:
            return {
                "session": {"route": "status", "cost": "无需模型调用"},
                "result": self.engine.status(project.root),
                "next_step": {
                    "label": "让 Coordinator 推荐下一步",
                    "reason": "状态已经读完；你可以让 Coordinator 根据当前进度选择最合适的动作。",
                    "prompt": "根据当前项目状态，给我推荐下一步并说明原因",
                },
            }
        if normalized in {"帮助", "help", "/help", "怎么用"}:
            return {
                "session": {"route": "help", "cost": "无需模型调用"},
                "help": [
                    "直接按平常说话表达目标，不需要记住固定关键词；有歧义时我只追问缺少的部分。",
                    "先讨论：例如“我想把男主改成双主角，先分析利弊”。",
                    "讨论后可以说“行，就照刚才那个方向写第 3 章，先别审查”。",
                    "需要验收时再说“审查第 3 章”；只有明确要求入正史才会接收。",
                    "批量草稿完成后可集中查看；接收批次后才会写入正史。",
                    "篇章结束时可说“复审第 1 到第 10 章并和规划对比”；它只产出报告。",
                ],
            }
        return None

    @staticmethod
    def _apply_steering(original_message: str, steering_messages: list[str]) -> str:
        """Append confirmed user guidance only at a model-call boundary.

        Providers used by InkFlow do not accept new input in the middle of one
        request.  Re-routing at this boundary gives the Coordinator the same
        practical steering semantics without exposing private model reasoning or
        letting a late message bypass the Novel Engine's authorization gates.
        """
        guidance = "\n\n".join(f"- {item.strip()}" for item in steering_messages if item.strip())
        if not guidance:
            return original_message
        return (
            f"{original_message}\n\n"
            "# 用户的运行中引导\n"
            "以下内容是用户在本轮运行中确认的方向修正。它约束后续判断和动作；"
            "不得恢复已经不适用的未完成步骤，也不得因此绕过验收、正史或其他权限门禁。\n"
            f"{guidance}"
        )

    def _resolve_intent(
        self,
        project: InkFlowProject,
        message: str,
        intent: TerminalIntent,
    ) -> tuple[TerminalIntent, dict[str, Any] | None]:
        """Resolve only unique project references, then apply execution policy.

        The model may interpret broad natural language, but the host remains the
        authority for missing parameters, ambiguity and mutations of canon.
        """

        updates: dict[str, Any] = {}
        relevant_fields = self._relevant_missing_fields(intent.action)
        missing = {item for item in intent.missing_fields if item in relevant_fields}
        action = intent.action

        if action == "settings_update":
            patch = self._sanitize_settings_patch(intent.settings_patch or self._parse_settings_patch(message))
            updates["settings_patch"] = patch
            if not patch:
                missing.add("settings_patch")
            else:
                missing.discard("settings_patch")

        explicit_start, explicit_end = self._explicit_chapter_range(message)
        if action in _CHAPTER_ACTIONS and explicit_start is not None:
            updates["chapter_no"] = explicit_start
            missing.discard("chapter_no")
        if action in {"plan", "plan_preview", "outline", "arc_audit", "batch_draft", "batch_draft_accept", "batch_repair"} and explicit_start is not None:
            updates["chapter_no"] = explicit_start
            updates["end_chapter_no"] = explicit_end or explicit_start
            if action != "plan":
                missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})
            elif explicit_end is None:
                updates.pop("chapter_no", None)
                updates.pop("end_chapter_no", None)
        if action == "continue_run" and explicit_start is not None:
            updates["end_chapter_no"] = explicit_end or explicit_start
            missing.difference_update({"end_chapter_no", "target"})
        if action in {"rollback_preview", "rollback_restore"} and not intent.checkpoint_id:
            before_match = _BEFORE_CHAPTER_PATTERN.search(message)
            if before_match and int(before_match.group(1)) > 1:
                updates["chapter_no"] = int(before_match.group(1)) - 1
                missing.discard("checkpoint_id")

        if action in _CHAPTER_ACTIONS and intent.chapter_no is None and "chapter_no" not in updates:
            chapter_no = self._unique_chapter_reference(project, action)
            if chapter_no is not None:
                updates["chapter_no"] = chapter_no
                missing.discard("chapter_no")

        if action in {"plan_preview", "outline", "arc_audit"} and (
            intent.chapter_no is None or intent.end_chapter_no is None
        ):
            bundle = project.db.get_current_plan_bundle()
            if bundle is not None:
                updates.setdefault("chapter_no", bundle.current_arc.chapter_start)
                updates.setdefault("end_chapter_no", bundle.current_arc.chapter_end)
                missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})

        if action == "batch_accept" and not intent.batch_id:
            ready_batches = self._ready_batch_ids(project)
            if len(ready_batches) == 1:
                updates["batch_id"] = ready_batches[0]
                missing.discard("batch_id")

        if action == "batch_repair":
            if not intent.batch_id:
                repairable_batches = self._repairable_batch_ids(project)
                if len(repairable_batches) == 1:
                    updates["batch_id"] = repairable_batches[0]
                    missing.discard("batch_id")
            candidate_batch = str(updates.get("batch_id") or intent.batch_id or "")
            if candidate_batch and (intent.chapter_no is None or intent.end_chapter_no is None):
                batch_range = self._batch_chapter_range(project, candidate_batch)
                if batch_range is not None:
                    updates.setdefault("chapter_no", batch_range[0])
                    updates.setdefault("end_chapter_no", batch_range[1])
                    missing.difference_update({"chapter_no", "end_chapter_no", "chapter_range"})

        resolved = intent.model_copy(update={**updates, "missing_fields": sorted(missing)})
        required_missing = self._required_missing(resolved)
        if required_missing:
            resolved = resolved.model_copy(
                update={"missing_fields": sorted(set(resolved.missing_fields) | required_missing)}
            )

        if (
            resolved.action == "plan_next_arc"
            and resolved.authorization == "approved"
            and resolved.operation_instruction.strip()
        ):
            resolved = resolved.model_copy(update={"plan_change_confirmed": True})

        if resolved.action in {"discuss", "chat"}:
            # 闲聊与讨论都不要求执行授权，直接由墨宝口吻回复。
            return resolved, None

        if resolved.missing_fields:
            return resolved, self._clarification_response(
                resolved,
                fallback="我已经理解大致目标，但还缺少执行所需的信息。",
            )

        inquiry_frequency = self.engine.settings.inquiry_frequency
        if resolved.confidence == "low" and inquiry_frequency != "low":
            return resolved, self._clarification_response(
                resolved,
                fallback="这句话有两种以上合理理解，我不想替你猜错。",
            )

        if resolved.alternative_action:
            return resolved, self._clarification_response(
                resolved,
                fallback="我能想到两种都合理的处理方式，需要你确认其中一种。",
            )

        should_optional_ask = (
            resolved.action not in _READ_ONLY_ACTIONS
            and bool(resolved.clarification_question.strip())
            and not _NO_OPTIONAL_QUESTION_PATTERN.search(message)
            and (
                inquiry_frequency == "ultra"
                or (inquiry_frequency == "high" and resolved.confidence == "medium")
            )
        )
        if should_optional_ask:
            return resolved, self._clarification_response(
                resolved,
                fallback="为了让这次创作更贴近你的想法，我先确认一个会明显影响结果的选择。",
            )

        if resolved.action not in _READ_ONLY_ACTIONS and resolved.authorization != "approved":
            risk_note = (
                "这一步会进入或改变正史/未来规划，需要你明确表示现在执行。"
                if resolved.action in _CANON_MUTATION_ACTIONS
                else "我把这句话理解为讨论或建议，还没有直接开始执行。"
            )
            if resolved.action in _RESTORE_ACTIONS:
                risk_note = "回退会改变当前项目状态，必须先预览影响并明确确认。"
            return resolved, self._clarification_response(resolved, fallback=risk_note)

        return resolved, None

    def _apply_acceptance_policy(self, message: str, intent: TerminalIntent) -> TerminalIntent:
        """把用户保存的自动验收授权应用到含 Reviewer 的固定工作流。"""

        mode = self.engine.settings.acceptance_confirmation_mode
        intent = intent.model_copy(update={"acceptance_confirmation_mode": mode})
        # A draft-only request is a hard per-request constraint.  It must win
        # over the saved batch/auto acceptance preference, including fuzzy
        # natural-language wording such as “先给我看成品，暂时别收进正史”。
        if re.search(
            r"(?:只|仅|先)(?:要|做|生成|给我看)?(?:草稿|审查|初稿)|"
            r"(?:不要|暂不|先别|不必)(?:验收|接收|入正史|写入正史)|"
            r"草稿(?:即可|就好|先看)",
            message,
        ):
            downgrade = {
                "batch_draft_accept": "batch_draft",
                "write_review_accept": "write_review",
                "revise_review_accept": "revise_review",
                "review_accept": "review",
            }.get(intent.action)
            return intent.model_copy(
                update={
                    "action": downgrade or intent.action,
                    "authorization_source": "current_request",
                }
            )
        if mode == "batch_once" and intent.action == "batch_draft" and intent.authorization == "approved":
            return intent.model_copy(
                update={"action": "batch_draft_accept", "authorization_source": "batch_preapproval"}
            )
        if mode != "auto_after_review":
            return intent
        upgraded = {
            "review": "review_accept",
            "write_review": "write_review_accept",
            "revise_review": "revise_review_accept",
            "batch_draft": "batch_draft_accept",
        }.get(intent.action)
        if not upgraded or intent.authorization != "approved":
            return intent
        return intent.model_copy(
            update={"action": upgraded, "authorization_source": "settings_auto_accept"}
        )

    @classmethod
    def _coerce_settings_intent(cls, message: str, intent: TerminalIntent) -> TerminalIntent:
        """Turn a clear settings request into a bounded Coordinator route.

        The language can be loose, but only values understood by the host are
        accepted.  Questions such as “怎么调” stay in discussion mode.
        """

        patch = cls._sanitize_settings_patch(cls._parse_settings_patch(message))
        setting_words = re.search(r"(?:设置|参数|温度|top[\s_-]*p|上下文|容错|自动朗读|语音输出|语音输入|模型|思考|推理)", message, re.I)
        imperative = re.search(r"(?:设置|调整|调到|调成|调为|调低|调高|降低|提高|改成|改为|换成|开启|关闭|设为|默认)", message)
        if patch and setting_words and imperative and intent.action in {"chat", "discuss", "settings_update"}:
            return intent.model_copy(
                update={
                    "action": "settings_update",
                    "settings_patch": patch,
                    "authorization": "approved",
                    "authorization_source": "current_request",
                    "requested_outcome": "按用户当前说法调整墨流设置",
                    "visible_reason": "Coordinator 已识别到明确设置变更，并会在完成后列出每一项变化。",
                    "confidence": "high",
                }
            )
        return intent

    @staticmethod
    def _sanitize_settings_patch(patch: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {}
        allowed = {
            "model", "reasoning_effort", "inquiry_frequency", "hook_strategy",
            "chapter_length_tolerance", "acceptance_confirmation_mode",
            "context_budget_mode", "context_soft_tokens", "context_hard_tokens",
            "voice_auto_read", "voice_output_enabled", "voice_input_enabled",
            "agent_generation",
        }
        clean: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in allowed:
                continue
            try:
                if key == "chapter_length_tolerance":
                    number = float(value)
                    if not math.isfinite(number):
                        continue
                    if number > 1:
                        number /= 100
                    if 0.10 <= number <= 1.00:
                        clean[key] = round(number, 4)
                elif key in {"context_soft_tokens", "context_hard_tokens"}:
                    number = int(value)
                    if 16_000 <= number <= 2_000_000:
                        clean[key] = number
                elif key in {"voice_auto_read", "voice_output_enabled", "voice_input_enabled"}:
                    if isinstance(value, str):
                        normalized = value.strip().casefold()
                        if normalized in {"1", "true", "yes", "on", "open", "开启", "打开", "是"}:
                            clean[key] = True
                        elif normalized in {"0", "false", "no", "off", "close", "关闭", "取消", "否"}:
                            clean[key] = False
                    elif isinstance(value, (bool, int, float)):
                        clean[key] = bool(value)
                elif key == "reasoning_effort" and str(value).casefold() in {"low", "medium", "high", "max"}:
                    clean[key] = str(value).casefold()
                elif key == "inquiry_frequency" and str(value).casefold() in {"low", "medium", "high", "ultra"}:
                    clean[key] = str(value).casefold()
                elif key == "hook_strategy" and str(value) in {"most_chapters", "key_chapters", "natural_afterglow"}:
                    clean[key] = str(value)
                elif key == "acceptance_confirmation_mode" and str(value) in {"per_chapter", "batch_once", "auto_after_review"}:
                    clean[key] = str(value)
                elif key == "context_budget_mode" and str(value) in {"unified", "custom"}:
                    clean[key] = str(value)
                elif key == "agent_generation" and isinstance(value, dict):
                    generation: dict[str, dict[str, float | int | None]] = {}
                    for role, raw in value.items():
                        if role not in {"coordinator", "writer", "reviewer", "memory_keeper"} or not isinstance(raw, dict):
                            continue
                        role_values: dict[str, float | int | None] = {}
                        if "temperature" in raw:
                            temperature = float(raw["temperature"])
                            if math.isfinite(temperature) and 0 <= temperature <= 2:
                                role_values["temperature"] = temperature
                        if "top_p" in raw:
                            top_p = float(raw["top_p"])
                            if math.isfinite(top_p) and 0 < top_p <= 1:
                                role_values["top_p"] = top_p
                        if "top_k" in raw and raw["top_k"] not in (None, "", 0, "0"):
                            top_k = int(raw["top_k"])
                            if 1 <= top_k <= 200:
                                role_values["top_k"] = top_k
                        if role_values:
                            generation[role] = role_values
                    if generation:
                        clean[key] = generation
                elif key == "model" and str(value).strip() and len(str(value).strip()) <= 160:
                    clean[key] = str(value).strip()
            except (TypeError, ValueError, OverflowError):
                # One malformed field must not discard otherwise valid changes.
                continue
        return clean

    @staticmethod
    def _parse_settings_patch(message: str) -> dict[str, Any]:
        compact = re.sub(r"\s+", "", message.lower())
        patch: dict[str, Any] = {}
        tolerance = re.search(r"(?:字数|章节)?(?:容错|误差)(?:率)?(?:改为|设为|调整为)?(\d{1,3})%", compact)
        if tolerance:
            patch["chapter_length_tolerance"] = int(tolerance.group(1)) / 100
        if re.search(r"(?:逐章|每章).*确认|每个章节.*确认", compact):
            patch["acceptance_confirmation_mode"] = "per_chapter"
        elif re.search(r"(?:批量|批次).*确认|确认一次", compact):
            patch["acceptance_confirmation_mode"] = "batch_once"
        elif re.search(r"自动(?:验收|接收)|通过.*自动", compact):
            patch["acceptance_confirmation_mode"] = "auto_after_review"
        if re.search(r"(?:少问|少打断|不要总问)", compact):
            patch["inquiry_frequency"] = "low"
        elif re.search(r"(?:多问|经常确认|主动问)", compact):
            patch["inquiry_frequency"] = "high"
        if re.search(r"各个模型.*独立|自定义.*上下文|分别调整", compact):
            patch["context_budget_mode"] = "custom"
        elif re.search(r"统一.*上下文|综合.*上下文", compact):
            patch["context_budget_mode"] = "unified"
        if re.search(r"关闭.*自动朗读|不要自动播报|取消自动朗读", compact):
            patch["voice_auto_read"] = False
        elif re.search(r"开启.*自动朗读|打开.*自动播报|自动朗读", compact):
            patch["voice_auto_read"] = True
        if re.search(r"关闭.*语音输出|不要.*语音输出", compact):
            patch["voice_output_enabled"] = False
        elif re.search(r"开启.*语音输出|打开.*语音输出", compact):
            patch["voice_output_enabled"] = True
        if re.search(r"关闭.*语音输入|不要.*语音输入", compact):
            patch["voice_input_enabled"] = False
        elif re.search(r"开启.*语音输入|打开.*语音输入", compact):
            patch["voice_input_enabled"] = True
        effort = re.search(r"(?:思考|推理)(?:强度)?(?:改为|设为|调到|调成|调整为|调|设|改)?(低|中|高|最高|max|low|medium|high)", compact)
        if effort:
            patch["reasoning_effort"] = {"低": "low", "中": "medium", "高": "high", "最高": "max"}.get(effort.group(1), effort.group(1))
        temperature = re.search(r"(?:(writer|写作|coordinator|协调|reviewer|审查|memorykeeper|记忆)[^\d]{0,8})?(?:temperature|温度)[^\d]{0,8}(0(?:\.\d+)?|1(?:\.\d+)?|2(?:\.0)?)", compact)
        top_p = re.search(r"(?:(writer|写作|coordinator|协调|reviewer|审查|memorykeeper|记忆)[^\d]{0,8})?(?:top[-_ ]?p|topp)[^\d]{0,8}(0?\.\d+|1(?:\.0)?)", compact)
        if temperature or top_p:
            role = "writer"
            role_hint = (temperature.group(1) if temperature else None) or (top_p.group(1) if top_p else None) or ""
            if re.search(r"coordinator|协调", role_hint):
                role = "coordinator"
            elif re.search(r"reviewer|审查", role_hint):
                role = "reviewer"
            elif re.search(r"memorykeeper|记忆", role_hint):
                role = "memory_keeper"
            generation: dict[str, Any] = {role: {}}
            if temperature:
                generation[role]["temperature"] = float(temperature.group(2))
            if top_p:
                generation[role]["top_p"] = float(top_p.group(2))
            patch["agent_generation"] = generation
        model = re.search(r"(?:模型|默认模型)(?:改为|设为|换成|使用)([a-z0-9_.:/-]{2,160})", compact)
        if model:
            patch["model"] = model.group(1)
        return patch

    @staticmethod
    def _explicit_chapter_range(message: str) -> tuple[int | None, int | None]:
        # A natural request can quote an audit scope before naming its real
        # target, e.g. "根据第 1 到 10 章复审，修第 9 到第 10 章".  The
        # final explicit range is normally the operative object, while the
        # earlier one is supporting evidence.
        range_matches = list(_CHAPTER_RANGE_PATTERN.finditer(message))
        if range_matches:
            range_match = range_matches[-1]
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start >= 1 and end >= start:
                return start, end
        number_match = _CHAPTER_NUMBER_PATTERN.search(message)
        if number_match:
            chapter_no = int(number_match.group(1))
            if chapter_no >= 1:
                return chapter_no, None
        return None, None

    def _unique_chapter_reference(self, project: InkFlowProject, action: str) -> int | None:
        drafts = project.db.chapter_numbers_by_status("draft")
        if action in {"review", "revise_draft", "revise_review", "review_accept", "revise_review_accept", "accept"}:
            return drafts[0] if len(drafts) == 1 else None
        if action in {"write_draft", "write_review"}:
            next_chapter = project.db.latest_accepted_chapter_no() + 1
            return next_chapter if project.db.get_chapter_card(next_chapter) is not None else None
        return None

    @staticmethod
    def _ready_batch_ids(project: InkFlowProject) -> list[str]:
        folder = project.internal / "batches"
        result: list[str] = []
        for path in folder.glob("batch-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("status") == "ready_for_acceptance" and value.get("batch_id") == path.stem:
                result.append(path.stem)
        return sorted(result)

    @staticmethod
    def _repairable_batch_ids(project: InkFlowProject) -> list[str]:
        folder = project.internal / "batches"
        result: list[str] = []
        for path in folder.glob("batch-*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            repairable_status = value.get("status") == "ready_for_acceptance" or (
                value.get("status") == "needs_revision" and isinstance(value.get("last_repair"), dict)
            )
            if repairable_status and value.get("batch_id") == path.stem:
                result.append(path.stem)
        return sorted(result)

    @staticmethod
    def _batch_chapter_range(project: InkFlowProject, batch_id: str) -> tuple[int, int] | None:
        path = project.internal / "batches" / f"{batch_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            start = int(value["start_chapter_no"])
            end = int(value["end_chapter_no"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        return (start, end) if start >= 1 and end >= start else None

    @staticmethod
    def _required_missing(intent: TerminalIntent) -> set[str]:
        missing: set[str] = set()
        if intent.action in _CHAPTER_ACTIONS and intent.chapter_no is None:
            missing.add("chapter_no")
        if intent.action in {"plan_preview", "outline", "arc_audit", "batch_draft", "batch_draft_accept", "batch_repair"}:
            if intent.chapter_no is None or intent.end_chapter_no is None:
                missing.add("chapter_range")
        if intent.action in {"batch_accept", "batch_repair"} and not intent.batch_id:
            missing.add("batch_id")
        if intent.action == "continue_run":
            if intent.target_characters is None and intent.end_chapter_no is None:
                missing.add("target")
        if intent.action == "settings_update" and not intent.settings_patch:
            missing.add("settings_patch")
        if intent.action in {"rollback_preview", "rollback_restore"}:
            if not intent.checkpoint_id and intent.chapter_no is None:
                missing.add("checkpoint_id")
        if intent.action == "rollback_restore" and not intent.confirmation_token:
            missing.add("confirmation_token")
        return missing

    @staticmethod
    def _relevant_missing_fields(action: str) -> set[str]:
        fields: set[str] = set()
        if action in _CHAPTER_ACTIONS:
            fields.add("chapter_no")
        if action in {"plan_preview", "outline", "arc_audit", "batch_draft", "batch_draft_accept", "batch_repair"}:
            fields.update({"chapter_no", "end_chapter_no", "chapter_range"})
        if action in {"batch_accept", "batch_repair"}:
            fields.add("batch_id")
        if action == "continue_run":
            fields.update({"end_chapter_no", "target_characters", "target"})
        if action == "settings_update":
            fields.add("settings_patch")
        if action in {"rollback_preview", "rollback_restore"}:
            fields.update({"checkpoint_id", "chapter_no"})
        if action == "rollback_restore":
            fields.add("confirmation_token")
        return fields

    @staticmethod
    def _clarification_response(intent: TerminalIntent, *, fallback: str) -> dict[str, Any]:
        labels = [_FIELD_LABELS.get(item, item) for item in intent.missing_fields]
        question = intent.clarification_question.strip()
        if not question and labels:
            question = f"请补充{'、'.join(labels)}，其余要求不用重说。"
        if not question:
            outcome = intent.requested_outcome.strip() or intent.visible_reason
            question = f"我理解你想要“{outcome}”。你是要我现在执行，还是先继续讨论？"
        return {
            "reply": f"{fallback} {question}".strip(),
            "needs_clarification": True,
            "questions": TerminalSession._question_cards(intent, fallback_question=question),
            "routing": {
                "candidate_action": intent.action,
                "alternative_action": intent.alternative_action,
                "missing_fields": labels,
                "confidence": intent.confidence,
                "authorization": intent.authorization,
            },
        }

    @staticmethod
    def _question_cards(intent: TerminalIntent, *, fallback_question: str = "") -> list[dict[str, Any]]:
        """Normalize model questions and guarantee a final free-text choice."""

        cards: list[dict[str, Any]] = []
        for index, question in enumerate(intent.clarification_questions[:3], start=1):
            options = [
                {
                    "id": f"q{index}-o{option_index}",
                    "label": option.label.strip(),
                    "description": option.description.strip(),
                    "recommended": option.recommended,
                    "kind": "choice",
                }
                for option_index, option in enumerate(question.options[:4], start=1)
                if option.label.strip() and "其他" not in option.label
            ]
            options.append(
                {
                    "id": f"q{index}-other",
                    "label": "其他",
                    "description": "用自己的话填写；不必套用上面的选项。",
                    "recommended": False,
                    "kind": "other",
                }
            )
            cards.append(
                {
                    "id": f"question-{index}",
                    "header": question.header.strip() or "需要确认",
                    "question": question.question.strip(),
                    "why_it_matters": question.why_it_matters.strip(),
                    "selection": question.selection,
                    "options": options,
                }
            )
        if cards:
            return cards

        question = fallback_question or intent.clarification_question.strip()
        if not question:
            outcome = intent.requested_outcome.strip() or intent.visible_reason
            question = f"我理解你想要“{outcome}”。你是要我现在执行，还是先继续讨论？"
        options: list[dict[str, Any]] = []
        if intent.alternative_action:
            options.extend(
                [
                    {
                        "id": "q1-primary",
                        "label": "按主要理解处理",
                        "description": f"采用当前识别到的 {intent.action} 路径。",
                        "recommended": intent.confidence != "low",
                        "kind": "choice",
                    },
                    {
                        "id": "q1-alternative",
                        "label": "采用另一种理解",
                        "description": f"改用 {intent.alternative_action} 路径。",
                        "recommended": False,
                        "kind": "choice",
                    },
                ]
            )
        elif not intent.missing_fields:
            options.extend(
                [
                    {
                        "id": "q1-run",
                        "label": "现在执行",
                        "description": "按刚才已经说明的目标开始，不再追加可选偏好。",
                        "recommended": intent.confidence == "high",
                        "kind": "choice",
                    },
                    {
                        "id": "q1-discuss",
                        "label": "先继续讨论",
                        "description": "先澄清方向，本轮不修改正文、规划或正史。",
                        "recommended": intent.confidence != "high",
                        "kind": "choice",
                    },
                ]
            )
        options.append(
            {
                "id": "q1-other",
                "label": "其他",
                "description": "补充章节号、范围、偏好，或用自己的话回答。",
                "recommended": False,
                "kind": "other",
            }
        )
        return [
            {
                "id": "question-1",
                "header": "需要确认",
                "question": question,
                "why_it_matters": "你的回答会决定下一步工作范围；未回答前不会执行这次任务。",
                "selection": "single",
                "options": options,
            }
        ]

    def _build_packet(self, project: InkFlowProject, message: str) -> ContextPacket:
        brief = project.db.get_brief()
        status = self.engine.status(project.root)
        status["latest_accepted_chapter"] = project.db.latest_accepted_chapter_no()
        status["draft_chapters"] = project.db.chapter_numbers_by_status("draft")
        status["ready_batches"] = self._ready_batch_ids(project)
        bundle = project.db.get_current_plan_bundle()
        if bundle is not None:
            status["current_arc"] = {
                "arc_id": bundle.current_arc.arc_id,
                "chapter_start": bundle.current_arc.chapter_start,
                "chapter_end": bundle.current_arc.chapter_end,
            }
        # Coordinator ???????????????????????
        # ????????????????????????????
        # ????????????????? token?
        queue_reconciliation = project.db.reconcile_collaboration_queue()
        status["queue_reconciliation"] = queue_reconciliation
        active_messages = project.db.list_collaboration_messages(active_only=True, limit=8)
        collaboration_digest = [
            {
                "message_id": item.get("message_id"),
                "thread_id": item.get("thread_id"),
                "message_type": item.get("message_type"),
                "chapter_no": item.get("chapter_no"),
                "chapter_version": item.get("chapter_version"),
                "sender_role": item.get("sender_role"),
                "recipient_role": item.get("recipient_role"),
                "status": item.get("status"),
                "claim": str(item.get("claim") or "")[:240],
                "evidence_refs": list(item.get("evidence_refs") or [])[:8],
                "created_at": item.get("created_at"),
            }
            for item in active_messages
        ]
        policy = {
            "roles": {
                "Coordinator": "理解需求、维护交流、拆解与派工；不写正文、不审批、不提交正史",
                "写作 Agent": "仅规划、写作、修订",
                "审查 Agent": "仅独立审查",
                "记忆 Agent": "仅从已接受正文提取并提交正史",
            },
            "hard_gates": [
                "不可直接修改 .inkflow/inkflow.db",
                "修订后必须重审",
                "只有 Reviewer pass 才可进入接受",
                "accept 永远使用 force=false",
            ],
            "settings_boundary": "用户明确要求时，可以通过 Novel Engine 修改白名单设置；执行后必须返回旧值、新值和下一步建议。",
            "inquiry_frequency": self.engine.settings.inquiry_frequency,
            "inquiry_policy": {
                "low": "只追问缺少的执行条件和真实歧义",
                "medium": "低把握时追问",
                "high": "中等把握且存在重要创作分岔时追问",
                "ultra": "存在会明显改变成品的未知项就先追问；用户要求直接开始时不重复问",
            },
        }
        sections = [
            ContextSection(key="A", title="当前用户自然语言请求", content=message, hard=True),
            ContextSection(
                key="B",
                title="项目契约与用户规则",
                content=json_dumps(
                    {
                        "书名": brief.title,
                        "题材": brief.genre,
                        "单章目标字数": brief.target_chapter_words,
                        "用户规则": brief.user_rules,
                    }
                ),
                hard=True,
            ),
            ContextSection(key="C", title="当前项目状态", content=json_dumps(status), hard=True),
            ContextSection(key="D", title="不可绕过的会话边界", content=json_dumps(policy), hard=True),
            ContextSection(
                key="F",
                title="最近用户讨论与待确认事项",
                content=self._recent_dialogue(project, limit=3, char_limit=6_000)
                or "这是一次新的会话，尚无待确认事项。",
            ),
            ContextSection(
                key="F1",
                title="结构化 Agent 分歧与待用户回答问题",
                content=json_dumps(collaboration_digest),
                source_ids=[item["message_id"] for item in collaboration_digest if item.get("message_id")],
            ),
            ContextSection(
                key="E",
                title="输出契约",
                content=(
                    "只输出系统规定的会话路由结构；一次只选择一个受限工作流；"
                    "同时给出完整目标、识别把握、授权状态、缺失字段和必要澄清；"
                    "不输出小说正文或隐藏推理。"
                ),
                hard=True,
            ),
        ]
        packet = ContextPacket(
            project_id=project.project_id,
            chapter_no=1,
            task="理解用户的自然语言讨论、确认或执行请求，并路由到既有墨流工作流",
            sections=sections,
            estimated_tokens=estimate_tokens("\n".join(item.content for item in sections)),
        )
        soft_limit, hard_limit = self.engine.settings.context_budget_for("coordinator")
        if packet.estimated_tokens > soft_limit:
            for key in ("F1", "F"):
                section = next((item for item in packet.sections if item.key == key), None)
                if section and len(section.content) > 1_000:
                    section.content = section.content[-max(1_000, len(section.content) // 2):]
                    packet.warnings.append(f"Coordinator 上下文接近预算，已缩短 {section.title}；项目硬状态未动。")
                    packet.estimated_tokens = estimate_tokens("\n".join(item.content for item in sections))
                    if packet.estimated_tokens <= soft_limit:
                        break
        if packet.estimated_tokens > hard_limit:
            raise ValidationGateError("Coordinator 的硬上下文超过自定义上限，请提高该 Agent 的最大上下文预算。")
        return packet

    @staticmethod
    def _dialogue_path(project: InkFlowProject) -> Path:
        return project.root / "DIALOGUE.md"

    def _recent_dialogue(self, project: InkFlowProject, limit: int = 6, char_limit: int = 12_000) -> str:
        path = self._dialogue_path(project)
        if not path.is_file():
            return ""
        try:
            content = path.read_text(encoding="utf-8")
            if content.startswith("# 墨流对话记录\n\n"):
                content = content.removeprefix("# 墨流对话记录\n\n")
            entries = [item.strip() for item in content.split("\n\n---\n\n") if item.strip()]
        except OSError:
            return ""
        return "\n\n---\n\n".join(entries[-limit:])[-max(1_000, int(char_limit)):]

    @classmethod
    def history(cls, root: str | Path, limit: int = 100) -> list[dict[str, str]]:
        """Return user-visible dialogue only; never expose provider reasoning or trace data."""

        project = InkFlowProject(root)
        path = cls._dialogue_path(project)
        if not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if content.startswith("# 墨流对话记录\n\n"):
            content = content.removeprefix("# 墨流对话记录\n\n")
        raw_entries = [item.strip() for item in content.split("\n\n---\n\n") if item.strip()]
        parsed: list[dict[str, str]] = []
        for index, entry in enumerate(raw_entries[-max(1, min(limit, 500)) :]):
            match = re.match(
                r"## 用户\n\n(?P<user>.*?)\n\n## (?:Coordinator|墨流会话主控)\n\n(?P<assistant>.*?)(?:\n\n> (?P<note>.*))?$",
                entry,
                flags=re.DOTALL,
            )
            if not match:
                continue
            note = (match.group("note") or "").strip()
            recorded_at = ""
            action_note = note
            if note.startswith("记录时间：") and " · " in note:
                recorded_at, action_note = note.removeprefix("记录时间：").split(" · ", 1)
            parsed.append(
                {
                    "id": f"dialogue-{len(raw_entries) - len(raw_entries[-max(1, min(limit, 500)) :]) + index + 1}",
                    "user": match.group("user").strip(),
                    "assistant": match.group("assistant").strip(),
                    "action_note": action_note,
                    "recorded_at": recorded_at,
                }
            )
        return parsed

    def _append_dialogue(
        self,
        project: InkFlowProject,
        user_message: str,
        intent: TerminalIntent,
        response: dict[str, Any],
    ) -> None:
        """Persist only user-visible conversation, never provider reasoning or keys."""

        reply = str(
            response.get("reply")
            or response.get("gate")
            or response.get("message")
            or response.get("summary")
            or intent.visible_reason
        ).strip()
        if response.get("needs_clarification"):
            action_note = f"等待补充或确认：{intent.action}"
        else:
            action_note = "继续讨论" if intent.action == "discuss" else f"已路由：{intent.action}"
        self._append_dialogue_entry(project, user_message, reply, action_note)

    def _append_dialogue_entry(
        self,
        project: InkFlowProject,
        user_message: str,
        reply: str,
        action_note: str,
        *,
        force: bool = False,
    ) -> bool:
        """按用户设置追加一条可见对话记录；返回本次是否真正写入文件。"""

        settings = Settings.from_env(project.root)
        if not force:
            if settings.dialogue_history_mode == "manual":
                return False
            interval = max(1, settings.dialogue_history_interval)
            if interval > 1:
                # 自动保存按“每 N 轮”计数；计数放在项目元数据里，不新增文件。
                pending = int(project.db.get_metadata("dialogue_turns_since_save", 0) or 0) + 1
                if pending < interval:
                    project.db.set_metadata("dialogue_turns_since_save", pending)
                    return False
        project.db.set_metadata("dialogue_turns_since_save", 0)
        self._write_dialogue_entry(
            project,
            user_message,
            reply,
            action_note,
            settings.dialogue_history_limit,
        )
        return True

    @classmethod
    def save_manual_entry(
        cls,
        root: str | Path,
        user_message: str,
        reply: str,
        action_note: str = "用户手动保存",
    ) -> dict[str, Any]:
        """用户明确要求时保存一条记录，不受“被动/主动”设置限制。"""

        project = InkFlowProject(root)
        message = user_message.strip()
        answer = reply.strip()
        if not message or not answer:
            raise ValueError("没有可保存的对话内容。")
        settings = Settings.from_env(project.root)
        cls._write_dialogue_entry(
            project, message, answer, action_note, settings.dialogue_history_limit
        )
        project.db.set_metadata("dialogue_turns_since_save", 0)
        return {
            "saved": True,
            "entries": len(cls._dialogue_entries(project)),
            "limit": settings.dialogue_history_limit,
        }

    @staticmethod
    def _dialogue_entries(project: InkFlowProject) -> list[str]:
        """读取 DIALOGUE.md 的原始条目；不截断长度。"""

        path = TerminalSession._dialogue_path(project)
        if not path.is_file():
            return []
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return []
        if content.startswith("# 墨流对话记录\n\n"):
            content = content.removeprefix("# 墨流对话记录\n\n")
        return [item.strip() for item in content.split("\n\n---\n\n") if item.strip()]

    @staticmethod
    def _write_dialogue_entry(
        project: InkFlowProject,
        user_message: str,
        reply: str,
        action_note: str,
        limit: int,
    ) -> None:
        """写入一条记录，并按保留上限裁剪旧记录。"""

        recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry = (
            f"## 用户\n\n{user_message}\n\n## Coordinator\n\n{reply}"
            f"\n\n> 记录时间：{recorded_at} · {action_note}"
        )
        entries = TerminalSession._dialogue_entries(project)
        entries.append(entry)
        keep = max(1, limit)
        atomic_write_text(
            TerminalSession._dialogue_path(project),
            "# 墨流对话记录\n\n" + "\n\n---\n\n".join(entries[-keep:]) + "\n",
        )

    async def _dispatch(self, root: Path, intent: TerminalIntent) -> dict[str, Any]:
        if (
            intent.action not in _READ_ONLY_ACTIONS
            and intent.action != "discuss"
            and intent.action != "chat"
            and intent.authorization != "approved"
        ):
            return {
                "gate": (
                    "这条消息还没有明确要求现在执行。你可以用自己的说法表示开始，"
                    "例如“行，就照刚才说的做”；不需要固定口令。"
                )
            }
        if intent.action == "exit":
            return {"ended": True, "message": "会话已结束。"}
        if intent.action == "help":
            return {
                "help": [
                    "不需要背命令：例如“帮我瞅瞅现在写到哪儿了”也能查看状态",
                    "为第 1 章生成规划",
                    "当前篇章完成后，规划下一个篇章",
                    "继续按完整门禁写作，直到已接受正文达到 10 万汉字",
                    "按章节卡写第 1 章并审查",
                    "生成第 11 到第 30 章的独立大纲（只写入 planning/outlines）",
                    "根据审查意见修订第 1 章，复审；仅通过才接受",
                    "审查第 1 章，通过后入正史",
                    "只尝试接受第 1 章，不要 force",
                    "查看当前项目状态",
                    "列出当前所有回退点",
                    "为当前状态创建一个名为‘第一篇章完成’的检查点",
                    "预览回退到生成第 2 章之前会影响哪些文件",
                    "使用预览返回的确认码确认回退",
                ]
            }
        if intent.action == "status":
            return {"result": self.engine.status(root)}
        if intent.action == "settings_update":
            patch = self._sanitize_settings_patch(intent.settings_patch)
            if not patch:
                return {"gate": "我还没有识别出可执行的设置项。请说清要改哪项，例如“把章节长度容错改为 30%”。"}
            before = Settings.from_env(root)
            saved = save_user_settings(patch)
            after = Settings.from_env(root)
            changes = {
                key: {"before": getattr(before, key, None), "after": getattr(after, key, saved.get(key))}
                for key in patch
                if getattr(before, key, None) != getattr(after, key, saved.get(key))
            }
            unchanged = [
                key
                for key in patch
                if key not in changes
            ]
            change_summary = [
                self._setting_change_summary(key, item["before"], item["after"])
                for key, item in changes.items()
            ]
            if change_summary:
                summary = "已修改设置：" + "；".join(change_summary)
            elif unchanged:
                summary = "设置没有变化：" + "、".join(self._setting_label(key) for key in unchanged)
            else:
                summary = "没有识别到可保存的设置变化。"
            return {
                "settings_updated": changes,
                "settings_unchanged": unchanged,
                "settings_change_summary": change_summary,
                "summary": summary,
                "next_action": "新的设置会用于下一次任务；你可以继续说要做什么，Coordinator 会按当前设置安排。",
            }
        if intent.action == "voice_clone_script":
            return {"result": await self.engine.generate_voice_clone_script(root)}
        if intent.action == "plan":
            return {
                "steps": [
                    {
                        "step": "writer.plan",
                        "result": await self.engine.generate_plan(
                            root,
                            instruction=intent.operation_instruction,
                            chapter_range=(intent.chapter_no, intent.end_chapter_no)
                            if intent.chapter_no is not None and intent.end_chapter_no is not None
                            else None,
                        ),
                    }
                ]
            }
        if intent.action == "plan_preview":
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "批次规划预览需要明确起止章节，例如“查看第 7～11 章规划”。"}
            return {
                "result": self.engine.preview_plan_range(root, intent.chapter_no, intent.end_chapter_no),
                "next_action": "你可以一次确认全部，或只指出需要调整的章节号；当前操作没有修改规划。",
            }
        if intent.action == "outline":
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "独立大纲需要明确起止章节，例如“生成第 11 到第 30 章大纲”。"}
            return {
                "result": await self.engine.generate_outline(
                    root,
                    intent.chapter_no,
                    intent.end_chapter_no,
                    instruction=intent.operation_instruction,
                ),
                "next_action": "大纲已经单独保存；你可以点开文件查看，再决定从哪一章开始写草稿。",
            }
        if intent.action == "plan_next_arc":
            if (
                intent.operation_instruction.strip()
                and not intent.plan_change_confirmed
                and intent.authorization != "approved"
            ):
                return {
                    "gate": (
                        "这会改变未来篇章规划。请先阅读篇章复审报告；若同意，"
                        "用任何清楚的说法告诉我现在执行即可，不需要固定口令。"
                    )
                }
            return {
                "steps": [
                    {
                        "step": "writer.plan.advance",
                        "result": await self.engine.advance_plan(root, instruction=intent.operation_instruction),
                    }
                ]
            }
        if intent.action == "plan_brief":
            return {
                "steps": [
                    {
                        "step": "writer.plan.brief",
                        "result": await self.engine.preview_next_arc(
                            root,
                            instruction=intent.operation_instruction,
                        ),
                    }
                ]
            }
        if intent.action == "arc_audit":
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "篇章复审需要明确起止章节，例如“复审第 1 到第 10 章并和规划对比”。"}
            return {
                "result": await self.engine.audit_range(
                    root,
                    intent.chapter_no,
                    intent.end_chapter_no,
                    batch_id=intent.batch_id,
                )
            }
        if intent.action in {"batch_draft", "batch_draft_accept"}:
            if intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "批量草稿必须明确起止章节，例如“生成第 9 到第 10 章的批量草稿”。"}
            drafted = await self.engine.draft_batch(
                root,
                intent.chapter_no,
                intent.end_chapter_no,
                instruction=intent.operation_instruction,
                max_revision_rounds=intent.max_revision_rounds,
            )
            if intent.action == "batch_draft_accept" and drafted.get("status") == "ready_for_acceptance":
                accepted = await self.engine.accept_batch(root, str(drafted["batch_id"]))
                return {
                    "result": accepted,
                    "batch_draft": drafted,
                    "authorization_source": intent.authorization_source,
                }
            return {"result": drafted}
        if intent.action == "batch_repair":
            if not intent.batch_id or intent.chapter_no is None or intent.end_chapter_no is None:
                return {"gate": "修复临时批次需要可确定的批次和章节范围；请说明批次编号或第 N 到第 M 章。"}
            return {
                "result": await self.engine.repair_batch(
                    root,
                    intent.batch_id,
                    start_chapter_no=intent.chapter_no,
                    end_chapter_no=intent.end_chapter_no,
                    instruction=intent.operation_instruction,
                    max_additional_revision_rounds=intent.max_revision_rounds,
                )
            }
        if intent.action == "batch_accept":
            if not intent.batch_id:
                return {"gate": "接收批次必须提供批次编号，例如“接收批次 batch-……”。"}
            return {"result": await self.engine.accept_batch(root, intent.batch_id)}
        if intent.action == "continue_run":
            if intent.target_characters is None and intent.end_chapter_no is None:
                return {"gate": "连续长跑必须明确终点，例如“到 10 万汉字”或“连续完成到第 10 章”。"}
            if intent.target_characters is not None and intent.end_chapter_no is not None:
                return {"gate": "一次长跑只能使用一种终点：字符数或结束章节号。请明确保留其中一个。"}
            return {
                "result": await self.engine.continue_until(
                    root,
                    intent.target_characters,
                    instruction=intent.operation_instruction,
                    target_chapter_no=intent.end_chapter_no,
                    max_revision_rounds=intent.max_revision_rounds,
                )
            }
        if intent.action == "checkpoint_list":
            return {"result": self.engine.checkpoint_list(root)}
        if intent.action == "checkpoint_create":
            label = intent.operation_instruction.strip() or "用户手动检查点"
            return {"result": self.engine.checkpoint_create(root, label)}
        if intent.action == "rollback_preview":
            return {
                "result": self.engine.rollback_preview(
                    root,
                    checkpoint_id=intent.checkpoint_id,
                    boundary_chapter=intent.chapter_no,
                )
            }
        if intent.action == "rollback_restore":
            if not intent.confirmation_token:
                return {"gate": "确认回退前必须先预览，并逐字提供 confirmation_token。"}
            return {
                "result": self.engine.rollback_restore(
                    root,
                    checkpoint_id=intent.checkpoint_id,
                    boundary_chapter=intent.chapter_no,
                    confirmation_token=intent.confirmation_token,
                )
            }
        if intent.action in _CHAPTER_ACTIONS and intent.chapter_no is None:
            return {"gate": "这条请求需要明确章节号，例如“第 1 章”。未猜测章节。"}

        chapter_no = int(intent.chapter_no or 1)
        instruction = intent.operation_instruction
        steps: list[dict[str, Any]] = []

        if intent.action == "write_draft":
            steps.append(await self._run_step("writer.write", self.engine.write_chapter(root, chapter_no, instruction)))
            return {
                "steps": steps,
                "next_action": "草稿已生成；你可以先阅读章节文件，确认后再说“审查第 N 章”或“按意见修改第 N 章”。",
            }

        if intent.action in {"write_review", "write_review_accept"}:
            written = await self._run_step(
                "writer.write", self.engine.write_chapter(root, chapter_no, instruction)
            )
            steps.append(written)
            if "gate" in written:
                return {"steps": steps}
            reviewed = await self._run_step("reviewer.review", self.engine.review_chapter(root, chapter_no))
            steps.append(reviewed)
            if "gate" in reviewed or intent.action == "write_review":
                return {"steps": steps}
            return await self._conditionally_accept(root, chapter_no, steps, reviewed)

        if intent.action == "revise_draft":
            steps.append(await self._run_step("writer.revise", self.engine.revise_chapter(root, chapter_no, instruction)))
            return {
                "steps": steps,
                "next_action": "修订草稿已生成；它尚未重审或进入正史。",
            }

        if intent.action in {"revise_review", "revise_review_accept"}:
            revised = await self._run_step(
                "writer.revise", self.engine.revise_chapter(root, chapter_no, instruction)
            )
            steps.append(revised)
            if "gate" in revised:
                return {"steps": steps}
            reviewed = await self._run_step("reviewer.review", self.engine.review_chapter(root, chapter_no))
            steps.append(reviewed)
            if "gate" in reviewed or intent.action == "revise_review":
                return {"steps": steps}
            return await self._conditionally_accept(root, chapter_no, steps, reviewed)

        if intent.action == "review":
            steps.append(await self._run_step("reviewer.review", self.engine.review_chapter(root, chapter_no)))
            return {
                "steps": steps,
                "next_action": "审查完成；只有你明确要求验收/入正史，且结论为 pass，才会调用 Memory Keeper。",
            }

        if intent.action == "review_accept":
            # Coordinator 先检查当前草稿是否已经有同版本、同正文哈希的
            # pass 报告。用户明确是在已有合格版本上接收时，不重复调用
            # Reviewer；这避免无意义的重复等待，同时仍由 accept_chapter
            # 再次校验版本、哈希、结论和 Memory Keeper 门禁。
            reused = self._reuse_current_pass_review(InkFlowProject(root), chapter_no)
            if reused is not None:
                steps.append(reused)
                return await self._conditionally_accept(root, chapter_no, steps, reused)
            reviewed = await self._run_step("reviewer.review", self.engine.review_chapter(root, chapter_no))
            steps.append(reviewed)
            if "gate" in reviewed:
                return {"steps": steps}
            return await self._conditionally_accept(root, chapter_no, steps, reviewed)

        if intent.action == "accept":
            steps.append(
                await self._run_step(
                    "memory.accept", self.engine.accept_chapter(root, chapter_no, force=False)
                )
            )
            return {"steps": steps}

        return {"gate": f"不支持的路由结果：{intent.action}"}

    @staticmethod
    def _setting_label(key: str) -> str:
        return _SETTING_LABELS.get(key, key)

    @staticmethod
    def _setting_value_label(key: str, value: Any) -> str:
        if key == "chapter_length_tolerance":
            try:
                return f"{float(value) * 100:g}%"
            except (TypeError, ValueError):
                return str(value)
        if key in {"voice_auto_read", "voice_output_enabled", "voice_input_enabled"}:
            return "开启" if bool(value) else "关闭"
        if key in {"context_soft_tokens", "context_hard_tokens"}:
            try:
                return f"{int(value):,} tokens"
            except (TypeError, ValueError):
                return str(value)
        if key == "agent_generation":
            if isinstance(value, dict):
                roles = "、".join(str(role) for role in value)
                return f"已调整（{roles or '四个 Agent'}）"
            return str(value)
        mapped = _SETTING_VALUE_LABELS.get(key, {})
        return str(mapped.get(str(value), value))

    @staticmethod
    def _setting_change_summary(key: str, before: Any, after: Any) -> str:
        if key == "agent_generation" and isinstance(before, dict) and isinstance(after, dict):
            role_labels = {"coordinator": "Coordinator", "writer": "Writer", "reviewer": "Reviewer", "memory_keeper": "Memory Keeper"}
            fields = ("temperature", "top_p", "top_k")
            items: list[str] = []
            for role, label in role_labels.items():
                old_values = before.get(role) if isinstance(before.get(role), dict) else {}
                new_values = after.get(role) if isinstance(after.get(role), dict) else {}
                for field in fields:
                    if old_values.get(field) != new_values.get(field):
                        items.append(f"{label} {field} {old_values.get(field)} → {new_values.get(field)}")
            if items:
                return "Agent 生成参数：" + "、".join(items)
        return f"{TerminalSession._setting_label(key)}：{TerminalSession._setting_value_label(key, before)} → {TerminalSession._setting_value_label(key, after)}"

    @staticmethod
    def _recommend_next_step(intent: TerminalIntent, response: dict[str, Any]) -> dict[str, str] | None:
        """Return one short, user-controlled next action for the Coordinator UI."""

        if response.get("gate") or response.get("needs_clarification"):
            return None
        suggestions: dict[str, tuple[str, str, str]] = {
            "plan": ("查看第一章规划", "先看章节卡，再决定从哪一章写草稿。", "查看当前规划"),
            "outline": ("从大纲开始写", "独立大纲已保存，选定起点后再生成草稿。", "根据大纲生成第 1 章草稿"),
            "write_draft": ("审查这章", "草稿还没有进入正史，先读一遍或交给 Reviewer。", "审查当前章"),
            "write_review": ("查看审查结果", "Writer 和 Reviewer 已完成当前版本的交接。", "打开当前审查报告"),
            "review": ("处理审查意见", "根据报告决定修订，或在通过后明确验收。", "按审查意见修改当前章"),
            "revise_draft": ("重新审查", "修订产生了新版本，旧报告不能替代新版本审查。", "重新审查当前章"),
            "revise_review": ("查看新的审查", "新版本已完成复审，可以继续阅读结果。", "打开当前审查报告"),
            "batch_draft": ("查看批次", "草稿批次仍在临时区，先查看逐章结果再决定是否验收。", "查看批次进度"),
            "settings_update": ("继续安排任务", "新设置会从下一次任务开始生效。", "查看当前项目状态"),
            "status": ("选择下一步", "Coordinator 已读完当前状态，可以直接告诉我想推进哪一项。", "先问我下一步建议"),
        }
        item = suggestions.get(intent.action)
        if not item:
            return None
        label, reason, prompt = item
        return {"label": label, "reason": reason, "prompt": prompt}

    def _reuse_current_pass_review(self, project: InkFlowProject, chapter_no: int) -> dict[str, Any] | None:
        """Return a safe handoff when the current draft already has a pass report."""

        chapter = project.db.get_chapter(chapter_no)
        record = project.db.latest_review_record(chapter_no)
        if not chapter or chapter.get("status") != "draft" or not record:
            return None
        report = record.get("report")
        if report is None or getattr(report, "verdict", "") != "pass":
            return None
        if int(record.get("chapter_version", -1)) != int(chapter.get("version", -2)):
            return None
        try:
            content = (project.root / str(chapter["path"])).read_text(encoding="utf-8")
        except OSError:
            return None
        source_hash = getattr(report, "source_hash", "")
        if source_hash and source_hash != content_hash(content):
            return None
        context_fingerprint = getattr(report, "context_fingerprint", "")
        if not context_fingerprint:
            return None
        packet = self.engine._context_builder(project, "reviewer").build(
            chapter_no,
            f"审查第 {chapter_no} 章草稿",
            mode="review",
            protected_input=content,
        )
        if context_fingerprint != content_hash(packet.gate_material()):
            return None
        return {
            "step": "reviewer.review",
            "result": {
                "chapter_no": chapter_no,
                "verdict": "pass",
                "confidence": getattr(report, "confidence", 1.0),
                "summary": "Coordinator 复用当前版本已存在的 pass 审查；正文哈希和版本一致，未重复调用 Reviewer。",
                "finding_count": len(getattr(report, "findings", []) or []),
                "score_total": sum(int(item.score) for item in (getattr(report, "scorecard", []) or [])),
                "review_path": str(project.root / str(record.get("path") or "")),
                "reused": True,
            },
        }

    async def _conditionally_accept(
        self,
        root: Path,
        chapter_no: int,
        steps: list[dict[str, Any]],
        reviewed: dict[str, Any],
    ) -> dict[str, Any]:
        report = reviewed.get("result", {})
        if report.get("verdict") != "pass":
            steps.append(
                {
                    "step": "memory.accept",
                    "skipped": True,
                    "reason": "Reviewer verdict 不是 pass；没有使用 force。",
                }
            )
            return {"steps": steps}
        steps.append(
            await self._run_step(
                "memory.accept", self.engine.accept_chapter(root, chapter_no, force=False)
            )
        )
        return {"steps": steps}

    @staticmethod
    async def _run_step(label: str, operation: Any) -> dict[str, Any]:
        try:
            return {"step": label, "result": await operation}
        except InkFlowError as exc:
            return {"step": label, "gate": str(exc)}
