"""Related offline checks only; temporary D-drive book, no model calls."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inkflow.errors import ProjectError
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief
from inkflow.studio import StudioService
from inkflow.utils import content_hash


def check():
    with TemporaryDirectory(prefix="pending-decisions-", dir="D:/墨流/cache") as folder:
        project = InkFlowProject.create(Path(folder)/"book", BookBrief(
            title="临时检查", genre="现实", protagonist="林安", premise="只核对待办安排和正文统计，不生成小说。"))
        task = project.record_pending_work(kind="workflow", reason="原节点", next_action="等待",
            source={"task_id":"original-task","conversation":"pending_creation_task"}, run_id="original-run",
            intent={"action":"review","chapter_no":1})
        project.update_pending_work(task["id"], processing_attempts=2)
        project.db.set_metadata("pending_creation_task", {"task_id":"original-task","status":"waiting_condition","intent":{"action":"review"}})
        child = project.record_pending_work(kind="coordinator_recovery", reason="关联诊断", next_action="条件",
            source={"task_id":"original-task","node":"review"},status="waiting_user")
        StudioService(project)
        with patch("inkflow.studio.StudioDatabase.latest_task_run_for_settings",return_value={"status":"running"}):
            try:
                project.decide_pending_work(task["id"],decision="reject",reason="停止安排")
            except ProjectError:
                pass
            else:
                raise AssertionError("Running original task must be stopped before rejection")
        project.decide_pending_work(task["id"], decision="reject", reason="暂停这个任务")
        assert not project.pending_work()
        historical = project.pending_work(include_rejected=True)
        assert len(historical)==2 and all(x["status"]=="rejected" and not x["can_process"] for x in historical)
        assert project.db.get_metadata("pending_creation_task")["status"]=="rejected"
        project.decide_pending_work(task["id"],decision="reopen")
        assert project.db.get_metadata("pending_work:"+task["id"])["processing_attempts"]==2
        assert next(x for x in project.pending_work() if x["id"]==child["id"])["status"]=="waiting_user"
        gate = project.record_pending_work(kind="review_gate",reason="必要门禁",next_action="核验",source={"content_hash":"body"},chapter_no=1)
        try:
            project.decide_pending_work(gate["id"], decision="reject", reason="不想核验")
        except ProjectError:
            pass
        else:
            raise AssertionError("Reject must never pass or hide a necessary gate")
        old = project.record_pending_work(kind="workflow", reason="旧报告续接",next_action="核验",
            source={"task_id":"old-task","conversation":"pending_creation_task"},run_id="old-run",
            intent={"action":"review","pending_work_id":gate["id"]})
        project.update_pending_work(gate["id"],status="superseded")
        project.reconcile_pending_work()
        assert project.db.get_metadata("pending_work:"+old["id"])["status"]=="superseded"
        body="# 第1章\n正文四个字"
        path=project.root/"chapters/chapter_00001.draft.md"; path.write_text(body,encoding="utf-8")
        with project.db.connect() as con:
            con.execute("INSERT INTO chapters(chapter_no,title,status,version,path,content_hash,content_text,summary,updated_at) VALUES(1,'临时','draft',1,'chapters/chapter_00001.draft.md',?,?, '', 'now')",(content_hash(body),body))
            con.commit()
        view=StudioService(project).dashboard()
        assert view["chapter_progress"]=={"written":1,"awaiting_review":1,"accepted":0,"written_through":1,"accepted_through":0}, view["chapter_progress"]
        assert view["written_characters"]>0 and view["accepted_characters"]==0
        diagnostic=project.record_pending_work(kind="review_evidence",reason="依赖未核",next_action="复核",source={"content_hash":content_hash(body)},chapter_no=1)
        project.db.set_metadata("pending_plan_revision:1",{"status":"awaiting_review","instruction":"只核对当前章节卡"})
        pending=next(x for x in project.pending_work() if x["id"]==diagnostic["id"])
        assert not pending["can_process"] and "规划复审" in pending["next_action"]
        print("Pending decisions/progress checks passed: rejection history, restore without resetting attempts, required gates, obsolete target handoff, written versus accepted.")


if __name__=="__main__":
    check()
