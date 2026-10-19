from dataclasses import replace

import pytest

from inkflow.config import Settings
from inkflow.context import ContextBuilder
from inkflow.errors import ConfigurationError
from setting_fixture import project_fixture


def test_custom_capacity_preserves_selected_strategy_and_generation(tmp_path):
    selected = Settings(quality_preset="economy", quality_custom_context=False)
    custom = replace(selected, quality_custom_context=True, context_hard_tokens=600000)
    restored = Settings.from_mapping(custom.to_mapping())
    assert restored.quality_preset == "economy" and restored.quality_custom_context
    assert restored.agent_generation == selected.agent_generation
    assert restored.review_min_confidence == selected.review_min_confidence
    assert restored.context_hard_tokens == 600000
    project = project_fixture(tmp_path)
    assert ContextBuilder(project, quality_preset="economy")._quality_guidance()
    assert ContextBuilder(project)._quality_guidance() == ""
    with pytest.raises(ConfigurationError):
        Settings.from_mapping({"quality_preset": "unknown"})


def test_quality_reads_ranked_history_not_fixed_recent_chapters(tmp_path):
    from inkflow.schemas import ContextSection
    from inkflow.errors import ValidationGateError
    project = project_fixture(tmp_path)
    builder = ContextBuilder(project, quality_preset="quality")
    def hit(number):
        row = project.db.get_chapter(number)
        return dict(source_id=f"chapter-summary:{number:05d}", source_type="chapter_summary",
                    chapter_no=number, version=row["version"], source_hash=row["content_hash"])
    sections = [ContextSection(key="E", title="衔接正文", content="已有全文", source_ids=["chapter:00010"], hard=True)]
    reads = builder._quality_reading([hit(2), hit(2), hit(10), {**hit(2), "chapter_no": 11}],
                                    sections, chapter_no=11)
    assert [s.key for s in sections] == ["E", "QR-2"]
    assert reads[0]["state"] == "canonical_source_loaded" and reads[1]["state"] == "already_in_context"
    assert sections[1].content == project.db.canonical_chapter_content(2).strip() and sections[1].hard
    with pytest.raises(ValidationGateError, match="版本发生变化"):
        builder._quality_reading([{**hit(3), "source_hash": "stale"}], [], chapter_no=11)


def test_quality_reading_shares_source_and_token_limits(tmp_path):
    from inkflow.schemas import ContextSection
    project = project_fixture(tmp_path)
    builder = ContextBuilder(project, quality_preset="quality")
    hits = [dict(source_id=f"fact-{n}", source_type="canon_fact", chapter_no=n) for n in range(1, 11)]
    sections = [ContextSection(key=f"TH-source-{n}", title="已有补读", content="原文",
                               source_ids=[f"chapter:{n:05d}"], hard=True) for n in (9, 10)]
    reads = builder._quality_reading(hits, sections, chapter_no=11)
    assert sum(s.key.startswith("QR-") for s in sections) == 4
    assert any(r["state"] == "omitted" for r in reads)
    builder.hard_token_limit = 4096
    sections = []
    reads = builder._quality_reading(hits[:1], sections, chapter_no=11)
    assert not sections and reads[0]["state"] == "excerpt_only"


@pytest.mark.parametrize("preset", ["economy", "balanced", "quality"])
def test_only_quality_promotes_ranked_full_history_in_writer_packet(tmp_path, monkeypatch, preset):
    project = project_fixture(tmp_path)
    builder = ContextBuilder(project, quality_preset=preset)
    row = project.db.get_chapter(2)
    hit = dict(source_id="chapter:00002", source_type="canon_source", chapter_no=2,
               version=row["version"], source_hash=row["content_hash"],
               body=project.db.canonical_chapter_content(2), score=1)
    monkeypatch.setattr(builder.retriever, "retrieve", lambda *args, **kwargs: [hit])
    packet = builder.build(10, "沿现有线索继续核对", mode="draft")
    full = next((s for s in packet.sections if s.key == "QR-2"), None)
    assert bool(full) == (preset == "quality")
    if full:
        assert full.hard and full.source_ids == ["chapter:00002"]
        assert builder.retriever.last_diagnostics["quality_reading"][0]["state"] == "canonical_source_loaded"


def test_coordinator_publishes_compiled_materials_without_claiming_a_call(tmp_path, monkeypatch):
    import json
    from inkflow.engine import InkFlowEngine
    from inkflow.provider import ScriptedProvider
    from inkflow.terminal_session import TerminalSession
    from inkflow.studio import StudioService
    from inkflow.task_settings import use_task_settings
    from inkflow.context_status import read_context_status
    project = project_fixture(tmp_path)
    settings = Settings(workspace_root=project.root)
    scope = StudioService(project).db.prepare_task_settings("context-display", novel_id=project.project_id,
        settings=settings, workspace_root=project.root)
    with use_task_settings(scope):
        packet = TerminalSession(InkFlowEngine(ScriptedProvider([]), scope.settings))._build_packet(project, "查看当前进度")
    monkeypatch.setattr("inkflow.context_status.usage_ledger_path", lambda: tmp_path / "no-calls.sqlite")
    value = read_context_status(project, settings)
    role = next(r for r in value["roles"] if r["role"] == "coordinator")
    assert role["request_state"] == "not_called" and role["used_percent"] is None
    assert role["materials"]["estimated_tokens"] == packet.estimated_tokens
    assert role["materials"]["configured_hard_limit_tokens"] > 0
    assert role["materials"]["record_kind"] == "compiled_materials"
    summary = json.loads((project.internal / "context-status-coordinator-summary.json").read_text(encoding="utf-8"))
    assert summary["budget_allocation"] and "material_contents" not in summary
