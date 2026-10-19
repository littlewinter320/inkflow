"""Offline contracts for collaboration selection and frozen provider attempts."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel

from inkflow.config import Settings
from inkflow.project import InkFlowProject
from inkflow.provider import DeepSeekProvider, ScriptedProvider, _model_role
from inkflow.errors import ProviderError
from inkflow.role_protocol import roles_for_mode, check_owners_for_mode
from inkflow.runtime import ensure_run_runtime, active_runtime
from inkflow.schemas import BookBrief, TerminalIntent
from inkflow.studio import StudioService, bind_active_collaboration
from inkflow.task_settings import (capture_task_settings, restore_task_settings, use_task_settings,
    active_task_settings, TaskSettingsError)
from inkflow.utils import content_hash
from inkflow.engine import InkFlowEngine
from inkflow.terminal_session import TerminalSession
from inkflow.schemas import ContextPacket


def project_and_scope(tmp_path, preset="adaptive", mode=None):
    project = InkFlowProject.create(tmp_path, BookBrief(title="协作检查", genre="悬疑", protagonist="调查员",
        premise="调查员按已有线索核对一桩旧案。"))
    db = StudioService(project).db
    scope = db.prepare_task_settings("offline-original", novel_id=project.project_id,
        settings=Settings(collaboration_preset=preset), workspace_root=project.root, collaboration_mode=mode)
    return project, db, scope


@pytest.mark.parametrize("preset,role,mode,count", [
    ("everyday", None, "everyday", 3), ("adaptive", "reviewer", "review_boost", 4),
    ("adaptive", "memory_keeper", "memory_boost", 4), ("full_specialist", None, "full_specialist", 5)])
def test_presets_bind_once_restore_and_keep_runtime(tmp_path, preset, role, mode, count):
    project, db, scope = project_and_scope(tmp_path, preset)
    original = scope.public_summary()
    with use_task_settings(scope), ensure_run_runtime() as runtime:
        runtime.reserve(17)
        bound = bind_active_collaboration(project, "write_review", specialist=role, reason="当前任务需要专项核对")
        assert bound.effective_collaboration_mode == mode
        assert len(roles_for_mode(mode)) == count
        assert bound.public_summary() == original
        assert active_runtime.get() is runtime and runtime.calls == 1
        rebound = bind_active_collaboration(project, "write_review", specialist="memory_keeper" if role == "reviewer" else "reviewer")
        assert rebound.effective_collaboration_mode == mode
        restored = db.prepare_task_settings("offline-original", novel_id=project.project_id,
            settings=lambda: (_ for _ in ()).throw(AssertionError("恢复不能读取当前设置")), workspace_root=project.root)
        assert restored.effective_collaboration_mode == mode and restored.snapshot_hash == scope.snapshot_hash
        child = db.prepare_batch_settings(novel_id=project.project_id, reference=None, settings=scope.settings,
            workspace_root=project.root)
        assert child.effective_collaboration_mode == mode and not child.collaboration_pending
    owners = check_owners_for_mode(mode)
    if mode == "review_boost":
        assert owners == {"expression": "editor", "logic_continuity": "reviewer", "memory": "editor"}
    if mode == "memory_boost":
        assert owners == {"general": "editor", "memory": "memory_keeper"}


@pytest.mark.parametrize("mode", ["everyday", "memory_boost", "review_boost", "full_specialist"])
def test_explicit_mode_overrides_adaptive_preset(tmp_path, mode):
    project, db, scope = project_and_scope(tmp_path, mode=mode)
    assert not scope.collaboration_pending
    assert db.bind_task_collaboration(scope, action="write_review", specialist="reviewer").effective_collaboration_mode == mode


def test_pending_adaptive_only_routes_or_reads_and_respects_no_extra_calls(tmp_path):
    project, db, scope = project_and_scope(tmp_path)
    with use_task_settings(scope):
        assert _model_role("coordinator") == "coordinator"
        for role in (None, "writer", "editor", "reviewer", "memory_keeper"):
            with pytest.raises(ProviderError, match="尚未固定"):
                _model_role(role)
        assert bind_active_collaboration(project, "discuss").collaboration_pending
        assert bind_active_collaboration(project, "status").collaboration_pending
        bound = bind_active_collaboration(project, "write_review", forbidden_actions=["extra_model_calls"])
        assert bound.effective_collaboration_mode == "everyday" and not bound.collaboration_pending
        assert _model_role("writer") == "writer"
        with pytest.raises(ProviderError, match="未启用"):
            _model_role("reviewer")


@pytest.mark.parametrize("damage", ["missing", "hash", "identity"])
def test_damaged_collaboration_binding_never_reselects(tmp_path, damage):
    project, db, scope = project_and_scope(tmp_path)
    bound = db.bind_task_collaboration(scope, action="story_settings_manage", specialist="memory_keeper")
    with db.connect() as connection:
        key = f"task_collaboration:{scope.task_id}"
        row = connection.execute("SELECT value_json FROM studio_metadata WHERE key=?", (key,)).fetchone()
        if damage == "missing":
            connection.execute("DELETE FROM studio_metadata WHERE key=?", (key,))
        else:
            data = json.loads(row["value_json"])
            data["record_hash" if damage == "hash" else "snapshot_hash"] = "damaged"
            connection.execute("UPDATE studio_metadata SET value_json=? WHERE key=?", (json.dumps(data), key))
        connection.commit()
    with pytest.raises(TaskSettingsError, match="缺失或损坏"):
        db.bind_task_collaboration(bound, action="write_review", specialist="reviewer")
    with pytest.raises(TaskSettingsError, match="缺失或损坏"):
        db.prepare_task_settings("offline-original", novel_id=project.project_id, workspace_root=project.root)


def test_old_snapshot_keeps_original_mode_and_static_optional_defaults(tmp_path):
    snapshot = capture_task_settings(Settings(), novel_id="old", collaboration_mode="memory_boost")
    for name in ("collaboration_preset", "quality_preset", "quality_custom_context"):
        snapshot["settings"].pop(name, None)
    snapshot["snapshot_hash"] = content_hash(json.dumps({k: v for k, v in snapshot.items() if k != "snapshot_hash"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    restored = restore_task_settings(snapshot, novel_id="old", workspace_root=tmp_path)
    assert restored.effective_collaboration_mode == "memory_boost" and not restored.collaboration_pending
    assert restored.settings.collaboration_preset == "everyday" and restored.settings.quality_preset == "custom"


@pytest.mark.parametrize("role,action,expected", [("reviewer", "story_settings_manage", "review_boost"),
    ("memory_keeper", "story_settings_manage", "memory_boost"), (None, "discuss", "everyday")])
def test_same_coordinator_route_freezes_specialist_before_dispatch(tmp_path, monkeypatch, role, action, expected):
    project, db, scope = project_and_scope(tmp_path)
    intent = TerminalIntent(action=action, specialist_role=role, specialist_reason="按本次目标分工",
        setting_change={"operation": "audit"}, authorization="approved", visible_reason="核对资料", user_message="核对当前资料")
    provider = ScriptedProvider([intent])
    session = TerminalSession(InkFlowEngine(provider, scope.settings))
    monkeypatch.setattr(session, "_quick_response", lambda *args: None)
    monkeypatch.setattr(session, "_build_packet", lambda *args: ContextPacket(project_id=project.project_id,
        chapter_no=1, task="离线理解", sections=[], estimated_tokens=0))
    observed = []
    async def dispatch(*args, **kwargs):
        current = active_task_settings.get()
        observed.append((current.effective_collaboration_mode, current.collaboration_pending))
        return {"status": "completed", "summary": "离线派工"}
    monkeypatch.setattr(session, "_dispatch", dispatch)
    with use_task_settings(scope):
        asyncio.run(session.handle(project.root, "请结合现有资料判断合适的处理方式。", opened_project=project))
    assert len(provider.calls) == 1 and provider.calls[0]["agent_role"] == "coordinator"
    if action == "discuss":
        assert observed == []
        assert db.prepare_task_settings("offline-original", novel_id=project.project_id,
            workspace_root=project.root).collaboration_pending
    else:
        assert observed == [(expected, False)]


def test_desktop_explicit_presets_and_cli_use_effective_mode(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from inkflow.app_server import _requested_task_mode
    import inkflow.cli as cli
    assert _requested_task_mode("conversation.send", {"message": "用按需加强模式，核对现有资料"}) == "adaptive"
    assert _requested_task_mode("conversation.send", {"message": "用全面协作模式，核对现有资料"}) == "full_specialist"
    project, db, scope = project_and_scope(tmp_path)
    bound = db.bind_task_collaboration(scope, action="review", specialist="memory_keeper")
    review = AsyncMock(return_value={"verdict": "pass"})
    monkeypatch.setattr(cli, "_engine", lambda *args: SimpleNamespace(review_chapter_mode=review))
    with use_task_settings(bound):
        cli._review(SimpleNamespace(root=project.root, chapter=1))
    assert review.call_args.kwargs["mode"] == "memory_boost"


def test_auxiliary_modes_and_pending_parent_cannot_start_an_unbound_batch(tmp_path):
    from inkflow.app_server import _requested_task_mode
    project, db, scope = project_and_scope(tmp_path)
    for method in ("project.ideate", "prompt.optimize", "provider.test"):
        mode = _requested_task_mode(method, {})
        snapshot = capture_task_settings(scope.settings, novel_id=project.project_id, collaboration_mode=mode)
        auxiliary = restore_task_settings(snapshot, novel_id=project.project_id, workspace_root=project.root)
        assert not auxiliary.collaboration_pending and auxiliary.effective_collaboration_mode == "everyday"
        with use_task_settings(auxiliary):
            assert _model_role("writer") == "writer"
    with use_task_settings(scope):
        with pytest.raises(TaskSettingsError, match="先固定原任务"):
            db.prepare_batch_settings(novel_id=project.project_id, reference=None, settings=scope.settings,
                workspace_root=project.root)


def test_four_role_review_merges_distinct_scores_and_rejects_old_coverage(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from inkflow.schemas import ContextSection, ModeCheckOutput, ReviewFocusObservation
    from inkflow.provider import ProviderResult
    from inkflow.utils import utc_now
    project, db, scope = project_and_scope(tmp_path)
    scope = db.bind_task_collaboration(scope, action="review", specialist="reviewer")
    body = "他沿着老街找到一封信，反复辨认信封上的字迹。想到失踪的朋友，他决定沿线继续追查。"
    plan = "主角沿着老街寻找失踪者，获得线索后决定继续追查。"
    path = "chapters/chapter_00001.draft.md"
    (project.root / path).write_text(body, encoding="utf-8")
    project.db.upsert_draft(1, "沿线追查", path, body)
    with project.db.connect() as connection:
        connection.execute("INSERT INTO plans(kind,plan_key,parent_key,data_json,updated_at) VALUES('chapter','chapter:00001','arc-test',?,?)",
            (json.dumps({"chapter_no": 1, "target_words": 18}), utc_now()))
        connection.commit()
    packet = ContextPacket(project_id=project.project_id, chapter_no=1, task="离线审查", estimated_tokens=0,
        sections=[ContextSection(key=name, title=name, content=plan, source_ids=[name], hard=True)
                  for name in ("OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md")])
    calls = []
    async def generate(**kwargs):
        role = kwargs["agent_role"]
        names = ["readability"] if role == "editor" else ["continuity", "causality", "requirements", "motivation", "progression"]
        calls.append((role, names))
        output = ModeCheckOutput(verdict="pass", confidence=.9, summary="已核对所负责项目",
            memory_patch={"chapter_no": 1, "chapter_summary": "本章推进寻找"} if role == "editor" else None,
            assessments=[dict(criterion=name, status="met", grade=4, chapter_evidence="他决定沿线继续追查。",
                source_id="OUTLINE.md", source_evidence=plan, reason="得到线索后继续寻找，符合本章目标。",
                alternative="线索尚未揭示真相，但本章只要求取得下一步方向。", evidence_relation="direct") for name in names],
            source_comparisons=[dict(source_id=name, source_evidence=plan, chapter_evidence="他决定沿线继续追查。",
                relation="aligned", reason="获得线索后继续推进主线。", evidence_relation="direct")
                for name in ("OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md")],
            focus_observation=ReviewFocusObservation(observed_goal="寻找失踪者线索", goal_evidence="他沿着老街找到一封信",
                observed_change="决定继续追查", change_evidence="他决定沿线继续追查。", plan_alignment="adapted",
                alignment_reason="获得线索后继续寻找"))
        return ProviderResult(data=output, model="offline", response_id=None, reasoning_content=None, usage={}, agent_role=role)
    engine = InkFlowEngine(SimpleNamespace(generate_json=generate), scope.settings)
    monkeypatch.setattr("inkflow.engine._deterministic_audit", lambda *args, **kwargs: ({}, []))
    monkeypatch.setattr(engine, "_context_builder", lambda *args: SimpleNamespace(build=lambda *args, **kwargs: packet))
    with use_task_settings(scope):
        result = asyncio.run(engine.review_chapter_mode(project.root, 1, mode="review_boost"))
        assert result["verdict"] == "pass"
        assert {role for role, _ in calls} == {"editor", "reviewer"} and len(calls) == 2
        assert len({criterion for _, names in calls for criterion in names}) == 6
        record = project.db.latest_review_record(1)
        record["mode_bundle"]["coverage"] = [dict(check_id=check, owner=owner, status="passed") for check, owner in
            {"general": "editor", "logic_continuity": "reviewer", "memory": "editor"}.items()]
        monkeypatch.setattr(project.db, "latest_review_record", lambda number: record)
        assert engine.current_pass_review(project, 1) is None
        assert len(calls) == 2


class RetryPayload(BaseModel):
    answer: str


@pytest.mark.parametrize("preset,attempts", [("economy", 2), ("balanced", 3)])
def test_provider_quality_retry_limit_uses_same_runtime(monkeypatch, preset, attempts):
    calls = []
    async def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("offline", request=request)
    client = httpx.AsyncClient
    monkeypatch.setattr("inkflow.provider.httpx.AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr("inkflow.provider.asyncio.sleep", AsyncMock())
    monkeypatch.setattr(Settings, "require_api_key", lambda self: "offline")
    with ensure_run_runtime() as runtime:
        with pytest.raises(ProviderError, match=f"经过 {attempts} 次") as failure:
            asyncio.run(DeepSeekProvider(Settings(quality_preset=preset)).generate_json(
                system_prompt="离线检查", user_prompt="完整内容", output_model=RetryPayload, agent_role="writer"))
        assert len(calls) == runtime.calls == attempts
        assert failure.value.recovery_attempts == attempts - 1 and failure.value.recovery_exhausted
