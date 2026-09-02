from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from inkflow import __version__
from inkflow.mcp_server import mcp


def test_mcp_exposes_editor_tools_resources_prompts_and_risk_hints() -> None:
    tools = asyncio.run(mcp.list_tools())
    resources = asyncio.run(mcp.list_resources())
    templates = asyncio.run(mcp.list_resource_templates())
    prompts = asyncio.run(mcp.list_prompts())
    by_name = {item.name: item for item in tools}

    assert mcp._mcp_server.version == __version__
    assert "novel_document_annotate" in by_name
    assert "novel_document_search" in by_name
    assert "novel_story_bible" in by_name
    assert "novel_task_history" in by_name
    assert by_name["novel_task_history"].annotations.readOnlyHint is True
    assert by_name["novel_project_status"].annotations.readOnlyHint is True
    assert by_name["novel_chapter_accept"].annotations.destructiveHint is True
    assert "force" not in by_name["novel_chapter_accept"].inputSchema["properties"]
    assert by_name["novel_rollback_restore"].annotations.destructiveHint is True
    assert {str(item.uri) for item in resources} >= {
        "inkflow://project/book",
        "inkflow://project/plan",
        "inkflow://project/state",
    }
    assert any("chapter" in item.uriTemplate for item in templates)
    assert {item.name for item in prompts} >= {
        "prompt_start_novel",
        "prompt_batch_draft",
        "prompt_arc_audit",
    }


def test_jsonl_app_server_once_reports_capabilities(tmp_path: Path) -> None:
    request = json.dumps({"jsonrpc": "2.0", "id": "test", "method": "app.initialize", "params": {}})
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["INKFLOW_SETTINGS_PATH"] = str(tmp_path / "settings.json")
    completed = subprocess.run(
        [sys.executable, "-m", "inkflow.app_server", "--once"],
        input=request + "\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    messages = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    response = next(item for item in messages if item.get("id") == "test")
    assert response["result"]["product"] == "墨流（InkFlow）"
    assert response["result"]["capabilities"]["mcp"] is True
