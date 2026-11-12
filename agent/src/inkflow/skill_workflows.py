"""Skill的有限流程声明接到原Coordinator工具图；不另建小说执行器。"""
from __future__ import annotations

import json
import re
from graphlib import CycleError, TopologicalSorter
from typing import Any
from pydantic import Field

from .errors import ValidationGateError
from .skills import _canonical, skill_connection
from .utils import content_hash, utc_now
from .schemas import TerminalIntent


# 原合同含稍后声明的澄清问题类型。先在它自己的模块命名空间内完成解析，
# 再派生技能路由合同，避免跨模块继承把未解析引用带到首次模型请求中。
TerminalIntent.model_rebuild()


class SkillRoutedIntent(TerminalIntent):
    """仅新布局在原Coordinator请求顺带选流程，不改变旧任务Schema或新增模型请求。"""
    workflow_skill_id: str | None = Field(default=None, max_length=80,
        description="从冻结技能目录选择一个适用于action的流程技能ID；无匹配留空，实质分歧须提问。")
    workflow_skill_confidence: str = Field(default="low", pattern="^(high|medium|low)$")
    workflow_skill_reason: str = Field(default="", max_length=240,
        description="简短可核对的选择依据；不得输出隐藏思考或把历史观察作为权限。")


CONDITIONS = {"always", "draft_missing", "revision_needed", "review_needed", "ready_to_accept"}


def validate_skill_workflow(flow: Any) -> None:
    """只接受已登记整体流程或有限章节DAG，不接受代码表达式、循环或新工具。"""
    from .coordinator import CHAPTER_TOOLS, CHAPTER_TOOL_GOALS, _REFERENCE_WORKFLOWS
    if not isinstance(flow, dict) or type(flow.get("schema_version")) is not int or flow.get("schema_version") != 1:
        raise ValidationGateError("技能流程需要schema_version=1。")
    executor = flow.get("executor")
    actions = flow.get("actions")
    if (not isinstance(executor, str) or executor not in {"chapter_tools", "registered_workflow"}
            or not isinstance(actions, list) or not actions
            or any(not isinstance(action, str) or action not in _REFERENCE_WORKFLOWS for action in actions)):
        raise ValidationGateError("流程只能接入墨流已登记的目标入口。")
    if executor == "registered_workflow":
        if set(flow) - {"schema_version", "executor", "actions", "description"}:
            raise ValidationGateError("事务流程由原引擎执行，不接受技能重写其内部提交步骤。")
        return
    if set(actions) - CHAPTER_TOOL_GOALS or set(flow) - {"schema_version", "executor", "actions", "steps", "description"}:
        raise ValidationGateError("章节流程包含未支持的目标或字段。")
    steps = flow.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValidationGateError("章节流程需要1至20个有限步骤。")
    identities = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
                "id", "tool", "depends_on", "when", "required", "default", "actions", "review_order", "round", "instruction"}:
            raise ValidationGateError("技能流程步骤含未登记字段。")
        identity = step.get("id")
        if (not isinstance(identity, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", identity)
                or identity in identities or not isinstance(step.get("tool"), str) or step.get("tool") not in CHAPTER_TOOLS):
            raise ValidationGateError("流程步骤需要唯一ID和已登记工具。")
        identities.add(identity)
        if (not isinstance(step.get("when", "always"), str) or step.get("when", "always") not in CONDITIONS or type(step.get("required", True)) is not bool
                or type(step.get("default", False)) is not bool
                or not isinstance(step.get("review_order", "any"), str) or step.get("review_order", "any") not in {"any", "before", "after"}
                or type(step.get("round", 0)) is not int or not 0 <= step.get("round", 0) <= 2
                or not isinstance(step.get("instruction", ""), str) or len(step.get("instruction", "")) > 1000):
            raise ValidationGateError("流程的条件、必做开关、修订轮次或说明不合法。")
        dependencies = step.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(value, str) for value in dependencies):
            raise ValidationGateError("流程依赖须为步骤ID列表。")
        if "actions" in step and (not isinstance(step["actions"], list)
                or any(not isinstance(action, str) for action in step["actions"]) or set(step["actions"]) - set(actions)):
            raise ValidationGateError("步骤适用目标须在本技能已登记目标内。")
    if any(set(step.get("depends_on", [])) - identities for step in steps):
        raise ValidationGateError("技能流程引用了不存在的前置步骤。")
    try:
        TopologicalSorter({step["id"]: step.get("depends_on", []) for step in steps}).prepare()
    except CycleError as exc:
        raise ValidationGateError("技能步骤形成循环，请改为有限且可停止的依赖。") from exc


def workflow_candidates(scope: Any, intent: Any) -> list[dict[str, Any]]:
    """只在冻结且已启用的版本内匹配目标和角色，显式指定优先。"""
    from .skill_runtime import effective_skill_versions, frozen_library, _request_selection
    from .role_protocol import roles_for_mode
    selection = _request_selection(scope)
    instruction = selection.get("instruction", "").casefold()
    skills = effective_skill_versions(frozen_library(scope)["skills"], selection["selected"])
    candidates = [item for item in skills if item.get("workflow") and intent.action in item["workflow"]["actions"]
        and set(item["adapter"]["roles"]) & set(roles_for_mode(scope.effective_collaboration_mode))
        and (item["id"] in selection["selected"] or item["automatic"] and item["adapter"]["activation"] == "automatic"
            and (not item["adapter"].get("keywords") or any(word.casefold() in instruction for word in item["adapter"]["keywords"]))
            and not any(word.casefold() in instruction for word in item["adapter"].get("exclude_keywords", [])))]
    explicit = [item for item in candidates if item["id"] in selection["selected"]]
    return explicit or candidates


def workflow_choice(scope: Any, action: str) -> dict[str, Any] | None:
    """本任务每个目标保留已选版本及依据，恢复不因近期评分变化而重选。"""
    with skill_connection() as connection:
        row = connection.execute("SELECT data_json FROM task_workflow_choices WHERE task_id=? AND action=?",
            (scope.task_id, action)).fetchone()
    return json.loads(row[0]) if row else None


def bind_routed_choice(scope: Any, intent: SkillRoutedIntent) -> None:
    """验证原Coordinator同次返回的选择；低把握不会偷偷选中，也不另调模型补选。"""
    if not intent.workflow_skill_id or intent.workflow_skill_confidence != "high" or not intent.workflow_skill_reason.strip():
        return
    candidates = workflow_candidates(scope, intent)
    item = next((item for item in candidates if item["id"] == intent.workflow_skill_id), None)
    if item is None:
        return  # 模型返回目录外的ID没有授权意义，沿正常澄清边界处理。
    _save_choice(scope, intent.action, item, "coordinator", intent.workflow_skill_reason)


def _save_choice(scope: Any, action: str, item: dict[str, Any], source: str, reason: str) -> None:
    """只有待选项可转成确定选择，既有确定选择不能由模型或恢复悄悄覆盖。"""
    with skill_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT data_json FROM task_workflow_choices WHERE task_id=? AND action=?",
            (scope.task_id, action)).fetchone()
        previous = json.loads(row[0]) if row else None
        if previous and previous.get("status") == "selected":
            if previous["id"] != item["id"] or previous["hash"] != item["hash"]:
                raise ValidationGateError("本任务已绑定另一版流程技能，请保留原选择完成或另起独立任务。")
            return
        connection.execute("INSERT INTO task_workflow_choices VALUES (?,?,?) ON CONFLICT(task_id,action) "
            "DO UPDATE SET data_json=excluded.data_json", (scope.task_id, action, _canonical({
                "status": "selected", "id": item["id"], "hash": item["hash"], "source": source, "reason": reason})))


def _workflow_candidate(scope: Any, intent: Any) -> dict[str, Any] | None:
    """明确匹配自动择一；有实质分歧先等作者，绝不并排执行两个流程。"""
    candidates = workflow_candidates(scope, intent)
    choice = workflow_choice(scope, intent.action)
    if choice and choice.get("status") == "selected":
        item = next((item for item in candidates if item["id"] == choice["id"] and item["hash"] == choice["hash"]), None)
        if item is None:
            raise ValidationGateError("原流程技能与当前冻结目标不一致，未换用别的技能。")
        return item
    if len(candidates) > 1:
        raise ValidationGateError("有多个流程技能适用，请明确选择后继续。")
    if candidates:
        _save_choice(scope, intent.action, candidates[0], "explicit_or_unique", "作者指定或只有一个明确匹配的流程。")
    return candidates[0] if candidates else None


def workflow_clarification(project: Any, scope: Any, intent: Any, message: str,
                           pending: dict[str, Any] | None) -> dict[str, Any] | None:
    """在执行前复用经验或处理绑定原任务的选择题；不新增选择调用。"""
    if scope is None or scope.settings.prompt_cache_layout_version < 22:
        return None
    candidates = workflow_candidates(scope, intent)
    saved = workflow_choice(scope, intent.action)
    if len(candidates) < 2 or saved and saved.get("status") == "selected":
        return None
    from .terminal_session import TerminalSession
    cards = (pending or {}).get("questions", [])
    card = next((card for card in cards if card.get("id") == "workflow-skill"), {})
    if card and (pending or {}).get("snapshot_task_id") == scope.task_id:
        matches = [option for option in card.get("options", [])
            if TerminalSession._question_answer_matches(message, card, str(option.get("label", "")))]
        if len(matches) == 1:
            item = next((item for item in candidates if item["id"] == matches[0].get("skill_id")
                and item["hash"] == matches[0].get("skill_hash")), None)
            if item:
                _save_choice(scope, intent.action, item, "author", "作者回答绑定原任务的流程选择题。")
                return None
    from .skill_learning import skill_choice_guidance
    guidance = skill_choice_guidance(project, intent.user_message or message, intent.action, candidates,
        review_before_revision=intent.review_before_revision, collaboration_mode=scope.effective_collaboration_mode)
    preferred = next((item for item in candidates if item["id"] == guidance.get("preferred")), None)
    if preferred:
        _save_choice(scope, intent.action, preferred, "book_experience", "本书同版相似任务已有明确反馈差距。")
        return None
    with skill_connection() as connection:
        connection.execute("INSERT OR IGNORE INTO task_workflow_choices VALUES (?,?,?)",
            (scope.task_id, intent.action, _canonical({"status": "waiting_user", "candidates": [item["id"] for item in candidates]})))
    return {"status": "waiting_user", "needs_clarification": True,
        "reply": "有几个不同流程都适用，做法存在分歧。请选本任务使用的一个；已有成果和预算继续保留。",
        "questions": [{"id": "workflow-skill", "header": "流程选择", "selection": "single",
            "question": "本次任务采用哪个流程技能？", "why_it_matters": "只选择一个流程，方法技能仍可组合，不额外调用模型判断。",
            "options": [{"id": "workflow-" + str(index), "label": "使用 @" + item["id"],
                "description": item["description"], "kind": "choice", "skill_id": item["id"], "skill_hash": item["hash"]}
                for index, item in enumerate(candidates)]}]}


def bind_run_plan(scope: Any, steps: list[Any]) -> None:
    """把已进入执行的图绑定实际run，避免把同任务其他计划归给本次费用或结果。"""
    from .runtime import active_runtime
    runtime = active_runtime.get()
    if scope is not None and runtime is not None and workflow_plan(scope, steps):
        with skill_connection() as connection:
            connection.execute("INSERT OR IGNORE INTO skill_run_plans VALUES (?,?,?)",
                (runtime.run_id, scope.task_id, _steps_hash(steps)))


def run_workflow_plans(task_id: str, run_id: str) -> list[dict[str, Any]]:
    """仅查询本次实际进入执行的图，计划和真实步骤状态分开展示。"""
    with skill_connection() as connection:
        rows = connection.execute("SELECT p.* FROM skill_workflow_plans p JOIN skill_run_plans r "
            "ON p.task_id=r.task_id AND p.steps_hash=r.plan_hash WHERE r.task_id=? AND r.run_id=?", (task_id, run_id)).fetchall()
    return [{**json.loads(row["data_json"]), "plan_hash": row["steps_hash"]} for row in rows]


def _steps_hash(steps: list[Any]) -> str:
    """只由实际计划计算身份，运行时间和状态不会改变可复用步骤。"""
    return content_hash(_canonical([step.model_dump(mode="json") for step in steps]))


def bind_workflow_plan(scope: Any, item: dict[str, Any], steps: list[Any], *, intent: Any,
                       skipped: list[dict[str, str]], rounds: dict[str, int]) -> None:
    """记录实际编译图与冻结技能版本，恢复仍由原工具来源回执负责。"""
    record = {"skill_id": item["id"], "name": item["display_name"], "hash": item["hash"],
        "executor": item["workflow"]["executor"], "action": intent.action,
        "steps": [step.model_dump(mode="json") for step in steps], "skipped": skipped,
        "repair_rounds": rounds, "selection_model_calls": 0,
        "selection": workflow_choice(scope, intent.action),
        "policy": "必做及按条件执行；已有同版成果复用，所有工具沿原授权与预算"}
    with skill_connection() as connection:
        connection.execute("INSERT OR IGNORE INTO skill_workflow_plans VALUES (?,?,?,?)",
            (scope.task_id, _steps_hash(steps), _canonical(record), utc_now()))


def compile_workflow_steps(intent: Any) -> tuple[list[Any] | None, dict[str, Any] | None]:
    """把技能步骤编译为原DispatchStep，角色、章号、工具授权都由宿主填入。"""
    from .task_settings import active_task_settings
    from .coordinator import CHAPTER_TOOLS, authorized_chapter_tools
    from .schemas import DispatchStep
    scope = active_task_settings.get()
    if scope is None or scope.settings.prompt_cache_layout_version < 22 or intent.authorization != "approved":
        return None, None
    item = _workflow_candidate(scope, intent)
    if item is None:
        return None, None
    flow = item["workflow"]
    # 批次与正式接受事务沿原整体入口执行，不能拆散来源晋升和逐章提交保证。
    if (flow["executor"] == "registered_workflow" or not intent.chapter_no
            or intent.end_chapter_no not in {None, intent.chapter_no} or intent.action in {"batch_accept", "continue_run"}):
        return None, item
    allowed = set(authorized_chapter_tools(intent))
    chosen_operations = {step.operation for step in intent.tool_steps}
    selected, skipped = [], []
    for node in flow["steps"]:
        reason = ""
        if node.get("actions") and intent.action not in node["actions"]:
            reason = "不属于当前目标分支"
        elif node.get("review_order") == "before" and not intent.review_before_revision:
            reason = "本次不是先审后改"
        elif node.get("review_order") == "after" and intent.review_before_revision:
            reason = "本次不是先改后审"
        elif node.get("round", 0) > intent.max_revision_rounds:
            reason = "超过原任务允许的修订轮次"
        elif node["tool"] not in allowed:
            if node.get("required", True):
                raise ValidationGateError(f"技能的必做步骤{node['tool']}未获本次授权；没有扩大目标或执行。")
            reason = "本次未授权该可选操作"
        elif not node.get("required", True) and not node.get("default", False) and node["tool"] not in chosen_operations:
            reason = "Coordinator未选择该可选步骤"
        if reason:
            skipped.append({"id": node["id"], "reason": reason})
        else:
            selected.append(node)
    if not selected:
        raise ValidationGateError("流程技能没有适用于当前目标的可执行步骤；请核对技能触发范围。")
    indexed = {node["id"]: node for node in flow["steps"]}
    selected_ids = {node["id"] for node in selected}

    def active_dependencies(identity: str) -> list[str]:
        """省略分支仍承接其前置节点，防止删除可选节点后跳过真正的依赖。"""
        if identity in selected_ids:
            return [identity]
        return list(dict.fromkeys(value for parent in indexed[identity].get("depends_on", []) for value in active_dependencies(parent)))

    steps, rounds = [], {}
    for node in selected:
        identity = "skill-" + node["id"]
        dependencies = list(dict.fromkeys(value for parent in node.get("depends_on", []) for value in active_dependencies(parent)))
        instruction = node.get("instruction", "")
        if node.get("round", 0) and node["tool"] == "chapter.revise":
            # 分轮指令形成独立回执，不把上一轮Writer成功返回误当下一轮已修好新问题。
            instruction += f"\n本次仅处理当前版本已核实的问题，属于原授权内第{node['round']}轮定点修订。"
        steps.append(DispatchStep(step_id=identity, role=CHAPTER_TOOLS[node["tool"]][0], operation=node["tool"],
            depends_on=["skill-" + dependency for dependency in dependencies],
            required_output=CHAPTER_TOOLS[node["tool"]][1], chapter_no=intent.chapter_no,
            instruction=instruction.strip(), when=node.get("when", "always"),
            gate="技能只编排原登记工具，执行前仍核当前来源、角色归属、预算和正式写入权限"))
        if node.get("round", 0) and node["tool"] == "chapter.revise":
            rounds[identity] = node["round"]
    return steps, {**item, "_compiled_skipped": skipped, "_compiled_rounds": rounds}


def workflow_plan(scope: Any, steps: list[Any]) -> dict[str, Any] | None:
    """查同一冻结任务、同一实际步骤的技能归属，不从当前目录重建。"""
    if scope is None or scope.settings.prompt_cache_layout_version < 22:
        return None
    with skill_connection() as connection:
        row = connection.execute("SELECT data_json FROM skill_workflow_plans WHERE task_id=? AND steps_hash=?",
            (scope.task_id, _steps_hash(steps))).fetchone()
    return {**json.loads(row[0]), "plan_hash": _steps_hash(steps)} if row else None


def recent_workflow_plans(task_ids: list[str]) -> list[dict[str, Any]]:
    """供设置展开实际编排及跳过原因，未运行的计划不会被标为完成。"""
    if not task_ids:
        return []
    with skill_connection() as connection:
        placeholders = ",".join("?" for _ in task_ids)
        rows = connection.execute("SELECT * FROM skill_workflow_plans WHERE task_id IN (" + placeholders
            + ") ORDER BY created_at DESC LIMIT 30", task_ids).fetchall()
    return [{**json.loads(row["data_json"]), "plan_hash": row["steps_hash"],
        "task_id": row["task_id"], "created_at": row["created_at"]} for row in rows]
