"""Scoped batch-board source checks; no model calls or user-book writes."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from inkflow.app_server import _list_batch_summaries
from inkflow.errors import InkFlowError
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief
from inkflow.utils import content_hash


def check():
    with TemporaryDirectory(prefix="batch-history-", dir="D:/墨流/cache") as folder:
        project = InkFlowProject.create(Path(folder)/"book", BookBrief(
            title="临时检查", genre="现实", protagonist="林安", premise="只核对旧批次展示，不生成小说。"))
        batches = project.internal / "batches"
        batches.mkdir(exist_ok=True)
        manifest = {"batch_id":"batch-empty", "status":"failed", "start_chapter_no":1,
                    "end_chapter_no":2, "chapters":[]}
        (batches/"batch-empty.json").write_text(json.dumps(manifest), encoding="utf-8")
        def view(running=False):
            with patch("inkflow.app_server._batch_source_run", return_value={"status":"running"} if running else None), \
                 patch("inkflow.app_server._batch_resume_params", side_effect=InkFlowError("缺少绑定快照")):
                return _list_batch_summaries(project)[0]
        assert not view()["historical"], "An unwritten scope must remain actionable"
        with project.db.connect() as connection:
            for number in (1, 2):
                body = f"第{number}章已保存正文"
                connection.execute("INSERT INTO chapters(chapter_no,title,status,version,path,content_hash,content_text,summary,updated_at) VALUES(?,?, 'draft',1,?,?,?,'','now')",
                    (number,"临时",f"chapters/{number}.md",content_hash(body),body))
            connection.commit()
        historical = view()
        assert historical["historical"] and historical["stored_status"] == "failed"
        assert "其他来源" in historical["history_reason"]
        for number in (1, 2):
            (project.root/f"chapters/{number}.md").write_text(f"第{number}章已保存正文",encoding="utf-8")
        with project.db.connect() as connection:
            connection.execute("UPDATE chapters SET content_text=NULL")
            connection.commit()
        assert view()["historical"], "Legacy drafts must be verified from their managed files"
        assert not view(running=True)["historical"], "A running source cannot be retired"
        with project.db.connect() as connection:
            connection.execute("UPDATE chapters SET content_hash='invalid' WHERE chapter_no=2")
            connection.commit()
        assert not view()["historical"], "A missing or damaged current source cannot prove completion"
        with project.db.connect() as connection:
            connection.execute("UPDATE chapters SET content_hash=?,status='accepted' WHERE chapter_no=2",(content_hash("第2章已保存正文"),))
            connection.execute("UPDATE chapters SET status='accepted' WHERE chapter_no=1")
            connection.commit()
        assert view()["historical"] and "全部进入正史" in view()["history_reason"]
        assert json.loads((batches/"batch-empty.json").read_text("utf-8")) == manifest
        print("Batch history checks passed: missing, complete, running and damaged scopes; original manifest retained.")


if __name__ == "__main__":
    check()
