"""从真实调用处生成产品设置使用的提示词索引；仅解析源码，不运行引擎或模型。"""
from __future__ import annotations

import ast
import json
from pathlib import Path


def generate_catalogue(source_root: Path) -> dict:
    """保留动态模板表达式和来源行号，避免把当前源码公式说成某次实际输入。"""
    entries = []
    for path in sorted(source_root.glob("*.py")):
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg not in {"system_prompt", "user_prompt"}:
                    continue
                expression = ast.get_source_segment(source, keyword.value) or ""
                try:
                    literal = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError):
                    literal = None
                entries.append({"id": f"{path.stem}:{node.lineno}:{keyword.arg}",
                    "module": path.stem, "source": "agent/src/inkflow/" + path.name,
                    "line": node.lineno, "parameter": keyword.arg,
                    "expression": expression, "literal": literal if isinstance(literal, str) else None})
    return {"schema_version": 1, "entries": sorted(entries, key=lambda item: (item["source"], item["line"], item["parameter"]))}


def main() -> None:
    """更新随Python包交付的索引，打包脚本在编译前调用一次。"""
    root = Path(__file__).resolve().parents[1] / "src" / "inkflow"
    catalogue = generate_catalogue(root)
    destination = root / "prompt_catalog.json"
    destination.write_text(json.dumps(catalogue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新产品提示词索引：{len(catalogue['entries'])}个调用参数。")


if __name__ == "__main__":
    main()
