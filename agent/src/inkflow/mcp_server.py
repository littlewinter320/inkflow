from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations

from . import __version__
from .config import Settings
from .engine import InkFlowEngine
from .project import InkFlowProject
from .project_lock import project_write_lock, project_write_lock_sync
from .provider import create_provider
from .references import ReferenceService
from .schemas import BookBrief
from .studio import StudioService


mcp = FastMCP("墨流 InkFlow", log_level="WARNING")
# FastMCP defaults this field to the Python MCP package version, which makes
# clients show (for example) 1.29.1 instead of the installed InkFlow version.
mcp._mcp_server.version = __version__

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
MODEL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
CANON_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
RECOVERY_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
EXTERNAL_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _workspace() -> Path:
    return Path(os.getenv("INKFLOW_WORKSPACE", os.getcwd())).resolve()


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else _workspace()


def _engine(root: Path) -> InkFlowEngine:
    settings = Settings.from_env(root)
    return InkFlowEngine(create_provider(settings), settings)


@mcp.tool(annotations=LOCAL_WRITE)
def novel_project_create(
    project_root: str,
    title: str,
    genre: str,
    premise: str,
    protagonist: str,
    target_audience: str = "中文网文读者",
    core_selling_point: str = "",
    target_chapter_words: int = 3000,
    estimated_chapters: int = 200,
    estimated_volumes: int = 6,
    user_rules: list[str] | None = None,
) -> dict[str, Any]:
    """创建一个墨流小说项目，但不调用模型。"""

    root = _root(project_root)
    brief = BookBrief(
        title=title,
        genre=genre,
        premise=premise,
        protagonist=protagonist,
        target_audience=target_audience,
        core_selling_point=core_selling_point,
        target_chapter_words=target_chapter_words,
        estimated_chapters=estimated_chapters,
        estimated_volumes=estimated_volumes,
        user_rules=user_rules or [],
    )
    return _engine(root).create_project(root, brief)


@mcp.tool(annotations=READ_ONLY)
def novel_project_status(project_root: str | None = None) -> dict[str, Any]:
    """读取当前项目的规划、章节、事实和未结线索统计。"""

    root = _root(project_root)
    return _engine(root).status(root)


@mcp.tool(annotations=EXTERNAL_READ)
async def novel_provider_balance(project_root: str | None = None) -> dict[str, Any]:
    """读取模型账户的 CNY 余额；不会把 API Key 写入项目或 Trace。"""

    root = _root(project_root)
    return await _engine(root).balance()


@mcp.tool(annotations=READ_ONLY)
def novel_checkpoint_list(limit: int = 50, project_root: str | None = None) -> dict[str, Any]:
    """列出不可变检查点及其父节点、分支和已接受章节边界。"""

    root = _root(project_root)
    return _engine(root).checkpoint_list(root, limit)


@mcp.tool(annotations=LOCAL_WRITE)
def novel_checkpoint_create(
    label: str = "用户手动检查点",
    project_root: str | None = None,
) -> dict[str, Any]:
    """为当前 SQLite 正史和托管 Markdown 投影创建一致、不可变的恢复点。"""

    root = _root(project_root)
    return _engine(root).checkpoint_create(root, label)


@mcp.tool(annotations=READ_ONLY)
def novel_task_history(limit: int = 30, project_root: str | None = None) -> dict[str, Any]:
    """读取桌面/编辑器长任务的完成、失败、中断与取消记录；不包含模型思维链。"""

    project = InkFlowProject(_root(project_root))
    return {"tasks": StudioService(project).db.list_tasks(limit)}


@mcp.tool(annotations=EXTERNAL_READ)
async def novel_reference_search(
    query: str,
    limit: int = 6,
    project_root: str | None = None,
) -> dict[str, Any]:
    """搜索公开写作资料并返回标题、摘要和来源；不会自动导入，也不会上传小说正文、正史或模型密钥。"""

    project = InkFlowProject(_root(project_root))
    return await ReferenceService(project).search_public(query, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def novel_rollback_preview(
    checkpoint_id: str | None = None,
    boundary_chapter: int | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """只预览回退将创建、覆盖和移入回收区的文件，并返回一次性确认码。"""

    root = _root(project_root)
    return _engine(root).rollback_preview(
        root,
        checkpoint_id=checkpoint_id,
        boundary_chapter=boundary_chapter,
    )


@mcp.tool(annotations=RECOVERY_WRITE)
def novel_rollback_restore(
    confirmation_token: str,
    checkpoint_id: str | None = None,
    boundary_chapter: int | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """确认后先创建当前安全点，再恢复目标检查点并开启新分支；不会删除旧历史。"""

    root = _root(project_root)
    return _engine(root).rollback_restore(
        root,
        checkpoint_id=checkpoint_id,
        boundary_chapter=boundary_chapter,
        confirmation_token=confirmation_token,
    )


@mcp.tool(annotations=MODEL_WRITE)
async def novel_plan_generate(project_root: str | None = None) -> dict[str, Any]:
    """调用写作 Agent 的 PLAN 模式，生成全书、卷、篇章和章节卡四级规划。"""

    root = _root(project_root)
    return await _engine(root).generate_plan(root)


@mcp.tool(annotations=MODEL_WRITE)
async def novel_plan_advance(
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """当前篇章全部进入正史后，基于实际结果细化紧邻的下一篇章；调用方应先取得用户对未来规划调整的明确确认。"""

    root = _root(project_root)
    return await _engine(root).advance_plan(root, instruction=instruction)


@mcp.tool(annotations=MODEL_WRITE)
async def novel_plan_brief(
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """生成紧邻下一篇章的公开判断单，展示可复核依据和取舍；不改正式 PLAN、正文或 SQLite。"""

    root = _root(project_root)
    return await _engine(root).preview_next_arc(root, instruction=instruction)


@mcp.tool(annotations=READ_ONLY)
def novel_plan_preview(
    start_chapter: int,
    end_chapter: int,
    project_root: str | None = None,
) -> dict[str, Any]:
    """一次集中查看 1～20 张已有章节卡；只读、不调用模型、不修改规划。"""

    root = _root(project_root)
    return _engine(root).preview_plan_range(root, start_chapter, end_chapter)


@mcp.tool(annotations=CANON_WRITE)
async def novel_continue_until(
    target_characters: int | None = None,
    instruction: str = "",
    target_chapter_no: int | None = None,
    max_revision_rounds: int = 1,
    project_root: str | None = None,
) -> dict[str, Any]:
    """循环执行写作、审查、必要修订、重审和记忆提交，直到正史达到字符目标、结束章节或门禁停止。"""

    root = _root(project_root)
    return await _engine(root).continue_until(
        root,
        target_characters,
        instruction=instruction,
        target_chapter_no=target_chapter_no,
        max_revision_rounds=max_revision_rounds,
    )


@mcp.tool(annotations=MODEL_WRITE)
async def novel_batch_draft(
    start_chapter_no: int,
    end_chapter_no: int,
    instruction: str = "",
    max_revision_rounds: int = 2,
    project_root: str | None = None,
) -> dict[str, Any]:
    """生成临时批次草稿；逐章即时审查，但不调用 Memory Keeper 或提交正史。"""

    root = _root(project_root)
    return await _engine(root).draft_batch(
        root,
        start_chapter_no,
        end_chapter_no,
        instruction=instruction,
        max_revision_rounds=max_revision_rounds,
    )


@mcp.tool(annotations=MODEL_WRITE)
async def novel_batch_repair(
    batch_id: str,
    start_chapter_no: int | None = None,
    end_chapter_no: int | None = None,
    instruction: str = "",
    max_additional_revision_rounds: int = 1,
    project_root: str | None = None,
) -> dict[str, Any]:
    """修订一个临时批次的连续章节，逐章重审并同步同一批次清单；绝不提交正史。"""

    root = _root(project_root)
    return await _engine(root).repair_batch(
        root,
        batch_id,
        start_chapter_no=start_chapter_no,
        end_chapter_no=end_chapter_no,
        instruction=instruction,
        max_additional_revision_rounds=max_additional_revision_rounds,
    )


@mcp.tool(annotations=CANON_WRITE)
async def novel_batch_accept(batch_id: str, project_root: str | None = None) -> dict[str, Any]:
    """经用户明确确认后，接收已通过即时审查的批次并依次提交正史。"""

    root = _root(project_root)
    return await _engine(root).accept_batch(root, batch_id)


@mcp.tool(annotations=MODEL_WRITE)
async def novel_arc_audit(
    start_chapter_no: int,
    end_chapter_no: int,
    batch_id: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """复审一段章节与篇章规划的对齐程度，只生成报告；绝不修改章节、规划或正史。"""

    root = _root(project_root)
    return await _engine(root).audit_range(
        root,
        start_chapter_no,
        end_chapter_no,
        batch_id=batch_id,
    )


@mcp.tool(annotations=READ_ONLY)
def novel_context_build(
    chapter_no: int,
    task: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """构建模型唯一可见的 Context Packet，用于诊断前后逻辑。"""

    root = _root(project_root)
    return _engine(root).build_context(root, chapter_no, task or None)


@mcp.tool(annotations=MODEL_WRITE)
async def novel_chapter_write(
    chapter_no: int,
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """按已批准章节卡生成草稿；缺少四级规划时门禁会拒绝。"""

    root = _root(project_root)
    return await _engine(root).write_chapter(root, chapter_no, instruction)


@mcp.tool(annotations=MODEL_WRITE)
async def novel_chapter_review(chapter_no: int, project_root: str | None = None) -> dict[str, Any]:
    """对完整章节执行代码层和独立审查 Agent 检查。"""

    root = _root(project_root)
    return await _engine(root).review_chapter(root, chapter_no)


@mcp.tool(annotations=MODEL_WRITE)
async def novel_chapter_revise(
    chapter_no: int,
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """让写作 Agent 读取当前草稿与同版本审查报告，完成定点修订；随后必须重新审查。"""

    root = _root(project_root)
    return await _engine(root).revise_chapter(root, chapter_no, instruction)


@mcp.tool(annotations=CANON_WRITE)
async def novel_chapter_accept(
    chapter_no: int,
    project_root: str | None = None,
) -> dict[str, Any]:
    """经用户明确确认后接受草稿，调用记忆 Agent 并把章节与事实事务提交为正史；不会绕过门禁。"""

    root = _root(project_root)
    return await _engine(root).accept_chapter(root, chapter_no, force=False)


@mcp.tool(annotations=LOCAL_WRITE)
def novel_reference_import(source_path: str, project_root: str | None = None) -> dict[str, Any]:
    """导入本地 TXT/MD 等文本参考资料。"""

    project = InkFlowProject(_root(project_root))
    with project_write_lock_sync(project.root):
        return ReferenceService(project).import_text(source_path)


@mcp.tool(annotations=EXTERNAL_READ)
async def novel_reference_fetch(url: str, project_root: str | None = None) -> dict[str, Any]:
    """读取无需登录即可访问的公开网页并保存清洗文本。"""

    project = InkFlowProject(_root(project_root))
    async with project_write_lock(project.root):
        return await ReferenceService(project).fetch_url(url)


@mcp.tool(annotations=EXTERNAL_READ)
async def novel_reference_fetch_fanqie(url: str, project_root: str | None = None) -> dict[str, Any]:
    """抓取无需登录的番茄公开页并标记适配器版本；不会绕过访问控制或登录。"""

    project = InkFlowProject(_root(project_root))
    async with project_write_lock(project.root):
        return await ReferenceService(project).fetch_fanqie_public(url)


@mcp.tool(annotations=READ_ONLY)
def novel_reference_analyze(reference_id: str, project_root: str | None = None) -> dict[str, Any]:
    """为已导入参考文本生成确定性节奏与文本特征卡。"""

    project = InkFlowProject(_root(project_root))
    with project_write_lock_sync(project.root):
        return ReferenceService(project).analyze(reference_id)


@mcp.tool(annotations=READ_ONLY)
def novel_reference_list(project_root: str | None = None) -> list[dict[str, Any]]:
    """列出已导入参考资料及其分析状态，不读取或返回全文。"""

    project = InkFlowProject(_root(project_root))
    return ReferenceService(project).list_references()


@mcp.tool(annotations=READ_ONLY)
def novel_file_read(relative_path: str, project_root: str | None = None) -> str:
    """读取小说工作区内的用户文件。"""

    return InkFlowProject(_root(project_root)).read_file(relative_path)


@mcp.tool(annotations=LOCAL_WRITE)
def novel_file_write(
    relative_path: str,
    content: str,
    overwrite: bool = True,
    project_root: str | None = None,
) -> dict[str, Any]:
    """在小说工作区内创建或覆盖用户文件。"""

    project = InkFlowProject(_root(project_root))
    path = project.write_file(relative_path, content, overwrite=overwrite)
    return {"path": str(path), "characters": len(content)}


@mcp.tool(annotations=RECOVERY_WRITE)
def novel_file_delete(
    relative_path: str,
    permanent: bool = False,
    project_root: str | None = None,
) -> dict[str, Any]:
    """删除项目内文件；默认移动到可恢复的 .inkflow/trash。"""

    return InkFlowProject(_root(project_root)).delete_file(relative_path, permanent=permanent)


@mcp.tool(annotations=RECOVERY_WRITE)
def novel_process_powershell(
    command: str,
    timeout_seconds: int = 60,
    project_root: str | None = None,
) -> dict[str, Any]:
    """用户在设置中明确开启后，以小说项目为 cwd 执行 PowerShell。"""

    return InkFlowProject(_root(project_root)).run_powershell(command, timeout_seconds)


@mcp.tool(annotations=READ_ONLY)
def novel_trace_read(trace_id: str, project_root: str | None = None) -> str:
    """读取一次运行的可折叠过程记录。"""

    project = InkFlowProject(_root(project_root))
    if not trace_id or any(char in trace_id for char in "\\/.."):
        raise ValueError("trace_id 不合法")
    path = project.internal / "runs" / trace_id / "trace.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 trace：{trace_id}")
    return path.read_text(encoding="utf-8")


@mcp.tool(annotations=READ_ONLY)
def novel_document_open(relative_path: str, project_root: str | None = None) -> dict[str, Any]:
    """读取用户文档、统计、行级批注、版本与正史保护状态。"""

    project = InkFlowProject(_root(project_root))
    return StudioService(project).read_document(relative_path)


@mcp.tool(annotations=LOCAL_WRITE)
def novel_document_save(
    relative_path: str,
    content: str,
    expected_hash: str | None = None,
    project_root: str | None = None,
) -> dict[str, Any]:
    """安全保存草稿或普通文档并留版本；已验收正文只建立未应用修改提案。"""

    project = InkFlowProject(_root(project_root))
    return StudioService(project).save_document(
        relative_path,
        content,
        expected_hash=expected_hash,
        source="mcp_editor",
    )


@mcp.tool(annotations=LOCAL_WRITE)
def novel_document_annotate(
    relative_path: str,
    start_offset: int,
    end_offset: int,
    comment: str,
    project_root: str | None = None,
) -> dict[str, Any]:
    """给文档精确字符区间添加可重定位批注，不直接修改正文。"""

    project = InkFlowProject(_root(project_root))
    return StudioService(project).create_annotation(relative_path, start_offset, end_offset, comment)


@mcp.tool(annotations=READ_ONLY)
def novel_document_search(
    query: str,
    limit: int = 100,
    project_root: str | None = None,
) -> dict[str, Any]:
    """在书籍设定、规划、章节与审查 Markdown 中进行本地全文搜索。"""

    project = InkFlowProject(_root(project_root))
    return StudioService(project).search(query, limit)


@mcp.tool(annotations=READ_ONLY)
def novel_story_bible(project_root: str | None = None) -> dict[str, Any]:
    """读取人工故事圣经以及 Memory Keeper 已提交的事实与开放线索。"""

    project = InkFlowProject(_root(project_root))
    studio = StudioService(project)
    return {
        "manual": studio.db.list_bible_entries(),
        "canon_facts": project.db.current_facts(),
        "open_threads": project.db.open_threads(),
    }


@mcp.tool(annotations=LOCAL_WRITE)
def novel_preference_remember(
    text: str,
    strength: str = "weak",
    scope: str = "project",
    project_root: str | None = None,
) -> dict[str, Any]:
    """记录用户明确给出的长期硬规则或弱偏好；不会从普通闲聊中自动猜测。"""

    project = InkFlowProject(_root(project_root))
    with project_write_lock_sync(project.root):
        item = project.db.upsert_preference(text=text, strength=strength, scope=scope, source="user")
        project.db.record_learning_event("preference_changed", item)
        return item


@mcp.tool(annotations=READ_ONLY)
def novel_collaboration_messages(
    chapter_no: int | None = None,
    active_only: bool = False,
    limit: int = 50,
    project_root: str | None = None,
) -> dict[str, Any]:
    """查看四个 Agent 之间带版本与证据的任务、交接、异议和记忆同步消息。"""

    project = InkFlowProject(_root(project_root))
    return {"messages": project.db.list_collaboration_messages(chapter_no=chapter_no, active_only=active_only, limit=limit)}


@mcp.resource(
    "inkflow://project/book",
    title="墨流书籍契约",
    description="当前小说的 BOOK.md；它是书籍契约的人类可读投影。",
    mime_type="text/markdown",
)
def resource_book() -> str:
    return InkFlowProject(_workspace()).read_file("BOOK.md")


@mcp.resource(
    "inkflow://project/plan",
    title="墨流当前规划",
    description="当前全书、卷、篇章与章节卡规划。",
    mime_type="text/markdown",
)
def resource_plan() -> str:
    return InkFlowProject(_workspace()).read_file("PLAN.md")


@mcp.resource(
    "inkflow://project/state",
    title="墨流正史状态",
    description="Memory Keeper 从已验收正文提交的正史状态投影。",
    mime_type="text/markdown",
)
def resource_state() -> str:
    return InkFlowProject(_workspace()).read_file("STATE.md")


@mcp.resource(
    "inkflow://chapter/{chapter_no}",
    title="墨流章节",
    description="按章节号读取正史正文，尚未验收时读取当前草稿。",
    mime_type="text/markdown",
)
def resource_chapter(chapter_no: int) -> str:
    project = InkFlowProject(_workspace())
    accepted = f"chapters/chapter_{chapter_no:05d}.md"
    draft = f"chapters/chapter_{chapter_no:05d}.draft.md"
    return project.read_file(accepted if (project.root / accepted).is_file() else draft)


@mcp.prompt(title="创建并规划一本墨流小说")
def prompt_start_novel(idea: str, chapter_words: int = 3000) -> str:
    return (
        "请先用自然语言和用户确认书名、题材、故事前提、主角与硬规则；信息够用后调用 "
        "novel_project_create，再调用 novel_plan_generate。不要替用户自动验收正文。\n\n"
        f"用户灵感：{idea}\n单章目标：约 {chapter_words} 字。"
    )


@mcp.prompt(title="批量写作并逐章审查")
def prompt_batch_draft(start_chapter: int, end_chapter: int, instruction: str = "") -> str:
    return (
        f"请调用 novel_batch_draft 生成第 {start_chapter}～{end_chapter} 章。"
        "逐章 Writer→Reviewer→必要时最多两轮 Writer 修订与重审；批次完成前不提交正史。"
        f"用户补充：{instruction or '无'}"
    )


@mcp.prompt(title="篇章复审与后续规划讨论")
def prompt_arc_audit(start_chapter: int, end_chapter: int) -> str:
    return (
        f"调用 novel_arc_audit 复审第 {start_chapter}～{end_chapter} 章，并把实际正文与篇章规划逐项对照。"
        "先把偏差、依据、扣分和未来影响展示给用户；需要改未来规划或已验收正文时先取得确认。"
    )


async def _run_stdio_server() -> None:
    # The MCP SDK normally creates a second TextIOWrapper around stdout. In a
    # PyInstaller one-file executable that wrapper can close the original
    # stream during shutdown and print a misleading "I/O operation on closed
    # file" traceback. InkFlow has already configured UTF-8 streams below, so
    # pass non-owning AnyIO wrappers and keep the process handles open.
    stdin = anyio.wrap_file(sys.stdin)
    stdout = anyio.wrap_file(sys.stdout)
    async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


def main() -> None:
    # MCP JSON-RPC is UTF-8. Windows may otherwise inherit a legacy console code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    anyio.run(_run_stdio_server)


if __name__ == "__main__":
    main()
