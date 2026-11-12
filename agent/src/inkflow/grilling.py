"""将Grill的依赖顺序与分轮追问接入现有协调/待答合同，不另建模型或创作流程。"""
import re
from .errors import ValidationGateError
from .schemas import TerminalIntent

# 参考 GrillMe 的决策依赖与分轮方法，供普通协调和工具续选共用。
GRILL_QUESTION_GUIDANCE = (
    "先从本书资料、已完成步骤和既有回答查明事实。若有两个以上合理选项会明显改变主线结果、人物选择或交付边界，"
    "主动提出当前能决定的问题。给可行选项、各自代价和一项推荐理由，交给作者选择；后题依赖前题时留到下一轮。"
    "已确定、已委托你决定或可以在任务内补齐的内容直接做。答案足够后继续已授权的原目标。"
)

GRILL_CONTRACT = """
Grill是需求理解、询问和后续创作共用的决策方法。
先核对当前作者要求、小说资料、已保存偏好和已答选择。仅在方向/范围仍模糊、关键取舍冲突、
重大规划变动缺少完成条件，或作者要求先推敲时触发question_strategy=grill。
明确、可直接执行且资料足够的请求直接走原工作流；缺章号或原文等事实用普通必要澄清。
原要求已包含制作时保留真实的创作或规划action与authorization，并给出本轮问题；宿主会暂停执行，
将原目标与范围绑定到待答。仅讨论想法而未要求制作时action=discuss，不能虚构制作许可。
先读现有小说资料、已存偏好和本聊天确认过的答案，事实缺口由已有资料核对；不能把查不到写成已证实。
按决策依赖分轮：只问前提已确定、此刻能回答的选择；前提未答的后续问题留到下一轮。
每轮最多3个独立的关键问题，优先目标/范围、因果与代价、冲突取舍、可观察的完成条件。
使用clarification_questions，每题解释why_it_matters，给具体可行选项、最多一个推荐及其取舍，保留自由回答。
不把所有问题一次铺开，不重复已答题，不把推荐、沉默、“不知道”或假设当成作者确认。
grill_summary只整理作者已明确的选择和仍未确定的分歧；不是新作者偏好、书籍设定或正史。
本聊天上一待答的question_strategy=grill时，读其grill_summary并根据本轮真实答案继续推敲。
每轮读pending_user_question.grill_goal及grill_summary，保留原授权范围。
实质未决选择已解决、答案足够且可继续原目标时，grill_ready=true，清空问题并归纳共识。
宿主只在本轮答案仍绑定原题、原目标已获授权且范围未扩大时交回原工作流；没有制作许可时仅归纳讨论。
用户要求结束推敲、换题或不再追问时尊重原话，不用提问数量作为完成标准。
仅讨论交互感受或画面好坏无法得到可靠答案时，说明需要可观察样例，不继续换措辞盘问。
普通缺章号/缺原文、接受门禁、恢复来源等必要澄清仍用question_strategy=clarify，原权限和待答校验不变。
"""

PLANNING_ACTIONS = {"plan", "outline", "redesign_story", "plan_next_arc", "plan_brief"}
GRILL_GOAL_ACTIONS = PLANNING_ACTIONS | {"write_draft", "write_review", "write_review_accept",
    "revise_draft", "revise_review", "revise_review_accept", "batch_draft", "batch_draft_accept", "continue_run"}


def grill_planning_candidate(message: str) -> bool:
    """开放式规划先让协调读资料判定是否需追问；明确局部同步等仍可走本地快捷路由。"""
    if grill_stop(message) or re.search(r"直接(?:开始|执行|做)|沿用.{0,12}(?:已确认|现有)|按.{0,12}已确认", message):
        return False
    return bool(re.search(r"没想好|尚未确定|还没确定|帮我.{0,8}(?:梳理|推敲)|从零|"
                          r"(?:重构|重做|重新设计).{0,12}(?:规划|大纲|细纲|全书)|"
                          r"(?:规划|大纲|细纲).{0,12}(?:方向|选择|取舍|不确定)", message))


def grill_requested(message: str) -> bool:
    """只识别作者显式启用，不从资料引用或模型自述启动推敲。"""
    from .skill_runtime import _instruction_text
    instruction = _instruction_text(message)
    if re.search(r"(?<![\w/@])@(?:(?:builtin|personal|book)/)?planning-grill\b", instruction):
        return not bool(re.search(r"(?:不用|不要|禁用|停用|停止).{0,20}@", instruction))
    return bool(re.match(r"\s*/grill(?:-me)?(?:\s|$)", message, re.I) or re.match(
        r"\s*(?:(?:墨宝|我想|我要)[，,\s]*)?(?:(?:请用|请|用|开启|启动)\s*Grill|(?:先|请)(?:连续追问|追问我|盘问我))", message, re.I))


def grill_stop(message: str) -> bool:
    """选项包装只读实际答案，题干里的“直接开始”不能成为停止/执行授权。"""
    answers = re.findall(r"^我的回答：(.*)$", message, re.M)
    text = "\n".join(answers) if answers else message
    if not answers and re.match(r"\s*(?:请|现在|直接)(?:先)?(?:写|续写|生成|修改|审查)(?:正文|\s*第)", text):
        return True  # 作者明确换到制作要求时，不把它扣在上一轮推敲里。
    return bool(re.search(r"/grill-end\b|(?:换个|另一个|换一个)(?:问题|话题|故事|目标)|(?:结束|退出|停止)(?:这轮|本轮)?\s*(?:Grill|推敲|追问)|"
                          r"(?:不用|不要|无需|别)再(?:问|追问)", text, re.I))


def grill_planning_request(message: str) -> bool:
    """识别从纯讨论转为明确制作的当前原话，问题选项包装不算新的制作请求。"""
    return not re.search(r"^我的回答：", message, re.M) and bool(re.match(
        r"\s*(?:请|现在|直接)(?:先)?(?:规划|制定|制作|生成|重构|重做|重新设计).{0,10}(?:规划|大纲|细纲)", message))


def grill_active(message: str, pending: dict | None) -> bool:
    """模式只沿当前聊天的原待答延续；新聊天没有上一聊天的Grill状态。"""
    previous = (pending or {}).get("intent", {})
    if previous.get("question_strategy") == "grill" and not (pending or {}).get("grill_goal") and grill_planning_request(message):
        return False  # 纯讨论后作者新授予制作要求，交普通路由理解具体目标。
    return not grill_stop(message) and (grill_requested(message) or
        previous.get("action") == "discuss" and previous.get("question_strategy") == "grill")


def constrain_grill(intent: TerminalIntent, message: str, pending: dict | None,
                    goal: dict | None = None) -> TerminalIntent:
    """保留未决选择，共识就绪后按已绑定的原授权继续原创作或规划。"""
    prior = (pending or {}).get("intent", {})
    if prior.get("question_strategy") == "grill" and re.fullmatch(
            r"\s*(?:/grill-end|(?:结束|退出|停止)(?:这轮|本轮)?\s*(?:Grill|推敲|追问))[。！!\s]*", message, re.I):
        return intent.model_copy(update={"action": "discuss", "question_strategy": "clarify",
            "authorization": "none", "authorization_source": "none", "clarification_questions": [],
            "clarification_question": "", "response_kind": "question_answer", "pending_question_id": pending["id"],
            "preference_observations": [], "quality_stages": [],
            "conversation_reply": "推敲已结束。" + (prior.get("grill_summary") or "已有对话保留，尚未安排创作执行。")})
    active = grill_active(message, pending)
    if not active and (intent.action not in PLANNING_ACTIONS | {"discuss"} or intent.question_strategy != "grill" or grill_stop(message)):
        return intent.model_copy(update={"question_strategy": "clarify"})
    continuing = active and prior.get("question_strategy") == "grill" and not grill_requested(message)
    questions = bool(intent.clarification_questions or intent.clarification_question.strip())
    if (not continuing and intent.action in GRILL_GOAL_ACTIONS and intent.grill_ready
            and not questions and not intent.missing_fields and intent.confidence == "high"):
        return intent.model_copy(update={"question_strategy": "clarify"})  # 已知信息足够，不为完成访谈形式阻断原请求。
    if (continuing and intent.grill_ready and not questions and not intent.missing_fields
            and intent.confidence == "high" and goal and goal.get("action") in GRILL_GOAL_ACTIONS
            and (goal.get("authorization") == "approved" or re.fullmatch(
                r"\s*(?:现在|直接)?(?:开始|开始规划|开始制作|按这个方案(?:执行|制作|规划))[。！!\s]*", message))):
        source = TerminalIntent.model_validate(goal)
        instruction = ((source.operation_instruction or source.user_message or source.requested_outcome)
                       + "\n作者已确认的选择：" + intent.grill_summary)
        if len(instruction) > 4_000:
            raise ValidationGateError("原要求和已答选择超出了本次指令容量。原任务保留，请简要补充当前问题。")
        updates = {"action": source.action, "question_strategy": "clarify", "authorization": "approved",
            "authorization_source": source.authorization_source if source.authorization == "approved" else "current_request", "response_kind": "question_answer",
            "pending_question_id": pending["id"], "related_task_id": pending.get("task_id"),
            "preference_observations": [], "user_message": message,
            "grill_ready": True, "grill_round": int(prior.get("grill_round", 0)) + 1,
            "grill_summary": intent.grill_summary, "clarification_question": "", "clarification_questions": [], "missing_fields": [],
             "operation_instruction": instruction,
            "forbidden_actions": list(dict.fromkeys([*source.forbidden_actions, *intent.forbidden_actions])),
            "preserve_constraints": list(dict.fromkeys([*source.preserve_constraints, *intent.preserve_constraints]))}
        # 原话明确指定的边界保留；原本未定的参数才由本轮选择补齐，不拿新模型猜测扩章。
        for field in ("chapter_no", "end_chapter_no", "planning_part", "outline_level", "planning_total_chapters", "planning_volume_count"):
            if field == "end_chapter_no" and source.target_characters is not None:
                continue
            original = getattr(source, field)
            if field not in goal or original is None or original == "none":
                updates[field] = getattr(intent, field)
        if source.end_chapter_no is None and source.target_characters is None and updates.get("end_chapter_no") is None:
            updates["target_characters"] = intent.target_characters
        # 从原目标恢复，不能带回新模型顺手加入的质量轮次、发布确认或其他制作权限。
        return source.model_copy(update=updates)
    updates = {"action": "discuss", "question_strategy": "grill", "authorization": "none",
        "authorization_source": "none", "alternative_action": None, "quality_stages": [], "setting_change": {},
        "preference_observations": [], "narrative_scope": "none", "edit_scope": "none",
        "grill_round": int(prior.get("grill_round", 0)) + 1 if continuing else 1,
        "grill_summary": intent.grill_summary or (prior.get("grill_summary", "") if continuing else "")}
    if continuing:
        updates.update(response_kind="question_answer", pending_question_id=pending["id"])
    if intent.action != "discuss" and questions:
        updates["conversation_reply"] = "先确认下面的选择，答完接着处理原任务。" if goal else "先把下面的创作选择确定。"
    if intent.action != "discuss" and not questions and not intent.grill_ready:
        updates.update(conversation_reply="先把关键选择确定，再接着原任务。" if goal else "先把关键选择确定。",
            clarification_questions=[], clarification_question="下一步你最需要墨宝帮你确定哪一个创作选择？")
    return intent.model_copy(update=updates)
