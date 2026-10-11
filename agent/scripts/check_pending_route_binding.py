"""Check the actual stale-binding branch without model calls or novel writes."""
import ast
from pathlib import Path
from types import SimpleNamespace
import re

from inkflow.schemas import TerminalIntent


def check():
    source = Path(__file__).parents[1]/"src/inkflow/terminal_session.py"
    tree = ast.parse(source.read_text("utf-8"))
    handle = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle")
    branch = next(node for node in ast.walk(handle) if isinstance(node, ast.If)
                  and "intent.pending_work_id" in ast.unparse(node.test)
                  and any(isinstance(child, ast.Expr) and isinstance(child.value, ast.Call)
                          and child.value.args and isinstance(child.value.args[0], ast.Constant)
                          and child.value.args[0].value == "controller.pending_work.selection" for child in node.body))
    wrapper = ast.parse("def binding(project,intent,text,pending,local_work,trace):\n pass").body[0]
    wrapper.body = [branch, ast.Return(value=ast.Name(id="intent",ctx=ast.Load()))]
    namespace = {"re":re}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(source),"exec"),namespace)
    trace = SimpleNamespace(record=lambda *args, **kwargs: None)
    project = SimpleNamespace(pending_work=lambda: [])
    intent = TerminalIntent(action="review",chapter_no=31,authorization="approved",pending_work_id="obsolete",visible_reason="只核对当前章")
    apply = lambda item=intent,text="只读核对当前章",pending=None,local_work=None: namespace["binding"](project,item,text,pending,local_work,trace)
    current = apply()
    assert current.pending_work_id is None and current.chapter_no == 31 and current.action == "review"
    assert apply(text="继续待办 obsolete").pending_work_id == "obsolete"
    assert apply(pending={"run_id":"original"}).pending_work_id == "obsolete"
    assert apply(local_work=intent).pending_work_id == "obsolete"
    assert apply(intent.model_copy(update={"related_task_id":"original"})).pending_work_id == "obsolete"
    assert apply(intent.model_copy(update={"response_kind":"question_answer"})).pending_work_id == "obsolete"
    bound = {"id":"obsolete","processing_attempts":2}
    project.pending_work = lambda: [bound]
    assert apply().pending_work_id is None and bound["processing_attempts"] == 2
    assert apply(text="处理当前待办").pending_work_id == "obsolete" and bound["processing_attempts"] == 2
    print("Pending route binding checks passed: stale model ID, explicit target, saved resume, fixed answer and current exhausted source.")


if __name__ == "__main__":
    check()
