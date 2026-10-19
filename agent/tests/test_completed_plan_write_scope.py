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


def test_authorized_single_chapter_excludes_postfixed_protected_ranges():
    session = TerminalSession(SimpleNamespace(settings=SimpleNamespace(acceptance_confirmation_mode="auto_after_review")))
    # Exact static message from run-authorized-ch51-cache-policy.py; never run
    # that verification script or send any requests from this offline check.
    message = (
        "我这次明确新增一次第51章写作尝试的授权：上一次Writer在MiMo断流，没有保存正文；"
        "那次失败和已用次数保留，这次新增授权最多发送一次Writer请求，包括长度修复也计入一次，不得外围再加。"
        "请仅单独完成第51章，用当前小米MiMo Flash2.6、平衡档和四人按需加强；专项沿用连续性审查分工。"
        "平衡档实际参加的各角色开启思考，360秒仅本地进度提示，单次最多等待720秒。"
        "现行51～58近期规划第14版已经采用，复用当前生效规划、设定和有效审核来源，不重新规划；"
        "请按必要的写作、审核、记忆和接受流程完成第51章。"
        "第1～50章保持原文，不覆盖；这次不要生成第52章及以后。"
        "不要为轻微换词重复调用，不要额外生成下一步建议；有实质非硬错误修改先问我，缺依据由原角色补读。"
    )
    assert session._explicit_chapter_range(message) == (51, None)
    assert session._deterministic_workflow_intent(message) is None
    for protection in ("保持原文", "保留原文", "不要覆盖", "不覆盖", "正文保持原样", "原稿不要重写"):
        for request in (
            f"请写第51章；第1～50章{protection}，不要生成第52章及以后。",
            f"第1～50章{protection}；请写第51章，360秒只作进度提示。",
        ):
            assert session._explicit_chapter_range(request) == (51, None)
            assert session._deterministic_workflow_intent(request) is None
            assert session._deterministic_single_chapter_write(request).chapter_no == 51
    for reference in ("第51～58章近期规划第14版已经采用", "第51～58章规划v14已生效"):
        assert session._explicit_chapter_range(reference + "；请完成第51章。") == (51, None)
    # Protection of another object/range does not negate an operative batch.
    for request, expected in (
        ("请批量写第1～50章，审查通过后接受。", (1, 50)),
        ("保护说明保持原文。请批量写第1～50章，审查通过后接受。", (1, 50)),
        ("请批量写第52～55章，审查通过后接受；第1～50章保留原文。", (52, 55)),
        ("第1～50章不要覆盖；请批量写第52～55章，审查通过后接受。", (52, 55)),
    ):
        assert session._explicit_chapter_range(request) == expected
        intent = session._deterministic_workflow_intent(request)
        assert (intent.action, intent.chapter_no, intent.end_chapter_no) == ("batch_draft_accept", *expected)
    assert session._explicit_chapter_range("第1～50章保持原文，不覆盖。") == (None, None)
    assert session._explicit_chapter_range("请阅读文章说明，等待360秒；版本14已经采用，输出720字。") == (None, None)
    assert session._explicit_chapter_range("请重新规划第51～58章近期规划第14版，不生成正文。") == (51, 58)
