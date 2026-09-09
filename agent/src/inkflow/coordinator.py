"""Bounded product-manager coordination for InkFlow's four formal AI agents."""
from __future__ import annotations

from uuid import uuid4

from .project import InkFlowProject
from .schemas import DispatchPlan, DispatchStep, RoleCapability, TaskTicket, TerminalIntent


ROLE_CAPABILITIES: tuple[RoleCapability, ...] = (
    RoleCapability(
        role="coordinator",
        novel_production_agent=False,
        can=["理解自然语言", "拆解任务", "选择预定义工作流", "提出最小澄清", "汇总分歧"],
        cannot=["写正文", "审查正文", "提交正史", "绕过引擎门禁", "自行扩权"],
    ),
    RoleCapability(
        role="writer",
        novel_production_agent=True,
        can=["规划", "写作", "按证据定点修订"],
        cannot=["批准自己的正文", "写入正史", "修改审核规则"],
    ),
    RoleCapability(
        role="reviewer",
        novel_production_agent=True,
        can=["引用证据审查", "说明拒绝原因", "复审新版本"],
        cannot=["直接修改正文", "写入正史", "批准未审版本"],
    ),
    RoleCapability(
        role="memory_keeper",
        novel_production_agent=True,
        can=["从已批准内容提取记忆补丁", "同步事实与伏笔"],
        cannot=["从未通过草稿写正史", "续写正文", "自行裁决故事方向"],
    ),
)


_WORKFLOWS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "status": (("engine", "status.read", "", "项目状态"),),
    "help": (("engine", "help.read", "", "使用说明"),),
    "plan": (("writer", "plan.generate", "", "四级规划"),),
    "plan_preview": (("engine", "plan.preview", "", "规划预览"),),
    "plan_brief": (("writer", "plan.brief", "", "公开规划判断单"),),
    "plan_next_arc": (("writer", "plan.advance", "", "下一篇章规划"),),
    "write_draft": (("writer", "chapter.write", "", "章节草稿"),),
    "write_review": (
        ("writer", "chapter.write", "", "章节草稿"),
        ("reviewer", "chapter.review", "step-1", "证据化审查报告"),
    ),
    "review": (("reviewer", "chapter.review", "", "证据化审查报告"),),
    "revise_draft": (("writer", "chapter.revise", "", "新草稿版本"),),
    "revise_review": (
        ("writer", "chapter.revise", "", "新草稿版本"),
        ("reviewer", "chapter.review", "step-1", "新版本审查报告"),
    ),
    "review_accept": (
        ("reviewer", "chapter.review", "", "当前版本审查报告"),
        ("memory_keeper", "chapter.accept", "step-1", "正史记忆补丁与提交结果"),
    ),
    "revise_review_accept": (
        ("writer", "chapter.revise", "", "新草稿版本"),
        ("reviewer", "chapter.review", "step-1", "新版本审查报告"),
        ("memory_keeper", "chapter.accept", "step-2", "正史记忆补丁与提交结果"),
    ),
    "accept": (("memory_keeper", "chapter.accept", "", "正史记忆补丁与提交结果"),),
    "arc_audit": (("reviewer", "arc.audit", "", "篇章复审报告"),),
    "batch_draft": (("engine", "batch.draft_loop", "", "逐章草稿、审查和临时记忆"),),
    "batch_repair": (("engine", "batch.repair_loop", "", "逐章修订和复审结果"),),
    "batch_accept": (("engine", "batch.accept_loop", "", "连续正史提交结果"),),
    "continue_run": (("engine", "chapter.gated_loop", "", "门禁循环进度"),),
    "checkpoint_list": (("engine", "checkpoint.list", "", "检查点列表"),),
    "checkpoint_create": (("engine", "checkpoint.create", "", "新检查点"),),
    "rollback_preview": (("engine", "rollback.preview", "", "回退影响和确认码"),),
    "rollback_restore": (("engine", "rollback.restore", "", "分支式恢复结果"),),
    "discuss": (),
    "exit": (),
}


class Coordinator:
    """Compile an intent into a bounded ticket and a validated workflow plan."""

    def __init__(self, project: InkFlowProject):
        self.project = project

    def compile(self, intent: TerminalIntent) -> tuple[TaskTicket, DispatchPlan]:
        template = _WORKFLOWS[intent.action]
        steps: list[DispatchStep] = []
        for index, (role, operation, dependency, output) in enumerate(template, 1):
            gate = ""
            if role == "reviewer":
                gate = "必须审查当前章节版本并引用证据"
            elif role == "memory_keeper":
                gate = "仅 Reviewer pass 且用户授权后执行；force=false"
            steps.append(
                DispatchStep(
                    step_id=f"step-{index}",
                    role=role,
                    operation=operation,
                    depends_on=[dependency] if dependency else [],
                    required_output=output,
                    gate=gate,
                )
            )
        chapter = self.project.db.get_chapter(intent.chapter_no) if intent.chapter_no else None
        chapter_count = max(1, (intent.end_chapter_no or intent.chapter_no or 1) - (intent.chapter_no or 1) + 1)
        estimated_calls = self._model_call_budget(intent, chapter_count)
        ticket = TaskTicket(
            ticket_id=f"ticket-{uuid4().hex}",
            objective=intent.requested_outcome.strip() or intent.visible_reason,
            chapter_no=intent.chapter_no,
            end_chapter_no=intent.end_chapter_no,
            chapter_version=int(chapter["version"]) if chapter else None,
            hard_constraints=[
                "用户当前明确指令优先，但不能绕过正史和安全门禁",
                "旧审查不能批准新版本",
                "Reviewer 不修改正文",
                "Memory Keeper 不读取未批准草稿写正史",
                "最多两轮 Agent 方向讨论，仍有分歧则交给用户",
            ],
            input_sources=self._input_sources(intent),
            deliverables=[item.required_output for item in steps],
            max_model_calls=estimated_calls,
            max_tokens=min(1_000_000, estimated_calls * 16_000),
            max_discussion_rounds=2,
        )
        plan = DispatchPlan(
            workflow=intent.action,
            steps=steps,
            parallel=False,
            stop_conditions=[
                "缺少必要输入或授权",
                "版本或正文哈希变化",
                "Reviewer 返回 patch、replan 或 unknown 且模板没有修订步骤",
                "两轮协作后仍存在方向分歧",
                "达到调用或 Token 预算",
            ],
        )
        self.validate(plan)
        return ticket, plan

    @staticmethod
    def validate(plan: DispatchPlan) -> None:
        template = _WORKFLOWS.get(plan.workflow)
        if template is None:
            raise ValueError("Coordinator 只能选择预定义工作流")
        allowed = [(role, operation) for role, operation, _, _ in template]
        actual = [(item.role, item.operation) for item in plan.steps]
        if actual != allowed or plan.parallel:
            raise ValueError("Coordinator 调度计划超出固定能力边界")

    @staticmethod
    def capabilities() -> list[dict]:
        return [item.model_dump(mode="json") for item in ROLE_CAPABILITIES]

    @staticmethod
    def _input_sources(intent: TerminalIntent) -> list[str]:
        sources = ["user:current", "canon:sqlite", "preferences:active", "plan:current"]
        if intent.chapter_no:
            sources.extend([f"chapter:{intent.chapter_no:05d}", f"review:{intent.chapter_no:05d}"])
        if intent.batch_id:
            sources.append(f"batch:{intent.batch_id}")
        return sources

    @staticmethod
    def _model_call_budget(intent: TerminalIntent, chapter_count: int) -> int:
        if intent.action in {"status", "help", "plan_preview", "checkpoint_list", "rollback_preview", "exit"}:
            return 0
        if intent.action == "discuss":
            return 1
        if intent.action in {"batch_draft", "batch_repair", "continue_run"}:
            return min(100, chapter_count * (2 + max(0, intent.max_revision_rounds)))
        if intent.action == "batch_accept":
            return min(100, chapter_count)
        return sum(item[0] in {"writer", "reviewer", "memory_keeper"} for item in _WORKFLOWS[intent.action])
