from inkflow.studio import StudioService
from setting_fixture import project_fixture


def test_confirmed_dead_run_reconciles_only_its_stale_work_and_preserves_new_run(tmp_path,monkeypatch):
    project=project_fixture(tmp_path)
    db=StudioService(project).db
    db.start_task("dead-run",owner_id="server-dead",method="conversation.send",params={"message":"错误批次"})
    db.start_task("new-single-run",owner_id="server-live",method="conversation.send",params={"message":"只写这一章"})
    monkeypatch.setattr("inkflow.studio._owner_process_alive",lambda owner:False if owner=="server-dead" else True)
    old=project.record_pending_work(kind="workflow",reason="错误批次需作者决定",next_action="先核对原任务",
        source={"task_id":"old-child-snapshot","conversation":"pending_creation_task"},run_id="dead-run",status="running",
        intent={"action":"batch_draft_accept","chapter_no":51,"end_chapter_no":58})
    new=project.record_pending_work(kind="workflow",reason="新的单章任务",next_action="等待当前结果",
        source={"task_id":"current-snapshot"},run_id="new-single-run",status="running",intent={"action":"write_review_accept","chapter_no":51})
    failed=project.record_pending_work(kind="workflow",reason="原有明确失败",next_action="保留原错误",
        source={"task_id":"failed-child"},run_id="dead-run",status="failed",intent={"action":"review","chapter_no":50})
    project.db.set_metadata("pending_creation_task",{"task_id":"old-child-snapshot","run_id":"dead-run","status":"running","intent":old["intent"]})
    assert db.reconcile_interrupted_tasks("server-current")==1
    old_after=project.db.get_metadata("pending_work:"+old["id"])
    new_after=project.db.get_metadata("pending_work:"+new["id"])
    assert old_after["status"]=="interrupted" and old_after["source"]==old["source"] and old_after["run_id"]==old["run_id"]
    assert old_after["intent"]==old["intent"] and old_after["reason"]==old["reason"]
    assert old_after["interruption"]["run_id"]=="dead-run"
    assert new_after==new and db.get_task("new-single-run")["status"]=="running"
    assert project.db.get_metadata("pending_work:"+failed["id"])==failed
    assert db.get_task("dead-run")["status"]=="interrupted" and db.get_task("dead-run")["error_code"]=="engine_process_exited"
    assert db.reconcile_interrupted_tasks("server-current")==0
    assert project.db.get_metadata("pending_work:"+old["id"])==old_after
    rejected=project.decide_pending_work(old["id"],decision="reject",reason="这次批次来自误路由，停止安排，保留历史。")
    assert rejected["status"]=="rejected" and project.db.get_metadata("pending_creation_task")["status"]=="rejected"
    assert project.db.get_metadata("pending_work:"+new["id"])==new
