"""Check the actual stale-binding branch without model calls or novel writes."""
import ast
from pathlib import Path
from types import SimpleNamespace
import re
import asyncio
import json
import tempfile

from inkflow.schemas import TerminalIntent
from inkflow.coordinator import Coordinator
from inkflow.terminal_session import TerminalSession
from inkflow.config import Settings
from inkflow.errors import ValidationGateError
from inkflow.utils import content_hash, json_dumps, workflow_result_status, workflow_failure_reason


def check():
    for result, status, failure in (
        ({"status": {"accepted": 30, "draft": 10}}, "completed", ""),
        ({"verdict": {"detail": "辅助统计"}}, "completed", ""),
        ({"status": {"accepted": 30}, "result": {"status": "waiting_condition"}}, "waiting_condition", ""),
        ({"status": {"accepted": 30}, "steps": [{"status": "failed", "stop_reason": "原节点失败"}]}, "failed", "原节点失败"),
    ):
        assert workflow_result_status(result) == status and workflow_failure_reason(result) == failure
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
    dispatch = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch")
    revision_branch = next(node for node in dispatch.body if isinstance(node, ast.If)
        and ast.unparse(node.test) == "intent.action in {'revise_review', 'revise_review_accept'}")
    wrapper = ast.parse("async def revise(self,root,intent):\n chapter_no=intent.chapter_no\n instruction=intent.operation_instruction\n steps=[]").body[0]
    wrapper.body.append(revision_branch)
    record = {"chapter_version": 1, "report": SimpleNamespace(source_hash="body")}
    project = SimpleNamespace(db=SimpleNamespace(latest_review_record=lambda number: record,
        get_chapter=lambda number: {"version": 1, "content_hash": "body"}))
    namespace = {"InkFlowProject": lambda root: project}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(source),"exec"),namespace)
    calls = []
    async def revise(*args, **kwargs):
        calls.append("writer")
        return {"version": 2}
    async def review(*args, **kwargs):
        calls.append("review")
        return {"verdict": "pass"}
    async def run_step(step, operation):
        return {"step": step, "result": await operation}
    session = SimpleNamespace(engine=SimpleNamespace(revise_chapter=revise,review_and_repair=review),
        _reuse_workflow_writer=lambda *args: None, _reuse_current_pass_review=lambda *args: None,
        _run_step=run_step)
    intent = TerminalIntent(action="revise_review",chapter_no=31,authorization="approved",
        visible_reason="先核对，有正文问题才修订",review_before_revision=True)
    asyncio.run(namespace["revise"](session,"fixture",intent))
    assert calls == ["review"]
    calls.clear()
    asyncio.run(namespace["revise"](session,"fixture",intent.model_copy(update={"review_before_revision":False})))
    assert calls == ["writer","review"]
    assert TerminalIntent(action="review",visible_reason="旧任务").review_before_revision is False
    routed = Coordinator.normalize_intent(TerminalIntent(action="revise_draft",chapter_no=31,
        authorization="approved",visible_reason="本章核对"),
        "先核对第31章，确实有正文问题才在本章必要位置修复并复审，不验收")
    assert routed.action == "revise_review" and routed.review_before_revision and "accept" in routed.forbidden_actions
    session = TerminalSession.__new__(TerminalSession)
    session.engine = SimpleNamespace(settings=Settings())
    read_only = session._deterministic_workflow_intent("墨宝，现在只补审现有临时批次 batch-current 的第31至40章当前草稿。复用已写正文，不调用Writer改正文，不重写或新增章节，不验收。完成现有十章核对就停。")
    assert read_only.action == "batch_repair" and read_only.batch_review_only and read_only.batch_id == "batch-current"
    assert (read_only.chapter_no,read_only.end_chapter_no)==(31,40) and read_only.max_revision_rounds==0
    assert set(read_only.forbidden_actions)=={"write","revise","accept"}
    forbidden_branch = next(node for node in dispatch.body if isinstance(node, ast.If)
        and any(isinstance(child,ast.Constant) and isinstance(child.value,str) and "明确禁止的" in child.value
                for child in ast.walk(node)))
    wrapper = ast.parse("def forbidden_gate(intent):\n forbidden=set(intent.forbidden_actions)\n pass").body[0]
    wrapper.body[-1:] = [forbidden_branch]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(source),"exec"),namespace)
    assert namespace["forbidden_gate"](read_only) is None
    assert namespace["forbidden_gate"](read_only.model_copy(update={"batch_review_only":False}))
    assert namespace["forbidden_gate"](read_only.model_copy(update={"forbidden_actions":["review"]}))
    assert namespace["forbidden_gate"](read_only.model_copy(update={"action":"revise_draft"}))
    assert namespace["forbidden_gate"](read_only.model_copy(update={"action":"batch_accept"}))
    response={"steps":[{"step":"batch_repair","result":{"status":"needs_revision","stopped_at_chapter":36,
        "stop_reason":"当前来源仍待核"}}]}
    assert session._recovery_nodes(response)[0]["result"]["stopped_at_chapter"]==36
    recovery = next(node for node in ast.walk(tree) if isinstance(node,ast.AsyncFunctionDef) and node.name=="_recover_failed_workflow")
    boundary = next(node.test for node in recovery.body if isinstance(node,ast.If)
                    and "same_round_recovery and" in ast.unparse(node.test))
    predicate=compile(ast.fix_missing_locations(ast.Expression(body=boundary)),str(source),"eval")
    assert eval(predicate,{"same_round_recovery":True,"node":"batch_repair","intent":read_only})
    assert not eval(predicate,{"same_round_recovery":True,"node":"batch_repair","intent":read_only.model_copy(update={"batch_review_only":False})})
    risk = next(node for node in ast.walk(handle) if isinstance(node,ast.If)
                and ast.unparse(node.test)=="failure or response.get('needs_clarification')")
    wrapper=ast.parse("def failure_source(self,project,response,ticket,intent):\n pass").body[0]
    wrapper.body=risk.body[:3]+[ast.Return(value=ast.Tuple(elts=[ast.Name(id="failure_chapter",ctx=ast.Load()),
        ast.Name(id="failed_source",ctx=ast.Load())],ctx=ast.Load()))]
    namespace={}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(source),"exec"),namespace)
    project=SimpleNamespace(db=SimpleNamespace(get_chapter=lambda number:{"version":5} if number==36 else None))
    ticket=SimpleNamespace(chapter_no=31)
    assert namespace["failure_source"](session,project,response,ticket,read_only)==(36,{"version":5})
    assert namespace["failure_source"](session,project,{},ticket,read_only)==(None,None)
    assert session._deterministic_workflow_intent("请核对第31至40章，不重写或新增章节，保留已经写好的草稿") is None
    assert session._deterministic_workflow_intent("请直接写第31至40章草稿，不验收").action == "batch_draft"
    engine_source = source.with_name("engine.py")
    repair = next(node for node in ast.walk(ast.parse(engine_source.read_text("utf-8")))
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "_repair_batch")
    wrapper = ast.parse("def allowed(manifest,review_only):\n pass").body[0]
    status_index = next(i for i, node in enumerate(repair.body)
                        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "repairable_status")
    wrapper.body = repair.body[status_index:status_index+2] + [ast.Return(value=ast.Name(id="repairable_status",ctx=ast.Load()))]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(engine_source),"exec"),namespace)
    with tempfile.TemporaryDirectory() as folder:
        project = SimpleNamespace(internal=Path(folder))
        (project.internal/"batches").mkdir()
        for status in ("failed", "writing", "accepted", "interrupted"):
            for phase in ("draft_or_review", "memory_handoff"):
                manifest = {"batch_id":"batch-fixture", "status":status, "current_phase":phase}
                (project.internal/"batches/batch-fixture.json").write_text(json.dumps(manifest),encoding="utf-8")
                for review_only in (False,True):
                    expected = status=="interrupted" or status=="failed" and (review_only or phase=="memory_handoff")
                    assert namespace["allowed"](manifest,review_only)==expected
                    assert bool(session._repairable_batch_ids(project,review_only=review_only))==expected
    begin = next(i for i,node in enumerate(repair.body) if isinstance(node,ast.Assign) and ast.unparse(node.targets[0])=="entries")
    end = next(i for i,node in enumerate(repair.body) if isinstance(node,ast.Assign) and ast.unparse(node.targets[0])=="trace")
    wrapper=ast.parse("def existing_drafts(project,manifest,start_chapter_no,end_chapter_no):\n pass").body[0]
    wrapper.body=repair.body[begin:end]+[ast.Return(value=ast.Name(id="selected",ctx=ast.Load()))]
    namespace={"ValidationGateError":ValidationGateError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(engine_source),"exec"),namespace)
    drafts={number:{"status":"draft","title":"fixture","path":f"chapters/{number}.md","version":5} for number in range(31,41)}
    project=SimpleNamespace(db=SimpleNamespace(get_chapter=drafts.get))
    manifest={"chapters":[],"start_chapter_no":31,"end_chapter_no":40}
    assert namespace["existing_drafts"](project,manifest,31,40)==list(range(31,41))
    drafts.pop(34)
    assert namespace["existing_drafts"](project,manifest,31,40)==[31,32,33]
    try:
        namespace["existing_drafts"](project,manifest,30,40)
    except ValidationGateError:
        pass
    else:
        raise AssertionError("Read-only repair cannot expand the original batch range")
    mode_review=next(node for node in ast.walk(ast.parse(engine_source.read_text("utf-8")))
        if isinstance(node,ast.AsyncFunctionDef) and node.name=="review_chapter_mode")
    source_gate=next(node for node in ast.walk(mode_review) if isinstance(node,ast.If)
        and "plan_revision.get('card_hash')" in ast.unparse(node.test))
    approval=next(node for node in ast.walk(mode_review) if isinstance(node,ast.If)
        and ast.unparse(node.test)=="verdict == 'pass' and plan_revision")
    wrapper=ast.parse("def reviewed_plan(project,chapter_no,card,chapter,plan_revision,verdict,review_id):\n pass").body[0]
    wrapper.body=[source_gate,approval]
    namespace={"ValidationGateError":ValidationGateError,"content_hash":content_hash,"json_dumps":json_dumps}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[])),str(engine_source),"exec"),namespace)
    saved=[]; card={"chapter_no":31,"goal":"核对来信"}; marker={"status":"needs_writer","card_hash":content_hash(json_dumps(card)),"draft_version":5}
    project=SimpleNamespace(db=SimpleNamespace(pending_plan_revision=lambda number:marker,set_metadata=lambda key,value:saved.append(value)))
    namespace["reviewed_plan"](project,31,card,{"version":5},marker,"unknown",99)
    assert not saved
    namespace["reviewed_plan"](project,31,card,{"version":5},marker,"pass",99)
    assert saved[-1]["status"]=="awaiting_review" and saved[-1]["write_skipped_after_review"] and saved[-1]["draft_version"]==5
    try:
        namespace["reviewed_plan"](project,31,{"chapter_no":31,"goal":"其他来源"},{"version":5},marker,"pass",99)
    except ValidationGateError:
        pass
    else:
        raise AssertionError("Plan source mismatch cannot be closed by a review")
    print("Pending route binding checks passed: stale model ID, explicit target, saved resume, fixed answer and current exhausted source.")
    print("Conditional revision checks passed: review first; explicit revision retains writer and review.")


if __name__ == "__main__":
    check()
