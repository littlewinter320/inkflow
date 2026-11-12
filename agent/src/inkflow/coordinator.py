"""共享目标权限、参考流程与模型选择的工具计划；执行绑定原任务配置。"""
from __future__ import annotations

import re
from graphlib import CycleError, TopologicalSorter
from uuid import uuid4

from .project import InkFlowProject
from .role_protocol import CollaborationMode, check_owners_for_mode, new_task_mode, roles_for_mode
from .schemas import BookBrief, DispatchPlan, DispatchStep, RoleCapability, TaskTicket, TerminalIntent
from .errors import ValidationGateError


# 各自然语言快捷入口共用正史动作词，否定仍优先，避免“转正史”被续接入口漏掉。
ACCEPTANCE_PATTERN = re.compile(r"验收|接收|接受|入正史|进入正史|收进(?:正史|正文)?|收进去|定稿|(?:转|变)(?:为|成|入)?正史")
NO_ACCEPTANCE_PATTERN = re.compile(
    r"(?:只|仅)(?:要|做|生成|给我看)?(?:草稿|审查|复审|初稿)|"
    r"(?:不要|暂不|暂时不|暂时别|先别|别|先不|不必|无需|不能|不可|不)"
    r"(?:再|先|自动|直接|立即|马上|现在|擅自|默认|替我|帮我)*"
    r"(?:验收|接收|接受|入正史|进入正史|(?:转|变)(?:为|成|入)?正史|写入正史|写进正史|提交正史|收进正史|收进正文|收进去|定稿|收(?=[，,。！？；\s]|$))|"
    r"(?:不要|暂不|暂时不|暂时别|先别|别|先不)(?:修改正文|改稿|写正文)或(?:验收|接收|入正史)|"
    r"草稿(?:即可|就好|先看)|(?:写好|写完)?(?:保存|存成|存为)草稿(?:后|就|即|便)*(?:停|结束)|"
    r"(?:我想|让我|我)先(?:看|看看|过目)(?:一下)?|先给我看"
)


ROLE_CAPABILITIES: tuple[RoleCapability, ...] = (
    RoleCapability(
        role="coordinator",
        novel_production_agent=False,
        can=["理解多样自然语言", "合并补充说明", "拆解任务", "识别进度和待处理事项并安排关联任务", "在当前授权内自主组合工具与依赖", "根据真实返回调整后续步骤", "提出最小澄清", "汇总分歧", "通过 Novel Engine 请求白名单设置变更"],
        cannot=["写正文", "审查正文", "提交正史", "直接写设置文件或数据库", "绕过引擎门禁", "自行扩权"],
    ),
    RoleCapability(
        role="writer",
        novel_production_agent=True,
        can=["规划", "写作", "按证据定点修订", "在同次交稿中解释叙事意图、提出设定候选并回答原审核角色的定点问题"],
        cannot=["批准自己的正文", "写入正史", "修改审核规则"],
    ),
    RoleCapability(
        role="editor", novel_production_agent=True,
        can=["日常综合审读", "给出定点修订建议", "复审同版本正文", "为其他角色定位原文、解释表达分歧和提供反证", "仅在承担记忆职责时产出记忆候选"],
        cannot=["直接改正文", "代替专项角色完成检查", "提交正史", "批准未审版本"],
    ),
    RoleCapability(
        role="reviewer", novel_production_agent=True,
        can=["按指定范围检查因果和连续性", "引用证据报告硬问题和缺失依据", "在已启用职责内协助定位来源、核对因果并提出有据替代解读"],
        cannot=["默认重复综合编辑", "直接改正文", "产出记忆补丁", "提交正史"],
    ),
    RoleCapability(
        role="memory_keeper", novel_production_agent=True,
        can=["整理有来源的事实与人物认知", "提出时间线和伏笔变更", "报告记忆冲突", "为写作和审核整理已核来源、人物认知与参考边界"],
        cannot=["批准正文文学质量", "无依据改写事实", "自行修改剧情", "提交正史"],
    ),
)


_REFERENCE_WORKFLOWS: dict[str, tuple[tuple[str, str, str, str], ...]] = {
    "pending_work": (("engine", "work.pending", "", "待处理事项、关联任务与用户处理选择"),),
    "status": (("engine", "status.read", "", "项目状态"),),
    "knowledge_query": (("engine", "knowledge.query", "", "沿知识关系与原文回答，并说明实际阅读范围"),),
    "help": (("engine", "help.read", "", "使用说明"),),
    "plan": (("writer", "plan.generate", "", "四级规划"),),
    "plan_preview": (("engine", "plan.preview", "", "规划预览"),),
    "planning_history_view": (("engine", "planning.history.read", "", "按用户指令查看旧版"),),
    "planning_history_restore": (("engine", "planning.history.restore", "", "恢复为新生效版本"),),
    "planning_publish_reviewed": (("engine", "planning.reviewed.publish", "", "按用户确认发布审核通过的规划"),),
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
    "story_settings_manage": (("engine", "setting.collection.manage", "", "按用户定义管理设定合集、记录与证据候选"),),
    "revise_selection": (("writer", "chapter.revise_selection", "", "指定选区的替换候选"),),
    "write_review": (
        ("writer", "chapter.write", "", "章节草稿"),
        ("engine", "chapter.review_mode", "step-1", "证据化审查报告"),
    ),
    "write_review_accept": (
        ("writer", "chapter.write", "", "章节草稿"),
        ("engine", "chapter.review_mode", "step-1", "当前版本审查报告"),
        ("engine", "chapter.accept", "step-2", "正史记忆补丁与提交结果"),
    ),
    "review": (("engine", "chapter.review_mode", "", "证据化审查报告"),),
    "revise_draft": (("writer", "chapter.revise", "", "新草稿版本"),),
    "repair_accepted": (("engine", "chapter.repair_accepted", "", "正史疑点诊断、局部候选与复核结果"),),
    "revise_review": (
        ("writer", "chapter.revise", "", "新草稿版本"),
        ("engine", "chapter.review_mode", "step-1", "新版本审查报告"),
    ),
    "review_accept": (
        ("engine", "chapter.review_mode", "", "当前版本审查报告"),
        ("engine", "chapter.accept", "step-1", "正史记忆补丁与提交结果"),
    ),
    "revise_review_accept": (
        ("writer", "chapter.revise", "", "新草稿版本"),
        ("engine", "chapter.review_mode", "step-1", "新版本审查报告"),
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

# 工具权限独立于参考流程的排列；模型选择组合，引擎仍执行各工具原有的版本和提交门禁。
CHAPTER_TOOLS = {
    "status.read": ("engine", "项目状态"),
    "chapter.read": ("engine", "当前章节与审核状态"),
    "source.search": ("engine", "有身份与版本的检索来源"),
    "plan.preview": ("engine", "当前生效规划"),
    "chapter.write": ("writer", "章节草稿"),
    "chapter.revise": ("writer", "局部修订后的草稿"),
    "chapter.review_mode": ("engine", "当前职责的审核与记忆候选"),
    "chapter.accept": ("engine", "当前版本的正史提交回执"),
}
CHAPTER_TOOL_GOALS = frozenset({"write_draft", "write_review", "write_review_accept", "revise_draft",
    "revise_review", "revise_review_accept", "review", "review_accept", "accept", "batch_draft",
    "batch_draft_accept", "batch_repair", "batch_accept", "continue_run"})


def authorized_chapter_tools(intent: TerminalIntent) -> list[str]:
    """从原用户目标与禁止项确定工具权限，不把模型选择本身当作授权。"""
    allowed = {"status.read", "chapter.read", "source.search", "plan.preview"}
    if intent.action not in CHAPTER_TOOL_GOALS:
        return []
    if intent.action.startswith("write_") or intent.action in {"batch_draft", "batch_draft_accept", "continue_run"}:
        allowed.add("chapter.write")
    if (intent.action.startswith("revise_") or intent.action in {
            "write_review", "write_review_accept", "batch_draft", "batch_draft_accept", "continue_run"}
            or intent.action == "batch_repair" and not intent.batch_review_only):
        allowed.add("chapter.revise")
    if intent.action in _REVIEW_ACTIONS - {"accept", "batch_accept"}:
        allowed.add("chapter.review_mode")
    if intent.action.endswith("_accept") or intent.action in {"accept", "continue_run"}:
        allowed.add("chapter.accept")
    for forbidden in intent.forbidden_actions:
        # 旧断点可保存动作别名或实际工具名，两者都不能因表示形式不同而失效。
        allowed.discard({"write": "chapter.write", "revise": "chapter.revise",
                        "review": "chapter.review_mode", "accept": "chapter.accept"}.get(forbidden, forbidden))
    return sorted(allowed)


def _mode_template(
    action: str, collaboration_mode: CollaborationMode = "everyday"
) -> tuple[tuple[str, str, str, str], ...]:
    template = _REFERENCE_WORKFLOWS[action]
    return tuple(
        (check_owners_for_mode(collaboration_mode).get("logic_continuity", "editor"), operation, dependency, output)
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
        """依据明确原话收束目标；只归一等价入口，模糊范围及未授权工具仍由门禁拒绝。"""
        text = user_text.strip()
        updates: dict[str, object] = {"user_message": text[:4_000]}
        action = intent.action
        # Conditional repair is review-first, not evidence-free rewriting.
        review_after_revision = bool(re.search(
            r"(?:改完|改好|修订(?:完成)?后|修改(?:完成)?后|修好|完成后).{0,8}(?:再|重新)?(?:审查|审核|检查|复审)", text
        )) and not re.search(r"(?:先|首先)(?:帮我|直接|重新|仔细|由|让)?\s*(?:编辑|Editor|Reviewer|审查角色)?\s*(?:来)?(?:审查|审核|检查|复审|核对)", text)
        # “有问题才修好，审核通过后…”仍是先核实；不能被后半句“修好…审核”误改为先写。
        if action in {"revise_draft", "revise_review", "revise_review_accept"} and re.search(
            r"(?:有|发现|如果|若).{0,10}(?:问题|错误|不通过|没过).{0,12}(?:改|修)", text
        ) and re.search(r"审|检查|核对", text):
            action = "revise_review_accept" if action.endswith("_accept") else "revise_review"
            updates["review_before_revision"] = True
        elif action in {"revise_draft", "revise_review", "revise_review_accept"} and review_after_revision:
            updates["review_before_revision"] = False
        no_accept = NO_ACCEPTANCE_PATTERN.search(text)
        no_review = re.search(r"(?:别|不要|不用|不|先不|暂不|暂时不|暂时别|不必|无需)(?:再|做|进行|追加|自动|重新|任何|额外|单独|一次|了|\s){0,8}(?:审查|审核|复审)", text)
        # Negation must govern the writing task itself. "别把丢失的原件写回来"
        # constrains story content; it does not mean "别写这一章".
        no_write = re.search(
            r"(?:别|不要|先不|不必|无需|暂不|暂时不|暂时别)(?:现在|先|自动)?(?:写|起草|生成)"
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
        if no_write and not discuss_first and action in {"write_draft", "write_review", "write_review_accept", "scene_draft"} and re.search(
            r"(?:先|请|只)?(?:审|审查|审核|复审|核对)\s*第\s*\d+\s*章", text
        ):
            forbidden.append("write")
            action = "review"
            updates["narrative_scope"] = "chapter"
        elif discuss_first or (no_write and action in {"write_draft", "write_review", "write_review_accept", "scene_draft"}):
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
        if (action in {"batch_draft_accept", "write_review_accept"}
                and intent.end_chapter_no is not None and len(intent.quality_stages) <= 1
                and re.search(r"(?:每章|逐章)[^。！？；\n]{0,24}(?:通过|合格|审好|审完|没问题|没有问题|无问题|(?:审[核查]|复核)(?:完成|完毕|好|完))[^。！？；\n]{0,16}(?:转为?正史|变为正史|收进正史|接收|接受)", text)
                and not re.search(r"(?:一起|整批|统一)[^。！？；\n]{0,8}(?:转为?正史|变为正史|收进正史|接收|接受)", text)
                and not re.search(r"(?:不要|别|不必|先不|暂不)[^。！？；\n]{0,4}(?:每章|逐章)", text)
                and "accept" not in forbidden and "write" not in forbidden):
            # “审好/没问题就转为正史”等表达沿逐章门禁执行；范围与单一档位不改变收一章再写一章的顺序。
            action = "continue_run"
            updates.update(narrative_scope="batch",
                visible_reason="按本次目标逐章推进；每章通过必要审查和记忆交接后转正史，再处理下一章。")
        if action == "scene_draft" and len(intent.tool_steps) == 1:
            scene_step = intent.tool_steps[0]
            # 独立场景沿现有固定Writer入口执行，不属于章节工具图。模型偶尔
            # 把同一动作再填成scene_draft/scene.draft；只移除这个严格等价声明，
            # 改章、接受、额外节点、依赖或其他角色仍保留并由原授权检查拒绝。
            if (scene_step.operation in {"scene_draft", "scene.draft"}
                    and scene_step.role == "writer" and scene_step.when == "always"
                    and not scene_step.depends_on and scene_step.chapter_no in {None, intent.chapter_no}):
                updates["tool_steps"] = []
                if not intent.operation_instruction.strip() and scene_step.instruction.strip():
                    updates["operation_instruction"] = scene_step.instruction
        updates["action"] = action
        updates["forbidden_actions"] = list(dict.fromkeys(forbidden))
        return intent.model_copy(update=updates)

    def compile(
        self, intent: TerminalIntent, *, role_protocol_version: int | None = None,
        collaboration_mode: CollaborationMode = "everyday",
        task_snapshot_hash: str | None = None,
    ) -> tuple[TaskTicket, DispatchPlan]:
        """把模型目标编译为绑定原权限的计划，执行者由已登记工具确定。"""
        if intent.user_message:
            intent = self.normalize_intent(intent, intent.user_message)
        collaboration_mode = new_task_mode(collaboration_mode)
        roles_for_mode(collaboration_mode)
        if not task_snapshot_hash:
            raise ValueError("工作流计划必须绑定已冻结的任务配置快照")
        # 方法技能继续供责任角色阅读；流程技能只产出原有DispatchStep，再走相同授权校验。
        from .skill_workflows import compile_workflow_steps, bind_workflow_plan
        from .task_settings import active_task_settings
        skill_steps, workflow_skill = compile_workflow_steps(intent)
        if skill_steps is not None:
            intent = intent.model_copy(update={"tool_steps": skill_steps})
        template = _mode_template(intent.action, collaboration_mode)
        steps: list[DispatchStep] = []
        for index, (role, operation, dependency, output) in enumerate(template, 1):
            gate = ""
            if operation == "chapter.review_mode":
                gate = "引擎按模式执行独占检查责任；同版本结果汇合并校验覆盖、记忆、权限和预算"
            elif operation == "scene.draft":
                gate = "仅 Writer 生成隔离草稿；不审查、不验收、不写正史"
            elif operation == "setting.update":
                gate = "仅用户明确授权的设定变更；引擎保存新版本、差异与影响范围"
            elif operation == "chapter.revise_selection":
                gate = "先冻结目标选区与来源版本；Writer 仅返回该范围替换候选"
            elif operation == "chapter.repair_accepted":
                gate = "仅最新已接受章的可定位疑点；审读诊断、Writer 单处补句、独立复核、保留旧版并校验版本后提交"
            elif role in {"editor", "reviewer", "memory_keeper"}:
                gate = "仅完成当前模式分配的责任；必须绑定来源版本并引用证据"
            elif operation == "chapter.accept":
                gate = "仅当前版本满足本次模式的全部必需审查、记忆候选及用户接受授权后提交；force=false"
            elif operation in {"chapter.write", "batch.draft_loop", "chapter.gated_loop"}:
                gate = "沿有效前文及作者要求写作；缺少小纲时在当前任务内准备临时目标，正式规划独立保存"
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
        # 批次接受绑定来源晋升，逐章续写绑定每章通过即提交；不能拆成失去这些保证的普通工具。
        # 模型仍决定目标与授权范围，其他任务保留自由选工具，事务本身走现有引擎门禁。
        chapter_sequence = intent.action in {"batch_accept", "continue_run"} or (
            intent.action in {"batch_draft", "batch_draft_accept", "batch_repair"}
            and intent.end_chapter_no != intent.chapter_no)
        selected_tools = bool(intent.tool_steps) and not chapter_sequence
        if selected_tools:
            if intent.action not in CHAPTER_TOOL_GOALS:
                raise ValueError("本次目标没有章节工具的执行授权。")
            steps = [item.model_copy(update={"chapter_no": item.chapter_no or intent.chapter_no,
                # 审核工具的执行者是引擎，模型常误填实际审核角色；仅纠正这一明确的职责层次混淆。
                "role": "engine" if item.operation == "chapter.review_mode"
                    and item.role in {"editor", "reviewer", "memory_keeper"} else item.role,
                "required_output": CHAPTER_TOOLS.get(item.operation, ("", "未知工具"))[1],
                "gate": "执行前核用户范围和当前版本；结果继续经过原工具的证据与提交门禁"})
                for item in intent.tool_steps]
        chapter = self.project.db.get_chapter(intent.chapter_no) if intent.chapter_no else None
        chapter_count = max(1, (intent.end_chapter_no or intent.chapter_no or 1) - (intent.chapter_no or 1) + 1)
        estimated_calls = self._model_call_budget(intent, chapter_count)
        if intent.action in _REVIEW_ACTIONS:
            role_count = len(set(check_owners_for_mode(collaboration_mode).values()))
            estimated_calls = min(100, max(estimated_calls, role_count * 6 * chapter_count))
        ticket = TaskTicket(
            collaboration_mode=collaboration_mode,
            task_snapshot_hash=task_snapshot_hash,
            ticket_id=f"ticket-{uuid4().hex}",
            objective=intent.requested_outcome.strip() or intent.visible_reason,
            user_message=intent.user_message,
            task_revision=intent.task_revision,
            related_task_id=intent.related_task_id,
            pending_work_id=intent.pending_work_id,
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
                "Editor、Reviewer 与 Memory Keeper 按模式分工；审查角色不直接修改正文",
                "记忆服务 不读取未批准草稿写正史",
                "最多两轮 Agent 方向讨论，仍有分歧则交给用户",
                "用户表达方式不绑定固定命令；同一目标允许多条合理实现路径",
                "失败时先在授权范围内复用成果、缩小范围或切换可用路径，再决定是否需要用户介入",
                "已授权正文范围内的必要补卡由引擎衔接 Writer，不额外逐章调用 Coordinator 或扩大章节范围",
            ],
            authorized_operations=authorized_chapter_tools(intent),
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
            collaboration_mode=collaboration_mode,
            task_snapshot_hash=task_snapshot_hash,
            required_checks=(
                list(check_owners_for_mode(collaboration_mode))
                if intent.action in _REVIEW_ACTIONS else []
            ),
            check_owners=(
                check_owners_for_mode(collaboration_mode)
                if intent.action in _REVIEW_ACTIONS else {}
            ),
            memory_owner=(
                check_owners_for_mode(collaboration_mode)["memory"]
                if intent.action in _REVIEW_ACTIONS else None
            ),
            return_to_base=collaboration_mode != "everyday",
            workflow=intent.action,
            selection_source="agent" if selected_tools else "reference",
            steps=steps,
            parallel=(
                not selected_tools and collaboration_mode in {"review_boost", "full_specialist"}
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
        if workflow_skill is not None:
            bind_workflow_plan(active_task_settings.get(), workflow_skill, plan.steps, intent=intent,
                skipped=workflow_skill.get("_compiled_skipped", []), rounds=workflow_skill.get("_compiled_rounds", {}))
        return ticket, plan

    @staticmethod
    def validate(plan: DispatchPlan, ticket: TaskTicket | None = None) -> None:
        allowed_roles = roles_for_mode(plan.collaboration_mode)
        if not plan.task_snapshot_hash:
            raise ValueError("工作流计划缺少任务配置快照")
        template = _REFERENCE_WORKFLOWS.get(plan.workflow)
        if template is None:
            raise ValueError("任务目标未登记，无法确定对应工具权限。")
        allowed = _mode_template(plan.workflow, plan.collaboration_mode)
        actual = [(item.role, item.operation, item.depends_on, item.required_output) for item in plan.steps]
        expected = [(role, operation, [dependency] if dependency else [], output)
                    for role, operation, dependency, output in allowed]
        expected_parallel = (
            plan.collaboration_mode in {"review_boost", "full_specialist"}
            and any(operation in {"chapter.review_mode", "batch.draft_loop", "batch.repair_loop", "chapter.gated_loop"}
                    for _, operation, _, _ in allowed)
        )
        if plan.selection_source == "agent":
            if ticket is None or plan.workflow not in CHAPTER_TOOL_GOALS or not plan.steps or plan.parallel:
                raise ValueError("工具计划缺少原目标、范围或任务凭据。")
            identities = {step.step_id for step in plan.steps}
            if len(identities) != len(plan.steps) or any(not identity.strip() for identity in identities):
                raise ValueError("工具步骤必须具有唯一身份。")
            lower, upper = ticket.chapter_no, ticket.end_chapter_no or ticket.chapter_no
            if not lower or not upper or lower > upper:
                raise ValueError("工具计划必须绑定明确的原章节范围。")
            for step in plan.steps:
                capability = CHAPTER_TOOLS.get(step.operation)
                if (capability is None or step.operation not in ticket.authorized_operations
                        or step.role != capability[0] or set(step.depends_on) - identities):
                    raise ValidationGateError("协调计划中的工具、执行角色或前置步骤不符合本次授权，尚未执行。请按原任务核对派工后继续。")
                if step.operation not in {"status.read", "plan.preview"}:
                    start = 1 if step.operation in {"chapter.read", "source.search"} else lower
                    if not start or not upper or not step.chapter_no or not start <= step.chapter_no <= upper:
                        raise ValidationGateError(
                            f"这一步指定第{step.chapter_no or '未指定'}章，超出本次允许的第{start}—{upper}章；"
                            "尚未执行，请核对任务总范围与分步章节。")
            try:
                TopologicalSorter({step.step_id: step.depends_on for step in plan.steps}).prepare()
            except CycleError as exc:
                raise ValueError("工具计划的依赖形成循环。") from exc
        elif actual != expected or plan.parallel != expected_parallel:
            raise ValueError("参考流程记录与实际执行入口不一致。")
        if any(
            item.role not in allowed_roles for item in plan.steps if item.role != "engine"
        ):
            raise ValueError("工作流包含当前协作模式未启用的角色")
        expected_checks = (
            check_owners_for_mode(plan.collaboration_mode)
            if plan.workflow in _REVIEW_ACTIONS else {}
        )
        if (plan.required_checks != list(expected_checks)
                or plan.check_owners != expected_checks
                or plan.memory_owner != expected_checks.get("memory")
                or plan.return_to_base != (plan.collaboration_mode != "everyday")
                or any(owner not in allowed_roles for owner in plan.check_owners.values())):
            raise ValueError("检查覆盖或记忆责任不符合协作模式")
        if ticket is not None:
            if (ticket.collaboration_mode != plan.collaboration_mode
                    or ticket.task_snapshot_hash != plan.task_snapshot_hash
                    or ticket.max_model_calls < len(set(plan.check_owners.values()))
                    or ticket.max_tokens < ticket.max_model_calls):
                raise ValueError("计划与任务快照或调用预算不一致")

    @staticmethod
    def workflow_reference(action: str, collaboration_mode: CollaborationMode = "everyday") -> dict:
        """从实际登记流程给出当前目标的默认顺序与变更边界，不另建一套可执行流程。"""
        steps = _mode_template(action, collaboration_mode)
        return {
            "workflow": action,
            "reference_only": True,
            "entry": "原目标、范围、配置和权限已确定；先核必要来源和当前版本，缺口转补读。",
            "default_steps": [{"id": f"step-{index}", "role": role, "operation": operation,
                "depends_on": [dependency] if dependency else [], "output": output}
                for index, (role, operation, dependency, output) in enumerate(steps, 1)],
            "completion": "以当前产物、有效审查和原目标所需提交回执为准；模型自述不算完成。",
            "routine_steps": (
                ["读取当前话题与必要前文", "直接回答或修改指定表达", "保存对话；无生产请求则结束"]
                if action in {"chat", "discuss", "ideate"} else
                ["定位指定大纲/细纲/近期计划及版本", "复用有效上层，只生成或修改目标部分", "保存候选和差异，按原授权处理后续"]
                if action in {"plan", "outline", "plan_next_arc", "redesign_story", "plan_brief"} else
                ["定位集合、记录与当前版本", "按来源修改指定字段或生成候选", "保存差异、来源和记录身份；仅修改记录时结束"]
                if action in {"story_settings_manage", "story_setting_edit"} else
                ["定位现有产物和目标范围", "复用有效结果，处理本次指定的生成或修改", "保存版本、差异和回执，按原目标结束或续接"]),
            "small_artifacts": "钩子与伏笔先定位本章目标和已有线索，只更新指定内容及推进记录；记忆先定位来源和事实身份，只更新受影响条目。明确不审核的修改保存为未审核版本，不追加模型审查，不自动成为正史。",
            "adaptation": {
                "source_gap": "退回产生缺口的最近前置步骤，调整取材、规划或交付内容，保存完善后的结果，再续受影响步骤；不机械重跑全链。",
                "review_gap": "引文缺失或疑似误读直接请作者抉择，展示原文依据和争议；选择前保留正文及报告，不追加自动补核。",
                "confirmed_error": "按授权局部修改并保存差异记录；明确不审核时到保存结束，后续接受正史另核权限和当前版本条件。",
                "version_changed": "重核受影响依赖；旧结果保留，不覆盖用户新版本。",
                "response_unknown": "先核保存返回、发送账本和提交状态；不自动重发未知付费请求。",
                "local_file_failure": "等待或恢复本地访问条件，不靠增加模型调用解除占用。",
            },
            "reordering": "有真实返回支持时可跳过有效完成项、补前置或重排剩余依赖；"
                "说明变化原因，只选本轮白名单工具，循环及批次提交仍由原引擎管理，不扩大权限或重置次数。",
        }

    @staticmethod
    def capabilities(protocol_version: int | None = None) -> list[dict]:
        return [item.model_dump(mode="json") for item in ROLE_CAPABILITIES]

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
        if intent.planning_revision_no is not None:
            sources.append(f"planning_history:revision:{intent.planning_revision_no}:{intent.planning_part}")
        return sources

    @staticmethod
    def _model_call_budget(intent: TerminalIntent, chapter_count: int) -> int:
        if intent.action in {"pending_work", "status", "help", "plan_preview", "planning_history_view", "planning_history_restore", "planning_publish_reviewed", "checkpoint_list", "rollback_preview", "exit"}:
            return 0
        if intent.action in {"discuss", "chat"}:
            return 1
        if intent.action == "batch_repair" and intent.batch_review_only:
            return min(100, chapter_count * 4)
        if intent.action in {"batch_draft", "batch_draft_accept", "batch_repair", "continue_run"}:
            return min(100, 1 + chapter_count * (4 + 4 * max(0, intent.max_revision_rounds)))
        if intent.action == "batch_accept":
            return min(100, chapter_count)
        return max(8, sum(item[0] in {"writer", "reviewer"} for item in _REFERENCE_WORKFLOWS[intent.action]) * 6)
