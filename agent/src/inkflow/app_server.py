from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from . import __version__
from .config import Settings, api_key_status, save_api_key_to_keyring, save_user_settings
from .engine import InkFlowEngine
from .errors import InkFlowError, ProviderError
from .learning import LearningService
from .project import InkFlowProject
from .project_lock import project_write_lock, project_write_lock_sync
from .provider import create_provider
from .references import ReferenceService
from .schemas import BookBrief, CollaborationReply, ContextPacket, ContextSection, MemoryPatch, NovelIdeaBundle, PrefillSuggestion, PromptOptimization, ProviderProbe, ReviewReport, WriterDirectionSet
from .review_verifier import verify_review
from .studio import StudioService
from .terminal_session import TerminalSession
from .trace import TraceRecorder
from .utils import content_hash, estimate_tokens
from .voice import VOICE_SETTING_NAMES, VoiceRuntime


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class InkFlowAppService:
    """桌面端与编辑器共享的本地应用服务。"""

    def __init__(self, instance_id: str | None = None) -> None:
        self.instance_id = instance_id or f"server-{os.getpid()}-{uuid.uuid4().hex}"
        self.voice = VoiceRuntime()

    async def dispatch(self, method: str, params: dict[str, Any], emit: EventSink) -> Any:
        if method == "app.initialize":
            return {
                "product": "墨流（InkFlow）",
                "version": __version__,
                "protocol_version": 1,
                "capabilities": {
                    "desktop": True,
                    "mcp": True,
                    "vscode": True,
                    "agents": ["Coordinator", "Writer", "Reviewer", "Memory Keeper"],
                    "formal_agents": ["Coordinator", "Writer", "Reviewer", "Memory Keeper"],
                    "novel_production_agents": ["Writer", "Reviewer", "Memory Keeper"],
                    "raw_chain_of_thought": False,
                    "voice_runtime": True,
                    "voice_is_formal_agent": False,
                },
                "provider": api_key_status(Settings.from_env().provider_kind),
                "voice": self.voice.status(),
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
                "max_output_tokens": settings.max_output_tokens,
                "inquiry_frequency": settings.inquiry_frequency,
                "agent_generation": settings.agent_generation,
                "review_verification_mode": settings.review_verification_mode,
                "review_local_nli_model": settings.review_local_nli_model,
                "review_judge_model": settings.review_judge_model,
                "retrieval_embedding_model": settings.retrieval_embedding_model,
                "retrieval_reranker_model": settings.retrieval_reranker_model,
                "powershell_enabled": settings.powershell_enabled,
                "input_price_per_million": settings.input_price_per_million,
                "output_price_per_million": settings.output_price_per_million,
                "capabilities": create_provider(settings).capabilities(),
            }
        if method == "provider.configure":
            provider_kind = str(params.get("provider_kind") or Settings.from_env().provider_kind).lower()
            key = str(params.get("api_key", "")).strip()
            if key:
                save_api_key_to_keyring(key, provider_kind)
            allowed = {
                name: params[name]
                for name in (
                    "provider_kind",
                    "base_url",
                    "model",
                    "reasoning_effort",
                    "context_soft_tokens",
                    "context_hard_tokens",
                    "max_output_tokens",
                    "inquiry_frequency",
                    "agent_generation",
                    "review_verification_mode",
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
                save_user_settings(allowed)
            current = Settings.from_env(params.get("workspace_root"))
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
            return self.voice.status(params.get("workspace_root") or params.get("project_root"))
        if method == "voice.settings.get":
            return self.voice.settings(params.get("workspace_root") or params.get("project_root"))
        if method == "voice.settings.configure":
            updates = {name: params[name] for name in VOICE_SETTING_NAMES if name in params}
            return self.voice.configure(updates, params.get("workspace_root") or params.get("project_root"))
        if method == "voice.models.prepare":
            return await self.voice.prepare_models(
                params.get("workspace_root") or params.get("project_root"),
                str(params.get("confirmation") or ""),
                emit,
            )
        if method == "voice.light.install":
            return await self.voice.prepare_light_models(
                str(params.get("confirmation") or ""),
                emit,
            )
        if method == "voice.qwen.install":
            return await self.voice.install_qwen(
                str(params.get("confirmation") or ""),
                emit,
            )
        if method == "voice.profile.list":
            return {"profiles": self.voice.list_profiles()}
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
        if method == "project.ideate":
            settings = Settings.from_env(params.get("workspace_root"))
            preferences = str(params.get("preferences") or "").strip()
            fast = bool(params.get("fast", True))
            requested_count = 1 if fast else 3
            await emit(
                {
                    "type": "writer.started",
                    "summary": "Writer 正在快速生成一个开书方向" if fast else "Writer 正在构思三个不同的开书方向",
                }
            )
            try:
                result = await create_provider(settings).generate_json(
                    system_prompt=(
                    "你是墨流的 Writer，当前只负责建项前构思，不写正文。面向中文网文读者，"
                    f"严格给出 {requested_count} 个可长线连载的原创方案。每个方案都必须有"
                    "清晰主角欲望、持续矛盾、前三章抓手与可升级的长期叙事引擎。不要依赖用户已有小说。"
                    "书名简洁可辨识；premise 至少写清人物、触发事件、目标与主要阻力。"
                    "用户偏好中已经明确说出的受众、主角性别、题材、时代、基调、禁区和开篇方式均是硬约束，"
                    "不得为了追求新奇而换掉。出现‘女频’且用户没有另行指定时，默认使用女性主角和女频叙事重点；"
                    "出现‘从……开始’时，opening_hook 必须从该事件或场面起笔。把这些明确约束逐条写入 user_rules。"
                    "绝不能把 Writer 自己选择的时代、题材或情节说成用户偏好；原文没有的内容只能称为创意提案。"
                    "user_rules 只能复述用户明确说过的内容，不能把本次方案细节升级为用户硬约束。"
                    "public_reasoning_summary 用 2～4 条简短中文公开说明你核对了哪些偏好、为何选择这个方向、"
                    "还有什么可调整；这是给用户看的判断摘要，不是隐藏思维链。"
                    ),
                    user_prompt=(
                    f"用户可以完全没有想法。请生成 {requested_count} 个可直接建立项目的开书方案。"
                    f"\n用户可选偏好：{preferences or '无，请主动做多样化选择。'}"
                    "\n默认单章 3000 字、约 200 章、6 卷；可按题材合理微调。"
                    ),
                    output_model=NovelIdeaBundle,
                    # 建项构思不是正文推演。关闭推理可以避免部分模型先耗尽
                    # reasoning token、却来不及返回小型 JSON 的兼容性故障。
                    effort="low",
                    max_tokens=1200 if fast else 2800,
                    thinking=False,
                    timeout_seconds=min(settings.request_timeout_seconds, 90.0)
                    if fast
                    else min(settings.planning_timeout_seconds, 180.0),
                    agent_role="writer",
                )
                fallback_used = False
                model_name = result.model
                idea_payload = result.data.model_dump()
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

        project = self._project(params)
        studio = StudioService(project)

        if method == "project.open":
            with project_write_lock_sync(project.root):
                studio.db.reconcile_interrupted_tasks(self.instance_id)
            return {
                "dashboard": studio.dashboard(),
                "tree": studio.tree(),
                "canon_migration": project.canonical_content_migration_status(),
            }
        if method == "voice.roles.get":
            return self.voice.get_role_map(project.root)
        if method == "voice.roles.set":
            return self.voice.set_role_map(project.root, params)
        if method == "voice.roles.analyze":
            return self.voice.analyze_roles(str(params.get("text") or ""))
        if method == "voice.job.create":
            return self.voice.create_job(project.root, params, emit)
        if method == "voice.job.list":
            return {"jobs": self.voice.list_jobs(project.root)}
        if method == "voice.job.status":
            return self.voice.job_status(str(params.get("job_id") or ""))
        if method == "voice.job.pause":
            return self.voice.pause_job(str(params.get("job_id") or ""))
        if method == "voice.job.resume":
            return self.voice.resume_job(str(params.get("job_id") or ""), emit)
        if method == "voice.job.cancel":
            return self.voice.cancel_job(str(params.get("job_id") or ""))
        if method == "project.canon_migration.status":
            return project.canonical_content_migration_status()
        if method == "project.canon_migration.apply":
            return project.apply_canonical_content_migration(str(params.get("confirmation_token") or ""))
        if method == "conversation.history":
            return {"entries": TerminalSession.history(project.root, int(params.get("limit", 100)))}
        if method == "project.status":
            return studio.dashboard()
        if method == "context.status":
            settings = Settings.from_env(project.root)
            status_path = project.internal / "context-status.json"
            if status_path.is_file():
                try:
                    return json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
            return {
                "status": "idle",
                "estimated_tokens": 0,
                "before_compression_tokens": 0,
                "soft_limit_tokens": settings.context_soft_tokens,
                "hard_limit_tokens": settings.context_hard_tokens,
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
        if method == "document.read":
            return studio.read_document(str(params["relative_path"]))
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
            packet = self._engine(project.root)._context_builder(project).build(
                chapter_no, "在光标处生成一段可选续写；不得保存、审查或提交正史。", mode="draft"
            )
            packet.sections.append(ContextSection(
                key="INPUT", title="当前光标附近正文", hard=True,
                content=json.dumps({"before_cursor": before, "after_cursor": after, "maximum_characters": length_limit}, ensure_ascii=False),
                source_ids=[f"draft:{chapter_no}:{current_hash}"],
            ))
            packet.estimated_tokens = estimate_tokens(packet.to_markdown())
            result = await create_provider(settings).generate_json(
                system_prompt=(
                    "你是墨流的 Writer，只生成编辑器光标处可插入的正文候选。保持人物、时态、视角和声线连续；"
                    "不要解释，不要重复光标前后的文字，不要修改文件，不要提交正史。"
                ),
                user_prompt=packet.to_markdown(),
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
            packet = self._engine(project.root)._context_builder(project).build(
                chapter_no, "为本章提出互相有明显区别的写作方向；只提交方案，不写正文。", mode="draft"
            )
            result = await create_provider(settings).generate_json(
                system_prompt="你是墨流 Writer 的候选方案阶段。只提出方向，最终正文仍由固定主笔统一完成。",
                user_prompt=packet.to_markdown() + f"\n\n必须给出 {count} 个方向。",
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
            return {"candidates": artifacts, "context_packet_id": content_hash(packet.to_markdown()), "model": result.model, "usage": result.usage}
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
            packet = self._engine(project.root)._context_builder(project).build(
                chapter_no, "多维审查当前草稿", mode="review", protected_input=content
            )
            reports = []
            merged: dict[tuple[str, str, str], dict[str, Any]] = {}
            run_id = str(params.get("run_id") or uuid.uuid4().hex)
            for dimension in dimensions:
                result = await create_provider(Settings.from_env(project.root)).generate_json(
                    system_prompt="你是墨流 Reviewer。只审查指定维度，逐条引用当前正文或 Context Packet；不直接修改正文，不以投票代替证据。",
                    user_prompt=packet.to_markdown() + f"\n\n# 审查维度\n{dimension}\n\n# 当前正文\n{content}",
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
            return {"reports": reports, "merged_findings": findings, "merge_method": "evidence_keyed_no_voting", "next_action": "由正式 Reviewer 对当前版本执行最终审查门禁"}
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
                raise ValueError("只有用户明确接受当前正文后，Memory Keeper 才能准备正史变更预览")
            chapter_no = int(params["chapter_no"])
            chapter = project.db.get_chapter(chapter_no)
            if not chapter or chapter["status"] != "draft":
                raise ValueError("当前章节没有可验收草稿")
            review = project.db.latest_review_record(chapter_no)
            if not review or review["chapter_version"] != int(chapter["version"]) or review["report"].verdict != "pass":
                raise ValueError("当前草稿版本尚未通过 Reviewer，不能准备正史预览")
            content = (project.root / chapter["path"]).read_text(encoding="utf-8")
            trace = TraceRecorder(project.root, f"memory-preview-{chapter_no:05d}", Settings.from_env(project.root).trace_level)
            patch, _, _ = await self._engine(project.root)._extract_memory_patch(project, chapter_no, content, trace, source_status="accepted")
            artifact = project.db.save_agent_artifact(
                artifact_type="memory_patch_preview", run_id=trace.run_id, role="memory_keeper",
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
            return {"preferences": project.db.list_preferences()}
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
            recipient = str(params.get("recipient_role") or (pending["recipient_role"] if pending else "writer"))
            role_boundaries = {
                "writer": "只回答规划、写作或修订问题，不审批，不提交正史。",
                "reviewer": "只进行证据化审查，不直接修改正文。",
                "memory_keeper": "只回答已接受正文的事实问题，不从草稿提交正史。",
                "coordinator": "只澄清目标、依赖和分工，不写正文、不审批、不提交正史。",
            }
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
                user_prompt=discussion_packet.to_markdown(),
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
        if method == "collaboration.overview":
            return {
                "threads": project.db.list_collaboration_threads(),
                "messages": project.db.list_collaboration_messages(active_only=False, limit=int(params.get("limit", 80))),
                "tasks": studio.db.list_tasks(int(params.get("task_limit", 30))),
                "batches": _list_batch_summaries(project),
                "learning_events": project.db.list_learning_events(int(params.get("learning_limit", 12))),
                "artifacts": project.db.list_agent_artifacts(limit=30),
                "usage": _usage_overview(project, Settings.from_env(project.root)),
            }
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
        if method == "learning.training.prepare":
            with project_write_lock_sync(project.root):
                return LearningService(project).prepare_training(
                    export_id=str(params["export_id"]),
                    base_model_path=str(params["base_model_path"]),
                    method=str(params.get("training_method") or "lora"),
                )
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
            message = str(params.get("message") or "").strip()
            if not message:
                raise ValueError("消息不能为空。")
            await emit({"type": "controller.routing", "summary": "正在理解目标与执行边界"})
            session = TerminalSession(self._engine(project.root))
            await emit({"type": "workflow.started", "summary": "已交给墨流确定性工作流"})
            result = await session.handle(project.root, message)
            await emit({"type": "workflow.completed", "summary": _visible_result_summary(result)})
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
        if method == "task.list":
            return {"tasks": studio.db.list_tasks(int(params.get("limit", 50)))}
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
        await emit({"type": "workflow.started", "action": action, "summary": "工作流已开始"})
        if action == "plan":
            result = await engine.generate_plan(project.root)
        elif action == "write":
            instruction = str(params.get("instruction") or "")
            selected = project.db.get_metadata(f"writer_selection:{int(params['chapter_no'])}", None)
            if isinstance(selected, dict) and selected.get("data"):
                instruction = instruction + "\n\n已选定候选方向：" + json.dumps(selected["data"], ensure_ascii=False)
            result = await engine.write_chapter(
                project.root, int(params["chapter_no"]), instruction
            )
        elif action == "review":
            result = await engine.review_chapter(project.root, int(params["chapter_no"]))
        elif action == "revise":
            result = await engine.revise_chapter(
                project.root, int(params["chapter_no"]), str(params.get("instruction") or "")
            )
        elif action == "accept":
            result = await engine.accept_chapter(project.root, int(params["chapter_no"]), force=False)
        elif action == "batch_draft":
            result = await engine.draft_batch(
                project.root,
                int(params["start_chapter_no"]),
                int(params["end_chapter_no"]),
                instruction=str(params.get("instruction") or ""),
                max_revision_rounds=int(params.get("max_revision_rounds", 2)),
            )
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
        else:
            raise ValueError(f"不支持的工作流动作：{action}")
        await emit({"type": "workflow.completed", "action": action, "summary": _visible_result_summary(result)})
        return result

    @staticmethod
    def _project(params: dict[str, Any]) -> InkFlowProject:
        value = params.get("project_root")
        if not value:
            raise ValueError("请求缺少 project_root。")
        return InkFlowProject(Path(str(value)).resolve())

    @staticmethod
    def _engine(root: Path) -> InkFlowEngine:
        settings = Settings.from_env(root)
        return InkFlowEngine(create_provider(settings), settings)


def _usage_overview(project: InkFlowProject, settings: Settings) -> dict[str, Any]:
    prompt_tokens = 0
    completion_tokens = 0
    calls = 0
    for event_path in (project.internal / "runs").glob("*/events.jsonl"):
        try:
            lines = event_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                usage = dict(json.loads(line).get("metadata", {}).get("usage") or {})
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not usage:
                continue
            calls += 1
            prompt_tokens += int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            completion_tokens += int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    estimated_cost = (
        prompt_tokens * settings.input_price_per_million
        + completion_tokens * settings.output_price_per_million
    ) / 1_000_000
    return {
        "calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated_cost": round(estimated_cost, 6),
        "currency": "CNY",
        "pricing_configured": settings.input_price_per_million > 0 or settings.output_price_per_million > 0,
    }


class JsonLineServer:
    def __init__(self) -> None:
        self.instance_id = f"server-{os.getpid()}-{uuid.uuid4().hex}"
        self.service = InkFlowAppService(self.instance_id)
        self.write_lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task[None]] = {}

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
                task = self.tasks.get(run_id)
                cancelled = bool(task and not task.done())
                if task and not task.done():
                    task.cancel()
                await self.write({"jsonrpc": "2.0", "id": request_id, "result": {"cancelled": cancelled}})
                continue
            run_id = str(params.get("run_id") or f"run-{uuid.uuid4().hex}")
            params["run_id"] = run_id
            task = asyncio.create_task(self.process(request_id, run_id, method, params))
            self.tasks[run_id] = task
            task.add_done_callback(lambda _task, key=run_id: self.tasks.pop(key, None))
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def process(self, request_id: str, run_id: str, method: str, params: dict[str, Any]) -> None:
        async def emit(event: dict[str, Any]) -> None:
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"run_id": run_id, **event},
                }
            )

        task_db = None
        if _should_track_task(method, params):
            try:
                project_root = params.get("project_root")
                if project_root:
                    task_db = StudioService(InkFlowProject(Path(str(project_root)).resolve())).db
                    task_db.start_task(
                        run_id,
                        owner_id=self.instance_id,
                        method=method,
                        params=_task_params(method, params),
                    )
            except Exception:
                task_db = None
        try:
            await emit({"type": "run.started", "method": method, "summary": "任务已进入墨流"})
            result = await self.service.dispatch(method, params, emit)
            if task_db is not None:
                task_db.finish_task(run_id, status="completed", summary=_visible_result_summary(result))
            await self.write({"jsonrpc": "2.0", "id": request_id, "result": result})
            await emit({"type": "run.completed", "method": method, "summary": "任务已完成"})
        except asyncio.CancelledError:
            if task_db is not None:
                task_db.finish_task(run_id, status="cancelled", summary="用户已取消任务。")
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": "cancelled", "message": "任务已取消。"},
                }
            )
            await emit({"type": "run.cancelled", "method": method, "summary": "任务已取消"})
        except Exception as exc:
            if task_db is not None:
                task_db.finish_task(run_id, status="failed", error_message=str(exc))
            await self.write({"jsonrpc": "2.0", "id": request_id, "error": _error_payload(exc)})
            await emit({"type": "run.failed", "method": method, "summary": str(exc)[:500]})

    async def write(self, value: dict[str, Any]) -> None:
        text = json.dumps(value, ensure_ascii=False, default=str)
        async with self.write_lock:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()


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
    if isinstance(result, dict):
        for key in ("reply", "help", "gate", "message", "summary", "next_action"):
            if result.get(key):
                return str(result[key])[:300]
        if result.get("result") and isinstance(result["result"], dict):
            nested = result["result"]
            for key in ("summary", "message", "status"):
                if nested.get(key):
                    return str(nested[key])[:300]
    return "工作流返回了可查看结果。"


def _list_batch_summaries(project: InkFlowProject) -> list[dict[str, Any]]:
    """Read visible batch state for the desktop board; malformed files stay invisible."""

    folder = project.internal / "batches"
    if not folder.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(folder.glob("batch-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:30]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not value.get("batch_id"):
                continue
            entries = value.get("chapters") if isinstance(value.get("chapters"), list) else []
            result.append(
                {
                    "batch_id": str(value["batch_id"]),
                    "status": str(value.get("status") or "unknown"),
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


def _task_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """只记录恢复任务所需的小参数，不复制正文、API Key 或模型输出。"""

    allowed_by_method = {
        "conversation.send": {"message"},
        "workflow.run": {
            "action",
            "chapter_no",
            "start_chapter_no",
            "end_chapter_no",
            "instruction",
            "max_revision_rounds",
            "batch_id",
            "label",
            "checkpoint_id",
            "boundary_chapter",
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
    if isinstance(exc, ValidationError):
        return {"code": "validation_error", "message": "输入或模型输出格式不完整。", "details": exc.errors()}
    if isinstance(exc, InkFlowError):
        return {"code": exc.__class__.__name__, "message": str(exc)}
    return {
        "code": exc.__class__.__name__,
        "message": str(exc) or "墨流遇到未知错误。",
        "details": traceback.format_exc(limit=5) if os.getenv("INKFLOW_DEBUG") == "1" else None,
    }


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
        run_id = str(params.get("run_id") or "run-once")
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
