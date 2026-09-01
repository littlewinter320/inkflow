from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import Settings, save_api_key_to_keyring
from .engine import InkFlowEngine
from .errors import InkFlowError
from .provider import DeepSeekProvider
from .schemas import BookBrief
from .terminal_session import TerminalSession


def _engine(root: str | Path) -> InkFlowEngine:
    settings = Settings.from_env(root)
    return InkFlowEngine(DeepSeekProvider(settings), settings)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inkflow", description="墨流（InkFlow）开发与诊断入口")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure-key", help="把 DeepSeek Key 写入系统凭据库")
    configure.set_defaults(handler=_configure_key)

    create = sub.add_parser("create", help="创建小说项目")
    create.add_argument("root")
    create.add_argument("--title", required=True)
    create.add_argument("--genre", required=True)
    create.add_argument("--premise", required=True)
    create.add_argument("--protagonist", required=True)
    create.add_argument("--target-words", type=int, default=3000)
    create.add_argument("--chapters", type=int, default=200)
    create.add_argument("--volumes", type=int, default=6)
    create.set_defaults(handler=_create)

    for name, handler in (("plan", _plan), ("status", _status), ("balance", _balance)):
        item = sub.add_parser(name)
        item.add_argument("root")
        item.set_defaults(handler=handler)

    plan_advance = sub.add_parser("plan-advance", help="在当前篇章进入正史后细化下一篇章")
    plan_advance.add_argument("root")
    plan_advance.add_argument("--instruction", default="")
    plan_advance.set_defaults(handler=_plan_advance)

    plan_brief = sub.add_parser("plan-brief", help="生成下一篇章的公开判断单，不改正式规划")
    plan_brief.add_argument("root")
    plan_brief.add_argument("--instruction", default="")
    plan_brief.set_defaults(handler=_plan_brief)

    continue_until = sub.add_parser("continue-until", help="按完整三 Agent 门禁续写到指定正史字符数")
    continue_until.add_argument("root")
    continue_until.add_argument("--target-characters", type=int)
    continue_until.add_argument("--target-chapter", type=int)
    continue_until.add_argument("--max-revision-rounds", type=int, default=1)
    continue_until.add_argument("--instruction", default="")
    continue_until.set_defaults(handler=_continue_until)

    batch_draft = sub.add_parser("batch-draft", help="生成临时批次草稿并逐章即时审查")
    batch_draft.add_argument("root")
    batch_draft.add_argument("--start-chapter", type=int, required=True)
    batch_draft.add_argument("--end-chapter", type=int, required=True)
    batch_draft.add_argument("--instruction", default="")
    batch_draft.add_argument("--max-revision-rounds", type=int, default=2)
    batch_draft.set_defaults(handler=_batch_draft)

    batch_repair = sub.add_parser("batch-repair", help="修订既有临时批次并逐章重审")
    batch_repair.add_argument("root")
    batch_repair.add_argument("--batch-id", required=True)
    batch_repair.add_argument("--start-chapter", type=int)
    batch_repair.add_argument("--end-chapter", type=int)
    batch_repair.add_argument("--instruction", default="")
    batch_repair.add_argument("--max-additional-revision-rounds", type=int, default=1)
    batch_repair.set_defaults(handler=_batch_repair)

    batch_accept = sub.add_parser("batch-accept", help="接收一个已通过即时审查的临时批次")
    batch_accept.add_argument("root")
    batch_accept.add_argument("--batch-id", required=True)
    batch_accept.set_defaults(handler=_batch_accept)

    arc_audit = sub.add_parser("arc-audit", help="复审连续章节与篇章规划的对齐情况")
    arc_audit.add_argument("root")
    arc_audit.add_argument("--start-chapter", type=int, required=True)
    arc_audit.add_argument("--end-chapter", type=int, required=True)
    arc_audit.add_argument("--batch-id")
    arc_audit.set_defaults(handler=_arc_audit)

    checkpoint_list = sub.add_parser("checkpoint-list", help="列出检查点")
    checkpoint_list.add_argument("root")
    checkpoint_list.add_argument("--limit", type=int, default=50)
    checkpoint_list.set_defaults(handler=_checkpoint_list)

    checkpoint_create = sub.add_parser("checkpoint-create", help="创建手动检查点")
    checkpoint_create.add_argument("root")
    checkpoint_create.add_argument("--label", default="用户手动检查点")
    checkpoint_create.set_defaults(handler=_checkpoint_create)

    rollback_preview = sub.add_parser("rollback-preview", help="预览分支式回退影响")
    rollback_preview.add_argument("root")
    rollback_preview.add_argument("--checkpoint-id")
    rollback_preview.add_argument("--boundary-chapter", type=int)
    rollback_preview.set_defaults(handler=_rollback_preview)

    rollback_restore = sub.add_parser("rollback-restore", help="使用预览确认码执行分支式回退")
    rollback_restore.add_argument("root")
    rollback_restore.add_argument("--confirmation-token", required=True)
    rollback_restore.add_argument("--checkpoint-id")
    rollback_restore.add_argument("--boundary-chapter", type=int)
    rollback_restore.set_defaults(handler=_rollback_restore)

    for name, handler in (("write", _write), ("review", _review), ("revise", _revise), ("accept", _accept)):
        item = sub.add_parser(name)
        item.add_argument("root")
        item.add_argument("chapter", type=int)
        if name in {"write", "revise"}:
            item.add_argument("--instruction", default="")
        if name == "accept":
            item.add_argument("--force", action="store_true")
        item.set_defaults(handler=handler)

    mcp_parser = sub.add_parser("mcp", help="启动 stdio MCP Server")
    mcp_parser.add_argument("--workspace", default=os.getcwd())
    mcp_parser.set_defaults(handler=_mcp)

    chat = sub.add_parser("chat", help="在终端用自然语言驱动受门禁约束的墨流工作流")
    chat.add_argument("root")
    chat.add_argument("--once", help="处理一条自然语言请求后退出")
    chat.set_defaults(handler=_chat)
    return parser


def _configure_key(_: argparse.Namespace) -> dict[str, Any]:
    key = getpass.getpass("DeepSeek API Key（不会回显）：")
    save_api_key_to_keyring(key)
    return {"stored": True, "destination": "Windows Credential Manager / system keyring"}


def _create(args: argparse.Namespace) -> dict[str, Any]:
    brief = BookBrief(
        title=args.title,
        genre=args.genre,
        premise=args.premise,
        protagonist=args.protagonist,
        target_chapter_words=args.target_words,
        estimated_chapters=args.chapters,
        estimated_volumes=args.volumes,
    )
    return _engine(args.root).create_project(args.root, brief)


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).generate_plan(args.root))


def _plan_advance(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).advance_plan(args.root, instruction=args.instruction))


def _plan_brief(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).preview_next_arc(args.root, instruction=args.instruction))


def _write(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).write_chapter(args.root, args.chapter, args.instruction))


def _review(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).review_chapter(args.root, args.chapter))


def _revise(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).revise_chapter(args.root, args.chapter, args.instruction))


def _accept(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).accept_chapter(args.root, args.chapter, force=args.force))


def _status(args: argparse.Namespace) -> dict[str, Any]:
    return _engine(args.root).status(args.root)


def _balance(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).balance())


def _continue_until(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(
        _engine(args.root).continue_until(
            args.root,
            args.target_characters,
            instruction=args.instruction,
            target_chapter_no=args.target_chapter,
            max_revision_rounds=args.max_revision_rounds,
        )
    )


def _batch_draft(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(
        _engine(args.root).draft_batch(
            args.root,
            args.start_chapter,
            args.end_chapter,
            instruction=args.instruction,
            max_revision_rounds=args.max_revision_rounds,
        )
    )


def _batch_accept(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(_engine(args.root).accept_batch(args.root, args.batch_id))


def _batch_repair(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(
        _engine(args.root).repair_batch(
            args.root,
            args.batch_id,
            start_chapter_no=args.start_chapter,
            end_chapter_no=args.end_chapter,
            instruction=args.instruction,
            max_additional_revision_rounds=args.max_additional_revision_rounds,
        )
    )


def _arc_audit(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(
        _engine(args.root).audit_range(
            args.root,
            args.start_chapter,
            args.end_chapter,
            batch_id=args.batch_id,
        )
    )


def _checkpoint_list(args: argparse.Namespace) -> dict[str, Any]:
    return _engine(args.root).checkpoint_list(args.root, args.limit)


def _checkpoint_create(args: argparse.Namespace) -> dict[str, Any]:
    return _engine(args.root).checkpoint_create(args.root, args.label)


def _rollback_preview(args: argparse.Namespace) -> dict[str, Any]:
    return _engine(args.root).rollback_preview(
        args.root,
        checkpoint_id=args.checkpoint_id,
        boundary_chapter=args.boundary_chapter,
    )


def _rollback_restore(args: argparse.Namespace) -> dict[str, Any]:
    return _engine(args.root).rollback_restore(
        args.root,
        checkpoint_id=args.checkpoint_id,
        boundary_chapter=args.boundary_chapter,
        confirmation_token=args.confirmation_token,
    )


def _mcp(args: argparse.Namespace) -> None:
    os.environ["INKFLOW_WORKSPACE"] = str(Path(args.workspace).resolve())
    from .mcp_server import main as run_mcp

    run_mcp()


def _chat(args: argparse.Namespace) -> dict[str, Any]:
    session = TerminalSession(_engine(args.root))
    if args.once is not None:
        return asyncio.run(session.handle(args.root, args.once))

    print("墨流终端会话已启动。用自然语言描述任务；输入“帮助”查看示例，输入“退出”结束。")
    while True:
        try:
            message = input("墨流> ")
        except EOFError:
            return {"ended": True, "message": "终端输入已结束。"}
        response = asyncio.run(session.handle(args.root, message))
        _print(response)
        if response.get("ended"):
            return {"ended": True}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
        if result is not None:
            _print(result)
    except InkFlowError as exc:
        parser.exit(2, f"InkFlow error: {exc}\n")


if __name__ == "__main__":
    main()
