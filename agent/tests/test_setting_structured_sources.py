"""Structured settings input drops only source-proven redundant projections."""
from copy import deepcopy
import json
from types import SimpleNamespace

from inkflow.planning_pipeline import _render_detail, _render_outline, _render_window, v2_execution_bundle
from inkflow.project import render_book_brief
from inkflow.schemas import BookBrief, BookOutlineV2, RollingPlanV2, VolumeDetailV2
from inkflow.story_settings import setting_structured_sources
from inkflow.utils import content_hash


def test_structured_sources_deduplicate_only_exact_current_projections(tmp_path):
    brief = BookBrief(title="离线设定核对", genre="现实悬疑", premise="普通人沿已有材料寻找街坊并核对疑问。", protagonist="主人公")
    outline = BookOutlineV2(title=brief.title, body="普通人的行动须有前因与后果。" * 500, volumes=[{
        "volume_no": 1, "title": "第一卷", "chapter_start": 1, "chapter_end": 20,
        "central_conflict": "主人公沿已有线索寻求证人信任，承担关系变化与现实阻力。",
        "outcome": "通过可定位的材料核对核心疑问，并承担新的生活与人情后果。"}])
    detail = VolumeDetailV2(volume_no=1, title="第一卷", chapter_start=1, chapter_end=20,
                            body="本卷沿已经成立的事实逐步安排人物行动。" * 300,
                            rough_chapter_beats=["沿已有线索行动"] * 5)
    window = RollingPlanV2(anchor_chapter=10, anchor_summary="此前已经核对成立的事实应当保留。" * 10,
        chapters=[{"chapter_no": n, "title": f"下一步{n}", "body": "人物通过已有材料核对疑问，并对得到的结果承担实际后果。" * 10}
                  for n in (11, 12)])
    sources = {"BOOK.md": render_book_brief(brief), "OUTLINE.md": _render_outline(outline),
               "STORY_DETAIL.md": _render_detail(detail), "RECENT_PLAN.md": _render_window(window)}
    original_sources = deepcopy(sources)
    for name, text in sources.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    source_dir = tmp_path / ".inkflow/runs/formal-source"
    source_dir.mkdir(parents=True)
    for name, value in (("outline-candidate.json", outline), ("volume-detail-candidate.json", detail),
                        ("chapter-window-candidate.json", window)):
        (source_dir / name).write_text(value.model_dump_json(), encoding="utf-8")
    manifest = {"status": "active", "trace_id": "formal-source", "revision_no": 3, "volume_no": 1,
                "chapter_window": [10, 12], "outline_hash": content_hash(sources["OUTLINE.md"]),
                "volume_detail_hash": content_hash(sources["STORY_DETAIL.md"]),
                "recent_plan_hash": content_hash(sources["RECENT_PLAN.md"])}
    (tmp_path / "planning").mkdir()
    (tmp_path / "planning/active-v2.json").write_text(json.dumps(manifest), encoding="utf-8")
    stored_plan = [None]

    def no_write(**_):
        raise AssertionError("结构投影不能写待办或数据库")

    project = SimpleNamespace(root=tmp_path, db=SimpleNamespace(get_brief=lambda: brief,
        get_current_plan_bundle=lambda: stored_plan[0]), record_pending_work=no_write)
    stored_plan[0] = v2_execution_bundle(project, manifest, outline, detail, window)
    original_plan = stored_plan[0].model_dump(mode="json")

    def project_sources(values):
        files_before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        value = setting_structured_sources(project, values)
        assert files_before == {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        return value

    matched = project_sources(sources)
    assert matched["book_brief"]["duplicate_projection_omitted"] is True
    assert matched["plan"]["duplicate_projection_omitted"] is True
    assert matched["plan"]["revision_no"] == 3 and matched["plan"]["chapter_window"] == [10, 12]
    assert matched["plan"]["source_hashes"]["OUTLINE.md"] == manifest["outline_hash"]
    assert "不是已发生正史" in matched["source_notes"]["plan"]

    changed_source = project_sources({**sources, "OUTLINE.md": sources["OUTLINE.md"] + "\n用户追加的独有内容。"})
    assert changed_source["plan"] == original_plan and "未省略差异" in changed_source["source_notes"]["plan"]
    changed_book = project_sources({**sources, "BOOK.md": sources["BOOK.md"] + "\n用户追加要求。"})
    assert changed_book["book_brief"] == brief.model_dump(mode="json")

    stored_plan[0] = stored_plan[0].model_copy(update={"current_arc": stored_plan[0].current_arc.model_copy(
        update={"public_risks_to_verify": ["数据库中仍须保留的独有约定。"]})})
    different_db = project_sources(sources)
    assert different_db["plan"] == stored_plan[0].model_dump(mode="json")
    assert "未省略独有内容" in different_db["source_notes"]["plan"]
    stored_plan[0] = stored_plan[0].model_copy(update={"current_arc": stored_plan[0].current_arc.model_copy(
        update={"public_risks_to_verify": []})})

    (source_dir / "outline-candidate.json").write_text("{}", encoding="utf-8")
    invalid_source = project_sources(sources)
    assert invalid_source["plan"] == original_plan and "未能完整核验" in invalid_source["source_notes"]["plan"]
    assert sources == original_sources and stored_plan[0].model_dump(mode="json") == original_plan
