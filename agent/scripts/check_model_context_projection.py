"""Offline assertions for lossless source projection and diagnostic isolation."""
import json

from inkflow.schemas import ContextSection, ModeCheckOutput
from inkflow.provider import _model_from_json_content
from pydantic import ValidationError
from inkflow.terminal_session import TerminalSession
from inkflow.writer_notes import annotation_gaps, clarify_annotation_quotes


def check():
    fact = {"事实编号": "fact:1", "内容": "原句  保留空格\n和换行。", "关联证据": [{"版本": 2, "引文": "一字不改。"}]}
    section = ContextSection(key="B", title="正史", content=json.dumps([fact], ensure_ascii=False, indent=2))
    assert json.loads(section.model_content()) == [fact]
    assert "\n  " in section.content
    hit = {"source_id": "chapter:1", "body": "真实原文。", "body_kind": "accepted_prose", "source_hash": "current",
           "resolved_source": {"version": 2, "start": 4, "end": 9}, "summary_body": "过时摘要", "score": .2}
    section = ContextSection(key="F", title="资料", content=json.dumps({"自适应结果": [hit]}, ensure_ascii=False))
    projected = json.loads(section.model_content())["自适应结果"][0]
    assert projected["body"] == hit["body"] and projected["resolved_source"] == hit["resolved_source"]
    assert "summary_body" not in projected and json.loads(section.content)["自适应结果"][0] == hit
    memory = {"source_gaps": [{"source_id": "missing"}], "retrieval": {"branches": {"diagnostic": "large"}, "source_conflicts": ["conflict"]}}
    section = ContextSection(key="MEMORY", title="来源", content=json.dumps(memory))
    assert json.loads(section.model_content()) == {"source_gaps": memory["source_gaps"], "retrieval": {"source_conflicts": ["conflict"]}}
    work = [{"id": "task", "reason": "缺少依据", "next_action": "补读", "source": {"snapshot_hash": "original", "failure_case": {"log": "large"}}, "progress": {"attempts": 2, "original_node_receipts": ["log"]}}]
    section = ContextSection(key="WORK", title="待办", content=json.dumps(work))
    projected = json.loads(section.model_content())[0]
    assert projected["progress"] == {"attempts": 2} and projected["source"] == {"snapshot_hash": "original"}
    assert projected["reason"] == work[0]["reason"] and projected["next_action"] == work[0]["next_action"]
    section = ContextSection(key="O0", title="规划", content="# 大纲\n\n原句  原样。")
    assert section.model_content() == section.content
    prose = "他把回执折回原样，压回台面底下那层纸。最后关了灯。"
    note = {"annotations": [{"kind": "new_fact", "statement": "收起回执", "quote": "他收好了回执。"}]}
    questions = annotation_gaps(note, prose)
    reply = {"answers": ["正文写了‘他把回执折回原样，压回台面底下那层纸。’，所以收起。"], "corrected_hook_note": None}
    corrected = clarify_annotation_quotes(note, questions, reply, prose)
    assert not annotation_gaps(corrected["corrected_hook_note"], prose)
    assert note["annotations"][0]["quote"] == "他收好了回执。" and reply["corrected_hook_note"] is None
    assert clarify_annotation_quotes(note, ["其他问题"], reply, prose) == reply
    assert clarify_annotation_quotes(note, questions, {"answers": ["语义一致，可以定位。"]}, prose).get("corrected_hook_note") is None
    inconsistent = {"verdict": "patch", "confidence": .9, "summary": "未发现问题", "findings": []}
    assert ModeCheckOutput.model_validate(inconsistent).verdict == "patch", "Historical output remains readable"
    try:
        _model_from_json_content(json.dumps(inconsistent), ModeCheckOutput)
    except ValidationError:
        pass
    else:
        raise AssertionError("A new needs-revision response without a problem must be corrected by its author")
    assert _model_from_json_content(json.dumps({**inconsistent, "verdict": "pass"}), ModeCheckOutput).verdict == "pass"
    route = TerminalSession._deterministic_single_chapter_review("审查并验收第44章")
    assert route.action == "review_accept" and route.chapter_no == 44 and route.max_revision_rounds == 0
    assert TerminalSession._deterministic_single_chapter_review("请审查第44章当前草稿").action == "review"
    for text in ("不要审查第44章", "能不能审查第44章？", "审查第44章然后写第45章", "审查第44至50章"):
        assert TerminalSession._deterministic_single_chapter_review(text) is None


if __name__ == "__main__":
    check()
