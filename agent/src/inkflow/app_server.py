from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import traceback
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from . import __version__
from .config import Settings, api_key_status, save_api_key_to_keyring, save_user_settings
from .coordinator import Coordinator
from .engine import InkFlowEngine
from .errors import InkFlowError, ProjectBusyError, ProjectError, ProviderError
from .learning import LearningService
from .project import InkFlowProject
from .project_lock import project_lock_wait_policy, project_write_lock, project_write_lock_sync
from .provider import create_provider
from .references import ReferenceService
from .role_protocol import normalize_role
from .schemas import BookBrief, CollaborationReply, ContextPacket, ContextSection, MemoryPatch, NovelIdeaBundle, NovelIdeaCandidate, PrefillSuggestion, PromptOptimization, ProviderProbe, ReviewReport, SuggestedPrompt, TerminalIntent, WriterDirectionSet
from .review_verifier import verify_review
from .studio import StudioDatabase, StudioService, chapter_retry_state, text_statistics
from .terminal_session import TerminalSession
from .trace import TraceRecorder, recent_trace_runs
from .runtime import RunRuntime, active_runtime
from .model_usage import UsageLedger, ValidationQuotaExceeded
from .planning_cleanup import cleanup_apply, cleanup_keep, cleanup_preview
from .task_settings import active_task_settings
from .utils import content_hash, estimate_tokens, project_source_revision, workflow_failure_reason, workflow_result_status
from .voice import VOICE_SETTING_NAMES, VoiceRuntime, _project_key


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


_COLLABORATION_MODES = {
    "everyday", "review_boost", "memory_boost", "deep", "full_specialist",
}
_EXPLICIT_MODE_PREFIX = re.compile(
    r"^\s*请?\s*(?:开启|使用|切换到|切换回|按|用)\s*"
    r"(日常|审查加强|记忆加强|深度|特殊五角色|五角色)模式(?=\s|[，,：:]|审|复|检|$)"
)
_SPOKEN_MODES = {
    "日常": "everyday",
    "审查加强": "review_boost",
    "记忆加强": "memory_boost",
    "深度": "deep",
    "特殊五角色": "full_specialist",
    "五角色": "full_specialist",
}


def _requested_task_mode(method: str, params: dict[str, Any]) -> tuple[int, str] | None:
    """Choose a mode only from an explicit request, never from risk keywords."""
    if method not in {"conversation.send", "workflow.run"}:
        return None
    raw_mode = params.get("collaboration_mode")
    if raw_mode is None and method == "conversation.send":
        match = _EXPLICIT_MODE_PREFIX.match(str(params.get("message") or ""))
        if match:
            raw_mode = _SPOKEN_MODES[match.group(1)]
    raw_version = params.get("role_protocol_version")
    if raw_mode is None and raw_version is None:
        return None
    mode = str(raw_mode or "everyday")
    if mode not in _COLLABORATION_MODES:
        raise InkFlowError("未知协作模式；没有启动专项模型调用。")
    if raw_version is not None and type(raw_version) is not int:
        raise InkFlowError("角色协议版本必须是明确的整数；没有启动专项模型调用。")
    task_scope = active_task_settings.get()
    version = (
        task_scope.role_protocol_version
        if raw_version is None and mode == "everyday" and task_scope is not None
        else 2 if raw_version is None else raw_version
    )
    if version not in {1, 2} or (version == 1 and mode != "everyday"):
        raise InkFlowError("角色协议与协作模式不匹配；没有启动专项模型调用。")
    return version, mode


def _task_rows_with_history(project: InkFlowProject, limit: int, instance_id: str) -> list[dict[str, Any]]:
    """Use one task interpretation in the project page and collaboration panel."""
    studio = StudioService(project)
    reconciliation_pending = False
    try:
        studio.db.reconcile_interrupted_tasks(instance_id)
    except sqlite3.OperationalError:
        reconciliation_pending = True
    tasks = studio.db.list_tasks(limit)
    pending = project.db.get_metadata("pending_creation_task")
    if isinstance(pending, dict) and pending.get("status") == "running" and pending.get("run_id"):
        # A process exit or an older error path may leave a stale "running"
        # marker.  Only the matching durable run can decide whether the task
        # is interrupted or failed; never resume from the marker alone.
        try:
            pending_run = studio.db.get_task(str(pending["run_id"]))
        except sqlite3.OperationalError:
            pending_run = None
        reconciled_status = (
            "interrupted" if pending_run and pending_run.get("status") == "interrupted"
            and pending_run.get("error_code") == "engine_process_exited"
            else "failed" if pending_run and pending_run.get("status") == "failed"
            else None
        )
        if reconciled_status:
            recovered_pending = {**pending, "status": reconciled_status}
            try:
                with project_write_lock_sync(project.root, timeout=0.25):
                    current = project.db.get_metadata("pending_creation_task")
                    if (
                        isinstance(current, dict)
                        and current.get("run_id") == pending.get("run_id")
                        and current.get("status") == "running"
                    ):
                        project.db.set_metadata("pending_creation_task", recovered_pending)
            except (InkFlowError, sqlite3.OperationalError):
                # A competing writer may still own the book. The matching
                # interrupted run remains authoritative for this UI response.
                pass
            pending = recovered_pending
    quality_hold = project.latest_accepted_quality_hold()
    held_chapter = int(quality_hold["chapter_no"]) if quality_hold else None
    chapters: dict[int, dict[str, Any] | None] = {}
    resumable_id = (
        str(pending.get("run_id") or "")
        if isinstance(pending, dict) and pending.get("status") in {"interrupted", "waiting_condition", "waiting_user"}
        and TerminalSession.pending_resume(project, "继续上次任务") is not None
        else ""
    )
    for task in tasks:
        if task.get("method") == "workflow.run" and task.get("action") == "write" and task.get("retryable"):
            guard = (task.get("params") or {}).get("_retry_guard")
            try:
                current_guard = chapter_retry_state(project, int((task.get("params") or {}).get("chapter_no")))
                if not isinstance(guard, dict) or current_guard != guard:
                    task["retryable"] = False
                    task["retry_note"] = "第 {0} 章已在原失败后发生变化，系统不会用旧请求覆盖当前草稿。".format(
                        (task.get("params") or {}).get("chapter_no", "目标")
                    )
                    task["next_step"] = "请打开当前章节核对已保存内容，再从当前版本继续；旧任务仍可查看。"
            except Exception:
                task["retryable"] = False
                task["retry_note"] = "无法核实目标章节版本，因此没有开放整条任务重放。"
                task["next_step"] = "请刷新项目并核对当前草稿；确认版本后再从未完成步骤续接。"
        if task["status"] in {"failed", "cancelled", "interrupted", "waiting_condition", "waiting_user"}:
            original = task.get("params") or {}
            value = original.get("chapter_no")
            if value is None:
                match = re.search(r"第\s*(\d+)\s*章", str(task.get("error_message") or task.get("summary") or ""))
                value = match.group(1) if match else None
            try:
                chapter_no = int(value) if value is not None else None
            except (TypeError, ValueError):
                chapter_no = None
            if chapter_no and chapter_no != held_chapter:
                if chapter_no not in chapters:
                    chapters[chapter_no] = project.db.get_chapter(chapter_no)
                chapter = chapters[chapter_no]
                if chapter and chapter.get("status") == "accepted" and chapter.get("accepted_at"):
                    try:
                        newer_result = datetime.fromisoformat(str(chapter["accepted_at"]).replace("Z", "+00:00")) > datetime.fromisoformat(str(task["updated_at"]).replace("Z", "+00:00"))
                    except ValueError:
                        newer_result = False
                    if newer_result:
                        task["historical"] = True
                        task["retryable"] = False
                        task["next_step"] = (
                            f"第 {chapter_no} 章在这次失败之后已进入正史；旧记录不代表现在仍卡在此处。"
                            "请以当前正文和审查为准，原错误留作追溯。"
                        )
        task["resume_available"] = bool(
            resumable_id and task["run_id"] == resumable_id
            and task["status"] in {"failed", "cancelled", "interrupted", "waiting_condition", "waiting_user"} and not task.get("historical")
        )
        if task["resume_available"]:
            task["retryable"] = False
            task["next_step"] = "已找到此任务的保存断点；可继续未完成的步骤，不重做已接受章节。"
        if reconciliation_pending and task["status"] == "running":
            task["status_check_pending"] = True
            task["next_step"] = "任务数据库暂时忙，尚未确认旧引擎是否仍在运行；稍后刷新状态，不要重发请求。"
    if tasks:
        run_ids = [str(item["run_id"]) for item in tasks if item.get("run_id")]
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            with project.db.connect() as connection:
                rows = connection.execute(
                    "SELECT run_id, COUNT(*) AS pending_count FROM agent_artifacts "
                    "WHERE artifact_type='user_steering' AND status IN ('pending','deferred') "
                    f"AND run_id IN ({placeholders}) GROUP BY run_id",
                    run_ids,
                ).fetchall()
            pending_counts = {str(row["run_id"]): int(row["pending_count"]) for row in rows}
            for item in tasks:
                count = pending_counts.get(str(item.get("run_id") or ""), 0)
                item["pending_user_update_count"] = count
                if count and item.get("status") == "completed":
                    item["next_step"] = f"有 {count} 条中途补充尚未用于已完成结果；请打开任务并从原话继续处理。"
    return tasks


def _recorded_user_updates(project: InkFlowProject, run_id: str) -> list[dict[str, Any]]:
    """Read raw mid-task user input with its immutable revision and disposition."""
    with project.db.connect() as connection:
        rows = connection.execute(
            "SELECT artifact_id, status, data_json, created_at FROM agent_artifacts "
            "WHERE artifact_type='user_steering' AND run_id=? ORDER BY created_at, artifact_id",
            (run_id,),
        ).fetchall()
    updates = []
    for row in rows:
        data = json.loads(row["data_json"])
        updates.append({
            "artifact_id": row["artifact_id"], "status": row["status"],
            "message": str(data.get("message") or ""),
            "task_revision": int(data.get("task_revision") or 0),
            "proposed_task_revision": int(data.get("proposed_task_revision") or 0),
            "applied_task_revision": int(data.get("applied_task_revision") or 0),
            "sequence": int(data.get("sequence") or 0),
            "related_task_id": str(data.get("related_task_id") or ""),
            "pending_question_id": str(data.get("pending_question_id") or ""),
            "response_kind": str(data.get("response_kind") or ""),
            "created_at": row["created_at"],
        })
    return sorted(updates, key=lambda item: (item["sequence"], item["created_at"]))


def _batch_source_run(project: InkFlowProject, db: StudioDatabase, manifest: dict[str, Any]) -> dict[str, Any] | None:
    for run_id in (manifest.get("last_run_id"), manifest.get("origin_run_id")):
        if not run_id:
            continue
        try:
            source = db.get_task(str(run_id))
            if source:
                return source
        except (KeyError, ProjectError, sqlite3.OperationalError):
            pass
    task_id = manifest.get("origin_task_id")
    if not task_id and isinstance(manifest.get("task_settings"), dict):
        task_id = manifest["task_settings"].get("task_id")
    return db.latest_task_run_for_settings(str(task_id)) if task_id else None


class InkFlowAppService:
    """桌面端与编辑器共享的本地应用服务。"""

    def __init__(self, instance_id: str | None = None) -> None:
        self.instance_id = instance_id or f"server-{os.getpid()}-{uuid.uuid4().hex}"
        self.voice = VoiceRuntime()

    async def dispatch(
        self,
        method: str,
        params: dict[str, Any],
        emit: EventSink,
        consume_steering: Callable[[], Awaitable[list[str]]] | None = None,
    ) -> Any:
        if method == "memory.audit":
            from .memory_records import memory_overview
            project = InkFlowProject(self._validated_project_root(params), recover_on_open=False)
            return memory_overview(project.db)
        if method == "preferences.manage":
            from .preferences import author_connection, change_item, history, list_items
            level = str(params.get("level", "author"))
            action = str(params.get("action", "list"))
            if level not in {"author", "project"}:
                raise ValueError("请选择作者默认习惯或本书偏好")
            preference_db = None
            if level == "project" or action in {"override", "usage"}:
                preference_db = InkFlowProject(self._validated_project_root(params), recover_on_open=False).db
            if action == "override":
                with project_write_lock_sync(self._validated_project_root(params)):
                    disabled = set(preference_db.get_metadata("author_preferences_disabled", []))
                    item_id = str(params["preference_id"])
                    if bool(params.get("disabled", True)):
                        disabled.add(item_id)
                    else:
                        disabled.discard(item_id)
                    preference_db.set_metadata("author_preferences_disabled", sorted(disabled))
                return {"disabled": sorted(disabled)}
            if action == "usage":
                return {"selection": preference_db.get_metadata("preference.selection", {}),
                        "disabled": preference_db.get_metadata("author_preferences_disabled", [])}
            with (author_connection() if level == "author" else preference_db.connect()) as connection:
                if action == "list":
                    return {"preferences": list_items(connection, active_only=False, level=level)}
                if action == "history":
                    return {"events": history(connection, str(params.get("preference_id", "")))}
                if action == "save":
                    return change_item(connection, level=level, **{key: params[key] for key in (
                        "preference_id", "text", "strength", "scope", "status", "source_quote",
                        "source_ref", "reason", "topic", "supersedes", "expected_revision"
                    ) if key in params})
            raise ValueError("未知的偏好管理操作")
        if method == "app.initialize":
            return {
                "product": "墨流（InkFlow）",
                "version": __version__,
                "protocol_version": 1,
                "role_protocol_version": 1,
                "role_contract_versions": [1, 2],
                "role_execution_versions": [1, 2],
                "capabilities": {
                    "desktop": True,
                    "mcp": True,
                    "vscode": True,
                    "agents": ["Coordinator", "Writer", "Editor"],
                    "formal_agents": ["Coordinator", "Writer", "Editor"],
                    "novel_production_agents": ["Writer", "Editor"],
                    "role_pool": Coordinator.capabilities(protocol_version=2),
                    "enabled_collaboration_modes": [
                        "everyday", "review_boost", "memory_boost", "deep", "full_specialist",
                    ],
                    "services": ["memory", "context", "runtime"],
                    "raw_chain_of_thought": False,
                    "voice_runtime": True,
                    "voice_is_formal_agent": False,
                },
                "provider": api_key_status(Settings.from_env().provider_kind),
                # Voice inventory can traverse large local model trees. The
                # welcome screen does not use it, so do not gate engine
                # compatibility and every startup RPC on that scan.
                "voice": {"status": "deferred"},
            }
        if method == "provider.status":
            settings = Settings.from_env(params.get("workspace_root"))
            return {
                **api_key_status(settings.provider_kind),
                "provider_kind": settings.provider_kind,
                "base_url": settings.base_url,
                "model": settings.model,
                "reasoning_effort": settings.reasoning_effort,
                "context_soft_tokens": settings.context_soft_tokens,
                "context_hard_tokens": settings.context_hard_tokens,
                "context_budget_mode": settings.context_budget_mode,
                "agent_context_budgets": settings.agent_context_budgets,
                "max_output_tokens": settings.max_output_tokens,
                "inquiry_frequency": settings.inquiry_frequency,
                "hook_strategy": settings.hook_strategy,
                "chapter_length_tolerance": settings.chapter_length_tolerance,
                "review_min_confidence": settings.review_min_confidence,
                "acceptance_confirmation_mode": settings.acceptance_confirmation_mode,
                "planning_publication_mode": settings.planning_publication_mode,
                "planning_window_chapters": settings.planning_window_chapters,
                "dialogue_history_mode": settings.dialogue_history_mode,
                "dialogue_history_interval": settings.dialogue_history_interval,
                "dialogue_history_limit": settings.dialogue_history_limit,
                "agent_generation": settings.agent_generation,
                "review_verification_mode": settings.review_verification_mode,
                "review_experience_detail": settings.review_experience_detail,
                "review_local_nli_model": settings.review_local_nli_model,
                "review_judge_model": settings.review_judge_model,
                "retrieval_embedding_model": settings.retrieval_embedding_model,
                "retrieval_reranker_model": settings.retrieval_reranker_model,
                "powershell_enabled": settings.powershell_enabled,
                "input_price_per_million": settings.input_price_per_million,
                "output_price_per_million": settings.output_price_per_million,
                "capabilities": create_provider(settings).capabilities(),
                "role_execution_version": 1,
                "active_collaboration_mode": "everyday",
                "enabled_collaboration_modes": [
                    "everyday", "review_boost", "memory_boost", "deep", "full_specialist",
                ],
                **settings.role_settings_view(params.get("role_settings_version", 1)),
                "role_settings_warnings": list(settings.role_settings_warnings),
            }
        if method == "provider.configure":
            provider_kind = str(params.get("provider_kind") or Settings.from_env().provider_kind).lower()
            key = str(params.get("api_key", "")).strip()
            if key:
                # Windows credential APIs can be slow or show a system prompt;
                # never make the asyncio loop wait for them.
                await asyncio.to_thread(save_api_key_to_keyring, key, provider_kind)
            allowed = {
                name: params[name]
                for name in (
                    "provider_kind",
                    "base_url",
                    "model",
                    "reasoning_effort",
                    "context_soft_tokens",
                    "context_hard_tokens",
                    "context_budget_mode",
                    "agent_context_budgets",
                    "max_output_tokens",
                    "inquiry_frequency",
                    "hook_strategy",
                    "chapter_length_tolerance",
                    "review_min_confidence",
                    "acceptance_confirmation_mode",
                    "planning_publication_mode",
                    "planning_window_chapters",
                    "dialogue_history_mode",
                    "dialogue_history_interval",
                    "dialogue_history_limit",
                    "agent_generation",
                    "role_settings_version",
                    "review_verification_mode",
                    "review_experience_detail",
                    "review_local_nli_model",
                    "review_judge_model",
                    "retrieval_embedding_model",
                    "retrieval_reranker_model",
                    "powershell_enabled",
                    "input_price_per_million",
                    "output_price_per_million",
                )
                if name in params
                and (
                    name in {
                        "review_local_nli_model",
                        "review_judge_model",
                        "retrieval_embedding_model",
                        "retrieval_reranker_model",
                        "powershell_enabled",
                    }
                    or params[name] not in (None, "")
                )
            }
            if allowed:
                await asyncio.to_thread(save_user_settings, allowed)
            current = await asyncio.to_thread(Settings.from_env, params.get("workspace_root"))
            return {"configured": True, **api_key_status(current.provider_kind), **allowed}
        if method == "provider.capabilities":
            settings = Settings.from_env(params.get("workspace_root"))
            return create_provider(settings).capabilities()
        if method == "provider.models":
            settings = Settings.from_env(params.get("workspace_root"))
            return {"models": await create_provider(settings).list_models()}
        if method == "provider.test":
            settings = Settings.from_env(params.get("workspace_root"))
            await emit({"type": "provider.testing", "summary": "正在验证密钥、接口与模型名称"})
            result = await create_provider(settings).generate_json(
                system_prompt="你只负责返回 API 连通性检查结果。",
                user_prompt=(
                    "请用一句简短中文向用户打招呼，明确表示你收到了这次请求；"
                    "status 返回 ok，并用一句公开说明解释本次只验证了模型能够接收请求和按格式回复。"
                ),
                output_model=ProviderProbe,
                effort="low",
                max_tokens=160,
                thinking=False,
                timeout_seconds=min(settings.request_timeout_seconds, 60.0),
            )
            return {
                "connected": result.data.status == "ok",
                "message": f"连接成功，当前模型：{result.model}",
                "model": result.model,
                "reply": result.data.reply,
                "public_reasoning_summary": result.data.public_reasoning_summary,
            }
        if method == "voice.status":
            # Directory-size checks and optional package discovery can touch a
            # large local model tree. Keep them off the asyncio event loop so
            # a settings click cannot freeze the desktop while status refreshes.
            return await asyncio.to_thread(
                self.voice.status,
                params.get("workspace_root") or params.get("project_root"),
            )
        if method == "voice.settings.get":
            return await asyncio.to_thread(
                self.voice.settings,
                params.get("workspace_root") or params.get("project_root"),
            )
        if method == "voice.settings.configure":
            updates = {name: params[name] for name in VOICE_SETTING_NAMES if name in params}
            return await asyncio.to_thread(
                self.voice.configure,
                updates,
                params.get("workspace_root") or params.get("project_root"),
            )
        if method == "voice.moss.install":
            return await self.voice.install_moss(str(params.get("confirmation") or ""), emit)
        if method == "voice.edge.install":
            return await self.voice.install_edge(str(params.get("confirmation") or ""), emit)
        if method in {"voice.asr.install", "voice.light.install"}:
            return await self.voice.install_asr(str(params.get("confirmation") or ""), emit)
        if method == "voice.models.delete":
            return await self.voice.delete_voice_component(
                str(params.get("component") or ""),
                str(params.get("confirmation") or ""),
            )
        if method == "voice.kokoro.install":
            raise ValueError("Kokoro 已由 MOSS 取代；语音输入请安装本地普通话识别组件。")
        if method == "voice.profile.list":
            return {"profiles": self.voice.list_profiles()}
        if method == "voice.clone_script.generate":
            return await self._generate_voice_clone_script(params, emit)
        if method == "voice.profile.clone":
            return await self.voice.create_clone(params)
        if method == "voice.profile.update":
            return self.voice.update_profile(str(params.get("profile_id") or ""), params)
        if method == "voice.profile.prepare_finetune":
            return self.voice.prepare_finetune(
                str(params.get("profile_id") or ""),
                str(params.get("dataset_path") or ""),
            )
        if method == "voice.transcribe":
            text = await self.voice.transcribe(str(params.get("audio_path") or ""))
            return {"text": text, "language": "zh-CN", "mode": "local_mandarin"}
        if method == "voice.speak":
            return await self.voice.speak(
                str(params.get("text") or ""),
                str(params.get("profile_id") or "") or None,
                str(params.get("purpose") or "dialogue"),
            )
        if method == "prompt.optimize":
            prompt = str(params.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("请先输入需要优化的提示词。")
            if len(prompt) > 8_000:
                raise ValueError("提示词超过 8000 字，请先缩小范围后再优化。")
            settings = Settings.from_env(params.get("workspace_root") or params.get("project_root"))
            await emit({"type": "prompt.optimizing", "summary": "正在核对目标、约束与交付格式"})
            result = await create_provider(settings).generate_json(
                system_prompt=(
                    "你是墨流输入框的提示词优化工具，不是新的小说 Agent，也不执行提示词。"
                    "先批评原提示词中缺失或含糊的目标、上下文、约束、输出格式和验收标准，再综合为一版可直接提交的中文提示词。"
                    "必须保持用户原始意图、语气、禁止项、费用边界、正史边界和授权范围；不得把建议升级为已确认命令，"
                    "不得擅自增加验收、写入正史、删除、发布、付费或其他外部操作。"
                    "不要冗长扩写，不要虚构项目事实，不要输出原始思维链。"
                    "optimized_prompt 只放优化后的提示词；change_summary 用 1～6 条短句说明可见修改；"
                    "preserved_constraints 逐条列出从原文保留的关键约束。"
                ),
                user_prompt=f"请优化下面这条提示词。\n\n--- 原提示词 ---\n{prompt}",
                output_model=PromptOptimization,
                effort="low",
                max_tokens=1800,
                thinking=False,
                timeout_seconds=min(settings.request_timeout_seconds, 90.0),
            )
            return {
                "original_prompt": prompt,
                **result.data.model_dump(),
                "model": result.model,
                "source_method": "critique_then_synthesize",
            }
        if method == "assistant.suggest":
            # This route is called automatically on project/message changes.
            # Suggestions are optional UI aids, not a reason to spend writing
            # budget or transmit recent conversations in the background.
            project = self._try_project(params.get("workspace_root") or params.get("project_root"))
            return {
                "suggestions": _local_suggested_prompts(project),
                "model": "", "fallback": False, "source": "local",
            }
        if method == "project.ideate":
            from .preferences import preference_prompt
            settings = Settings.from_env(params.get("workspace_root"))
            preferences = str(params.get("preferences") or "").strip() + preference_prompt()
            fast = bool(params.get("fast", True))
            requested_count = 1 if fast else 3
            await emit(
                {
                    "type": "writer.started",
                    "summary": "Writer 正在快速生成一个开书方向" if fast else "Writer 正在构思三个不同的开书方向",
                }
            )
            try:
                if fast:
                    result = await self._ideate_direction(
                        settings, preferences, angle=None, fast=True
                    )
                    candidates = list(result.data.candidates[:1])
                    reasoning = list(result.data.public_reasoning_summary)
                    model_names = [result.model]
                else:
                    # 三个方向分开调用，每次只负责一条互斥的叙事引擎。让模型在一次
                    # 调用里同时给出三个方案时，低推理预算下很容易复读同一份内容。
                    settled = await asyncio.gather(
                        *(
                            self._ideate_direction(settings, preferences, angle=angle, fast=False)
                            for angle in _IDEA_ANGLES
                        ),
                        return_exceptions=True,
                    )
                    usable = [item for item in settled if not isinstance(item, BaseException)]
                    if not usable:
                        failure = next(
                            (item for item in settled if isinstance(item, BaseException)),
                            None,
                        )
                        raise failure if isinstance(failure, ProviderError) else ProviderError(
                            "模型没有返回任何可用的开书方向。"
                        )
                    candidates = await self._separate_repeated_directions(
                        settings,
                        preferences,
                        [item.data.candidates[0] for item in usable],
                    )
                    reasoning = [
                        note for item in usable for note in item.data.public_reasoning_summary
                    ]
                    model_names = [item.model for item in usable]
                fallback_used = False
                model_name = "、".join(dict.fromkeys(model_names))
                idea_payload = NovelIdeaBundle(
                    candidates=candidates[:requested_count],
                    public_reasoning_summary=_idea_reasoning_summary(reasoning),
                ).model_dump()
            except ProviderError as exc:
                # 建项窗口不能因模型一次结构化输出截断而让用户无法创建项目。
                # 保底方案只是可编辑的起点，明确标注来源，不伪装成模型成功产物。
                fallback_used = True
                model_name = "本地保底构思"
                idea_payload = _fallback_idea_bundle(preferences, requested_count).model_dump()
                await emit(
                    {
                        "type": "writer.fallback",
                        "summary": "模型构思未能按时返回完整方案，已提供可编辑的保底开书方向",
                        "details": _short_provider_error(exc),
                    }
                )
            await emit(
                {
                    "type": "writer.completed",
                    "summary": "快速方案已经准备好" if fast else "三个开书方案已经准备好，等待用户选择",
                }
            )
            original_rule = f"用户原始偏好（不可擅自改写）：{preferences}" if preferences else ""
            for index, candidate in enumerate(idea_payload["candidates"], start=1):
                # 模型给出的 concept_id 可能重复、带空格，甚至在同一次响应里复用。
                # 项目创建界面不能把模型生成字段当作 UI 主键，因此在本地按展示顺序
                # 重建稳定且唯一的标识；这不会改变方案正文，也不会增加模型调用。
                candidate["concept_id"] = f"idea-{index}"
                candidate["user_rules"] = [original_rule] if original_rule else []
                _apply_requested_scale(candidate, preferences)
                if not candidate["core_selling_point"].strip():
                    candidate["core_selling_point"] = (
                        f"{candidate['genre']}题材下的低起点成长、持续升级矛盾与长线悬念"
                    )
                candidate["choice_note"] = (
                    f"Writer 选择这个方向，是因为：{candidate['core_selling_point']}。"
                    "这是本次创意提案，不代表用户已经确认。"
                )
            safe_reasoning = [
                item
                for item in idea_payload["public_reasoning_summary"]
                if not any(marker in item for marker in ("用户", "偏好", "要求", "核对"))
            ]
            preference_note = (
                f"已按原文记录用户偏好：{preferences}"
                if preferences
                else "用户没有填写硬性偏好，本次细节均为 Writer 的创意提案。"
            )
            idea_payload["public_reasoning_summary"] = [preference_note, *safe_reasoning[:2]]
            if len(idea_payload["public_reasoning_summary"]) < 2:
                idea_payload["public_reasoning_summary"].append(
                    "除用户原始偏好外，其余题材与情节细节都可以继续修改。"
                )
            return {
                **idea_payload,
                "message": "Writer 已生成开书方向；选中后再建立项目，不会自动写入正史。",
                "model": model_name,
                "mode": "quick" if fast else "compare",
                "fallback_used": fallback_used,
                "notice": (
                    "模型本次没有在预算内返回完整结构化方案，下面是可直接修改的保底方向；"
                    "可先采用并编辑，也可以稍后重新生成。"
                    if fallback_used
                    else ""
                ),
            }
        if method == "project.create":
            root = Path(str(params["project_root"])).resolve()
            brief = BookBrief.model_validate(params["brief"])
            result = self._engine(root).create_project(root, brief)
            return {**result, "tree": StudioService(InkFlowProject(root)).tree()}

        if method in {
            "voice.roles.get", "voice.roles.set", "voice.roles.analyze",
            "voice.job.create", "voice.job.list", "voice.job.status",
            "voice.job.pause", "voice.job.resume", "voice.job.cancel",
        }:
            # Voice role maps and job manifests are stored outside the novel
            # database. Do not construct InkFlowProject here: its recovery
            # waits on the Writer's project lock during a long chapter run.
            root = self._validated_project_root(params)
            if method == "voice.roles.get":
                return self.voice.get_role_map(root)
            if method == "voice.roles.set":
                return self.voice.set_role_map(root, params)
            if method == "voice.roles.analyze":
                return self.voice.analyze_roles(str(params.get("text") or ""))
            if method == "voice.job.create":
                return self.voice.create_job(root, params, emit)
            if method == "voice.job.list":
                return {"jobs": self.voice.list_jobs(root)}

            job_id = str(params.get("job_id") or "")
            job = self.voice.job_status(job_id)
            if job.get("project_key") != _project_key(root):
                raise ValueError("当前项目没有这个语音转换任务。")
            if method == "voice.job.status":
                return job
            if method == "voice.job.pause":
                return self.voice.pause_job(job_id)
            if method == "voice.job.resume":
                return self.voice.resume_job(job_id, emit)
            return self.voice.cancel_job(job_id)

        if method == "task.rename":
            # A display-name edit must remain responsive during a long Writer
            # call and must never acquire the novel's canonical write lock.
            root = self._validated_project_root(params)
            db_path = root / ".inkflow" / "studio.db"
            if not db_path.is_file():
                raise InkFlowError("当前项目尚无任务记录，无法改名。")
            db = await asyncio.to_thread(StudioDatabase, db_path)
            task = await asyncio.to_thread(
                db.rename_task, str(params["task_id"]), str(params["title"]),
            )
            return {"task": task}

        if method == "task.status":
            # The renderer may need this answer while another model request
            # still owns the project mutation lock. Do not run project recovery
            # before reading the durable task record.
            root = self._validated_project_root(params)
            db_path = root / ".inkflow" / "studio.db"
            if not db_path.is_file():
                raise InkFlowError("当前项目尚无任务记录，无法确认原任务状态。")
            db = await asyncio.to_thread(StudioDatabase, db_path)

            def reconcile_task_owner() -> bool:
                try:
                    db.reconcile_interrupted_tasks(self.instance_id)
                    return True
                except sqlite3.OperationalError:
                    return False

            reconciled = await asyncio.to_thread(reconcile_task_owner)
            task = await asyncio.to_thread(db.get_task, str(params["task_id"]))
            updates = await asyncio.to_thread(
                lambda: _recorded_user_updates(InkFlowProject(root, recover_on_open=False), str(task["run_id"]))
            )
            return {
                "task": {key: task[key] for key in (
                    "run_id", "title", "status", "summary", "error_message", "retryable", "retry_note", "next_step"
                )},
                "user_updates": updates,
                "reconciled": reconciled,
            }

        if method == "task.list":
            root = self._validated_project_root(params)

            def read_tasks() -> list[dict[str, Any]]:
                project = InkFlowProject(root, recover_on_open=False)
                return _task_rows_with_history(project, int(params.get("limit", 50)), self.instance_id)

            return {"tasks": await asyncio.to_thread(read_tasks)}

        if method == "workflow.run" and str(params.get("action") or "") == "checkpoint_list":
            root = self._validated_project_root(params)
            result = await asyncio.to_thread(self._engine(root).checkpoint_list, root, int(params.get("limit", 50)))
            return result

        if method == "project.open":
            # A chapter run can hold the mutation lock throughout a remote
            # model call. Opening the read-only workspace must not queue
            # behind it. Attempt startup repair only if the lock is free.
            root = self._validated_project_root(params)
            project = await asyncio.to_thread(InkFlowProject, root, recover_on_open=False)
            studio = await asyncio.to_thread(StudioService, project)

            def recover_if_idle() -> None:
                try:
                    with project_write_lock_sync(root, timeout=0.25):
                        project.recover_open_state()
                except InkFlowError:
                    project.recovery_warnings = [
                        "项目正在运行其他任务；本次只读打开，待写锁空闲后再执行恢复检查。"
                    ]

            await asyncio.to_thread(recover_if_idle)
            try:
                await asyncio.to_thread(studio.db.reconcile_interrupted_tasks, self.instance_id)
            except sqlite3.OperationalError:
                project.recovery_warnings.append("任务状态数据库暂时忙；旧任务状态稍后再核对，当前项目仍可只读打开。")
            dashboard, tree, migration = await asyncio.to_thread(
                lambda: (
                    studio.dashboard(),
                    studio.tree(),
                    project.canonical_content_migration_status(),
                )
            )
            return {"dashboard": dashboard, "tree": tree, "canon_migration": migration}

        if method == "batch.progress":
            root = self._validated_project_root(params)
            return {"batches": await asyncio.to_thread(
                lambda: _list_batch_summaries(InkFlowProject(root, recover_on_open=False))
            )}

        if method == "collaboration.overview":
            # The desktop refresh awaits this alongside project.open. It only
            # reads committed records and should not wait for a long Writer call.
            root = self._validated_project_root(params)

            def read_overview() -> dict[str, Any]:
                project = InkFlowProject(root, recover_on_open=False)
                return {
                    "threads": project.db.list_collaboration_threads(),
                    "messages": project.db.list_collaboration_messages(active_only=False, limit=int(params.get("limit", 80))),
                    "tasks": _task_rows_with_history(project, int(params.get("task_limit", 30)), self.instance_id),
                    "batches": _list_batch_summaries(project),
                    "learning_events": project.db.list_learning_events(int(params.get("learning_limit", 12))),
                    "artifacts": project.db.list_agent_artifacts(limit=30),
                    "trace_runs": recent_trace_runs(root, int(params.get("trace_limit", 12))),
                    "usage": _usage_overview(project, Settings.from_env(root)),
                }

            return await asyncio.to_thread(read_overview)

        # Project construction may perform recovery under a synchronous write
        # lock.  Keep that wait off the asyncio event loop so a concurrent
        # Writer/Editor task can make progress and release its own lock.
        project = await asyncio.to_thread(self._project, params)
        studio = StudioService(project)

        if method == "chapter.quality_hold.approve":
            return await asyncio.to_thread(
                project.approve_accepted_quality_hold,
                int(params["chapter_no"]), str(params["expected_hash"]),
            )

        if method == "project.canon_migration.status":
            return project.canonical_content_migration_status()
        if method == "project.canon_migration.apply":
            return project.apply_canonical_content_migration(str(params.get("confirmation_token") or ""))
        if method == "conversation.history":
            settings = Settings.from_env(project.root)
            return {
                "entries": TerminalSession.history(
                    project.root, settings.dialogue_history_limit
                ),
                "settings": {
                    "dialogue_history_mode": settings.dialogue_history_mode,
                    "dialogue_history_interval": settings.dialogue_history_interval,
                    "dialogue_history_limit": settings.dialogue_history_limit,
                },
            }
        if method == "conversation.history.save":
            return TerminalSession.save_manual_entry(
                project.root,
                str(params.get("user_message") or ""),
                str(params.get("reply") or ""),
            )
        if method == "project.status":
            return studio.dashboard()
        if method == "context.status":
            settings = Settings.from_env(project.root)
            status_path = project.internal / "context-status.json"
            if status_path.is_file():
                try:
                    value = json.loads(status_path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        saved_project_id = str(value.get("project_id") or "")
                        if saved_project_id in {"", project.project_id}:
                            saved_revision = str(value.get("source_revision") or "")
                            current_revision = project_source_revision(project.root, project.internal)
                            if saved_revision and saved_revision != current_revision:
                                return {
                                    **value,
                                    "status": "stale",
                                    "stale": True,
                                    "stale_reason": "项目资料已经变化，下一次 Writer、Editor 审查或修订任务会重新编译 Context Packet。",
                                    "current_source_revision": current_revision,
                                }
                            return value
                except (OSError, json.JSONDecodeError):
                    pass
            return {
                "status": "idle",
                "estimated_tokens": 0,
                "before_compression_tokens": 0,
                "previous_updated_at": "",
                "previous_estimated_tokens": None,
                "change_since_previous_tokens": None,
                "soft_limit_tokens": settings.context_budget_for("writer")[0],
                "hard_limit_tokens": settings.context_budget_for("writer")[1],
                "hard_usage_percent": 0,
                "compression_applied": False,
                "hard_sections": [],
                "compressible_sections": [],
                "warnings": [],
            }
        if method == "context.pins.list":
            chapter_no = int(params["chapter_no"]) if params.get("chapter_no") is not None else None
            return {"pins": studio.db.list_context_pins(chapter_no)}
        if method == "context.pins.set":
            with project_write_lock_sync(project.root):
                return studio.db.set_context_pin(
                    str(params.get("source_id") or ""),
                    chapter_no=int(params["chapter_no"]) if params.get("chapter_no") is not None else None,
                    note=str(params.get("note") or ""),
                    pinned=bool(params.get("pinned", True)),
                )
        if method == "project.tree":
            return studio.tree()
        if method == "planning.cleanup.preview":
            return cleanup_preview(project)
        if method == "planning.cleanup.keep":
            async with project_write_lock(project.root):
                return cleanup_keep(project, confirmation_token=str(params.get("confirmation_token") or ""),
                                    revision_id=str(params.get("revision_id") or ""))
        if method == "planning.cleanup.apply":
            paths = params.get("selected_paths")
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                raise ProjectError("请在预览中选择准确的旧候选文件。")
            async with project_write_lock(project.root):
                return cleanup_apply(project, confirmation_token=str(params.get("confirmation_token") or ""),
                                     selected_paths=paths)
        if method == "document.read":
            return studio.read_document(str(params["relative_path"]))
        if method == "document.read_reference":
            # 运行记录位于 .inkflow 内部目录，只允许只读预览；前端不会再把
            # 这类证据交给系统 shell 打开，也不能借此绕过通用文件写入门禁。
            relative_path = str(params.get("relative_path") or "").replace("\\", "/").lstrip("/")
            path = project.resolve_user_path(relative_path, allow_internal=True)
            if not path.is_file():
                raise ProjectError(f"运行记录不存在：{relative_path}")
            if path.suffix.lower() not in {".md", ".json", ".jsonl", ".txt", ".log"}:
                raise ProjectError("只读复核面板不支持预览此类文件。")
            content = path.read_text(encoding="utf-8", errors="replace")
            return {
                "relative_path": relative_path,
                "content": content,
                "content_hash": content_hash(content),
                "statistics": text_statistics(content),
                "annotations": [],
                "versions": [],
                "read_only": True,
                "reason": "运行记录只读预览；如需修改请回到对应正文、规划或审查入口。",
            }
        if method == "document.prefill":
            relative_path = str(params.get("relative_path") or "")
            chapter_match = re.search(r"chapter_(\d+)\.draft\.md$", relative_path)
            if not chapter_match:
                raise ValueError("预填续写只用于章节草稿")
            chapter_no = int(chapter_match.group(1))
            content = str(params.get("content") or "")
            cursor_offset = int(params.get("cursor_offset", len(content)))
            if cursor_offset < 0 or cursor_offset > len(content):
                raise ValueError("预填位置已经失效")
            expected_hash = str(params.get("expected_hash") or "")
            current_hash = content_hash(content)
            if expected_hash and expected_hash != current_hash:
                raise ValueError("正文已经变化，本次预填候选已取消")
            length = str(params.get("length") or "medium")
            length_limit = {"short": 80, "medium": 240, "long": 600}.get(length, 240)
            before = content[max(0, cursor_offset - 6_000):cursor_offset]
            after = content[cursor_offset:cursor_offset + 2_000]
            settings = Settings.from_env(project.root)
            prefill_trace = TraceRecorder(project.root, "prefill", settings.trace_level)
            packet = self._engine(project.root)._context_builder(project, "writer").build(
                chapter_no, "在光标处生成一段可选续写；不得保存、审查或提交正史。", mode="draft"
            )
            packet.sections.append(ContextSection(
                key="INPUT", title="当前光标附近正文", hard=True,
                content=json.dumps({"before_cursor": before, "after_cursor": after, "maximum_characters": length_limit}, ensure_ascii=False),
                source_ids=[f"draft:{chapter_no}:{current_hash}"],
            ))
            packet.estimated_tokens = estimate_tokens(packet.to_model_prompt())
            result = await create_provider(settings).generate_json(
                system_prompt=(
                    "你是墨流的 Writer，只生成编辑器光标处可插入的正文候选。保持人物、时态、视角和声线连续；"
                    "不要解释，不要重复光标前后的文字，不要修改文件，不要提交正史。"
                ),
                user_prompt=packet.to_model_prompt(),
                output_model=PrefillSuggestion,
                max_tokens=min(1_200, settings.max_output_tokens),
                thinking=False,
                agent_role="writer",
            )
            prefill_trace.record_model("writer.prefill", result, "Writer 已生成未落盘的光标候选")
            prefill_trace.finish(summary="预填候选已返回；未修改文件")
            return {
                **result.data.model_dump(),
                "document_hash": current_hash,
                "cursor_offset": cursor_offset,
                "model": result.model,
                "usage": result.usage,
            }
        if method == "document.save":
            return studio.save_document(
                str(params["relative_path"]),
                str(params.get("content", "")),
                expected_hash=params.get("expected_hash"),
                source=str(params.get("source") or "desktop_manual"),
            )
        if method == "document.delete":
            return studio.delete_document(
                str(params["relative_path"]),
                expected_hash=str(params.get("expected_hash") or ""),
            )
        if method == "project.trash.list":
            return {"items": project.list_document_trash()}
        if method == "project.trash.restore":
            result = project.restore_document(
                str(params.get("trash_id") or ""),
                expected_hash=str(params.get("expected_hash") or ""),
            )
            return result
        if method == "computer.actions.list":
            return {"items": project.db.list_pending_computer_action_requests()}
        if method == "computer.action.resolve":
            approved = bool(params.get("approved"))
            if approved and not Settings.from_env(project.root).powershell_enabled:
                raise InkFlowError("PowerShell 权限当前已关闭；命令没有执行。")
            return project.db.resolve_computer_action_request(
                str(params.get("request_id") or ""),
                str(params.get("command_hash") or ""),
                approved=approved,
            )
        if method == "document.version.read":
            return studio.db.get_version(str(params["version_id"]))
        if method == "document.annotate":
            return studio.create_annotation(
                str(params["relative_path"]),
                int(params["start_offset"]),
                int(params["end_offset"]),
                str(params["comment"]),
            )
        if method == "document.revise_selection":
            await emit({"type": "writer.started", "summary": "Writer 正在只修改选中的文字"})
            result = await self._engine(project.root).revise_selection(
                project.root,
                str(params["relative_path"]),
                int(params["start_offset"]),
                int(params["end_offset"]),
                str(params["comment"]),
                expected_hash=str(params.get("expected_hash") or "") or None,
            )
            await emit({"type": "writer.completed", "summary": "选区已生成新草稿版本，等待重新审查"})
            return result
        if method == "annotation.update":
            return studio.set_annotation_status(str(params["annotation_id"]), str(params["status"]))
        if method == "document.search":
            return studio.search(str(params["query"]), int(params.get("limit", 100)))
        if method == "chapter.workspace":
            return studio.chapter_workspace(int(params["chapter_no"]))
        if method == "chapter.writer_candidates":
            chapter_no = int(params["chapter_no"])
            chapter = studio.chapter_workspace(chapter_no)
            count = max(2, min(5, int(params.get("count", 3))))
            settings = Settings.from_env(project.root)
            packet = self._engine(project.root)._context_builder(project, "writer").build(
                chapter_no, "为本章提出互相有明显区别的写作方向；只提交方案，不写正文。", mode="draft"
            )
            result = await create_provider(settings).generate_json(
                system_prompt="你是墨流 Writer 的候选方案阶段。只提出方向，最终正文仍由固定主笔统一完成。",
                user_prompt=packet.to_model_prompt() + f"\n\n必须给出 {count} 个方向。",
                output_model=WriterDirectionSet,
                max_tokens=3_000, thinking=False, agent_role="writer",
            )
            run_id = str(params.get("run_id") or uuid.uuid4().hex)
            artifacts = []
            with project_write_lock_sync(project.root):
                for direction in result.data.directions[:count]:
                    artifacts.append(project.db.save_agent_artifact(
                        artifact_type="writer_direction", run_id=run_id, role="writer",
                        chapter_no=chapter_no, chapter_version=(chapter.get("chapter") or {}).get("version"),
                        data=direction.model_dump(), status="awaiting_selection",
                    ))
            return {"candidates": artifacts, "context_packet_id": content_hash(packet.to_model_prompt()), "model": result.model, "usage": result.usage}
        if method == "chapter.writer_candidate.select":
            with project_write_lock_sync(project.root):
                selected = project.db.select_agent_artifact(str(params["artifact_id"]))
                project.db.set_metadata(f"writer_selection:{selected['chapter_no']}", selected)
            return selected
        if method == "writer.profile.get":
            return project.db.get_metadata("writer_profile", {"locked": True, "provider": Settings.from_env(project.root).provider_kind, "model": Settings.from_env(project.root).model, "prompt_version": "built-in"})
        if method == "writer.profile.update":
            profile = {"locked": bool(params.get("locked", True)), "provider": str(params.get("provider") or Settings.from_env(project.root).provider_kind), "model": str(params.get("model") or Settings.from_env(project.root).model), "prompt_version": str(params.get("prompt_version") or "built-in")}
            with project_write_lock_sync(project.root):
                project.db.set_metadata("writer_profile", profile)
            return profile
        if method == "chapter.review_panel":
            chapter_no = int(params["chapter_no"])
            chapter = project.db.get_chapter(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ValueError("当前章节没有待审草稿")
            content = (project.root / chapter["path"]).read_text(encoding="utf-8")
            dimensions = list(params.get("dimensions") or ["continuity", "character", "narrative", "style"])
            allowed_dimensions = {"continuity", "character", "narrative", "style"}
            dimensions = [item for item in dimensions if item in allowed_dimensions][:4]
            packet = self._engine(project.root)._context_builder(project, "reviewer").build(
                chapter_no, "多维审查当前草稿", mode="review", protected_input=content
            )
            reports = []
            merged: dict[tuple[str, str, str], dict[str, Any]] = {}
            run_id = str(params.get("run_id") or uuid.uuid4().hex)
            for dimension in dimensions:
                result = await create_provider(Settings.from_env(project.root)).generate_json(
                    system_prompt="你是墨流的 Editor，当前处于审查模式。只审查指定维度，逐条引用当前正文或 Context Packet；不直接修改正文，不以投票代替证据。",
                    user_prompt=packet.to_model_prompt() + f"\n\n# 审查维度\n{dimension}\n\n# 当前正文\n{content}",
                    output_model=ReviewReport, max_tokens=5_000, thinking=False, agent_role="reviewer",
                )
                verified, verdict = verify_review(result.data, content, packet)
                payload = result.data.model_dump(mode="json")
                payload["verdict"] = verdict
                payload["findings"] = [item.model_dump(mode="json") for item in verified]
                with project_write_lock_sync(project.root):
                    current = project.db.get_chapter(chapter_no)
                    if not current or int(current["version"]) != int(chapter["version"]) or (project.root / current["path"]).read_text(encoding="utf-8") != content:
                        raise ValueError("多维预审期间正文版本已经变化，请重新运行")
                    artifact = project.db.save_agent_artifact(
                        artifact_type="review_dimension", run_id=run_id, role="reviewer", dimension=dimension,
                        chapter_no=chapter_no, chapter_version=int(chapter["version"]), data=payload, status="evidence_checked",
                    )
                reports.append(artifact)
                for finding in payload["findings"]:
                    key = (str(finding.get("rule_id")), str(finding.get("evidence")), str(finding.get("explanation")))
                    merged.setdefault(key, finding)
            severity_rank = {"blocking": 0, "major": 1, "minor": 2, "info": 3}
            findings = sorted(merged.values(), key=lambda item: severity_rank.get(str(item.get("severity")), 9))
            return {"reports": reports, "merged_findings": findings, "merge_method": "evidence_keyed_no_voting", "next_action": "由 Editor 对当前版本执行最终审查门禁"}
        if method == "scene_note.upsert":
            return studio.upsert_scene_note(
                int(params["chapter_no"]), int(params["scene_no"]), dict(params.get("data") or {})
            )
        if method == "bible.list":
            return {
                "manual": studio.db.list_bible_entries(),
                "canon_facts": project.db.current_facts(),
                "open_threads": project.db.open_threads(),
            }
        if method == "bible.upsert":
            return studio.upsert_bible_entry(
                entry_id=params.get("entry_id"),
                kind=str(params["kind"]),
                name=str(params["name"]),
                aliases=list(params.get("aliases") or []),
                data=dict(params.get("data") or {}),
            )
        if method == "memory.preview":
            if params.get("user_accepted") is not True:
                raise ValueError("只有用户明确接受当前正文后，记忆服务 才能准备正史变更预览")
            chapter_no = int(params["chapter_no"])
            chapter = project.db.get_chapter(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ValueError("当前章节没有可验收草稿")
            review = project.db.latest_review_record(chapter_no)
            if not review or review["chapter_version"] != int(chapter["version"]) or review["report"].verdict != "pass":
                raise ValueError("当前草稿版本尚未通过 Editor 审查，不能准备正史预览")
            content = (project.root / chapter["path"]).read_text(encoding="utf-8")
            trace = TraceRecorder(project.root, f"memory-preview-{chapter_no:05d}", Settings.from_env(project.root).trace_level)
            patch, _, _ = await self._engine(project.root)._extract_memory_patch(project, chapter_no, content, trace, source_status="accepted")
            artifact = project.db.save_agent_artifact(
                artifact_type="memory_patch_preview", run_id=trace.run_id, role="engine",
                chapter_no=chapter_no, chapter_version=int(chapter["version"]), data=patch.model_dump(mode="json"), status="awaiting_commit",
            )
            trace.finish(summary="正史变更预览已生成，尚未提交 SQLite")
            return {"preview": artifact, "facts": len(patch.facts), "threads": len(patch.threads), "notice": "正文已经由用户接受；当前只生成变更预览，尚未提交正史。"}
        if method == "memory.commit_preview":
            artifacts = project.db.list_agent_artifacts(chapter_no=int(params["chapter_no"]), artifact_type="memory_patch_preview", limit=20)
            artifact = next((item for item in artifacts if item["artifact_id"] == str(params["artifact_id"]) and item["status"] == "awaiting_commit"), None)
            if artifact is None:
                raise ValueError("正史预览不存在或已经失效")
            chapter = project.db.get_chapter(int(params["chapter_no"]))
            if not chapter or int(chapter["version"]) != int(artifact["chapter_version"]):
                raise ValueError("正文版本已经变化，请重新生成正史预览")
            result = await self._engine(project.root).accept_chapter(project.root, int(params["chapter_no"]), _prepared_patch=MemoryPatch.model_validate(artifact["data"]))
            project.db.set_agent_artifact_status(artifact["artifact_id"], "committed")
            return result
        if method == "preference.list":
            return {"preferences": project.db.list_preferences(active_only=bool(params.get("active_only", True)))}
        if method == "preference.upsert":
            with project_write_lock_sync(project.root):
                item = project.db.upsert_preference(
                    preference_id=params.get("preference_id"),
                    text=str(params["text"]),
                    strength=str(params.get("strength") or "weak"),
                    scope=str(params.get("scope") or "project"),
                    source="user",
                )
                project.db.record_learning_event("preference_changed", item)
                return item
        if method == "preference.set_status":
            with project_write_lock_sync(project.root):
                item = project.db.set_preference_status(
                    str(params["preference_id"]), str(params["status"])
                )
                project.db.record_learning_event("preference_changed", item)
                return item
        if method == "preference.delete":
            with project_write_lock_sync(project.root):
                item = project.db.delete_preference(str(params["preference_id"]))
                project.db.record_learning_event("preference_changed", item)
                return item
        if method == "collaboration.list":
            return {
                "threads": project.db.list_collaboration_threads(),
                "messages": project.db.list_collaboration_messages(
                    chapter_no=int(params["chapter_no"]) if params.get("chapter_no") is not None else None,
                    active_only=bool(params.get("active_only", False)),
                    limit=int(params.get("limit", 50)),
                )
            }
        if method == "collaboration.open":
            with project_write_lock_sync(project.root):
                thread = project.db.open_collaboration_thread(
                    run_id=str(params.get("run_id") or uuid.uuid4().hex),
                    topic=str(params.get("topic") or ""),
                    chapter_no=int(params["chapter_no"]) if params.get("chapter_no") is not None else None,
                    chapter_version=int(params["chapter_version"]) if params.get("chapter_version") is not None else None,
                    context_packet_id=str(params.get("context_packet_id") or ""),
                )
                message = project.db.append_collaboration_message(
                    thread_id=thread["thread_id"], run_id=thread["run_id"],
                    sender_role=str(params.get("sender_role") or "coordinator"),
                    recipient_role=str(params.get("recipient_role") or "writer"),
                    message_type=str(params.get("message_type") or "fact_query"),
                    claim=str(params.get("claim") or thread["topic"]),
                    requested_response=str(params.get("requested_response") or "请给出有证据的简短答复"),
                    evidence_refs=list(params.get("evidence_refs") or []),
                    chapter_no=thread["chapter_no"], chapter_version=thread["chapter_version"],
                    context_packet_id=str(thread.get("context_packet_id") or ""),
                )
            return {"thread": thread, "message": message}
        if method == "collaboration.reply":
            thread = project.db.get_collaboration_thread(str(params["thread_id"]))
            messages = project.db.list_collaboration_messages(limit=100)
            scoped = [item for item in messages if item["thread_id"] == thread["thread_id"]]
            pending = next((item for item in scoped if item["status"] == "pending"), scoped[-1] if scoped else None)
            if pending and int(pending.get("role_protocol_version") or 1) != 1:
                raise ValueError("该协作议题属于新版角色协议；当前旧版回复入口不能调用专项角色。")
            recipient = str(params.get("recipient_role") or (pending["recipient_role"] if pending else "writer"))
            role_boundaries = {
                "writer": "只回答规划、写作或修订问题，不审批，不提交正史。",
                "reviewer": "只进行证据化审查，不直接修改正文。",
                "coordinator": "只澄清目标、依赖和分工，不写正文、不审批、不提交正史。",
            }
            if recipient not in role_boundaries:
                raise ValueError("只能向协调者、写作者或编辑者请求模型回复；记忆保存是引擎服务。")
            allowed_evidence = {str(ref) for item in scoped for ref in item.get("evidence_refs", [])}
            discussion_content = json.dumps({"thread": thread, "messages": scoped, "question": params.get("question", "")}, ensure_ascii=False)
            discussion_packet = ContextPacket(
                project_id=project.root.name,
                chapter_no=int(thread.get("chapter_no") or 0),
                task=f"回答结构化协作议题：{thread['topic']}",
                sections=[ContextSection(key="A", title="议题、版本与证据", content=discussion_content, source_ids=sorted(allowed_evidence), hard=True)],
                estimated_tokens=estimate_tokens(discussion_content),
            )
            result = await create_provider(Settings.from_env(project.root)).generate_json(
                system_prompt=f"你是墨流的 {recipient}。{role_boundaries.get(recipient, '')} 最多两轮定向交流，不得自由群聊。",
                user_prompt=discussion_packet.to_model_prompt(),
                output_model=CollaborationReply,
                max_tokens=2_000,
                thinking=False,
                agent_role=recipient,
            )
            evidence_refs = [ref for ref in result.data.evidence_refs if ref in allowed_evidence]
            if result.data.evidence_refs and len(evidence_refs) != len(result.data.evidence_refs):
                raise ValueError("目标 Agent 引用了本议题 Context Packet 之外的证据，回复已拒绝")
            with project_write_lock_sync(project.root):
                response = project.db.append_collaboration_message(
                    thread_id=thread["thread_id"], run_id=thread["run_id"], sender_role=recipient,
                    recipient_role="coordinator", message_type="answer", claim=result.data.answer,
                    evidence_refs=evidence_refs, requested_response=result.data.remaining_question,
                    chapter_no=thread["chapter_no"], chapter_version=thread["chapter_version"],
                    context_packet_id=str(thread.get("context_packet_id") or ""), status="responded",
                    response_to=pending["message_id"] if pending else None,
                )
                state = project.db.close_collaboration_thread(thread["thread_id"], result.data.answer) if result.data.resolved else project.db.advance_collaboration_thread(thread["thread_id"], resolution=result.data.remaining_question)
                if state["status"] == "escalated":
                    project.db.append_collaboration_message(
                        thread_id=thread["thread_id"], run_id=thread["run_id"], sender_role="coordinator",
                        recipient_role="user", message_type="risk",
                        claim=result.data.remaining_question or f"关于“{thread['topic']}”仍有分歧，需要你确认。",
                        evidence_refs=evidence_refs, requested_response="请只回答这个最小分歧点；相关分支已暂停。",
                        chapter_no=thread["chapter_no"], chapter_version=thread["chapter_version"],
                        context_packet_id=str(thread.get("context_packet_id") or ""), status="escalated",
                    )
            return {"thread": state, "message": response, "model": result.model, "usage": result.usage}
        if method == "usage.overview":
            return _usage_overview(project, Settings.from_env(project.root))
        if method == "learning.settings.get":
            return project.db.get_metadata("learning_settings", {"enabled": True, "allow_training_exports": False})
        if method == "learning.settings.update":
            value = {"enabled": bool(params.get("enabled", True)), "allow_training_exports": bool(params.get("allow_training_exports", False))}
            with project_write_lock_sync(project.root):
                project.db.set_metadata("learning_settings", value)
            return value
        if method == "learning.strategy.choose":
            return project.db.choose_learning_strategy(list(params.get("candidates") or []))
        if method == "learning.strategy.feedback":
            with project_write_lock_sync(project.root):
                return project.db.update_learning_strategy(str(params["strategy_key"]), float(params["reward"]))
        if method == "learning.preference.compare":
            with project_write_lock_sync(project.root):
                return {"pair_id": project.db.save_preference_pair(str(params["chosen_artifact_id"]), str(params["rejected_artifact_id"]), dict(params.get("features") or {}))}
        if method == "learning.overview":
            return LearningService(project).overview()
        if method == "learning.dataset.export":
            with project_write_lock_sync(project.root):
                return LearningService(project).export_dataset(include_prose=bool(params.get("include_prose", False)))
        if method == "learning.preference.train":
            with project_write_lock_sync(project.root):
                return LearningService(project).train_preference_model()
        if method == "retrieval.feedback":
            with project_write_lock_sync(project.root):
                return {
                    "feedback_id": project.db.record_retrieval_feedback(
                        query=str(params["query"]),
                        source_id=str(params["source_id"]),
                        outcome=str(params["outcome"]),
                        role=str(params.get("role") or "coordinator"),
                        chapter_no=int(params["chapter_no"]) if params.get("chapter_no") is not None else None,
                        packet_id=str(params.get("packet_id") or "") or None,
                    )
                }
        if method == "conversation.send":
            requested_mode = _requested_task_mode(method, params)
            task_scope = active_task_settings.get()
            if requested_mode and (
                task_scope is None
                or (task_scope.role_protocol_version, task_scope.collaboration_mode) != requested_mode
            ):
                raise InkFlowError("专项模式必须先绑定本次任务快照，不能临时切换。")
            message = str(params.get("message") or "").strip()
            if not message:
                raise ValueError("消息不能为空。")
            await emit({"type": "controller.routing", "summary": "正在理解目标与执行边界"})
            session = TerminalSession(self._engine(project.root))
            await emit({"type": "workflow.started", "summary": "已交给墨流确定性工作流"})
            result = await session.handle(
                project.root,
                message,
                consume_steering=consume_steering,
                emit=emit,
                opened_project=project,
            )
            session_result = result.get("session") if isinstance(result, dict) else None
            if isinstance(session_result, dict) and session_result.get("task_ticket"):
                await emit(
                    {
                        "type": "workflow.planned",
                        "summary": str(session_result.get("visible_reason") or "Coordinator 已生成真实任务单"),
                        "action": session_result.get("route"),
                        "task_ticket": session_result.get("task_ticket"),
                        "dispatch_plan": session_result.get("dispatch_plan"),
                    }
                )
            await emit({"type": f"workflow.{workflow_result_status(result)}", "summary": _visible_result_summary(result)})
            return result
        if method == "workflow.run":
            return await self._run_workflow(project, params, emit)
        if method == "reference.import":
            with project_write_lock_sync(project.root):
                return ReferenceService(project).import_text(str(params["source_path"]))
        if method == "reference.list":
            return ReferenceService(project).list_references()
        if method == "reference.search":
            await emit({"type": "reference.searching", "summary": "正在搜索公开写作资料；结果不会自动导入。"})
            return await ReferenceService(project).search_public(
                str(params.get("query") or ""),
                limit=int(params.get("limit", 6)),
            )
        if method == "reference.fetch":
            url = str(params["url"])
            async with project_write_lock(project.root):
                if "fanqienovel.com" in url:
                    return await ReferenceService(project).fetch_fanqie_public(url)
                return await ReferenceService(project).fetch_url(url)
        if method == "reference.analyze":
            with project_write_lock_sync(project.root):
                return ReferenceService(project).analyze(str(params["reference_id"]))
        if method == "task.dismiss":
            with project_write_lock_sync(project.root):
                return studio.db.finish_task(str(params["task_id"]), status="dismissed")
        if method == "task.retry":
            task = studio.db.get_task(str(params["task_id"]))
            if not task["retryable"]:
                raise InkFlowError(
                    "该任务不能一键重试。验收和正式回退必须重新查看当前状态并再次确认。"
                )
            original_params = dict(task["params"])
            retry_guard = original_params.pop("_retry_guard", None)
            if task["method"] == "workflow.run" and task["action"] == "write":
                chapter_no = int(original_params.get("chapter_no") or 0)
                if chapter_no < 1 or not isinstance(retry_guard, dict):
                    raise InkFlowError("旧写作任务没有可核实的章节版本快照；已保留现有文件，请从当前章节状态续接。")
                if chapter_retry_state(project, chapter_no) != retry_guard:
                    raise InkFlowError(f"第 {chapter_no} 章在原任务失败后已有新版本，已阻止旧写作请求覆盖当前草稿；请从当前版本续接。")
                original_params["_retry_expected_state"] = retry_guard
            elif task["method"] == "workflow.run":
                raise InkFlowError("该流程可能已经保存部分规划或章节，请使用当前保存的断点续接，不重放整条工作流。")
            if not studio.db.claim_task_retry(str(params["task_id"])):
                raise InkFlowError("这条任务已被另一项重试领取，或不再处于可重试状态；没有重复启动第二次请求。")
            original_params["project_root"] = str(project.root)
            original_params["run_id"] = str(params.get("run_id") or uuid.uuid4().hex)
            await emit(
                {
                    "type": "task.retrying",
                    "summary": f"正在重新运行：{task['action'] or task['method']}",
                }
            )
            return {
                "retried_task_id": task["run_id"],
                "result": await self.dispatch(task["method"], original_params, emit),
            }
        raise ValueError(f"不支持的应用方法：{method}")

    async def _run_workflow(
        self,
        project: InkFlowProject,
        params: dict[str, Any],
        emit: EventSink,
    ) -> Any:
        action = str(params.get("action") or "")
        engine = self._engine(project.root)
        if action == "batch_resume":
            batch_id = str(params.get("batch_id") or "")
            runtime = active_runtime.get()
            if not batch_id or runtime is None or not runtime.run_id:
                raise InkFlowError("续接批次缺少批次编号或本次任务记录，未启动重复写作。")
            db = StudioService(project).db
            async with project_write_lock(project.root):
                manifest = engine._load_batch_manifest(project, batch_id)
                source_run = _batch_source_run(project, db, manifest)
                status = str(manifest.get("status") or "unknown")
                if status == "drafting":
                    if not source_run or source_run.get("status") not in {"interrupted", "cancelled", "failed"}:
                        raise InkFlowError("批次仍显示写作中，且没有确认原任务已结束；请刷新状态，不要重复启动。")
                    manifest["status"] = "interrupted"
                    manifest["stop_reason"] = manifest.get("stop_reason") or "原写作进程已结束；保留已保存章节并从批次断点继续。"
                    engine._save_batch_manifest(project, manifest)
                elif status not in {"failed", "interrupted"}:
                    raise InkFlowError("该批次当前不是可续接状态；已保留现有章节，请先查看批次状态。")
                if source_run and source_run.get("status") == "running":
                    raise InkFlowError("原批次仍有运行中的任务，未重复启动。请刷新任务状态后再决定。")
                if not db.claim_batch_resume(batch_id, runtime.run_id):
                    raise InkFlowError("另一项任务已领取这个批次，或本次任务记录尚未就绪；没有重复启动。")
            resumed_params = {
                **params,
                "action": "batch_draft",
                "batch_id": batch_id,
                "start_chapter_no": int(manifest["start_chapter_no"]),
                "end_chapter_no": int(manifest["end_chapter_no"]),
                "instruction": str(manifest.get("instruction") or ""),
                "max_revision_rounds": int(manifest.get("max_revision_rounds", 2)),
                "draft_only": True,
            }
            return await self._run_workflow(project, resumed_params, emit)
        if action in {"batch_draft", "batch_accept"}:
            async with engine.batch_operation_async(
                project.root, action,
                start_chapter_no=int(params["start_chapter_no"]) if params.get("start_chapter_no") is not None else None,
                end_chapter_no=int(params["end_chapter_no"]) if params.get("end_chapter_no") is not None else None,
                instruction=str(params.get("instruction") or ""),
                batch_id=str(params.get("batch_id") or "") or None,
            ) as (bound_engine, manifest):
                bound_params = {**params, "batch_id": manifest["batch_id"]}
                if action == "batch_draft":
                    bound_params.update(start_chapter_no=manifest["start_chapter_no"], end_chapter_no=manifest["end_chapter_no"])
                return await self._run_workflow_bound(project, bound_params, emit, bound_engine)
        return await self._run_workflow_bound(project, params, emit, engine)

    async def _run_workflow_bound(
        self, project: InkFlowProject, params: dict[str, Any], emit: EventSink, engine: InkFlowEngine,
    ) -> Any:
        action = str(params.get("action") or "")
        requested_mode = _requested_task_mode("workflow.run", params)
        task_scope = active_task_settings.get()
        if requested_mode and (
            task_scope is None
            or (task_scope.role_protocol_version, task_scope.collaboration_mode) != requested_mode
        ):
            raise InkFlowError("专项模式必须先绑定本次任务快照，不能临时切换。")
        settings = engine.settings
        workflow_actions = {
            "plan": "plan",
            "outline": "outline",
            "write": "write_draft",
            "review": "review_accept" if settings.acceptance_confirmation_mode == "auto_after_review" else "review",
            "revise": "revise_draft",
            "accept": "accept",
            "batch_draft": (
                "batch_draft_accept"
                if settings.acceptance_confirmation_mode in {"batch_once", "auto_after_review"}
                and not bool(params.get("draft_only", False))
                else "batch_draft"
            ),
            "batch_accept": "batch_accept",
            "arc_audit": "arc_audit",
            "checkpoint_create": "checkpoint_create",
            "checkpoint_list": "checkpoint_list",
            "rollback_preview": "rollback_preview",
            "rollback_restore": "rollback_restore",
        }
        workflow_action = workflow_actions.get(action)
        ticket = None
        dispatch_plan = None
        if workflow_action:
            task_scope = active_task_settings.get()
            if task_scope and task_scope.role_protocol_version == 2 and workflow_action == "arc_audit":
                raise InkFlowError("篇章复审尚未接入专项模式；请另起日常模式任务执行。")
            authorization_source = str(params.get("authorization_source") or "current_request")
            if authorization_source not in {
                "none", "current_request", "per_chapter_click", "batch_preapproval", "settings_auto_accept"
            }:
                authorization_source = "current_request"
            if action == "review" and workflow_action == "review_accept":
                authorization_source = "settings_auto_accept"
            if action == "batch_draft" and workflow_action == "batch_draft_accept":
                authorization_source = (
                    "settings_auto_accept"
                    if settings.acceptance_confirmation_mode == "auto_after_review"
                    else "batch_preapproval"
                )
            intent = TerminalIntent(
                action=workflow_action,
                requested_outcome=str(params.get("instruction") or f"执行{action}工作流"),
                authorization="approved",
                authorization_source=authorization_source,
                acceptance_confirmation_mode=settings.acceptance_confirmation_mode,
                chapter_no=(
                    int(params["chapter_no"])
                    if params.get("chapter_no") is not None
                    else int(params["start_chapter_no"])
                    if params.get("start_chapter_no") is not None
                    else None
                ),
                end_chapter_no=(
                    int(params["end_chapter_no"])
                    if params.get("end_chapter_no") is not None
                    else None
                ),
                batch_id=str(params.get("batch_id") or "") or None,
                max_revision_rounds=int(params.get("max_revision_rounds", 1)),
                operation_instruction=str(params.get("instruction") or ""),
                visible_reason=f"按用户当前操作执行{action}工作流。",
            )
            task_scope = active_task_settings.get()
            ticket, dispatch_plan = Coordinator(project).compile(
                intent,
                role_protocol_version=task_scope.role_protocol_version if task_scope else 1,
                collaboration_mode=task_scope.collaboration_mode if task_scope else "everyday",
                task_snapshot_hash=task_scope.snapshot_hash if task_scope else None,
            )
            Coordinator.validate(dispatch_plan, ticket)
            if dispatch_plan.workflow != workflow_action:
                raise InkFlowError("任务计划与桌面工作流不一致，已停止执行。")
            # Ticket figures estimate work for routing and reporting; they are
            # not implicit per-request stop limits. RunRuntime keeps its own
            # explicit guard against runaway calls.
        await emit(
            {
                "type": "workflow.started",
                "action": action,
                "summary": "工作流已开始",
                **({"task_ticket": ticket.model_dump(mode="json")} if ticket else {}),
                **({"dispatch_plan": dispatch_plan.model_dump(mode="json")} if dispatch_plan else {}),
            }
        )
        if action == "plan":
            await emit(
                {
                    "type": "writer.started",
                    "stage": "plan.model",
                    "role": "writer",
                    "model": settings.model,
                    "summary": "Writer 正在生成四级规划；完成后才会写入 PLAN.md",
                }
            )
            result = await engine.generate_plan(
                project.root,
                instruction=str(params.get("instruction") or ""),
                chapter_range=(
                    int(params.get("start_chapter_no") or params.get("chapter_no") or 1),
                    int(params["end_chapter_no"]),
                )
                if params.get("end_chapter_no") is not None
                else None,
            )
        elif action == "outline":
            await emit(
                {
                    "type": "writer.started",
                    "stage": "outline.model",
                    "role": "writer",
                    "model": settings.model,
                    "summary": "Writer 正在生成独立大纲；不会修改正式规划或正史",
                }
            )
            if params.get("outline_level") == "detail":
                result = await engine.generate_story_detail(project.root, instruction=str(params.get("instruction") or ""))
            else:
                result = await engine.generate_outline(
                    project.root,
                    int(params.get("start_chapter_no") or params.get("chapter_no") or 1),
                    int(params.get("end_chapter_no") or project.db.get_brief().estimated_chapters),
                    instruction=str(params.get("instruction") or ""),
                )
        elif action == "write":
            await emit(
                {
                    "type": "writer.started",
                    "stage": "writer.model",
                    "role": "writer",
                    "model": settings.model,
                    "summary": "Writer 正在生成当前章节草稿；结果会先进入临时版本",
                }
            )
            instruction = str(params.get("instruction") or "")
            selected = project.db.get_metadata(f"writer_selection:{int(params['chapter_no'])}", None)
            if isinstance(selected, dict) and selected.get("data"):
                instruction = instruction + "\n\n已选定候选方向：" + json.dumps(selected["data"], ensure_ascii=False)
            result = await engine.write_chapter(
                project.root,
                int(params["chapter_no"]),
                instruction,
                retry_expected_state=(
                    params.get("_retry_expected_state")
                    if isinstance(params.get("_retry_expected_state"), dict)
                    else None
                ),
            )
        elif action == "review":
            task_scope = active_task_settings.get()
            specialist = task_scope is not None and task_scope.role_protocol_version == 2
            await emit(
                {
                    "type": "workflow.stage" if specialist else "reviewer.started",
                    "stage": "review.mode" if specialist else "review.model",
                    "role": "engine" if specialist else "reviewer",
                    "model": settings.model,
                    "summary": (
                        "引擎正在按本次任务模式执行当前版本的检查覆盖；检查完成前不会验收"
                        if specialist else "Editor 正在审查当前草稿版本；不会直接修改正文"
                    ),
                }
            )
            if specialist:
                result = await engine.review_chapter_mode(
                    project.root, int(params["chapter_no"]),
                    mode=task_scope.collaboration_mode,
                    instruction=str(params.get("instruction") or ""),
                )
            else:
                result = await engine.review_chapter(project.root, int(params["chapter_no"]))
            if settings.acceptance_confirmation_mode == "auto_after_review" and result.get("verdict") == "pass":
                acceptance = await engine.accept_chapter(project.root, int(params["chapter_no"]), force=False)
                result = {
                    **result,
                    "automatic_acceptance": acceptance,
                    "authorization_source": "settings_auto_accept",
                    "next_action": "本章已按设置自动验收并提交正史",
                }
        elif action == "revise":
            await emit(
                {
                    "type": "writer.started",
                    "stage": "writer.revise",
                    "role": "writer",
                    "model": settings.model,
                    "summary": "Writer 正在读取 Editor 的审查证据并生成新版本；旧草稿仍会保留在版本记录中",
                }
            )
            result = await engine.revise_chapter(
                project.root, int(params["chapter_no"]), str(params.get("instruction") or "")
            )
        elif action == "accept":
            result = await engine.accept_chapter(project.root, int(params["chapter_no"]), force=False)
        elif action == "batch_draft":
            await emit(
                {
                    "type": "workflow.stage",
                    "stage": "batch-draft",
                    "role": "writer",
                    "model": settings.model,
                    "summary": "批次开始：逐章由 Writer 写作、Editor 审查，再同步临时记忆；不会提交正史",
                }
            )
            result = await engine.draft_batch(
                project.root,
                int(params["start_chapter_no"]),
                int(params["end_chapter_no"]),
                instruction=str(params.get("instruction") or ""),
                max_revision_rounds=int(params.get("max_revision_rounds", 2)),
                batch_id=str(params.get("batch_id") or "") or None,
            )
            if workflow_action == "batch_draft_accept" and result.get("status") == "ready_for_acceptance":
                drafted = result
                result = {
                    **(await engine.accept_batch(project.root, str(drafted["batch_id"]))),
                    "batch_draft": drafted,
                    "authorization_source": authorization_source,
                }
        elif action == "batch_accept":
            result = await engine.accept_batch(project.root, str(params["batch_id"]))
        elif action == "arc_audit":
            result = await engine.audit_range(
                project.root,
                int(params["start_chapter_no"]),
                int(params["end_chapter_no"]),
                batch_id=params.get("batch_id"),
            )
        elif action == "checkpoint_create":
            result = engine.checkpoint_create(project.root, str(params.get("label") or "桌面手动检查点"))
        elif action == "checkpoint_list":
            result = engine.checkpoint_list(project.root, int(params.get("limit", 50)))
        elif action == "rollback_preview":
            result = engine.rollback_preview(
                project.root,
                checkpoint_id=params.get("checkpoint_id"),
                boundary_chapter=params.get("boundary_chapter"),
            )
        elif action == "rollback_restore":
            result = engine.rollback_restore(
                project.root,
                checkpoint_id=params.get("checkpoint_id"),
                boundary_chapter=params.get("boundary_chapter"),
                confirmation_token=str(params["confirmation_token"]),
            )
        elif action == "rollback_recover":
            result = engine.rollback_recover(project.root)
        else:
            raise ValueError(f"不支持的工作流动作：{action}")
        if isinstance(result, dict) and not workflow_failure_reason(result):
            recommendation = _workflow_next_step(action, result)
            if recommendation:
                result = {**result, "next_step": recommendation}
        await emit({"type": f"workflow.{workflow_result_status(result)}", "action": action, "summary": _visible_result_summary(result)})
        return result

    async def _generate_voice_clone_script(
        self,
        params: dict[str, Any],
        emit: EventSink,
    ) -> dict[str, Any]:
        """Let Writer create a read-aloud aid without touching novel production data."""

        project = self._project(params)
        settings = Settings.from_env(project.root)
        intent = TerminalIntent(
            action="voice_clone_script",
            requested_outcome="生成本地声音克隆参考朗读稿",
            authorization="approved",
            authorization_source="current_request",
            acceptance_confirmation_mode=settings.acceptance_confirmation_mode,
            visible_reason="按用户当前点击生成仅用于本地声音克隆的朗读稿。",
        )
        ticket, dispatch_plan = Coordinator(project).compile(intent)
        await emit(
            {
                "type": "voice.clone_script.started",
                "summary": "Writer 正在生成声音克隆参考朗读稿",
                "task_ticket": ticket.model_dump(mode="json"),
                "dispatch_plan": dispatch_plan.model_dump(mode="json"),
            }
        )
        payload = await self._engine(project.root).generate_voice_clone_script(project.root)
        await emit({"type": "voice.clone_script.completed", "summary": "声音克隆参考朗读稿已生成"})
        return {
            **payload,
            "task_ticket": ticket.model_dump(mode="json"),
            "dispatch_plan": dispatch_plan.model_dump(mode="json"),
        }

    async def _ideate_direction(
        self,
        settings: Settings,
        preferences: str,
        *,
        angle: tuple[str, str] | None,
        fast: bool,
        avoid: list[NovelIdeaCandidate] | None = None,
    ) -> Any:
        """生成一个开书方向；angle 指定本次必须采用的差异化叙事引擎。"""

        system_prompt = (
            "你是墨流的 Writer，当前只负责建项前构思，不写正文。面向中文网文读者。"
            "本次只提交 1 个方案。每个方案都必须有清晰主角欲望、持续矛盾、前三章抓手与可升级的长期叙事引擎。"
            "不要依赖用户已有小说。书名简洁可辨识；premise 至少写清人物、触发事件、目标与主要阻力。"
            "用户偏好中已经明确说出的受众、主角性别、题材、时代、基调、禁区和开篇方式均是硬约束，"
            "不得为了追求新奇而换掉。出现‘女频’且用户没有另行指定时，默认使用女性主角和女频叙事重点；"
            "出现‘从……开始’时，opening_hook 必须从该事件或场面起笔。把这些明确约束逐条写入 user_rules。"
            "绝不能把 Writer 自己选择的时代、题材或情节说成用户偏好；原文没有的内容只能称为创意提案。"
            "user_rules 只能复述用户明确说过的内容，不能把本次方案细节升级为用户硬约束。"
            "public_reasoning_summary 用 2～4 条简短中文公开说明你核对了哪些偏好、为何选择这个方向、"
            "还有什么可调整；这是给用户看的判断摘要，不是隐藏思维链。"
        )
        if angle is not None:
            system_prompt += (
                f"这一次只提交 1 个方案，并且它的叙事引擎必须是“{angle[0]}”：{angle[1]}"
                "在满足用户全部硬约束的前提下，围绕这个引擎设计主角动机、主要阻力和前三章抓手。"
            )
        if avoid:
            system_prompt += (
                "下面这些方向已经确定，本次必须与它们在题材细分、主角动机、矛盾来源和开篇场面上都明显不同："
                + json.dumps(
                    [
                        {
                            "title": item.title,
                            "premise": item.premise,
                            "opening_hook": item.opening_hook,
                        }
                        for item in avoid
                    ],
                    ensure_ascii=False,
                )
            )
        return await create_provider(settings).generate_json(
            system_prompt=system_prompt,
            user_prompt=(
                "用户可以完全没有想法。请生成 1 个可直接建立项目的开书方案。"
                f"\n用户可选偏好：{preferences or '无，请主动做多样化选择。'}"
                "\n用户明确给出总字数、章节数或卷数时必须优先服从，并据此换算其余规模字段。"
                "只有用户没有给出这些规模要求时，才使用单章 3000 字、约 200 章、6 卷的默认值。"
            ),
            output_model=NovelIdeaBundle,
            # 建项构思不是正文推演。关闭推理可以避免部分模型先耗尽
            # reasoning token、却来不及返回小型 JSON 的兼容性故障。
            effort="low",
            max_tokens=1200 if fast else 1600,
            thinking=False,
            timeout_seconds=min(settings.request_timeout_seconds, 90.0)
            if fast
            else min(settings.planning_timeout_seconds, 180.0),
            agent_role="writer",
        )

    async def _separate_repeated_directions(
        self,
        settings: Settings,
        preferences: str,
        candidates: list[NovelIdeaCandidate],
    ) -> list[NovelIdeaCandidate]:
        """检出实质重复的方向，并对重复项各补一次定向重写。"""

        result = list(candidates)
        for index in range(len(result)):
            duplicate_of = next(
                (
                    other
                    for other in range(index)
                    if _direction_similarity(result[index], result[other])
                    >= _IDEA_DUPLICATE_RATIO
                ),
                None,
            )
            if duplicate_of is None:
                continue
            try:
                replacement = await self._ideate_direction(
                    settings,
                    preferences,
                    angle=_IDEA_ANGLES[index % len(_IDEA_ANGLES)],
                    fast=False,
                    avoid=list(result[:index]),
                )
            except ProviderError:
                # 重写失败时保留原方向：三个方案仍可比较，只是可能偏近。
                continue
            result[index] = replacement.data.candidates[0]
        return result

    @staticmethod
    def _validated_project_root(params: dict[str, Any]) -> Path:
        value = params.get("project_root")
        if not value:
            raise ValueError("请求缺少 project_root。")
        root = Path(str(value)).resolve()
        config_path = root / ".inkflow" / "project.json"
        if not config_path.is_file():
            raise ValueError(f"这里不是墨流小说项目：{root}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not config.get("project_id"):
            raise ValueError(f"墨流小说项目配置无效：{root}")
        return root

    @staticmethod
    def _project(params: dict[str, Any]) -> InkFlowProject:
        value = params.get("project_root")
        if not value:
            raise ValueError("请求缺少 project_root。")
        return InkFlowProject(Path(str(value)).resolve())

    @staticmethod
    def _try_project(root: str | Path | None) -> InkFlowProject | None:
        """尽力解析项目；路径无效或尚未开书时返回 None，不抛错。"""

        if not root:
            return None
        try:
            project = InkFlowProject(Path(str(root)).resolve())
            if not project.project_id:
                return None
            return project
        except Exception:
            return None

    @staticmethod
    def _engine(root: Path) -> InkFlowEngine:
        scope = active_task_settings.get()
        if scope is not None:
            if scope.settings.workspace_root is None or scope.settings.workspace_root.resolve() != root.resolve():
                raise InkFlowError("任务配置快照不属于当前项目，未跨作品执行。")
            settings = scope.settings
        else:
            settings = Settings.from_env(root)
        return InkFlowEngine(create_provider(settings), settings)


def _usage_overview(project: InkFlowProject, settings: Settings) -> dict[str, Any]:
    def empty_bucket() -> dict[str, int]:
        return {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "cache_reported_calls": 0,
            "cache_unknown_calls": 0,
            "cache_unknown_prompt_tokens": 0,
        }

    overall = empty_bucket()
    by_agent_role: dict[str, dict[str, int]] = {}
    by_stage: dict[str, dict[str, int]] = {}
    for event_path in (project.internal / "runs").glob("*/events.jsonl"):
        try:
            lines = event_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
                metadata = dict(event.get("metadata") or {})
                usage = dict(metadata.get("usage") or {})
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not usage:
                continue
            raw_role = str(metadata.get("agent_role") or "unknown")
            try:
                role = normalize_role(raw_role, event.get("role_protocol_version", 1))
            except ValueError:
                role = "unknown"
            bucket = by_agent_role.setdefault(role, empty_bucket())
            # Keep repair/retry stages separate; an agent-wide average hides their cost.
            stage_key = f"{metadata.get('model') or 'unknown'}:{role}:{event.get('stage') or 'unknown'}"
            stage_bucket = by_stage.setdefault(stage_key, empty_bucket())
            prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            prompt_details = usage.get("prompt_tokens_details") or {}
            hit_reported = "prompt_cache_hit_tokens" in usage or (
                isinstance(prompt_details, dict) and "cached_tokens" in prompt_details
            )
            miss_reported = "prompt_cache_miss_tokens" in usage
            cache_hit = int(
                usage.get("prompt_cache_hit_tokens", prompt_details.get("cached_tokens", 0)) or 0
            ) if isinstance(prompt_details, dict) else int(usage.get("prompt_cache_hit_tokens", 0) or 0)
            cache_miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0) if miss_reported else max(0, prompt - cache_hit)
            for target in (overall, bucket, stage_bucket):
                target["calls"] += 1
                target["prompt_tokens"] += prompt
                target["completion_tokens"] += completion
                if hit_reported or miss_reported:
                    target["cache_reported_calls"] += 1
                    target["prompt_cache_hit_tokens"] += cache_hit
                    target["prompt_cache_miss_tokens"] += cache_miss
                else:
                    target["cache_unknown_calls"] += 1
                    target["cache_unknown_prompt_tokens"] += prompt

    def public_bucket(bucket: dict[str, int]) -> dict[str, Any]:
        cache_total = bucket["prompt_cache_hit_tokens"] + bucket["prompt_cache_miss_tokens"]
        return {
            **bucket,
            "total_tokens": bucket["prompt_tokens"] + bucket["completion_tokens"],
            "prompt_cache_hit_rate": round(bucket["prompt_cache_hit_tokens"] / cache_total, 4)
            if cache_total else None,
            "prompt_cache_miss_rate": round(bucket["prompt_cache_miss_tokens"] / cache_total, 4)
            if cache_total else None,
            "average_prompt_tokens": round(bucket["prompt_tokens"] / bucket["calls"])
            if bucket["calls"] else 0,
            "average_completion_tokens": round(bucket["completion_tokens"] / bucket["calls"])
            if bucket["calls"] else 0,
        }

    usage = public_bucket(overall)
    estimated_cost = (
        overall["prompt_tokens"] * settings.input_price_per_million
        + overall["completion_tokens"] * settings.output_price_per_million
    ) / 1_000_000
    try:
        ledger = UsageLedger()
        accounting = {"available": True, "project": ledger.summary(getattr(project, "project_id", "")),
                      "all_projects": ledger.summary()}
        recent_deepseek_cache = ledger.recent_deepseek_cache(getattr(project, "project_id", ""))
    except (OSError, sqlite3.Error):
        accounting = {"available": False, "warning": "本地用量账本暂不可读，金额未知；不影响查看或编辑正文。"}
        recent_deepseek_cache = None
    return {
        **usage,
        "by_agent_role": {role: public_bucket(bucket) for role, bucket in by_agent_role.items()},
        "by_stage": {stage: public_bucket(bucket) for stage, bucket in by_stage.items()},
        "recent_deepseek_cache": recent_deepseek_cache,
        "estimated_cost": round(estimated_cost, 6),
        "currency": "CNY",
        "pricing_configured": settings.input_price_per_million > 0 or settings.output_price_per_million > 0,
        "accounting": accounting,
    }


class JsonLineServer:
    def __init__(self) -> None:
        self.instance_id = f"server-{os.getpid()}-{uuid.uuid4().hex}"
        self.service = InkFlowAppService(self.instance_id)
        self.write_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.task_methods: dict[str, str] = {}
        self.steering_messages: dict[str, list[dict[str, Any]]] = {}
        self.steering_delivered: dict[str, list[dict[str, Any]]] = {}
        self.steering_applied: dict[str, list[dict[str, Any]]] = {}
        self.steering_route_seen: set[str] = set()
        self.steering_sequence: dict[str, int] = {}
        self.run_project_roots: dict[str, Path] = {}
        self.cancel_requested: set[str] = set()
        self.cancel_context: dict[str, dict[str, str]] = {}
        self.dispatch_finished_runs: set[str] = set()

    async def serve(self) -> None:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                request = json.loads(line)
                request_id = str(request.get("id") or uuid.uuid4().hex)
                method = str(request["method"])
                params = dict(request.get("params") or {})
            except Exception as exc:
                await self.write({"jsonrpc": "2.0", "id": None, "error": _error_payload(exc)})
                continue
            if method == "run.cancel":
                run_id = str(params.get("run_id") or "")
                result = self.cancel_run(
                    run_id,
                    source=str(params.get("source") or "unknown"),
                    reason=str(params.get("reason") or "用户主动停止"),
                )
                await self.write({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"run_id": run_id, **result},
                })
                continue
            if method == "run.steer":
                run_id = str(params.get("run_id") or "")
                message = str(params.get("message") or "").strip()
                task = self.tasks.get(run_id)
                accepted = bool(
                    message
                    and task
                    and not task.done()
                    and self.task_methods.get(run_id) == "conversation.send"
                    and run_id not in self.dispatch_finished_runs
                    and run_id in self.run_project_roots
                )
                if accepted:
                    sequence = self.steering_sequence.get(run_id, 0) + 1
                    self.steering_sequence[run_id] = sequence
                    try:
                        root = self.run_project_roots[run_id]
                        artifact = await asyncio.to_thread(
                            lambda: InkFlowProject(root, recover_on_open=False).db.save_agent_artifact(
                                artifact_type="user_steering", run_id=run_id, role="user",
                                status="pending", data={
                                    "message": message, "sequence": sequence,
                                    # Receipt is not permission to alter the
                                    # active task. Its initial ticket is v1;
                                    # only a later routed target change may
                                    # advance the applied task revision.
                                    "task_revision": 1, "proposed_task_revision": sequence + 1,
                                    "target_run_id": run_id,
                                    "related_task_id": str(params.get("related_task_id") or run_id),
                                    "pending_question_id": str(params.get("pending_question_id") or ""),
                                    "response_kind": str(params.get("response_kind") or "new_input"),
                                    "received_at": datetime.now(timezone.utc).isoformat(),
                                },
                            )
                        )
                    except Exception:
                        accepted = False
                    else:
                        self.steering_messages.setdefault(run_id, []).append({
                            "message": message, "artifact_id": artifact["artifact_id"],
                            "sequence": sequence,
                        })
                        await self.write({
                            "jsonrpc": "2.0", "method": "event", "params": {
                                "run_id": run_id, "type": "run.steered",
                                "summary": "已保存你的补充；将在安全节点处理，若本次来不及应用会保留为待处理输入。",
                                "artifact_id": artifact["artifact_id"], "proposed_task_revision": sequence + 1,
                            },
                        })
                reason = "" if accepted else (
                    "empty_message" if not message else
                    "run_finished" if not task or task.done() or run_id in self.dispatch_finished_runs else
                    "unsupported_run" if self.task_methods.get(run_id) != "conversation.send" else
                    "project_unavailable" if run_id not in self.run_project_roots else
                    "record_failed"
                )
                await self.write({"jsonrpc": "2.0", "id": request_id, "result": {
                    "accepted": accepted, "reason": reason,
                    **({"artifact_id": artifact["artifact_id"], "proposed_task_revision": sequence + 1} if accepted else {}),
                }})
                continue
            run_id = str(params.get("run_id") or f"run-{uuid.uuid4().hex}")
            params["run_id"] = run_id
            current_task = self.tasks.get(run_id)
            if current_task is not None:
                await self.write({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": "duplicate_run_id",
                        "title": "运行编号已经在使用",
                        "message": "相同运行编号已存在于当前引擎；墨流没有再次启动模型或文件操作。",
                        "impact": "原任务状态保持不变，本次重复请求未执行。",
                        "actions": [{"id": "open_process", "label": "核对原任务状态"}],
                        "retryable": False,
                    },
                })
                continue
            task = asyncio.create_task(self.process(request_id, run_id, method, params))
            self.tasks[run_id] = task
            self.task_methods[run_id] = method
            if params.get("project_root"):
                self.run_project_roots[run_id] = Path(str(params["project_root"])).resolve()
            def cleanup(_task: asyncio.Task[None], key: str = run_id) -> None:
                if self.tasks.get(key) is not _task:
                    return
                self.tasks.pop(key, None)
                self.task_methods.pop(key, None)
                self.steering_messages.pop(key, None)
                self.steering_delivered.pop(key, None)
                self.steering_applied.pop(key, None)
                self.steering_route_seen.discard(key)
                self.steering_sequence.pop(key, None)
                self.run_project_roots.pop(key, None)
                self.cancel_requested.discard(key)
                self.cancel_context.pop(key, None)
            task.add_done_callback(cleanup)
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def cancel_run(self, run_id: str, *, source: str, reason: str) -> dict[str, Any]:
        task = self.tasks.get(run_id)
        if not task or task.done():
            return {"cancelled": False}
        if run_id in self.dispatch_finished_runs:
            return {
                "cancelled": False,
                "finalizing": True,
                "summary": "写作步骤已经返回并进入保存结果阶段；停止请求未撤销已完成的成果。",
            }
        self.cancel_requested.add(run_id)
        self.cancel_context[run_id] = {
            "source": source,
            "reason": reason,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        task.cancel()
        return {"cancelled": True, **self.cancel_context[run_id]}

    async def _mark_steering(self, run_id: str, records: list[dict[str, Any]], status: str) -> bool:
        root = self.run_project_roots.get(run_id)
        if root is None or not records:
            return not records
        try:
            def update() -> None:
                db = InkFlowProject(root, recover_on_open=False).db
                for record in records:
                    db.set_agent_artifact_status(str(record["artifact_id"]), status)
            await asyncio.to_thread(update)
            return True
        except Exception:
            # The original user wording remains saved as pending; never drop
            # it because a secondary status update failed.
            return False

    async def _defer_unapplied_steering(self, run_id: str) -> list[dict[str, Any]]:
        queued = self.steering_messages.pop(run_id, [])
        delivered = self.steering_delivered.pop(run_id, [])
        records = sorted([*queued, *delivered], key=lambda item: int(item["sequence"]))
        if records:
            await self._mark_steering(run_id, records, "deferred")
        return records

    async def _bind_applied_steering_revision(self, run_id: str, ticket: dict[str, Any]) -> None:
        records = self.steering_delivered.get(run_id, [])
        root = self.run_project_roots.get(run_id)
        revision = int(ticket.get("task_revision") or 1)
        if not records or root is None:
            return

        def bind() -> None:
            project = InkFlowProject(root, recover_on_open=False)
            with project.db.connect() as connection:
                for record in records:
                    row = connection.execute(
                        "SELECT data_json FROM agent_artifacts WHERE artifact_id=? AND run_id=? "
                        "AND artifact_type='user_steering' AND status='pending'",
                        (record["artifact_id"], run_id),
                    ).fetchone()
                    if row is None:
                        continue
                    data = json.loads(row["data_json"])
                    data["applied_task_revision"] = revision
                    data["related_task_id"] = str(ticket.get("related_task_id") or data.get("related_task_id") or run_id)
                    data["pending_question_id"] = str(ticket.get("pending_question_id") or data.get("pending_question_id") or "")
                    data["response_kind"] = str(ticket.get("response_kind") or data.get("response_kind") or "task_revision")
                    connection.execute(
                        "UPDATE agent_artifacts SET data_json=?, status='applied' WHERE artifact_id=?",
                        (json.dumps(data, ensure_ascii=False, separators=(",", ":")), record["artifact_id"]),
                    )
                connection.commit()

        try:
            await asyncio.to_thread(bind)
            self.steering_applied.setdefault(run_id, []).extend(records)
            self.steering_delivered.pop(run_id, None)
        except Exception:
            # The original user text remains pending and will be deferred.
            pass

    async def process(self, request_id: str, run_id: str, method: str, params: dict[str, Any]) -> None:
        last_public_event: dict[str, Any] = {}

        async def emit(event: dict[str, Any]) -> None:
            if event.get("type") == "session.steer":
                self.steering_route_seen.add(run_id)
            if (event.get("type") == "workflow.planned" and run_id in self.steering_route_seen
                    and isinstance(event.get("task_ticket"), dict)):
                await self._bind_applied_steering_revision(run_id, event["task_ticket"])
            last_public_event.clear()
            last_public_event.update(event)
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"run_id": run_id, **event},
                }
            )
            event_type = str(event.get("type") or "")
            summary = str(event.get("summary") or "")
            if (
                task_started and task_db is not None and summary
                and not event_type.startswith(("run.", "task.settings_"))
                and not event_type.endswith((".delta", ".token"))
            ):
                try:
                    await asyncio.to_thread(task_db.record_task_progress, run_id, event_type, summary)
                except Exception:
                    # Progress text is secondary metadata, never a reason to
                    # fail an otherwise healthy writing request.
                    pass

        async def report_lock_wait(attempt: int, delay: float) -> None:
            public_events.put_nowait({
                "type": "recovery.waiting",
                "stage": "project.write.lock",
                "summary": "另一项本地操作暂时占用项目；当前步骤尚未开始修改，墨流会自动等待后继续。",
                "metadata": {"attempt": attempt, "retry_after_seconds": delay},
            })

        async def consume_steering() -> list[str]:
            records = sorted(self.steering_messages.pop(run_id, []), key=lambda item: int(item["sequence"]))
            if records:
                self.steering_route_seen.discard(run_id)
                self.steering_delivered.setdefault(run_id, []).extend(records)
            return [str(item["message"]) for item in records]

        public_events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        owner_task = asyncio.current_task()
        delivery_error: Exception | None = None
        dispatch_finished = False
        async def forward_events() -> None:
            nonlocal delivery_error
            while True:
                event = await public_events.get()
                try:
                    if event is None:
                        return
                    if delivery_error is None:
                        try:
                            await emit(event)
                        except Exception as exc:
                            # Keep draining the queue: otherwise join() waits
                            # forever after the first broken output event.
                            delivery_error = exc
                            if not dispatch_finished and owner_task is not None and not owner_task.cancelling():
                                owner_task.cancel()
                finally:
                    public_events.task_done()

        event_worker = asyncio.create_task(forward_events())
        runtime = RunRuntime(publish=public_events.put_nowait, task_id=run_id, run_id=run_id)
        runtime_token = active_runtime.set(runtime)
        task_db = None
        task_started = False
        settings_token = None

        def persist_outcome(
            status: str, *, summary: str = "", error_message: str = "",
            error_code: str | None = None, retry_allowed: bool | None = None,
        ) -> str:
            if task_db is None or not task_started:
                return ""
            try:
                task_db.finish_task(
                    run_id, status=status, summary=summary, error_message=error_message,
                    error_code=error_code, retry_allowed=retry_allowed,
                )
                return ""
            except Exception as record_error:
                return (
                    "任务结果已返回，但本地任务记录暂时无法核实"
                    f"（{type(record_error).__name__}）：{str(record_error)[:160]}。"
                    "请先查看当前文件与任务状态，不要重发整项请求。"
                )

        try:
            if _should_track_task(method, params):
                project_root = params.get("project_root")
                if project_root:
                    # Task bookkeeping must not synchronously wait for another
                    # chapter's project lock on the JSON-RPC event loop. The
                    # actual dispatcher performs recovery before mutation.
                    project = await asyncio.to_thread(
                        InkFlowProject, Path(str(project_root)).resolve(), recover_on_open=False
                    )
                    task_db = StudioService(project).db
                    recorded_method = method
                    recorded_params = _task_params(method, params)
                    if method == "workflow.run" and str(params.get("action") or "") == "write":
                        try:
                            chapter_no = int(params.get("chapter_no") or 0)
                        except (TypeError, ValueError):
                            chapter_no = 0
                        if chapter_no > 0:
                            recorded_params["_retry_guard"] = chapter_retry_state(project, chapter_no)
                    resume_run_id = None
                    suggested_title = None
                    capture_source = "task_start"
                    if method == "task.retry":
                        original = task_db.get_task(str(params["task_id"]))
                        if not original["retryable"]:
                            raise InkFlowError("该任务不能一键重试，请查看当前状态后重新说明要求。")
                        resume_run_id = original["run_id"]
                        recorded_method = original["method"]
                        recorded_params = original["params"]
                        suggested_title = _follow_up_task_title("重试", str(original["title"]), original["params"])
                    elif method == "workflow.run" and str(params.get("action") or "") == "batch_resume":
                        batch_id = str(params.get("batch_id") or "")
                        if not batch_id:
                            raise InkFlowError("续接批次缺少批次编号，未启动写作。")
                        manifest = self.service._engine(project.root)._load_batch_manifest(project, batch_id)
                        source_run = _batch_source_run(project, task_db, manifest)
                        if source_run:
                            resume_run_id = str(source_run["run_id"])
                            suggested_title = _follow_up_task_title("继续", str(source_run["title"]), manifest)
                        else:
                            capture_source = "legacy_recovery"
                            await emit({"type": "task.settings_legacy", "summary": "此批次没有可关联的原任务快照；将按批次清单中可用的本地设置恢复。"})
                    elif method == "conversation.send":
                        pending = TerminalSession.pending_resume(project, str(params.get("message") or ""))
                        if pending and pending.get("run_id"):
                            resume_run_id = str(pending["run_id"])
                            original = task_db.get_task(resume_run_id)
                            suggested_title = _follow_up_task_title("继续", str(original["title"]), pending.get("intent"))
                        elif pending:
                            capture_source = "legacy_recovery"
                            await emit({"type": "task.settings_legacy", "summary": "这个旧任务没有可追溯的配置版本。本次从已保存进度继续，并固定当前设置。"})
                    # Register the run before capturing settings. If snapshot
                    # validation fails, the same run remains visible with its
                    # actual failure reason instead of disappearing from the
                    # task list. A duplicate run ID is still rejected by the
                    # task_runs primary key before any model work starts.
                    task_db.start_task(
                        run_id,
                        owner_id=self.instance_id,
                        method=recorded_method,
                        params=recorded_params,
                        suggested_title=suggested_title,
                    )
                    task_started = True
                    if recorded_method in {"conversation.send", "workflow.run", "document.revise_selection"}:
                        requested_mode = _requested_task_mode(recorded_method, params)
                        scope = task_db.prepare_task_settings(
                            run_id,
                            novel_id=project.project_id,
                            settings=lambda: Settings.from_env(project.root),
                            resume_run_id=resume_run_id,
                            workspace_root=project.root,
                            capture_source=capture_source,
                            registered_now=True,
                            **({
                                "role_protocol_version": requested_mode[0],
                                "collaboration_mode": requested_mode[1],
                            } if requested_mode else {}),
                        )
                        settings_token = active_task_settings.set(scope)
                        runtime.task_id = scope.task_id
                        await emit({
                            "type": "task.settings_bound",
                            "summary": (
                                "旧任务没有历史配置快照，本次恢复已记录当前配置，后续续跑沿用此版本。"
                                if scope.source == "legacy_recovery" else
                                "已沿用原任务的配置版本；设置页面修改只影响新任务。"
                                if resume_run_id else "已固定本次任务的配置版本。"
                            ),
                            "metadata": scope.public_summary(),
                        })
            await emit({"type": "run.started", "method": method, "summary": "任务已进入墨流"})
            with project_lock_wait_policy(on_wait=report_lock_wait, probe_timeout=2.0):
                result = await self.service.dispatch(method, params, emit, consume_steering)
            if (run_id in self.steering_route_seen and self.steering_delivered.get(run_id)
                    and workflow_result_status(result) in {"completed", "waiting_user"}):
                await self._bind_applied_steering_revision(run_id, {
                    "task_revision": 1, "response_kind": "new_task",
                })
            dispatch_finished = True
            self.dispatch_finished_runs.add(run_id)
            await public_events.join()
            unapplied = await self._defer_unapplied_steering(run_id)
            if unapplied:
                if isinstance(result, dict):
                    result = {**result, "unapplied_user_updates": [
                        {"artifact_id": item["artifact_id"], "message": item["message"],
                         "task_revision": 1, "proposed_task_revision": item["sequence"] + 1} for item in unapplied
                    ]}
                await emit({
                    "type": "run.steering_deferred",
                    "summary": f"有 {len(unapplied)} 条中途补充尚未用于本次输出，原话已保存；请在当前任务继续处理。",
                    "count": len(unapplied),
                })
            scope = active_task_settings.get()
            if isinstance(result, dict) and scope is not None:
                result = {**result, "task_settings": scope.public_summary()}
            if isinstance(result, dict) and runtime.calls:
                result = {**result, "runtime_usage": runtime.snapshot()}
            outcome = workflow_result_status(result)
            failure = workflow_failure_reason(result)
            failure_guidance = None
            if outcome == "failed" and failure and isinstance(result, dict):
                result = {**result, "workflow_failure": failure}
                failure_guidance = _error_payload(InkFlowError(failure))
            record_warning = persist_outcome(
                outcome,
                summary=_visible_result_summary(result),
                error_message=failure if failure_guidance is not None else "",
                error_code="workflow_result" if failure_guidance is not None else None,
                retry_allowed=False if failure_guidance is not None else None,
            )
            if record_warning and isinstance(result, dict):
                result = {**result, "task_record_warning": record_warning}
            if delivery_error is None:
                try:
                    await self.write({"jsonrpc": "2.0", "id": request_id, "result": result})
                except (OSError, ValueError):
                    # The result is already durable. A closed desktop pipe
                    # must not relabel successful work as a failed workflow.
                    return
                try:
                    await emit({
                        "type": f"run.{outcome}", "method": method,
                        "summary": record_warning or (_visible_result_summary(result) if outcome != "completed" else "任务已完成"),
                    })
                except (OSError, ValueError):
                    # A lost final notification cannot revoke the committed
                    # task result; the task list remains the recovery source.
                    return
        except asyncio.CancelledError:
            requested = run_id in self.cancel_requested
            cancel_context = self.cancel_context.get(run_id, {})
            source = cancel_context.get("source", "unknown")
            stage = str(last_public_event.get("summary") or last_public_event.get("type") or method)
            if delivery_error is not None and not requested:
                message = "桌面与本地引擎的事件通道中断"
                impact = f"最后公开阶段：{stage}。当前步骤已停止；重新打开任务记录后从保存点继续。"
            elif requested:
                message = "任务由界面的停止操作中断"
                impact = f"停止前最后公开阶段：{stage}。该阶段尚未完成，因此没有被伪装成通过。"
            else:
                message = "任务在运行期间意外中断"
                impact = f"最后公开阶段：{stage}。墨流会保留断点，下一次继续时从已保存版本恢复。"
            record_warning = persist_outcome(
                "cancelled" if requested else "interrupted",
                summary=(
                    f"{message}；来源 {source}；最后阶段 {stage}"
                    if requested else f"{message}；最后阶段 {stage}"
                ),
                error_message=(f"事件通道异常：{type(delivery_error).__name__}" if delivery_error is not None else ""),
                retry_allowed=False,
            )
            if delivery_error is None:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": "cancelled" if requested else "interrupted",
                            "title": "任务已停止" if requested else "任务意外中断",
                            "message": message,
                            "impact": f"{impact} {record_warning}".strip(),
                            "preserved": "已有正文、草稿版本和正史未被回退",
                            "actions": [
                                {"label": "继续未完成批次，复用已保存草稿"},
                                {"label": "在协作台查看停止前的最后阶段"},
                            ],
                        },
                    }
                )
                public_events.put_nowait(
                    {
                        "type": "run.cancelled" if requested else "run.interrupted",
                        "method": method,
                        "summary": message,
                        "details": impact,
                        "cancel_requested": requested,
                        "cancel_source": source,
                        "cancel_reason": cancel_context.get("reason", ""),
                        "last_stage": stage,
                    }
                )
        except ProjectBusyError as exc:
            record_warning = persist_outcome(
                "waiting_condition", summary=str(exc), error_message=str(exc),
                error_code=type(exc).__name__, retry_allowed=False,
            )
            error = _error_payload(exc)
            if record_warning:
                error["record_warning"] = record_warning
                error["impact"] = f"{error.get('impact', '')} {record_warning}".strip()
            await self.write({"jsonrpc": "2.0", "id": request_id, "error": error})
            public_events.put_nowait({"type": "run.waiting_condition", "method": method, "summary": str(exc)})
        except Exception as exc:
            error = _error_payload(exc)
            action_labels = [
                str(action.get("label") or "").strip()
                for action in error.get("actions", [])
                if isinstance(action, dict) and action.get("label")
            ]
            durable_error = "\n".join(
                line for line in (
                    str(error.get("message") or str(exc)),
                    f"影响：{error.get('impact')}" if error.get("impact") else "",
                    f"已保留：{error.get('preserved')}" if error.get("preserved") else "",
                    f"下一步：{'；'.join(action_labels)}" if action_labels else "",
                ) if line
            )
            record_warning = persist_outcome(
                "failed", summary=str(error.get("title") or "本次操作未完成"),
                error_message=durable_error, error_code=str(error["code"]),
                retry_allowed=bool(error["retryable"]),
            )
            if record_warning:
                error["record_warning"] = record_warning
                error["impact"] = f"{error.get('impact', '')} {record_warning}".strip()
            await self.write({"jsonrpc": "2.0", "id": request_id, "error": error})
            public_events.put_nowait({"type": "run.failed", "method": method, "summary": str(exc)[:500]})
        finally:
            if self.steering_messages.get(run_id) or self.steering_delivered.get(run_id):
                await self._defer_unapplied_steering(run_id)
            self.dispatch_finished_runs.discard(run_id)
            self.steering_route_seen.discard(run_id)
            if settings_token is not None:
                active_task_settings.reset(settings_token)
            active_runtime.reset(runtime_token)
            public_events.put_nowait(None)
            await event_worker
            self.steering_messages.pop(run_id, None)

    async def write(self, value: dict[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, default=str)
        async with self.write_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


# 三个对比方向各自的叙事引擎。它们只改变矛盾来源与长线动力，
# 不改变用户在偏好里写下的题材、受众、性别、时代与基调等硬约束。
_IDEA_ANGLES: tuple[tuple[str, str], ...] = (
    (
        "外部压迫",
        "让主角被一个具体、持续升级的外在压力推着走：生存、竞争、追捕或期限；"
        "矛盾主要来自对手和环境做了什么。",
    ),
    (
        "关系拉扯",
        "让主线成立在人与人的信任、误解、背叛与结盟上；矛盾主要来自重要的人如何选择，"
        "外部事件只作为放大器。",
    ),
    (
        "规则解谜",
        "先立下一条读者能理解的规则，再让主角一层层试探它的边界与代价；"
        "矛盾主要来自规则本身藏着什么，长线靠真相递进。",
    ),
)

# 标题、前提与开篇抓手的文本相似度超过该阈值即视为实质重复。
_IDEA_DUPLICATE_RATIO = 0.72


def _direction_similarity(first: NovelIdeaCandidate, second: NovelIdeaCandidate) -> float:
    """用一个方向的标题、前提与开篇抓手判断两个方向是否实质重复。"""

    title = SequenceMatcher(None, first.title, second.title).ratio()
    body = (
        SequenceMatcher(None, first.premise, second.premise).ratio()
        + SequenceMatcher(None, first.opening_hook, second.opening_hook).ratio()
    ) / 2
    return max(title, body)


def _idea_reasoning_summary(notes: list[str]) -> list[str]:
    """合并多个方向的公开说明，并保持 Schema 要求的 2～6 条。"""

    unique = list(dict.fromkeys(note.strip() for note in notes if note and note.strip()))
    for filler in (
        "除用户原始偏好外，其余题材与情节细节都可以继续修改。",
        "这些方向只用于填写建项信息，采用前不会写入任何文件。",
    ):
        if len(unique) >= 2:
            break
        if filler not in unique:
            unique.append(filler)
    return unique[:6]


def _apply_requested_scale(candidate: dict[str, Any], preferences: str) -> None:
    """Deterministically preserve explicit project scale from the user's own text."""

    # A volume-local count is not the book's chapter count.
    book_scope = re.sub(r"第[一二三四五六七八九十百\d]+卷[^，。；;\n]*", "", preferences)
    chapter_match = re.search(r"(?<![\d第])(\d{1,4})\s*章", book_scope)
    volume_match = re.search(r"(?:分(?:成|为)?|共|总共|全书|预计)?\s*(?<!第)([一二三四五六七八九十\d]+)\s*卷", book_scope)
    total_match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*万\s*字", preferences)
    exact_total_match = re.search(r"(?:全书|总共|总字数|整(?:个|本)(?:故事|小说)?)[^，。；;\n\d]{0,10}(\d{4,7})\s*字", preferences)
    explicit_scale = bool(chapter_match or total_match or exact_total_match)
    chapter_words = max(500, int(candidate.get("target_chapter_words") or 3000))
    total_words = (
        round(float(total_match.group(1)) * 10_000)
        if total_match
        else int(exact_total_match.group(1)) if exact_total_match else None
    )
    if chapter_match:
        candidate["estimated_chapters"] = max(10, min(int(chapter_match.group(1)), 5000))
    elif total_words:
        candidate["estimated_chapters"] = max(10, min(round(total_words / chapter_words), 5000))
    if volume_match:
        value = volume_match.group(1)
        digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        count = int(value) if value.isdigit() else (digits.get(value, 0) or (digits.get(value.split("十")[0], 1) * 10 + digits.get(value.split("十")[-1], 0) if "十" in value else 0))
        if count:
            candidate["estimated_volumes"] = max(1, min(count, 100))
    elif explicit_scale:
        chapters = int(candidate.get("estimated_chapters") or 1)
        candidate["estimated_volumes"] = max(1, min((chapters + 39) // 40, 100))


def _fallback_idea_bundle(preferences: str, requested_count: int) -> NovelIdeaBundle:
    """Return a clearly labelled, editable starting point when ideation is truncated.

    This is deliberately small and deterministic. It prevents a transient model
    formatting failure from blocking project creation, while keeping every
    creative detail editable before anything is written to disk.
    """

    source = preferences.strip()
    female_lead = "女频" in source or "女主" in source or "女性" in source
    if "末日" in source or "求生" in source:
        genre = "末日求生"
        setting = "灾变后的封锁城区"
        trigger = "最后一条撤离通道突然关闭"
        obstacle = "资源规则每天变化，幸存者之间也必须争夺有限的安全区"
        titles = ["灰烬里的气象站", "第七天的安全屋", "末日倒计时手册"]
    elif "古" in source or "朝" in source or "宫" in source:
        genre = "古代成长"
        setting = "权力即将易主的都城"
        trigger = "主角无意拿到一封不能公开的密信"
        obstacle = "每个盟友都可能因家族利益改变立场"
        titles = ["长安密信", "灯下见山河", "春风不渡旧王庭"]
    else:
        genre = "成长冒险"
        setting = "规则正在失效的熟人社会"
        trigger = "主角被迫接下一件无人愿意承担的事"
        obstacle = "每次解决眼前危机，都会暴露更高层的代价与对手"
        titles = ["把明天借给我", "逆风的人间", "未寄出的第七封信"]

    lead = "林雾" if female_lead else "沈砚"
    audience = "女频中文网文读者" if female_lead else "中文网文读者"
    candidates: list[dict[str, Any]] = []
    for index in range(requested_count):
        variation = (
            "先活下来，再找到失散的家人"
            if index == 0
            else "先守住一个承诺，再判断它是否值得"
            if index == 1
            else "先查清规则从何而来，再决定是否推翻它"
        )
        candidates.append(
            {
                "concept_id": f"fallback-{index + 1}",
                "title": titles[index],
                "genre": genre,
                "premise": (
                    f"{lead}身处{setting}，因{trigger}不得不{variation}；"
                    f"最大的阻力是{obstacle}。"
                ),
                "protagonist": lead,
                "target_audience": audience,
                "core_selling_point": "低起点选择不断产生可见代价，短期求生目标与长期真相互相牵引。",
                "target_chapter_words": 3000,
                "estimated_chapters": 200,
                "estimated_volumes": 6,
                "user_rules": [],
                "opening_hook": f"开篇当夜，{lead}发现{trigger}，必须在天亮前做出第一次不可逆选择。",
                "long_term_engine": "每次获得安全、线索或同伴，都要付出新代价，并推动主线规则逐层揭开。",
                "choice_note": "这是模型未返回完整结构时提供的可编辑保底提案，适合先修改再建项。",
            }
        )
    return NovelIdeaBundle(
        candidates=candidates,
        public_reasoning_summary=[
            "模型本次未完成结构化输出，因此没有把任何模型细节当作结论。",
            f"保底方案只保留了你写下的方向：{source or '未设置偏好'}；其余内容都可修改。",
        ],
    )


def _short_provider_error(error: ProviderError) -> str:
    text = str(error)
    if "finish_reason=length" in text or "空 JSON" in text:
        return "模型推理耗尽了本次预算，未留下完整方案。"
    return "模型未能返回可解析方案，已切换为可编辑保底提案。"


def _visible_result_summary(result: Any) -> str:
    if isinstance(result, dict) and result.get("unapplied_user_updates"):
        return f"本次阶段已结束，但有 {len(result['unapplied_user_updates'])} 条中途补充尚未应用；原话已保存供继续处理。"
    failure = workflow_failure_reason(result)
    if failure:
        return failure[:300]
    if isinstance(result, dict):
        if result.get("settings_change_summary"):
            return "；".join(str(item) for item in result["settings_change_summary"])[:300]
        if result.get("settings_updated"):
            return f"已修改 {len(result['settings_updated'])} 项设置。"
        for key in ("reply", "help", "gate", "message", "summary", "next_action"):
            if result.get(key):
                return str(result[key])[:300]
        if result.get("result") and isinstance(result["result"], dict):
            nested = result["result"]
            for key in ("summary", "message", "status", "outline_path"):
                if nested.get(key):
                    return str(nested[key])[:300]
    return "工作流返回了可查看结果。"


def _workflow_next_step(action: str, result: dict[str, Any]) -> dict[str, str] | None:
    """给桌面按钮触发的固定工作流也提供同一套用户可控引导。"""

    if action == "review" and result.get("automatic_acceptance"):
        return {"label": "继续下一章", "reason": "本次审查已通过且当前设置已自动验收，可以继续安排下一章。", "prompt": "推荐下一章安排"}
    if action == "review":
        if result.get("verdict") == "pass":
            return {"label": "查看审查报告", "reason": "本次审查已通过；是否入正史仍按接受授权和引擎门禁决定。", "prompt": "打开当前审查报告"}
        if result.get("verdict") == "unknown":
            return {"label": "补足审查依据", "reason": "本次审查资料不足，先核对缺失来源，不改动已保存草稿。", "prompt": "查看当前审查缺口"}
        return {"label": "查看需修问题", "reason": "先核对审查指出的原文问题，再决定定向修订。", "prompt": "打开当前审查报告"}
    if action == "batch_draft" and result.get("authorization_source") in {"batch_preapproval", "settings_auto_accept"}:
        return {"label": "查看已提交批次", "reason": "本批次已按当前确认策略处理，先查看提交结果再继续。", "prompt": "查看当前项目状态"}

    suggestions = {
        "plan": ("查看章节规划", "先核对章节卡，再决定从哪一章开始写。", "查看当前规划"),
        "outline": ("从大纲开始写", "独立大纲已保存，选择起点后再生成草稿。", "根据大纲生成草稿"),
        "write": ("审查当前章节", "草稿已生成，下一步按当前模式检查这一版本。", "审查当前章"),
        "revise": ("重新审查", "修订产生了新版本，旧报告不能替代新版本审查。", "重新审查当前章"),
        "accept": ("继续下一章", "当前章节已经完成正史提交，可以继续安排下一章。", "推荐下一章安排"),
        "batch_draft": ("查看批次进度", "批量草稿仍在临时区，先查看逐章结果再决定是否验收。", "查看批次进度"),
        "batch_accept": ("安排后续章节", "批次提交完成，可以继续规划或生成下一段。", "推荐后续章节安排"),
        "arc_audit": ("查看复审报告", "跨章复审只生成报告，先查看风险与规划差异。", "打开篇章复审报告"),
        "checkpoint_create": ("继续创作", "检查点已经保存，接下来可以继续写作。", "推荐下一步"),
    }
    item = suggestions.get(action)
    if not item:
        return None
    label, reason, prompt = item
    return {"label": label, "reason": reason, "prompt": prompt}


def _list_batch_summaries(project: InkFlowProject) -> list[dict[str, Any]]:
    """Read visible batch state for the desktop board; malformed files stay invisible."""

    folder = project.internal / "batches"
    if not folder.is_dir():
        return []
    db = StudioService(project).db
    result: list[dict[str, Any]] = []
    for path in sorted(folder.glob("batch-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:30]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not value.get("batch_id"):
                continue
            entries = value.get("chapters") if isinstance(value.get("chapters"), list) else []
            status = str(value.get("status") or "unknown")
            source_run = _batch_source_run(project, db, value)
            resumable = status in {"failed", "interrupted"}
            if status == "drafting" and source_run and source_run.get("status") in {"interrupted", "cancelled", "failed"}:
                status = "interrupted"
                resumable = True
            if source_run and source_run.get("status") == "running":
                resumable = False
            result.append(
                {
                    "batch_id": str(value["batch_id"]),
                    "status": status,
                    "run_id": value.get("last_run_id"),
                    "resume_available": resumable,
                    "stop_reason": str(value.get("stop_reason") or ""),
                    "stopped_at_chapter": value.get("stopped_at_chapter"),
                    "start_chapter_no": value.get("start_chapter_no"),
                    "end_chapter_no": value.get("end_chapter_no"),
                    "chapters": [
                        {
                            "chapter_no": item.get("chapter_no"),
                            "version": item.get("version"),
                            "review_verdict": item.get("verdict"),
                            "memory_status": item.get("memory_status"),
                        }
                        for item in entries
                        if isinstance(item, dict)
                    ],
                    "updated_at": value.get("updated_at") or value.get("accepted_at") or "",
                }
            )
        except (OSError, json.JSONDecodeError):
            continue
    return result


def _local_suggested_prompts(project: InkFlowProject | None) -> list[dict[str, str]]:
    """提示词预测的本地规则兜底：不调用模型，按项目阶段给固定建议。"""

    prompts = [
        {
            "label": "先问我",
            "prompt": "先不要执行任务。请根据当前项目状态和最近讨论，用选项卡主动问我一到三个最值得确认、容易回答的问题。",
        }
    ]
    if project is None:
        prompts.extend(
            [
                {"label": "想想点子", "prompt": "帮我想几个全新的故事点子"},
                {"label": "查看状态", "prompt": "查看当前项目状态"},
            ]
        )
        return prompts
    has_plan = False
    drafts: list[int] = []
    try:
        has_plan = project.db.get_current_plan_bundle() is not None
        drafts = list(project.db.chapter_numbers_by_status("draft"))
    except Exception:
        pass
    if not has_plan:
        if not (project.root / "OUTLINE.md").is_file():
            next_step = {"label": "整理全书大纲", "prompt": "根据设定帮我整理全书大纲，先不写正文。"}
        elif not (project.root / "STORY_DETAIL.md").is_file():
            next_step = {"label": "展开剧情细纲", "prompt": "参考设定和大纲展开剧情细纲，写清事件因果、人物选择和后果，先不分章节。"}
        else:
            next_step = {"label": "安排近期章节", "prompt": "参考设定、大纲和剧情细纲，帮我安排接下来要写的章节。"}
        return [next_step, {"label": "看看故事逻辑", "prompt": "帮我看看现有设定和故事安排有没有不合理的地方，先讨论，不改文件。"}]
    else:
        prompts.append({"label": "规划当前篇章", "prompt": "帮我规划当前篇章"})
        if drafts:
            first = drafts[0]
            prompts.extend(
                [
                    {"label": "审查当前章", "prompt": f"审查第 {first} 章"},
                    {"label": "验收进正史", "prompt": f"审查第 {first} 章，通过后入正史"},
                ]
            )
        else:
            prompts.append({"label": "写当前章", "prompt": "写当前章的草稿，先不要审查"})
    prompts.append({"label": "查看状态", "prompt": "查看当前项目状态"})
    return prompts[:5]


def _project_status_summary(project: InkFlowProject) -> dict[str, Any]:
    """给提示词预测器的小型项目状态摘要，只取稳定字段。"""

    summary: dict[str, Any] = {}
    try:
        summary["latest_accepted_chapter"] = project.db.latest_accepted_chapter_no()
        summary["draft_chapters"] = project.db.chapter_numbers_by_status("draft")
        bundle = project.db.get_current_plan_bundle()
        if bundle is not None:
            summary["current_arc"] = {
                "arc_id": bundle.current_arc.arc_id,
                "chapter_start": bundle.current_arc.chapter_start,
                "chapter_end": bundle.current_arc.chapter_end,
            }
    except Exception:
        pass
    return summary


def _follow_up_task_title(verb: str, previous_title: str, source: Any = None) -> str | None:
    """Keep the work's identity when a short 'continue' or retry starts a new run."""

    base = re.sub(r"^(?:(?:继续|重试)\s*·\s*)+", "", previous_title).strip()
    if base in {"未命名任务", "自然语言任务", "墨流任务", "继续上次任务"} and isinstance(source, dict):
        request = str(source.get("requested_outcome") or source.get("message") or source.get("instruction") or "")
        first = re.split(r"[。！？\n]", request, maxsplit=1)[0].strip(" ，。！？：:；; \t")
        if first and first not in {"继续", "继续完成这批", "继续上次任务"}:
            base = first[:34] + ("…" if len(first) > 34 else "")
        else:
            action = str(source.get("action") or "")
            base = {
                "batch_draft": "批量写章节", "batch_draft_accept": "批量写作与验收",
                "write_review": "写作与审查", "write_review_accept": "写作、审查与验收",
                "revise_review": "修订与复审", "revise_review_accept": "修订、复审与验收",
                "review_accept": "审查与验收", "continue_run": "续接小说任务",
            }.get(action, "小说任务")
            chapter_no = source.get("chapter_no")
            end_chapter_no = source.get("end_chapter_no")
            if isinstance(chapter_no, int) and chapter_no > 0:
                scope = f"第 {chapter_no}～{end_chapter_no} 章" if isinstance(end_chapter_no, int) and end_chapter_no > chapter_no else f"第 {chapter_no} 章"
                base = f"{scope} · {base}"
    if not base:
        return None
    return f"{verb} · {base}"[:60]


def _task_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """只记录恢复任务所需的小参数，不复制正文、API Key 或模型输出。"""

    allowed_by_method = {
        "conversation.send": {"message", "role_protocol_version", "collaboration_mode"},
        "workflow.run": {
            "action",
            "chapter_no",
            "start_chapter_no",
            "end_chapter_no",
            "draft_only",
            "start_chapter",
            "end_chapter",
            "instruction",
            "max_revision_rounds",
            "batch_id",
            "label",
            "checkpoint_id",
            "boundary_chapter",
            "role_protocol_version",
            "collaboration_mode",
        },
        "reference.fetch": {"url"},
        "reference.search": {"query", "limit"},
        "reference.analyze": {"reference_id"},
        "document.revise_selection": {
            "relative_path",
            "start_offset",
            "end_offset",
            "comment",
            "expected_hash",
        },
        "task.retry": {"task_id"},
    }
    allowed = allowed_by_method.get(method, set())
    return {key: params[key] for key in allowed if key in params}


def _should_track_task(method: str, params: dict[str, Any]) -> bool:
    if method == "workflow.run":
        return str(params.get("action") or "") not in {"checkpoint_list", "rollback_preview"}
    return method in {
        "conversation.send",
        "document.revise_selection",
        "reference.search",
        "reference.fetch",
        "reference.analyze",
        "task.retry",
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    message = str(exc) or "墨流遇到未知错误。"
    normalized = message.casefold()
    http_match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.IGNORECASE)
    http_status = int(http_match.group(1)) if http_match else None
    payload: dict[str, Any] = {
        "code": exc.__class__.__name__,
        "title": "本次操作未完成",
        "message": message,
        "impact": "本次操作已停止。",
        "preserved": "已经保存的正文、版本和正史保持当前状态。",
        "actions": [{"id": "open_process", "label": "查看协作台最后阶段"}],
        "retryable": False,
    }
    if isinstance(exc, ValidationQuotaExceeded):
        payload.update(title="训练验证边界需要处理", impact="没有继续发送训练验证请求；日常写作和本地训练不受次数限制。",
                       retryable=False, actions=[{"id": "open_learning", "label": "查看训练验证记录"}])
        return payload
    if isinstance(exc, ProjectBusyError):
        payload.update(
            title="项目写入暂时繁忙",
            impact="当前写入步骤未获得项目写锁；已保存的正文和审查不会被撤回或自动重做。",
            actions=[{"id": "open_process", "label": "查看正在执行的任务与已保存成果"}],
            retryable=False,
        )
        return payload
    if isinstance(exc, ValidationError):
        issues = exc.errors(include_input=False)
        brief = "；".join(
            f"{'.'.join(map(str, item.get('loc') or ())) or '结果'}：{item.get('msg') or item.get('type')}"
            for item in issues[:3]
        )
        payload.update(
            code="validation_error",
            title="输入或模型输出不完整",
            message=f"输入或模型输出格式不完整：{brief}" if brief else "输入或模型输出格式不完整。",
            impact="当前步骤没有通过格式门禁，因此没有继续写入后续结果。",
            actions=[{"id": "retry", "label": "补齐输入或重试当前步骤"}],
            retryable=False,
            details=[
                {key: item.get(key) for key in ("type", "loc", "msg")}
                for item in issues
            ],
        )
        return payload
    if http_status in {401, 403}:
        payload.update(
            title="模型认证失败", impact="当前模型步骤没有完成；已保存成果不受影响。",
            actions=[{"id": "open_settings", "label": "检查模型密钥与服务权限"}], retryable=False,
        )
    elif http_status in {408, 429, 500, 502, 503, 504}:
        payload.update(
            title="模型服务暂时不可用" if http_status != 429 else "模型服务暂时限流",
            impact="已在当前模型步骤内限次重试；没有重做已保存的正文或提交。",
            actions=[{"id": "open_process", "label": "查看最后阶段"},
                     {"id": "retry", "label": "恢复后续接未完成步骤"}],
            retryable=True,
        )
    elif http_status == 402:
        payload.update(
            title="模型服务余额或计费状态需要处理", impact="服务商拒绝了当前模型请求；本地成果保留。",
            actions=[{"id": "open_settings", "label": "检查服务商账户与模型配置"}], retryable=False,
        )
    elif http_status == 413:
        payload.update(
            title="模型输入超过接口限制", impact="当前请求未完成；正文和硬约束不会被自动删除。",
            actions=[{"id": "open_context", "label": "查看上下文占用"}], retryable=False,
        )
    elif http_status in {400, 404, 422}:
        payload.update(
            title="模型请求参数或格式不兼容", impact="当前模型步骤没有完成；不建议原样反复发送。",
            actions=[{"id": "open_settings", "label": "检查模型与接口配置"},
                     {"id": "open_process", "label": "查看接口错误详情"}], retryable=False,
        )
    elif any(word in normalized for word in ("api key", "密钥", "模型接口", "服务商")):
        payload.update(
            title="模型配置需要处理",
            impact="当前模型任务没有完成。",
            actions=[{"id": "open_settings", "label": "打开模型设置"}],
            retryable=False,
        )
    elif any(word in normalized for word in ("版本", "哈希", "正文已变化", "重新审查")):
        payload.update(
            title="版本门禁已阻止继续",
            impact="旧审查或旧正文不能用于提交当前版本。",
            preserved="当前草稿版本仍然保留。",
            actions=[{"id": "open_chapter_review", "label": "打开当前章重新审查"}],
            retryable=False,
        )
    elif "章节卡" in normalized or (
        any(word in normalized for word in ("尚未生成", "缺少")) and "规划" in normalized
    ):
        payload.update(
            title="当前章节缺少规划约束",
            impact="Writer 没有可靠章节卡，写作步骤没有启动。",
            actions=[{"id": "open_planning", "label": "生成或补齐篇章规划"}],
            retryable=False,
        )
    elif any(word in normalized for word in ("上下文容量", "最大上下文", "soft token", "hard token")):
        payload.update(
            title="上下文容量不足",
            impact="墨流保留了硬正史和用户要求，并停止本次模型调用。",
            actions=[{"id": "open_context", "label": "查看上下文占用"}],
            retryable=False,
        )
    elif "finish_reason=length" in normalized or "空 json 内容" in normalized:
        payload.update(
            title="模型输出被截断或没有正文",
            impact="当前模型步骤未形成完整结果；已保存的正文与正史保持不变。",
            actions=[{"id": "open_process", "label": "查看本次输出与重试记录"},
                     {"id": "open_settings", "label": "检查单次输出上限"}],
            retryable=False,
        )
    elif _has_transient_transport_cause(exc) or (
        isinstance(exc, ProviderError)
        and any(word in normalized for word in (
            "超时", "网络", "连接", "timeout", "timed out", "network", "connecterror", "connection refused",
        ))
    ):
        payload.update(
            title="模型调用没有正常返回",
            impact="当前远程步骤未完成。",
            actions=[
                {"id": "open_process", "label": "先查看任务状态"},
                {"id": "retry", "label": "确认后重试"},
            ],
            retryable=True,
        )
    elif isinstance(exc, sqlite3.Error):
        payload.update(
            title="本地任务记录暂时不可用",
            impact="当前步骤未能可靠记录状态；已保存文件不自动撤回，也不重发模型请求。",
            actions=[{"id": "open_process", "label": "核对原任务和当前文件"}],
            retryable=False,
        )
    elif isinstance(exc, OSError):
        payload.update(
            title="本地文件或进程访问失败",
            impact="当前步骤未完成；请检查文件是否仍存在、被其他程序占用或无写入权限。",
            actions=[{"id": "open_process", "label": "查看失败位置与已保存成果"}],
            retryable=False,
        )
    elif isinstance(exc, InkFlowError):
        payload["retryable"] = False
    if os.getenv("INKFLOW_DEBUG") == "1" and not isinstance(exc, ValidationError):
        payload["details"] = traceback.format_exc(limit=5)
    return payload


def _has_transient_transport_cause(exc: BaseException) -> bool:
    """Recognize wrapped httpx/socket failures without treating unknown errors as replayable."""
    seen: set[int] = set()
    current: BaseException | None = exc
    transient_names = {
        "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "TimeoutException", "TimeoutError", "NetworkError", "RemoteProtocolError",
    }
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in transient_names:
            return True
        current = current.__cause__ or current.__context__
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="墨流桌面本地 JSONL 服务")
    parser.add_argument("--once", action="store_true", help="处理标准输入中的一个请求后退出")
    parser.add_argument("--mcp", action="store_true", help="作为 stdio MCP Server 运行")
    return parser


async def _serve_once() -> None:
    server = JsonLineServer()
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        return
    try:
        request = json.loads(line)
        request_id = str(request.get("id") or "once")
        method = str(request["method"])
        params = dict(request.get("params") or {})
        run_id = str(params.get("run_id") or f"run-{uuid.uuid4().hex}")
        params["run_id"] = run_id
        await server.process(request_id, run_id, method, params)
    except Exception as exc:
        await server.write({"jsonrpc": "2.0", "id": None, "error": _error_payload(exc)})


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    if args.mcp:
        from .mcp_server import main as mcp_main

        mcp_main()
        return
    asyncio.run(_serve_once() if args.once else JsonLineServer().serve())


if __name__ == "__main__":
    main()
