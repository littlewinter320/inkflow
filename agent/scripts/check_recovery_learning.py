"""Explicit offline check: PYTHONPATH=agent/src python agent/scripts/check_recovery_learning.py."""
from pathlib import Path
from tempfile import TemporaryDirectory

from inkflow.engine import InkFlowEngine
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief, ReviewFinding, ReviewReport
from inkflow.utils import content_hash


def check():
    with TemporaryDirectory() as directory:
        project = InkFlowProject.create(Path(directory) / "book", BookBrief(
            title="恢复记录", genre="现实", premise="只验证失败学习的隔离与归因。", protagonist="林安"))
        body = "林安把铁盒搬进库房。"
        project.db.upsert_draft(20, "库房", "drafts/chapter_00020.md", body)
        chapter = project.db.get_chapter(20)
        report = ReviewReport(verdict="pass", confidence=0.9, summary="同源复核完成",
            source_hash=content_hash(body), evidence_recovery={"roles": {"editor": {
                "attempted": True, "status": "rechecked", "gaps": ["对照资料未定位"]}}})
        review_id = project.db.save_review(20, chapter["version"], report, "reviews/local.md")
        InkFlowEngine._record_review_collaboration(project, 20, chapter["version"], report,
            "offline", "", review_id=review_id)
        assert not project.db.learning_guidance(chapter_no=16, role="editor")["recovery_cases"]
        cases = project.db.learning_guidance(chapter_no=21, role="editor")["recovery_cases"]
        assert cases[0]["status"] == "verified" and body not in str(cases)
        assert not project.db.learning_guidance(chapter_no=21, role="memory_keeper")["recovery_cases"]
        report = report.model_copy(update={"verdict": "unknown", "findings": [ReviewFinding(
            category="knowledge", severity="major", evidence="搬进库房", canon_refs=["fact:box"],
            explanation="指控不受支持", repair_instruction="先核证", verification_status="unsupported")]})
        review_id = project.db.save_review(20, chapter["version"], report, "reviews/local2.md")
        InkFlowEngine._record_review_collaboration(project, 20, chapter["version"], report,
            "offline2", "", review_id=review_id)
        assert not project.db.retrieval_feedback_scores()
        assert all(item["status"] == "unresolved" for item in
                   project.db.learning_guidance(chapter_no=21, role="editor")["recovery_cases"])
        assert project.db.learning_guidance()["explicit_user_feedback"] == 0
        project.db.upsert_draft(20, "库房", "drafts/chapter_00020.md", body + "他锁了门。")
        assert not project.db.learning_guidance(chapter_no=21)["recovery_cases"]
        project.db.set_metadata("learning_settings", {"enabled": False})
        assert project.db.record_learning_event("review_recovery", {"action": "source_reread"}) == ""


if __name__ == "__main__":
    check()
