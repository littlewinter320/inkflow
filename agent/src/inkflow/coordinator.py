"""Bounded workflow plans for legacy and five-role task snapshots."""
from __future__ import annotations

import re
from uuid import uuid4

from .project import InkFlowProject
from .role_protocol import CollaborationMode, check_owners_for_mode, normalize_role, roles_for_mode
from .schemas import BookBrief, DispatchPlan, DispatchStep, RoleCapability, TaskTicket, TerminalIntent


ROLE_CAPABILITIES: tuple[RoleCapability, ...] = (
    RoleCapability(
        role="coordinator",
        novel_production_agent=False,
        can=["理解多样自然语言", "合并补充说明", "拆解任务", "选择与切换预定义工作流", "提出最小澄清", "汇总分歧", "通过 Novel Engine 请求白名单设置变更"],
        cannot=["写正文", "审查正文", "提交正史", "直接写设置文件或数据库", "绕过引擎门禁", "自行扩权"],
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
        can=["审读正文", "给出定点修订建议", "复审新版本", "产出同版本记忆更新候选"],
        cannot=["直接修改正文", "写入正史", "批准未审版本"],
    ),
)

# Capability declarations are informational; plans and engine gates authorize calls.
ROLE_CAPABILITIES_V2: tuple[RoleCapability, ...] = (
    ROLE_CAPABILITIES[0],
    ROLE_CAPABILITIES[1],
    RoleCapability(
        role="editor", novel_production_agent=True,
        can=["日常综合审读", "给出定点修订建议", "复审同版本正文", "仅在承担记忆职责时产出记忆候选"],
        cannot=["直接改正文", "代替专项角色完成检查", "提交正史", "批准未审版本"],
    ),
    RoleCapability(
        role="reviewer", novel_production_agent=True,
        can=["按指定范围检查因果和连续性", "引用证据报告硬问题和缺失依据"],
        cannot=["默认重复综合编辑", "直接改正文", "产出记忆补丁", "提交正史"],
    ),
    RoleCapability(
        role="memory_keeper", novel_production_agent=True,
        can=["整理有来源的事实与人物认知", "提出时间线和伏笔变更", "报告记忆冲突"],
        cannot=["批准正文文学质量", "无依据改写事实", "自行修改剧情", "提交正史"],
    ),
)


_WORKFLOWS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "status": (("engine", "status.read", "", "项目状态"),),
    "help": (("engine", "help.read", "", "使用说明"),),
    "plan": (("writer", "plan.generate", "", "四级规划"),),
    "plan_preview": (("engine", "plan.preview", "", "规划预览"),),
    "outline": (("writer", "plan.outline", "", "独立章节大纲"),),
    "redesign_story": (
        ("writer", "planning.book_outline", "", "全书大纲候选"),
        ("engine", "planning.review_outline", "step-1", "按当前模式执行大纲审核"),
        ("writer", "planning.volume_detail", "step-2", "逐卷细纲候选"),
        ("engine", "planning.review_detail", "step-3", "按当前模式执行卷细纲审核"),
        ("writer", "planning.chapter_window", "step-4", "近期章节规划"),
        ("engine", "planning.review_window", "step-5", "按当前模式执行规划审核与发布"),
    ),
    "plan_brief": (("writer", "plan.brief", "", "公开规划判断单"),),
    "plan_next_arc": (("writer", "plan.advance", "", "下一篇章规划"),),
    "write_draft": (("writer", "chapter.write", "", "章节草稿"),),
    "scene_draft": (("writer", "scene.draft", "", "隔离场景草稿"),),
    "story_setting_edit": (("engine", "setting.update", "", "设定新版本、差异与受影响范围"),),
    "revise_selection": (("writer", "chapter.revise_selection", "", "指定选区的替换候选"),),
    "write_review": (
        ("writer", "chapter.write", "", "章节草稿"),
        ("reviewer", "chapter.review", "step-1", "证据化审查报告"),
    ),
    "write_review_accept": (
        ("writer", "chapter.write", "", "章节草稿"),
        ("reviewer", "chapter.review", "step-1", "当前版本审查报告"),
        ("engine", "chapter.accept", "step-2", "正史记忆补丁与提交结果"),
    ),
    "review": (("reviewer", "chapter.review", "", "证据化审查报告"),),
    "revise_draft": (("writer", "chapter.revise", "", "新草稿版本"),),
    "repair_accepted": (("engine", "chapter.repair_accepted", "", "正史疑点诊断、局部候选与复核结果"),),
    "revise_review": (
        ("writer", "chapter.revise", "", "新草稿版本"),
        ("reviewer", "chapter.review", "step-1", "新版本审查报告"),
    ),
    "review_accept": (
        ("reviewer", "chapter.review", "", "当前版本审查报告"),
        ("engine", "chapter.accept", "step-1", "正史记忆补丁与提交结果"),
    ),
    "revise_review_accept": (
        ("writer", "chapter.revise", "", "新草稿版本"),
        ("reviewer", "chapter.review", "step-1", "新版本审查报告"),
        ("engine", "chapter.accept", "step-2", "正史记忆补丁与提交结果"),
    ),
    "accept": (("engine", "chapter.accept", "", "正史记忆补丁与提交结果"),),
    "arc_audit": (("reviewer", "arc.audit", "", "篇章复审报告"),),
    "batch_draft": (("engine", "batch.draft_loop", "", "逐章草稿、审查和临时记忆"),),
    "batch_draft_accept": (
        ("engine", "batch.draft_loop", "", "逐章草稿、审查和临时记忆"),
        ("engine", "batch.accept_loop", "step-1", "仅连续通过章节的正史提交结果"),
    ),
    "batch_repair": (("engine", "batch.repair_loop", "", "逐章修订和复审结果"),),
    "batch_accept": (("engine", "batch.accept_loop", "", "连续正史提交结果"),),
    "continue_run": (("engine", "chapter.gated_loop", "", "门禁循环进度"),),
    "checkpoint_list": (("engine", "checkpoint.list", "", "检查点列表"),),
    "checkpoint_create": (("engine", "checkpoint.create", "", "新检查点"),),
    "rollback_preview": (("engine", "rollback.preview", "", "回退影响和确认码"),),
    "rollback_restore": (("engine", "rollback.restore", "", "分支式恢复结果"),),
    "chat": (),
    "ideate": (("writer", "idea.brainstorm", "", "创意提案"),),
    "voice_clone_script": (("writer", "voice.clone_script", "", "声音克隆参考朗读稿"),),
    "settings_update": (("engine", "settings.update", "", "设置变更结果"),),
    "discuss": (),
    "exit": (),
}

_REVIEW_ACTIONS = frozenset({
    "write_review", "write_review_accept", "review", "revise_review",
    "review_accept", "revise_review_accept", "accept", "arc_audit",
    "batch_draft", "batch_draft_accept", "batch_repair", "batch_accept", "continue_run",
})


def _mode_template(
    action: str, protocol_version: int, collaboration_mode: CollaborationMode = "everyday"
) -> tuple[tuple[str, str, str, str], ...]:
    template = _WORKFLOWS[action]
    if protocol_version == 1:
        return template
    return tuple(
        ("engine", "chapter.review_mode", dependency, output)
        if role == "reviewer" and operation == "chapter.review"
        else ("editor" if collaboration_mode == "everyday" else "reviewer", operation, dependency, output)
        if operation == "arc.audit"
        else (role, operation, dependency, output)
        for role, operation, dependency, output in template
    )


class Coordinator:
    """Compile an intent into a bounded ticket and a validated workflow plan."""

    def __init__(self, project: InkFlowProject):
        self.project = project

    @staticmethod
    def normalize_intent(
        intent: TerminalIntent, user_text: str, *, previous_intent: TerminalIntent | None = None,
    ) -> TerminalIntent:
        """Apply only clear user wording and links; uncertain edits stay scoped."""
        text = user_text.strip()
        updates: dict[str, object] = {"user_message": text[:4_000]}
        action = intent.action
        # Conditional repair is review-first, not evidence-free rewriting.
        if action in {"revise_review", "revise_review_accept"} and re.search(
            r"(?:有|发现|如果|若).{0,10}(?:问题|错误|不通过|没过).{0,8}(?:改|修)", text
        ) and re.search(r"审|检查|核对", text):
            action = "review_accept" if action.endswith("_accept") else "revise_review"
        no_accept = re.search(r"(?:别|不要|先不|不必|无需).{0,8}(?:接收|接受|验收|入正史|进入正史|收进正史|收进正文|收进去|定稿)", text)
        no_review = re.search(r"(?:别|不要|先不|不必|无需).{0,8}(?:审查|审核|复审)", text)
        # Negation must govern the writing task itself. "别把丢失的原件写回来"
        # constrains story content; it does not mean "别写这一章".
        no_write = re.search(
            r"(?:别|不要|先不|不必|无需|暂不)(?:现在|先|自动)?(?:写|起草|生成)"
            r"(?:第\s*\d+\s*章|正文|章节|草稿|新章)?(?=[，,。！？\s]|$)",
            text,
        )
        discuss_first = re.search(r"(?:先|暂时|现在)(?:聊聊|聊|讨论|谈谈)|(?:聊聊|讨论).{0,12}(?:后面|接下来).{0,8}(?:发展|走向)", text)
        scene_request = re.search(r"(?:写|起草|生成).{0,12}(?:一小段|一段|片段|场景|短草稿)|(?:一小段|片段|场景).{0,10}(?:草稿|写)", text)
        local_edit = re.search(r"(?:这段留着|保留这段|只改结尾|只改对话|只改这一段|仅改选中)", text)
        forbidden = list(intent.forbidden_actions)
        if no_accept:
            forbidden.append("accept")
            action = {
                "write_review_accept": "write_review", "review_accept": "review",
                "revise_review_accept": "revise_review", "batch_draft_accept": "batch_draft",
                "accept": "discuss", "batch_accept": "discuss", "repair_accepted": "discuss",
            }.get(action, action)
            updates.update(authorization_source="current_request" if intent.authorization == "approved" else "none")
        if no_review:
            forbidden.append("review")
            action = {
                "write_review": "write_draft", "write_review_accept": "write_draft",
                "revise_review": "revise_draft", "revise_review_accept": "revise_draft",
                "review": "discuss", "review_accept": "discuss",
                "batch_draft_accept": "batch_draft", "batch_accept": "discuss",
            }.get(action, action)
        if scene_request and not no_write and action in {"write_draft", "write_review", "write_review_accept", "scene_draft"}:
            action = "scene_draft"
            updates["narrative_scope"] = "scene"
        elif action in {"batch_draft", "batch_draft_accept", "batch_repair", "batch_accept", "continue_run"}:
            updates["narrative_scope"] = "batch"
        elif action in {"write_draft", "write_review", "write_review_accept", "revise_draft", "revise_review", "revise_review_accept", "repair_accepted"}:
            updates["narrative_scope"] = "chapter"
        if local_edit and action in {"write_draft", "revise_draft", "revise_review", "revise_review_accept", "revise_selection"}:
            action = "revise_selection"
            updates["edit_scope"] = "selection"
            updates["narrative_scope"] = "none"
            updates["preserve_constraints"] = list(dict.fromkeys([*intent.preserve_constraints, "保留未选中的原文与剧情"]))
        if (discuss_first or (no_write and action in {"write_draft", "write_review", "write_review_accept", "scene_draft"})):
            forbidden.append("write")
            action = "discuss"
            updates["authorization"] = "none"
            updates["authorization_source"] = "none"
            updates["narrative_scope"] = "none"
        if intent.response_kind == "question_answer":
            linked = bool(intent.pending_question_id) and (
                previous_intent is None or intent.pending_question_id == previous_intent.pending_question_id
            )
            if not linked:
                action = "discuss"
                updates["authorization"] = "none"
                updates["authorization_source"] = "none"
                updates["clarification_question"] = "这条回答对应哪一个待答问题？请指定后继续原任务。"
        if intent.response_kind == "task_revision" and not intent.related_task_id:
            action = "discuss"
            updates["authorization"] = "none"
            updates["authorization_source"] = "none"
            updates["clarification_question"] = "这次修改对应哪一项正在进行的任务？"
        if action == "story_setting_edit":
            change = intent.setting_change
            valid_book = (intent.document_kind == "book" and bool(change)
                          and set(change) <= set(BookBrief.model_fields))
            valid_document = (intent.document_kind in {"outline", "story_detail"}
                              and set(change) == {"old_text", "new_text"}
                              and all(isinstance(change[key], str) and change[key] for key in change))
            if not (valid_book or valid_document):
                scope_text = " ".join((text, intent.operation_instruction, intent.requested_outcome))
                broad_scope = re.search(r"全书|整卷|第[一二三四五六七八九十\d]+卷|第\s*\d+\s*(?:到|至|～|~|-)\s*第?\s*\d+\s*章", scope_text)
                target = intent.document_kind
                if target == "none":
                    target = "story_detail" if "细纲" in scope_text and "大纲" not in scope_text else (
                        "outline" if "大纲" in scope_text and "细纲" not in scope_text else "none"
                    )
                if (target in {"outline", "story_detail"} and broad_scope
                        and re.search(r"重新|重做|重构|重排|修订|修改|调整|优化|生成|规划|整理|展开|完善|顺一遍", scope_text)):
                    # Whole-volume/range revision is a Writer planning task,
                    # not a literal old_text/new_text document patch.  Asking
                    # for that patch again made "现在执行" loop indefinitely.
                    action = "outline"
                    updates.update(
                        outline_level="detail" if target == "story_detail" else "story",
                        document_kind="none", setting_change={}, missing_fields=[],
                        clarification_question="", clarification_questions=[],
                    )
                else:
                    action = "discuss"
                    updates["authorization"] = "none"
                    updates["authorization_source"] = "none"
                    updates["clarification_question"] = "要修改书籍设定、大纲还是剧情细纲？请指出具体字段或需要替换的原文。"
        updates["action"] = action
        updates["forbidden_actions"] = list(dict.fromkeys(forbidden))
        return intent.model_copy(update=updates)

    def compile(
        self, intent: TerminalIntent, *, role_protocol_version: int = 1,
        collaboration_mode: CollaborationMode = "everyday",
        task_snapshot_hash: str | None = None,
    ) -> tuple[TaskTicket, DispatchPlan]:
        if intent.user_message:
            intent = self.normalize_intent(intent, intent.user_message)
        normalize_role("coordinator", role_protocol_version)
        roles_for_mode(collaboration_mode)
        if role_protocol_version == 1 and collaboration_mode != "everyday":
            raise ValueError("旧角色协议只支持日常模式")
        if role_protocol_version == 2 and not task_snapshot_hash:
            raise ValueError("五角色计划必须绑定已冻结的任务配置快照")
        template = _mode_template(intent.action, role_protocol_version, collaboration_mode)
        steps: list[DispatchStep] = []
        for index, (role, operation, dependency, output) in enumerate(template, 1):
            gate = ""
            if role_protocol_version == 2 and operation == "chapter.review_mode":
                gate = "引擎按模式执行独占检查责任；同版本结果汇合并校验覆盖、记忆、权限和预算"
            elif operation == "scene.draft":
                gate = "仅 Writer 生成隔离草稿；不审查、不验收、不写正史"
            elif operation == "setting.update":
                gate = "仅用户明确授权的设定变更；引擎保存新版本、差异与影响范围"
            elif operation == "chapter.revise_selection":
                gate = "先冻结目标选区与来源版本；Writer 仅返回该范围替换候选"
            elif operation == "chapter.repair_accepted":
                gate = "仅最新已接受章的可定位疑点；审读诊断、Writer 单处补句、独立复核、保留旧版并校验版本后提交"
            elif role == "reviewer":
                gate = "旧协议 reviewer 执行综合 Editor 审查；必须绑定当前章节版本并引用证据"
            elif operation == "chapter.accept":
                gate = "仅当前版本 Editor 审查通过且用户授权后执行；force=false"
            elif operation in {"chapter.write", "batch.draft_loop", "chapter.gated_loop"}:
                gate = "缺卡先在已授权范围内由 Writer 补必要近期计划；不改旧卡或正史，依据缺失不猜写"
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
        if role_protocol_version == 2 and intent.action in _REVIEW_ACTIONS:
            role_count = len(set(check_owners_for_mode(collaboration_mode).values()))
            estimated_calls = min(100, max(estimated_calls, role_count * 6 * chapter_count))
        ticket = TaskTicket(
            role_protocol_version=role_protocol_version,
            collaboration_mode=collaboration_mode,
            task_snapshot_hash=task_snapshot_hash,
            ticket_id=f"ticket-{uuid4().hex}",
            objective=intent.requested_outcome.strip() or intent.visible_reason,
            user_message=intent.user_message,
            task_revision=intent.task_revision,
            related_task_id=intent.related_task_id,
            pending_question_id=intent.pending_question_id,
            response_kind=intent.response_kind,
            narrative_scope=intent.narrative_scope,
            edit_scope=intent.edit_scope,
            preserve_constraints=intent.preserve_constraints,
            forbidden_actions=intent.forbidden_actions,
            target_excerpt=intent.target_excerpt,
            document_kind=intent.document_kind,
            setting_change=intent.setting_change,
            chapter_no=intent.chapter_no,
            end_chapter_no=intent.end_chapter_no,
            chapter_version=int(chapter["version"]) if chapter else None,
            hard_constraints=[
                "用户当前明确指令优先，但不能绕过正史和安全门禁",
                "旧审查不能批准新版本",
                "旧协议 reviewer 是综合 Editor；Editor 不修改正文",
                "记忆服务 不读取未批准草稿写正史",
                "最多两轮 Agent 方向讨论，仍有分歧则交给用户",
                "用户表达方式不绑定固定命令；同一目标允许多条合理实现路径",
                "失败时先在授权范围内复用成果、缩小范围或切换可用路径，再决定是否需要用户介入",
                "已授权正文范围内的必要补卡由引擎衔接 Writer，不额外逐章调用 Coordinator 或扩大章节范围",
            ],
            input_sources=self._input_sources(intent),
            deliverables=[item.required_output for item in steps],
            max_model_calls=estimated_calls,
            # Routing estimate only; not a runtime cap or user budget.
            max_tokens=min(1_000_000, estimated_calls * 32_000),
            max_discussion_rounds=2,
            authorization_source=(
                intent.authorization_source
                if intent.authorization_source != "none"
                else ("current_request" if intent.authorization == "approved" else "none")
            ),
            acceptance_confirmation_mode=intent.acceptance_confirmation_mode,
        )
        plan = DispatchPlan(
            role_protocol_version=role_protocol_version,
            collaboration_mode=collaboration_mode,
            task_snapshot_hash=task_snapshot_hash,
            required_checks=(
                list(check_owners_for_mode(collaboration_mode))
                if role_protocol_version == 2 and intent.action in _REVIEW_ACTIONS else []
            ),
            check_owners=(
                check_owners_for_mode(collaboration_mode)
                if role_protocol_version == 2 and intent.action in _REVIEW_ACTIONS else {}
            ),
            memory_owner=(
                check_owners_for_mode(collaboration_mode)["memory"]
                if role_protocol_version == 2 and intent.action in _REVIEW_ACTIONS else None
            ),
            return_to_base=role_protocol_version == 2 and collaboration_mode != "everyday",
            workflow=intent.action,
            steps=steps,
            parallel=(
                role_protocol_version == 2 and collaboration_mode in {"review_boost", "full_specialist"}
                and any(item.operation in {"chapter.review_mode", "batch.draft_loop", "batch.repair_loop", "chapter.gated_loop"} for item in steps)
            ),
            stop_conditions=[
                "缺少必要输入或授权",
                "版本或正文哈希变化",
                "Editor 返回 patch、replan 或 unknown，且已授权的限次自修与替代路径均已用尽",
                "两轮协作后仍存在方向分歧",
                "达到调用或 Token 预算",
            ],
        )
        self.validate(plan, ticket)
        return ticket, plan

    @staticmethod
    def validate(plan: DispatchPlan, ticket: TaskTicket | None = None) -> None:
        normalize_role("coordinator", plan.role_protocol_version)
        allowed_roles = roles_for_mode(plan.collaboration_mode)
        if plan.role_protocol_version == 1 and plan.collaboration_mode != "everyday":
            raise ValueError("旧角色协议只支持日常模式")
        if plan.role_protocol_version == 2 and not plan.task_snapshot_hash:
            raise ValueError("五角色计划缺少任务配置快照")
        template = _WORKFLOWS.get(plan.workflow)
        if template is None:
            raise ValueError("Coordinator 只能选择预定义工作流")
        allowed = _mode_template(plan.workflow, plan.role_protocol_version, plan.collaboration_mode)
        actual = [(item.role, item.operation, item.depends_on, item.required_output) for item in plan.steps]
        expected = [(role, operation, [dependency] if dependency else [], output)
                    for role, operation, dependency, output in allowed]
        expected_parallel = (
            plan.role_protocol_version == 2 and plan.collaboration_mode in {"review_boost", "full_specialist"}
            and any(operation in {"chapter.review_mode", "batch.draft_loop", "batch.repair_loop", "chapter.gated_loop"}
                    for _, operation, _, _ in allowed)
        )
        if actual != expected or plan.parallel != expected_parallel:
            raise ValueError("Coordinator 调度计划超出固定能力边界")
        if plan.role_protocol_version == 2 and any(
            item.role not in allowed_roles for item in plan.steps if item.role != "engine"
        ):
            raise ValueError("工作流包含当前协作模式未启用的角色")
        expected_checks = (
            check_owners_for_mode(plan.collaboration_mode)
            if plan.role_protocol_version == 2 and plan.workflow in _REVIEW_ACTIONS else {}
        )
        if (plan.required_checks != list(expected_checks)
                or plan.check_owners != expected_checks
                or plan.memory_owner != expected_checks.get("memory")
                or plan.return_to_base != (plan.role_protocol_version == 2 and plan.collaboration_mode != "everyday")
                or any(owner not in allowed_roles for owner in plan.check_owners.values())):
            raise ValueError("检查覆盖或记忆责任不符合协作模式")
        if ticket is not None:
            if (ticket.role_protocol_version != plan.role_protocol_version
                    or ticket.collaboration_mode != plan.collaboration_mode
                    or ticket.task_snapshot_hash != plan.task_snapshot_hash
                    or ticket.max_model_calls < len(set(plan.check_owners.values()))
                    or ticket.max_tokens < ticket.max_model_calls):
                raise ValueError("计划与任务快照或调用预算不一致")

    @staticmethod
    def capabilities(protocol_version: int = 1) -> list[dict]:
        normalize_role("coordinator", protocol_version)  # Reject unknown versions.
        roles = ROLE_CAPABILITIES if protocol_version == 1 else ROLE_CAPABILITIES_V2
        return [item.model_dump(mode="json") for item in roles]

    @staticmethod
    def _input_sources(intent: TerminalIntent) -> list[str]:
        if intent.action == "voice_clone_script":
            return ["user:current", "voice:local-only"]
        if intent.action == "settings_update":
            return ["user:current", "settings:global"]
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
        if intent.action in {"discuss", "chat"}:
            return 1
        if intent.action in {"batch_draft", "batch_draft_accept", "batch_repair", "continue_run"}:
            return min(100, 1 + chapter_count * (4 + 4 * max(0, intent.max_revision_rounds)))
        if intent.action == "batch_accept":
            return min(100, chapter_count)
        return max(8, sum(item[0] in {"writer", "reviewer"} for item in _WORKFLOWS[intent.action]) * 6)
