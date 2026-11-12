"""Bounded Writer task delegates; proposals never gain planning or canon authority."""
from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from .errors import ConfigurationError, ProviderError
from .project_lock import project_write_lock
from .runtime import RunBudgetExceeded, active_generation_attempt_limit, active_runtime
from .schemas import PromiseBeat, PromisePlan, StrictModel
from .task_settings import active_task_settings
from .utils import content_hash, json_dumps, utc_now

if TYPE_CHECKING:
    from .engine import InkFlowEngine
    from .project import InkFlowProject
    from .schemas import ContextPacket
    from .trace import TraceRecorder


class PromiseGuidance(StrictModel):
    summary: str = Field(min_length=1, max_length=400)
    recommendations: list[str] = Field(default_factory=list, max_length=8)
    risks: list[str] = Field(default_factory=list, max_length=6)
    chapter_plan_suggestions: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def bounded_public_deliverable(self):
        entries = [self.summary, *self.recommendations, *self.risks, *self.chapter_plan_suggestions]
        if any(not value.strip() for value in entries) or sum(map(len, entries)) > 1800:
            raise ValueError("分身公开建议须非空，合计不得超过1800字")
        return self


_FOCUSES = {
    "causality": "核对伏笔与上层规划、人物动机、既有事实的因果联系，给出本章自然埋设或推进建议",
    "braiding": "分担多条承诺的交织安排，避免重复埋设、信息拥挤或新线挤占既定主线",
    "payoff": "核对本章拟回收线索的触发条件、过程和完成判据，防止把猜测或局部解释误当兑现",
}
_SYSTEM = (
    "你是Writer的伏笔与钩子任务分身，只提供可公开的创作建议。"
    "当前Context Packet是唯一资料，规划、已接受事实和临时材料须保留各自身份。"
    "不得写正文，不执行工具，不更改正史或规划文件，不新增未授权主线。"
    # 设定随已证实剧情演进，但分身只有建议权，不能越过正式设定和规划的采用流程。
    "大纲、卷细纲不随写章动态改动；设定变化须绑定正文证据，交原责任角色形成候选并核验采用。"
    "可建议当前章的轻微呈现调整，或提出须由原规划审核与发布流程处理的章节规划候选。"
    "预计章节范围是参考窗口，不能替代剧情触发条件；不得为到达某章强行回收。"
    "人物猜测不等于事实，再次出现不等于兑现；资料不足时明确风险，不编造依据。"
    "只输出规定JSON，总计不超过1800字，保留有意留白与角色声线；不输出隐藏推理。"
)


def _focuses(packet: ContextPacket) -> list[str]:
    sections = [section for section in packet.sections if section.key == "P0"]
    if len(sections) != 1:
        return []
    try:
        contract = json.loads(sections[0].content)
        if not isinstance(contract, dict):
            return []
        plans = contract.get("plans", [])
        beats = contract.get("beats", [])
        if not isinstance(plans, list) or not isinstance(beats, list):
            return []
        declared = set()
        for item in plans:
            if (not isinstance(item, dict) or not isinstance(item.get("source_id"), str)
                    or not item["source_id"].strip()
                    or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("source_hash", "")))):
                return []
            plan = PromisePlan.model_validate({key: value for key, value in item.items()
                                               if key in PromisePlan.model_fields})
            declared.add(plan.thread_id)
        actions = [PromiseBeat.model_validate(item) for item in beats]
        if not actions:
            return []
        identities = {beat.thread_id for beat in actions}
        if not identities <= declared:
            return []
    except (ValueError, TypeError, ValidationError):
        return []
    result = ["causality"]
    if len(identities) >= 3:
        result.append("braiding")
    if any(beat.action == "payoff" for beat in actions):
        result.append("payoff")
    return result


async def promise_guidance(engine: InkFlowEngine, project: InkFlowProject,
                           packet: ContextPacket, chapter_no: int, trace: TraceRecorder) -> str:
    """Reuse the original task budget and durable attempt state, with no implicit retries."""
    scope, runtime = active_task_settings.get(), active_runtime.get()

    def skipped(reason: str) -> str:
        trace.record("writer.promise", "skipped", reason,
                     metadata={"chapter_no": chapter_no, "agent_role": "writer", "extra_model_calls": 0})
        return ""

    if (scope is None or runtime is None or not scope.task_id or not scope.snapshot_hash
            or runtime.task_id != scope.task_id or scope.novel_id != project.project_id
            or packet.project_id != project.project_id or packet.chapter_no != chapter_no):
        return skipped("伏笔分身缺少一致的原任务快照、共享预算或章节身份，沿用现有角色检查")
    if scope.settings.quality_preset != "quality":
        return skipped("当前质量档复用Writer与记忆责任角色的原有调用，不额外启动伏笔分身")
    if runtime.role_attempt_limits or scope.settings.writer_task_attempt_limit is not None:
        return skipped("当前任务有显式角色请求次数约束，保留配额给原责任角色")
    focuses = _focuses(packet)
    if not focuses:
        return skipped("当前章无可核验的规划承诺或行动，未额外启动伏笔分身")
    engine._assert_task_settings()
    settings = scope.settings
    packet_prompt = packet.to_model_prompt()
    packet_hash = content_hash(packet_prompt)
    outputs = []
    schema_prompt = json_dumps(PromiseGuidance.model_json_schema())
    model = settings.model_for("writer")
    for focus in focuses:
        stage = f"writer.promise.{focus}"
        identity = {"task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash,
                    "packet_hash": packet_hash, "chapter_no": chapter_no, "focus": focus, "schema_version": 1}
        key = "promise_worker:" + content_hash(json_dumps(identity, indent=None))
        user_prompt = (packet_prompt + f"\n\n本次分工：{_FOCUSES[focus]}。"
                       "每条建议注明对应线索，建议仅供主Writer选择，并由当前记忆责任角色审核。")
        async with project_write_lock(project.root):
            saved = project.db.get_metadata(key)
            if saved is not None:
                if (not isinstance(saved, dict) or saved.get("identity") != identity
                        or saved.get("status") != "completed"):
                    trace.record(stage, "skipped", "已有尝试未闭合、失败或记录不可核验，未重复请求",
                                 metadata={"chapter_no": chapter_no, "agent_role": "writer"})
                    outputs.append(f"{focus}分身已有未采用的尝试；未重发，请主Writer按原资料保守处理。")
                    continue
                try:
                    advice = PromiseGuidance.model_validate(saved.get("output"))
                except ValidationError:
                    trace.record(stage, "skipped", "分身缓存不符合当前输出合同，未采用或重发")
                    continue
                outputs.append(f"分工：{_FOCUSES[focus]}\n{json_dumps(advice.model_dump(mode='json'))}")
                trace.record(stage, "completed", "复用同一任务、同版资料的分身公开建议，未新增请求",
                             metadata={"chapter_no": chapter_no, "agent_role": "writer", "cache_hit": True})
                continue
            if runtime.max_calls - runtime.calls < 3:
                trace.record(stage, "skipped", "原任务余量不足，至少保留两次调用给主Writer及必要步骤")
                break
            input_estimate = settings.estimate_input_tokens(_SYSTEM + user_prompt + schema_prompt) + 256
            try:
                settings.validate_input_budget(input_estimate, "writer")
            except ConfigurationError:
                trace.record(stage, "skipped", "完整资料超过Writer输入预算；未截断原文启动分身")
                break
            upper = settings.output_reservation_upper("writer")
            if runtime.max_tokens is not None and (upper is None or runtime.unknown_estimate_calls
                    or runtime.tokens + 3 * (input_estimate + upper) > runtime.max_tokens):
                trace.record(stage, "skipped", "共享Token余量不足或无法核定，保留原任务必要步骤预算")
                break
            state = {"identity": identity, "status": "attempted", "run_id": trace.run_id, "attempted_at": utc_now()}
            project.db.set_metadata(key, state)
        limit_token = active_generation_attempt_limit.set(1)
        try:
            trace.record_model_started(stage, model=model, agent_role="writer", max_tokens=2400,
                                       timeout_seconds=settings.request_timeout_seconds, thinking=True,
                                       thinking_purpose="payoff" if focus == "payoff" else "causality")
            result = await engine.provider.generate_json(
                system_prompt=_SYSTEM, user_prompt=user_prompt, output_model=PromiseGuidance,
                cache_prefix=packet.model_prompt_parts()[0] or None,
                effort=settings.reasoning_effort, max_tokens=2400, thinking=True,
                thinking_purpose="payoff" if focus == "payoff" else "causality",
                timeout_seconds=settings.request_timeout_seconds, agent_role="writer")
            advice = PromiseGuidance.model_validate(result.data)
            async with project_write_lock(project.root):
                project.db.set_metadata(key, {**state, "status": "completed", "completed_at": utc_now(),
                                              "output": advice.model_dump(mode="json")})
            trace.record_model(stage, result, "Writer任务分身已返回公开建议，尚未成为正文、规划或正史")
            outputs.append(f"分工：{_FOCUSES[focus]}\n{json_dumps(advice.model_dump(mode='json'))}")
        except (asyncio.CancelledError, RunBudgetExceeded) as exc:
            async with project_write_lock(project.root):
                project.db.set_metadata(key, {**state, "status": "cancelled" if isinstance(exc, asyncio.CancelledError)
                                              else "budget_blocked", "finished_at": utc_now()})
            trace.record(stage, "cancelled" if isinstance(exc, asyncio.CancelledError) else "skipped",
                         "分身已停止，保留尝试记录与原任务预算；未自动重发")
            raise
        except ProviderError as exc:
            async with project_write_lock(project.root):
                project.db.set_metadata(key, {**state, "status": "failed", "finished_at": utc_now(),
                                              "error_type": type(exc).__name__})
            trace.record_model_failed(stage, exc, model=model, agent_role="writer")
            outputs.append(f"{focus}分身请求未完成（{type(exc).__name__}，原因见本次运行记录）；未重发。"
                           "主Writer须自行按原规划与正文核对，不把辅助失败当作已通过审核。")
            break
        finally:
            active_generation_attempt_limit.reset(limit_token)
    if not outputs:
        return ""
    guidance = ("\n\n【Writer伏笔任务分身公开建议：仅供参考，非正文、非有效规划或正史；"
                "最终由当前记忆责任角色审核。章节规划候选仍须走原审核与发布流程。】\n" + "\n\n".join(outputs))
    _, hard = settings.context_budget_for("writer")
    if settings.estimate_input_tokens(packet_prompt + guidance) + 2048 >= hard:
        return skipped("分身建议加入后超出Writer输入余量，未挤掉原文或必要依据")
    return guidance
