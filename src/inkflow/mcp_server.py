from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .engine import InkFlowEngine
from .project import InkFlowProject
from .provider import DeepSeekProvider
from .references import ReferenceService
from .schemas import BookBrief


mcp = FastMCP("墨流 InkFlow")


def _workspace() -> Path:
    return Path(os.getenv("INKFLOW_WORKSPACE", os.getcwd())).resolve()


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else _workspace()


def _engine(root: Path) -> InkFlowEngine:
    settings = Settings.from_env(root)
    return InkFlowEngine(DeepSeekProvider(settings), settings)


@mcp.tool()
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


@mcp.tool()
def novel_project_status(project_root: str | None = None) -> dict[str, Any]:
    """读取当前项目的规划、章节、事实和未结线索统计。"""

    root = _root(project_root)
    return _engine(root).status(root)


@mcp.tool()
async def novel_provider_balance(project_root: str | None = None) -> dict[str, Any]:
    """读取模型账户的 CNY 余额；不会把 API Key 写入项目或 Trace。"""

    root = _root(project_root)
    return await _engine(root).balance()


@mcp.tool()
def novel_checkpoint_list(limit: int = 50, project_root: str | None = None) -> dict[str, Any]:
    """列出不可变检查点及其父节点、分支和已接受章节边界。"""

    root = _root(project_root)
    return _engine(root).checkpoint_list(root, limit)


@mcp.tool()
def novel_checkpoint_create(
    label: str = "用户手动检查点",
    project_root: str | None = None,
) -> dict[str, Any]:
    """为当前 SQLite 正史和托管 Markdown 投影创建一致、不可变的恢复点。"""

    root = _root(project_root)
    return _engine(root).checkpoint_create(root, label)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def novel_plan_generate(project_root: str | None = None) -> dict[str, Any]:
    """调用写作 Agent 的 PLAN 模式，生成全书、卷、篇章和章节卡四级规划。"""

    root = _root(project_root)
    return await _engine(root).generate_plan(root)


@mcp.tool()
async def novel_plan_advance(
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """当前篇章全部进入正史后，基于实际结果细化紧邻的下一篇章；调用方应先取得用户对未来规划调整的明确确认。"""

    root = _root(project_root)
    return await _engine(root).advance_plan(root, instruction=instruction)


@mcp.tool()
async def novel_plan_brief(
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """生成紧邻下一篇章的公开判断单，展示可复核依据和取舍；不改正式 PLAN、正文或 SQLite。"""

    root = _root(project_root)
    return await _engine(root).preview_next_arc(root, instruction=instruction)


@mcp.tool()
def novel_plan_preview(
    start_chapter: int,
    end_chapter: int,
    project_root: str | None = None,
) -> dict[str, Any]:
    """一次集中查看 1～20 张已有章节卡；只读、不调用模型、不修改规划。"""

    root = _root(project_root)
    return _engine(root).preview_plan_range(root, start_chapter, end_chapter)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def novel_batch_accept(batch_id: str, project_root: str | None = None) -> dict[str, Any]:
    """接收一个已通过即时审查的批次，并按连续前缀依次提交正史。"""

    root = _root(project_root)
    return await _engine(root).accept_batch(root, batch_id)


@mcp.tool()
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


@mcp.tool()
def novel_context_build(
    chapter_no: int,
    task: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """构建模型唯一可见的 Context Packet，用于诊断前后逻辑。"""

    root = _root(project_root)
    return _engine(root).build_context(root, chapter_no, task or None)


@mcp.tool()
async def novel_chapter_write(
    chapter_no: int,
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """按已批准章节卡生成草稿；缺少四级规划时门禁会拒绝。"""

    root = _root(project_root)
    return await _engine(root).write_chapter(root, chapter_no, instruction)


@mcp.tool()
async def novel_chapter_review(chapter_no: int, project_root: str | None = None) -> dict[str, Any]:
    """对完整章节执行代码层和独立审查 Agent 检查。"""

    root = _root(project_root)
    return await _engine(root).review_chapter(root, chapter_no)


@mcp.tool()
async def novel_chapter_revise(
    chapter_no: int,
    instruction: str = "",
    project_root: str | None = None,
) -> dict[str, Any]:
    """让写作 Agent 读取当前草稿与同版本审查报告，完成定点修订；随后必须重新审查。"""

    root = _root(project_root)
    return await _engine(root).revise_chapter(root, chapter_no, instruction)


@mcp.tool()
async def novel_chapter_accept(
    chapter_no: int,
    force: bool = False,
    project_root: str | None = None,
) -> dict[str, Any]:
    """接受草稿，调用记忆 Agent 并把章节与事实事务提交为正史。"""

    root = _root(project_root)
    return await _engine(root).accept_chapter(root, chapter_no, force=force)


@mcp.tool()
def novel_reference_import(source_path: str, project_root: str | None = None) -> dict[str, Any]:
    """导入本地 TXT/MD 等文本参考资料。"""

    project = InkFlowProject(_root(project_root))
    return ReferenceService(project).import_text(source_path)


@mcp.tool()
async def novel_reference_fetch(url: str, project_root: str | None = None) -> dict[str, Any]:
    """读取无需登录即可访问的公开网页并保存清洗文本。"""

    project = InkFlowProject(_root(project_root))
    return await ReferenceService(project).fetch_url(url)


@mcp.tool()
async def novel_reference_fetch_fanqie(url: str, project_root: str | None = None) -> dict[str, Any]:
    """抓取无需登录的番茄公开页并标记适配器版本；不会绕过访问控制或登录。"""

    project = InkFlowProject(_root(project_root))
    return await ReferenceService(project).fetch_fanqie_public(url)


@mcp.tool()
def novel_reference_analyze(reference_id: str, project_root: str | None = None) -> dict[str, Any]:
    """为已导入参考文本生成确定性节奏与文本特征卡。"""

    project = InkFlowProject(_root(project_root))
    return ReferenceService(project).analyze(reference_id)


@mcp.tool()
def novel_file_read(relative_path: str, project_root: str | None = None) -> str:
    """读取小说工作区内的用户文件。"""

    return InkFlowProject(_root(project_root)).read_file(relative_path)


@mcp.tool()
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


@mcp.tool()
def novel_file_delete(
    relative_path: str,
    permanent: bool = False,
    project_root: str | None = None,
) -> dict[str, Any]:
    """删除项目内文件；默认移动到可恢复的 .inkflow/trash。"""

    return InkFlowProject(_root(project_root)).delete_file(relative_path, permanent=permanent)


@mcp.tool()
def novel_process_powershell(
    command: str,
    timeout_seconds: int = 60,
    project_root: str | None = None,
) -> dict[str, Any]:
    """以小说项目为 cwd 执行 PowerShell，并返回退出码与截断输出。"""

    return InkFlowProject(_root(project_root)).run_powershell(command, timeout_seconds)


@mcp.tool()
def novel_trace_read(trace_id: str, project_root: str | None = None) -> str:
    """读取一次运行的可折叠过程记录。"""

    project = InkFlowProject(_root(project_root))
    if not trace_id or any(char in trace_id for char in "\\/.."):
        raise ValueError("trace_id 不合法")
    path = project.internal / "runs" / trace_id / "trace.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 trace：{trace_id}")
    return path.read_text(encoding="utf-8")


def main() -> None:
    # MCP JSON-RPC is UTF-8. Windows may otherwise inherit a legacy console code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
