"""Focused check for source-bound quote decoding and public failure guidance; no model calls."""
from pydantic import ValidationError

from inkflow.app_server import _error_payload
from inkflow.errors import ProviderError
from inkflow.review_verifier import resolve_review_quote_refs, restore_review_quote_choices
from inkflow.schemas import ModeCheckOutput
from inkflow.utils import content_hash


def check():
    output = ModeCheckOutput.model_validate({"verdict": "unknown", "confidence": 0, "summary": "仍需语义核验",
        "findings": [{"category": "style", "severity": "info", "explanation": "句子可以再简洁", "repair_instruction": "不强制改稿", "evidence": "@quote:0"}],
        "memory_patch": {"chapter_no": 2, "chapter_summary": "核对原件", "facts": [
            {"fact_id": "f", "subject": "甲", "predicate": "持有", "value": "原件", "valid_from_chapter": 2,
             "evidence": "@quote:0", "evidence_refs": [
                {"source_chapter": 2, "quote": "@quote:0", "relation": "support"},
                {"source_chapter": 1, "quote": "@quote:0", "relation": "support"},
                {"source_chapter": 3, "quote": "@quote:future", "relation": "support"}]}],
            "operations": [{"action": "supplement", "target_fact_id": "f", "reason": "原文补充证据", "evidence": [
                {"source_chapter": 1, "quote": "@quote:prior", "relation": "support"}]}]}})
    sources = {"当前正文": "甲取出了原件。", "chapter:00001": "甲收起了原件。", "chapter:00003": "甲交出了原件。"}
    registry = {key: {"source_id": source, "quote_hash": content_hash(sources[source])} for key, source in
        [("@quote:0", "当前正文"), ("@quote:prior", "chapter:00001"), ("@quote:future", "chapter:00003")]}
    choices = restore_review_quote_choices(registry, sources)
    decoded = resolve_review_quote_refs(output, choices)
    assert decoded.findings[0].evidence == sources["当前正文"]
    assert decoded.memory_patch.facts[0].evidence_refs[0].quote == sources["当前正文"]
    assert decoded.memory_patch.facts[0].evidence_refs[1].quote == "@quote:0", "A current quote cannot impersonate a prior chapter"
    assert decoded.memory_patch.facts[0].evidence_refs[2].quote == "@quote:future", "Future evidence cannot enter earlier canon"
    assert decoded.memory_patch.operations[0].evidence[0].quote == sources["chapter:00001"]
    assert decoded.verdict == "unknown" and output.findings[0].evidence == "@quote:0"
    assert "@quote:0" not in restore_review_quote_choices(registry, {**sources, "当前正文": "甲拿出了副本。"})
    operation = {"stage": "mode.editor", "chapter_no": 42}
    error = _error_payload(KeyError("stop_reason"), operation)
    assert "第 42 章" in error["message"] and "任务衔接的程序问题" in error["message"]
    assert "stop_reason" not in error["message"] and "preserved" not in error and "impact" not in error
    error = _error_payload(ProviderError("HTTP 429"), operation)
    assert error["retryable"] and "过于频繁" in error["message"] and "限次重试" not in error["message"]
    assert not _error_payload(RuntimeError("unknown"))["retryable"]
    try:
        ModeCheckOutput.model_validate({"verdict": "pass"})
    except ValidationError as exc:
        assert "处需要核对" in _error_payload(exc, operation)["message"]
    print("Quote references and public guidance passed: current/prior/future identity, unchanged unknown verdict, contextual cause, no preservation boilerplate.")


if __name__ == "__main__":
    check()
