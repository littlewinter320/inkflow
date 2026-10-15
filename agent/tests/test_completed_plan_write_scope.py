from types import SimpleNamespace

from inkflow.terminal_session import TerminalSession


def test_completed_planning_reference_cannot_expand_current_prose_scope():
    session = TerminalSession(SimpleNamespace(settings=SimpleNamespace(acceptance_confirmation_mode="auto_after_review")))
    message = (
        "第51到58章的近期规划已经采用了。接着完成第51章的写作待办，沿用上次平衡档和只写这一章的范围；"
        "核对相关前文和现行设定，按现行规划写，正常完成当前分工的审查，通过后接受。"
        "保留前面已接受的正文，不生成第52章以后的正文，也不重新做规划。"
    )
    assert session._deterministic_workflow_intent(message) is None
    assert session._explicit_chapter_range(message) == (51, None)
    for reference in ("第51到58章的近期规划已经采用了", "第51～58章规划已生效"):
        request = reference + "。请接着写第51章，审查通过后接受。"
        assert session._explicit_chapter_range(request) == (51, None)
        assert session._deterministic_workflow_intent(request) is None
        assert session._deterministic_single_chapter_write(request).chapter_no == 51
    batch = session._deterministic_workflow_intent("请批量写第52到55章，审查通过后整批接受。")
    assert (batch.action, batch.chapter_no, batch.end_chapter_no) == ("batch_draft_accept", 52, 55)
    planning = session._explicit_chapter_range("请重新规划第51到58章，不生成正文。")
    assert planning == (51, 58)
