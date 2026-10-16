import pytest
from pydantic import ValidationError

from inkflow.render import render_review
from inkflow.schemas import ModeCheckOutput, ReviewCopyEdit, ReviewReport


def test_review_copyedit_contract_preserves_legacy_reports_and_bounded_suggestions():
    original = {"verdict": "pass", "confidence": 1.0, "summary": "当前版本通过。"}
    assert ModeCheckOutput(**original).copyedits == ReviewReport(**original).copyedits == []
    edit = ReviewCopyEdit(target_excerpt="他低低低声说。", replacement="他低声说。",
                         kind="duplicate", reason="重复字。", meaning_preserved=True)
    assert edit.author_role is None
    report = ReviewReport(**original, copyedits=[edit.model_copy(update={"author_role": "editor"})])
    assert ReviewReport.model_validate_json(report.model_dump_json()).copyedits[0].author_role == "editor"
    rendered = render_review(1, report, {})
    assert "## 微改建议" in rendered and "原句：他低低低声说。" in rendered
    assert "建议替换：他低声说。" in rendered and "原因：重复字。" in rendered
    for model in (ModeCheckOutput, ReviewReport):
        with pytest.raises(ValidationError):
            model(**original, copyedits=[edit] * 5)
    with pytest.raises(ValidationError):
        ReviewCopyEdit(**{**edit.model_dump(), "author_role": "writer"})
