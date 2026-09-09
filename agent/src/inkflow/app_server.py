from __future__ import annotations

import argparse
import asyncio
import json
import os
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
from .project import InkFlowProject
from .provider import DeepSeekProvider
from .references import ReferenceService
from .schemas import BookBrief, NovelIdeaBundle, PromptOptimization, ProviderProbe
from .studio import StudioService
from .terminal_session import TerminalSession


EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class InkFlowAppService:
    """桌面端与编辑器共享的本地应用服务。"""

    def __init__(self, instance_id: str | None = None) -> None:
        self.instance_id = instance_id or f"server-{os.getpid()}-{uuid.uuid4().hex}"

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
                    "formal_agents": ["Writer", "Reviewer", "Memory Keeper"],
                    "raw_chain_of_thought": False,
                },
                "provider": api_key_status(),
            }
        if method == "provider.status":
            settings = Settings.from_env(params.get("workspace_root"))
            return {
                **api_key_status(),
                "base_url": settings.base_url,
                "model": settings.model,
                "reasoning_effort": settings.reasoning_effort,
                "context_soft_tokens": settings.context_soft_tokens,
                "context_hard_tokens": settings.context_hard_tokens,
                "max_output_tokens": settings.max_output_tokens,
                "inquiry_frequency": settings.inquiry_frequency,
                "agent_generation": settings.agent_generation,
            }
        if method == "provider.configure":
            key = str(params.get("api_key", "")).strip()
            if key:
                save_api_key_to_keyring(key)
            allowed = {
                name: params[name]
                for name in (
                    "base_url",
                    "model",
                    "reasoning_effort",
                    "context_soft_tokens",
                    "context_hard_tokens",
                    "max_output_tokens",
                    "inquiry_frequency",
                    "agent_generation",
                )
                if name in params and params[name] not in (None, "")
            }
            if allowed:
                save_user_settings(allowed)
            return {"configured": True, **api_key_status(), **allowed}
        if method == "provider.test":
            settings = Settings.from_env(params.get("workspace_root"))
            await emit({"type": "provider.testing", "summary": "正在验证密钥、接口与模型名称"})
            result = await DeepSeekProvider(settings).generate_json(
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
        if method == "prompt.optimize":
            prompt = str(params.get("prompt") or "").strip()
            if not prompt:
                raise ValueError("请先输入需要优化的提示词。")
            if len(prompt) > 8_000:
                raise ValueError("提示词超过 8000 字，请先缩小范围后再优化。")
            settings = Settings.from_env(params.get("workspace_root") or params.get("project_root"))
            await emit({"type": "prompt.optimizing", "summary": "正在核对目标、约束与交付格式"})
            result = await DeepSeekProvider(settings).generate_json(
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
                result = await DeepSeekProvider(settings).generate_json(
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
            studio.db.reconcile_interrupted_tasks(self.instance_id)
            return {"dashboard": studio.dashboard(), "tree": studio.tree()}
        if method == "conversation.history":
            return {"entries": TerminalSession.history(project.root, int(params.get("limit", 100)))}
        if method == "project.status":
            return studio.dashboard()
        if method == "project.tree":
            return studio.tree()
        if method == "document.read":
            return studio.read_document(str(params["relative_path"]))
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
            return studio.db.set_annotation_status(str(params["annotation_id"]), str(params["status"]))
        if method == "document.search":
            return studio.search(str(params["query"]), int(params.get("limit", 100)))
        if method == "chapter.workspace":
            return studio.chapter_workspace(int(params["chapter_no"]))
        if method == "scene_note.upsert":
            return studio.db.upsert_scene_note(
                int(params["chapter_no"]), int(params["scene_no"]), dict(params.get("data") or {})
            )
        if method == "bible.list":
            return {
                "manual": studio.db.list_bible_entries(),
                "canon_facts": project.db.current_facts(),
                "open_threads": project.db.open_threads(),
            }
        if method == "bible.upsert":
            return studio.db.upsert_bible_entry(
                entry_id=params.get("entry_id"),
                kind=str(params["kind"]),
                name=str(params["name"]),
                aliases=list(params.get("aliases") or []),
                data=dict(params.get("data") or {}),
            )
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
            if "fanqienovel.com" in url:
                return await ReferenceService(project).fetch_fanqie_public(url)
            return await ReferenceService(project).fetch_url(url)
        if method == "reference.analyze":
            return ReferenceService(project).analyze(str(params["reference_id"]))
        if method == "task.list":
            return {"tasks": studio.db.list_tasks(int(params.get("limit", 50)))}
        if method == "task.dismiss":
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
            result = await engine.write_chapter(
                project.root, int(params["chapter_no"]), str(params.get("instruction") or "")
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
        return InkFlowEngine(DeepSeekProvider(settings), settings)


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
