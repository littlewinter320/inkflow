import copy

from inkflow.context import _covered_recent_source, _fit_soft_content, _RECENT_PROSE_SOFT_LIMIT
from inkflow.review_verifier import evidence_matches, packet_sources
from inkflow.schemas import ContextPacket, ContextSection
from inkflow.utils import content_hash


def test_resolved_summary_reuses_only_retained_same_version_canonical_prose():
    text = "# 第 50 章\n\n他把收据收进抽屉。\n"
    digest = content_hash(text)
    loaded = {50: {"chapter_no": 50, "version": 2, "content_hash": digest, "content": text}}
    hit = {"source_id": "chapter-summary:00050", "source_type": "chapter_summary",
           "body_kind": "accepted_prose", "body": text, "version": 2, "source_hash": digest,
           "resolved_source": {"source_id": "chapter:00050", "chapter_no": 50, "version": 2,
                               "source_hash": digest, "start": 0, "end": len(text)}}
    e = f"### 第 50 章\n\n{text}"
    retained = _fit_soft_content(e, _RECENT_PROSE_SOFT_LIMIT, keep_tail=True)
    assert _covered_recent_source(hit, loaded, retained) == "chapter:00050"
    assert hit["source_id"] == "chapter-summary:00050"
    packet = ContextPacket(project_id="offline", chapter_no=51, task="审查", estimated_tokens=0,
        sections=[ContextSection(key="E", title="近期原文", content=e, source_ids=["chapter:00050"])])
    sources = packet_sources(packet)
    assert "chapter-summary:00050" not in sources
    assert evidence_matches("他把收据收进抽屉。", sources["chapter:00050"])
    for field, value in (("chapter_no", 49), ("version", 1), ("source_hash", "0" * 64),
                         ("source_id", "chapter:00049"), ("end", len(text) - 1)):
        changed = copy.deepcopy(hit)
        changed["resolved_source"][field] = value
        assert _covered_recent_source(changed, loaded, retained) is None
    for changed in ({**hit, "resolved_source": None}, {**hit, "body_kind": "summary"},
                    {**hit, "body": text + "独有动作。"}, {**hit, "version": 1},
                    {**hit, "source_hash": "0" * 64}):
        assert _covered_recent_source(changed, loaded, retained) is None
    # A later source can push this chapter out of the guaranteed E tail: retain F then.
    shortened = _fit_soft_content(e + "\n\n### 第 51 章\n\n" + "后续材料。" * 6000,
                                 _RECENT_PROSE_SOFT_LIMIT, keep_tail=True)
    assert _covered_recent_source(hit, loaded, shortened) is None
    assert _covered_recent_source(hit, {50: {**loaded[50], "content": text + "新动作。"}}, retained) is None
