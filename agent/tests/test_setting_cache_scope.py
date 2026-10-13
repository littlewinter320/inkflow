"""Changing a selected setting must not displace unchanged book foundations."""
from inkflow.context import ContextBuilder
from inkflow.story_settings import StorySettingsService
from setting_fixture import project_fixture, arc


def test_real_setting_context_stays_after_book_prefix(tmp_path):
    project = project_fixture(tmp_path)
    bundle = project.db.get_current_plan_bundle()
    project.db.save_plan_bundle(bundle.model_copy(update={"current_arc": arc(11, 15)}))
    settings = StorySettingsService(project)
    collection = settings.save_collection(name="人物", template_id="character")
    record = settings.save_record(collection["collection_id"], title="修锁匠", values={"state": "等待核实"})
    first = ContextBuilder(project).build(11, "写第11章", mode="draft")
    settings.save_record(collection["collection_id"], record_id=record["record_id"], expected_revision=record["revision"],
        title="修锁匠", values={"state": "准备询问证人"})
    second = ContextBuilder(project).build(12, "写第12章", mode="draft")
    assert next(section for section in first.sections if section.key == "CSET").cache_scope == "chapter"
    first_prompt, second_prompt = first.to_model_prompt(), second.to_model_prompt()
    assert first_prompt[:first_prompt.index("## CSET.")] == second_prompt[:second_prompt.index("## CSET.")]
    assert "等待核实" in first_prompt and "准备询问证人" in second_prompt
