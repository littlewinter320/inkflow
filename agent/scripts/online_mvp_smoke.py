from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from inkflow.config import Settings
from inkflow.engine import InkFlowEngine
from inkflow.provider import DeepSeekProvider
from inkflow.schemas import BookBrief


async def run() -> dict[str, object]:
    settings = Settings.from_env()
    settings.context_soft_tokens = 32_000
    settings.request_timeout_seconds = max(settings.request_timeout_seconds, 300)
    engine = InkFlowEngine(DeepSeekProvider(settings), settings)
    brief = BookBrief(
        title="潮痕试写",
        genre="都市悬疑",
        premise="港口档案员收到一张来自明日的失踪登记表，必须在名单生效前找到第一个失踪者。",
        protagonist="沈砚",
        target_chapter_words=500,
        estimated_chapters=12,
        estimated_volumes=1,
        user_rules=["首章只建立一个核心谜团", "人物必须因主动选择付出代价"],
    )
    with tempfile.TemporaryDirectory(prefix="inkflow-online-") as temp_dir:
        root = Path(temp_dir) / "novel"
        print("stage=create", flush=True)
        engine.create_project(root, brief)
        print("stage=plan", flush=True)
        plan = await engine.generate_plan(root)
        print(f"plan_range={plan['chapter_range']}", flush=True)
        print("stage=write", flush=True)
        draft = await engine.write_chapter(root, 1)
        print(f"draft_version={draft['version']}", flush=True)
        print("stage=review", flush=True)
        review = await engine.review_chapter(root, 1)
        print(f"review_verdict={review['verdict']}", flush=True)
        print("stage=accept", flush=True)
        accepted = await engine.accept_chapter(root, 1, force=True)
        status = engine.status(root)
        required = [
            root / "PLAN.md",
            root / "STATE.md",
            root / "chapters" / "chapter_00001.md",
            root / "reviews" / "chapter_00001.review.md",
        ]
        if not all(path.exists() for path in required):
            raise RuntimeError("在线闭环结束但缺少预期输出文件")
        return {
            "status": "ok",
            "model": settings.model,
            "chapter_status": accepted["status"],
            "review_verdict": review["verdict"],
            "accepted_chapters": status["chapters"].get("accepted", 0),
            "facts_committed": accepted["facts_committed"],
            "threads_updated": accepted["threads_updated"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="墨流真实 DeepSeek MVP 闭环烟雾测试")
    parser.add_argument("--live", action="store_true", help="确认执行会产生 API 用量的真实在线测试")
    args = parser.parse_args()
    if not args.live:
        parser.error("必须显式传入 --live；该测试会调用真实 DeepSeek API")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    result = asyncio.run(run())
    print("RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
