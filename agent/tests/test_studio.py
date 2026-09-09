from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from inkflow.app_server import InkFlowAppService
from inkflow.config import Settings, load_user_settings, save_user_settings
from inkflow.errors import ConfigurationError
from inkflow.project import InkFlowProject
from inkflow.schemas import BookBrief, MemoryPatch
from inkflow.studio import StudioService


def _brief() -> BookBrief:
    return BookBrief(
        title="纸月站台",
        genre="都市奇谭",
        premise="末班地铁每月多停一站，值班员必须找到被遗漏的乘客。",
        protagonist="许衡",
        target_chapter_words=2_000,
        estimated_chapters=24,
        estimated_volumes=2,
    )


def test_user_settings_are_non_secret_and_cap_output(tmp_path: Path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(settings_path))

    saved = save_user_settings(
        {
            "model": "custom-model",
            "base_url": "https://example.test/v1",
            "max_output_tokens": 16_000,
        }
    )

    assert saved["model"] == "custom-model"
    assert load_user_settings()["max_output_tokens"] == 16_000
    assert "api_key" not in settings_path.read_text(encoding="utf-8")
    assert Settings.from_env().is_deepseek is False
    with pytest.raises(ConfigurationError, match="16K"):
        save_user_settings({"max_output_tokens": 16_001})
    with pytest.raises(ConfigurationError, match="密钥"):
        save_user_settings({"api_key": "must-not-be-written"})


def test_studio_saves_draft_versions_and_reanchors_annotations(tmp_path: Path) -> None:
    project = InkFlowProject.create(tmp_path / "story", _brief())
    draft_path = project.root / "chapters" / "chapter_00001.draft.md"
    draft_path.write_text("# 第一章\n\n列车停在没有名字的站台。\n", encoding="utf-8")
    project.db.upsert_draft(1, "没有名字的站台", "chapters/chapter_00001.draft.md", draft_path.read_text("utf-8"))
    studio = StudioService(project)
    document = studio.read_document("chapters/chapter_00001.draft.md")
    start = document["content"].index("没有名字")
    annotation = studio.create_annotation(
        "chapters/chapter_00001.draft.md", start, start + len("没有名字"), "这里需要更具体的视觉线索"
    )

    changed = document["content"].replace("列车停在", "雨夜里，列车停在")
    saved = studio.save_document(
        "chapters/chapter_00001.draft.md",
        changed,
        expected_hash=document["content_hash"],
    )

    assert saved["saved"] is True
    anchored = next(item for item in saved["annotations"] if item["annotation_id"] == annotation["annotation_id"])
    assert anchored["status"] == "open"
    assert changed[anchored["start_offset"] : anchored["end_offset"]] == "没有名字"
    assert studio.db.list_versions("chapters/chapter_00001.draft.md")[0]["applied"] == 1


def test_studio_turns_accepted_edit_into_unapplied_proposal(tmp_path: Path) -> None:
    project = InkFlowProject.create(tmp_path / "story", _brief())
    relative = "chapters/chapter_00001.md"
    original = "# 第一章\n\n许衡看见了不存在的站牌。\n"
    path = project.root / relative
    path.write_text(original, encoding="utf-8")
    project.db.upsert_draft(1, "不存在的站牌", relative, original)
    project.db.accept_chapter(
        1,
        "不存在的站牌",
        relative,
        original,
        MemoryPatch(chapter_no=1, chapter_summary="许衡看见异常站牌。"),
    )
    studio = StudioService(project)
    document = studio.read_document(relative)

    result = studio.save_document(
        relative,
        original.replace("看见", "错过"),
        expected_hash=document["content_hash"],
    )

    assert result["saved"] is False
    assert result["requires_canon_revision"] is True
    assert result["proposal"]["applied"] is False
    assert path.read_text(encoding="utf-8") == original


def test_app_service_initializes_and_opens_project(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INKFLOW_SETTINGS_PATH", str(tmp_path / "settings.json"))
    project = InkFlowProject.create(tmp_path / "story", _brief())
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    service = InkFlowAppService()
    initialized = asyncio.run(service.dispatch("app.initialize", {}, emit))
    opened = asyncio.run(service.dispatch("project.open", {"project_root": str(project.root)}, emit))

    assert initialized["product"] == "墨流（InkFlow）"
    assert initialized["capabilities"]["formal_agents"] == ["Writer", "Reviewer", "Memory Keeper"]
    assert opened["dashboard"]["brief"]["title"] == "纸月站台"
    assert opened["tree"]["groups"][0]["label"] == "章节"


def test_task_history_recovers_only_dead_engine_runs(tmp_path: Path, monkeypatch) -> None:
    project = InkFlowProject.create(tmp_path / "story", _brief())
    database = StudioService(project).db
    database.start_task(
        "run-alive",
        owner_id="server-101-alive",
        method="workflow.run",
        params={"action": "write", "chapter_no": 1},
    )
    database.start_task(
        "run-dead",
        owner_id="server-202-dead",
        method="workflow.run",
        params={"action": "review", "chapter_no": 1},
    )
    monkeypatch.setattr(
        "inkflow.studio._owner_process_alive",
        lambda owner_id: owner_id == "server-101-alive",
    )

    changed = database.reconcile_interrupted_tasks("server-303-current")

    assert changed == 1
    assert database.get_task("run-alive")["status"] == "running"
    assert database.get_task("run-dead")["status"] == "interrupted"
    assert database.get_task("run-dead")["retryable"] is True


def test_task_retry_boundary_blocks_ambiguous_or_canon_actions(tmp_path: Path) -> None:
    project = InkFlowProject.create(tmp_path / "story", _brief())
    database = StudioService(project).db
    cases = [
        ("safe-write", "workflow.run", {"action": "write", "chapter_no": 2}, True),
        ("unsafe-accept", "workflow.run", {"action": "accept", "chapter_no": 2}, False),
        (
            "ambiguous-chat",
            "conversation.send",
            {"message": "验收第二章，如果不行就回退"},
            False,
        ),
    ]
    for run_id, method, params, expected in cases:
        database.start_task(run_id, owner_id="server-1-test", method=method, params=params)
        database.finish_task(run_id, status="failed", error_message="模拟中断")
        task = database.get_task(run_id)
        assert task["retryable"] is expected
        if not expected:
            assert "重新说明并确认" in task["retry_note"]
