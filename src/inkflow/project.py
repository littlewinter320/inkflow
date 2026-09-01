from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .database import ProjectDatabase
from .errors import ProjectError
from .schemas import BookBrief
from .utils import atomic_write_json, atomic_write_text, content_hash, safe_filename, utc_now


class InkFlowProject:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.internal = self.root / ".inkflow"
        self.config_path = self.internal / "project.json"
        if not self.config_path.exists():
            raise ProjectError(f"这里不是墨流小说项目：{self.root}")
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.db = ProjectDatabase(self.internal / "inkflow.db")

    @classmethod
    def create(cls, root: str | Path, brief: BookBrief) -> "InkFlowProject":
        project_root = Path(root).resolve()
        project_root.mkdir(parents=True, exist_ok=True)
        internal = project_root / ".inkflow"
        if (internal / "project.json").exists():
            raise ProjectError(f"项目已经存在：{project_root}")
        for path in (
            project_root / "chapters",
            project_root / "reviews",
            internal / "runs",
            internal / "cache",
            internal / "index",
            internal / "trash",
            internal / "references" / "raw",
            internal / "references" / "features",
            internal / "checkpoints",
        ):
            path.mkdir(parents=True, exist_ok=True)
        project_id = f"inkflow-{content_hash(str(project_root))[:10]}"
        atomic_write_json(
            internal / "project.json",
            {
                "project_id": project_id,
                "product": "墨流 / InkFlow",
                "schema_version": 1,
                "created_at": utc_now(),
                "permission_profile": "trusted_workspace",
                "trace_level": "full",
                "show_provider_reasoning": False,
            },
        )
        database = ProjectDatabase(internal / "inkflow.db")
        database.set_brief(brief)
        database.set_metadata("project_id", project_id)
        atomic_write_text(project_root / "BOOK.md", render_book_brief(brief))
        atomic_write_text(
            project_root / "PLAN.md",
            "# 小说规划\n\n> 尚未生成四级规划。请在编辑器中让墨流执行 `novel_plan_generate`。\n",
        )
        atomic_write_text(
            project_root / "STATE.md",
            "# 当前正史状态\n\n> 尚无已接受章节。草稿不会进入正史。\n",
        )
        return cls(project_root)

    @property
    def project_id(self) -> str:
        return str(self.config["project_id"])

    def resolve_user_path(self, relative_path: str | Path, *, allow_internal: bool = False) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.root):
            raise ProjectError("路径越出小说项目根目录。")
        if not allow_internal and candidate.is_relative_to(self.internal):
            raise ProjectError("通用文件工具不能直接修改 .inkflow；请调用对应的结构化工具。")
        return candidate

    def read_file(self, relative_path: str) -> str:
        path = self.resolve_user_path(relative_path)
        if not path.is_file():
            raise ProjectError(f"文件不存在：{relative_path}")
        return path.read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str, *, overwrite: bool = True) -> Path:
        path = self.resolve_user_path(relative_path)
        if path.exists() and not overwrite:
            raise ProjectError(f"文件已经存在：{relative_path}")
        return atomic_write_text(path, content)

    def delete_file(self, relative_path: str, *, permanent: bool = False) -> dict[str, Any]:
        path = self.resolve_user_path(relative_path)
        if not path.exists():
            raise ProjectError(f"目标不存在：{relative_path}")
        if permanent:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            return {"deleted": str(path), "recoverable": False}
        stamp = utc_now().replace(":", "").replace("+00:00", "Z")
        trash_dir = self.internal / "trash" / stamp
        trash_dir.mkdir(parents=True, exist_ok=True)
        destination = trash_dir / safe_filename(path.name)
        shutil.move(str(path), str(destination))
        atomic_write_json(
            trash_dir / "manifest.json",
            {"source": str(path), "destination": str(destination), "deleted_at": utc_now()},
        )
        return {"deleted": str(path), "recoverable": True, "trash_path": str(destination)}

    def run_powershell(self, command: str, timeout_seconds: int = 60) -> dict[str, Any]:
        if not command.strip():
            raise ProjectError("PowerShell 命令不能为空。")
        environment = os.environ.copy()
        environment["INKFLOW_PROJECT_ROOT"] = str(self.root)
        powershell = (
            Path(environment.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=max(1, min(timeout_seconds, 600)),
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return {
            "cwd": str(self.root),
            "command_hash": content_hash(command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-20_000:],
            "stderr": completed.stderr[-20_000:],
        }


def render_book_brief(brief: BookBrief) -> str:
    rules = "\n".join(f"- {item}" for item in brief.user_rules) or "- 暂无额外规则"
    return f"""# {brief.title}

## 项目契约

- 题材：{brief.genre}
- 目标读者：{brief.target_audience}
- 主角：{brief.protagonist}
- 核心卖点：{brief.core_selling_point or '待规划时细化'}
- 单章目标：{brief.target_chapter_words} 字
- 预计规模：{brief.estimated_volumes} 卷 / {brief.estimated_chapters} 章

## 故事前提

{brief.premise}

## 用户规则

{rules}

> 本文件是数据库的人类可读投影。修改后应通过墨流的计划补丁工具导入，不要让宿主 Agent 直接改数据库。
"""
