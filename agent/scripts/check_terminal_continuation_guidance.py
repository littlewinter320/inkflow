"""Check continuation constraints and public waiting causes without model/file writes."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inkflow.coordinator import Coordinator
from inkflow.schemas import TerminalIntent
from inkflow.terminal_session import TerminalSession, _suggestions_forbidden, _with_waiting_guidance


def check():
    original = "请完成第51章，审查通过后接收，也不用生成下一步推荐。"
    intent = TerminalIntent(action="write_review_accept", chapter_no=51, user_message=original, visible_reason="完成原任务")
    assert _suggestions_forbidden(intent)
    assert _suggestions_forbidden(intent.model_copy(update={"user_message": "继续", "forbidden_actions": ["额外生成下一步推荐"]}))
    assert _suggestions_forbidden(intent.model_copy(update={"user_message": "继续", "forbidden_actions": ["suggested_prompts"]}))
    for text in ("不是不用生成下一步推荐，请继续推荐。", "不要禁止生成下一步推荐。",
                 "如果不用生成下一步推荐，会怎样？", "他说不用生成下一步推荐，我想先讨论。",
                 "“不用生成下一步推荐”，这只是例句。", '"不用生成下一步推荐"是引用。',
                 "不用生成下一步推荐？", "请不要‘禁止’生成下一步推荐。",
                 "不用生成新的正文，先正常审查。"):
        assert not _suggestions_forbidden(intent.model_copy(update={"user_message": text})), text

    # Run the actual same-task continuation branch with routing already normalized.
    path = Path(__file__).parents[1] / "src/inkflow/terminal_session.py"
    tree = ast.parse(path.read_text("utf-8"))
    handle = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle")
    start = next(node for node in ast.walk(handle) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "same_task_resume" for target in node.targets))
    end = next(node for node in ast.walk(handle) if isinstance(node, ast.If)
               and ast.unparse(node.test).startswith("_suggestions_forbidden(intent, user_message=authorization_text)"))
    block = "\n".join(path.read_text("utf-8").splitlines()[start.lineno - 1:end.end_lineno])
    wrapper = ast.parse("def resume(self,project,pending,task_scope,text,intent):\n pass").body[0]
    wrapper.body = ast.parse("\n".join(line[12:] for line in block.splitlines())).body + [ast.Return(value=ast.Name(id="intent", ctx=ast.Load()))]
    namespace = {"_suggestions_forbidden": _suggestions_forbidden}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])), str(path), "exec"), namespace)
    session = SimpleNamespace(_fresh_writer_authorization=lambda *args: None)
    pending = {"task_id": "original", "intent": intent.model_dump(mode="json"), "resume_end": 2}
    normalized = Coordinator.normalize_intent(intent, "继续")
    resumed = namespace["resume"](session, None, pending, SimpleNamespace(task_id="original"), "继续", normalized)
    assert resumed.user_message == original and "suggested_prompts" in resumed.forbidden_actions
    assert resumed.action == "write_review_accept" and not set(resumed.forbidden_actions) & {"review", "extra_calls", "extra_model_calls"}
    full = normalized.model_copy(update={"forbidden_actions": [f"constraint-{index}" for index in range(16)]})
    bounded = namespace["resume"](session, None, pending, SimpleNamespace(task_id="original"), "继续", full)
    assert bounded.forbidden_actions == full.forbidden_actions and _suggestions_forbidden(bounded)
    unrelated = namespace["resume"](session, None, pending, SimpleNamespace(task_id="other"), "继续", normalized)
    assert unrelated.user_message == "继续" and "suggested_prompts" not in unrelated.forbidden_actions
    # The defensive recommendation guard returns before project/provider work.
    bare = object.__new__(TerminalSession)
    assert asyncio.run(bare._suggest_after_result(None, resumed, {}, None)) == []

    cause = "第51章正文1963字，低于本次要求2610字；Writer已用完本次1次授权，未继续扩写。"
    result = {"status": "waiting_user", "summary": "等待你的选择", "steps": [
        {"step": "writer.write", "result": {"status": "waiting_user", "reason": cause}}]}
    guidance = _with_waiting_guidance(result)
    assert guidance["reply"] == cause and guidance["reason"] == cause and guidance["status"] == "waiting_user"
    assert "reply" not in result, "Public projection must preserve original workflow receipts"
    assert _with_waiting_guidance({**result, "summary": "这一轮已经暂停"})["reply"] == cause
    gated = _with_waiting_guidance({"status": "waiting_condition", "reply": "等待你的选择：等待你的选择", "result": {"gate": cause}})
    assert gated["reply"] == cause and gated["result"]["gate"] == cause
    complete = {"status": "completed", "result": {"arbitrary_artifact": {"gate": "unrelated"}}}
    assert _with_waiting_guidance(complete) is complete
    assert "尚未返回" in _with_waiting_guidance({"status": "waiting_user"})["reply"]
    assert _with_waiting_guidance({"status": "waiting_user", "questions": [{"question": "采用哪个结尾？"}]})["reply"] == "采用哪个结尾？"
    # Reading old saved recommendations must honor the original task's prohibition too.
    from inkflow.app_server import InkFlowAppService
    service = object.__new__(InkFlowAppService)
    saved = {"basis": {"same": True}, "suggestions": [{"label": "继续", "prompt": "旧建议"}]}
    pending = {"intent": resumed.model_dump(mode="json")}
    project = SimpleNamespace(db=SimpleNamespace(get_metadata=lambda key, default=None:
        saved if key == "next_step_suggestions" else pending if key == "pending_creation_task" else default))
    service._project = lambda params: project
    async def no_events(event):
        raise AssertionError("Reading recommendations must not start any work")
    with patch.object(TerminalSession, "_suggestion_basis", return_value=saved["basis"]):
        hidden = asyncio.run(service.dispatch("assistant.suggest", {"workspace_root": "offline-only"}, no_events))
    assert hidden["suggestions"] == [] and hidden["source"] == "none"
    print("Terminal continuation guidance passed: original constraints, zero recommendation work, actual nested waiting causes.")


if __name__ == "__main__":
    check()
