"""Prepare private fiction material and checkpoint a Writer training job.

The preparation stage uses the standard library only. It does not infer a
writer's original instructions from published prose or call a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from .utils import atomic_write_json, atomic_write_text


HEADING = re.compile(
    r"^\s*(?:第\s*[0-9０-９零〇一二三四五六七八九十百千万两]+\s*[卷章回节篇]"
    r"|(?:Chapter|CHAPTER)\s+\d+)(?:\s+|[：:.、]?)[^\r\n]{0,65}$"
)
AD_LINE = re.compile(r"(?:https?://|www\.|精校吧下载|更多精校小说尽在|手机阅读请访问|本书来自.{0,20}下载)", re.I)
SEPARATOR = re.compile(r"^(?:[=\-*·＊—─~～_]{4,}|[……]{2,})$")
SCENE_MARK = re.compile(r"^\s*(?:[＊＊＊*]{3,}|[◇◆※]{2,})\s*$")
PUNCTUATION = "。！？!?；;"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding, errors="strict")
            return text, encoding
        except UnicodeError:
            continue
    raise ValueError("文本无法按 UTF-8 或 GB18030 严格解码")


def _read_book(archive: Path) -> tuple[str, str, str, str, int]:
    with zipfile.ZipFile(archive) as zf:
        members = [info for info in zf.infolist() if not info.is_dir() and info.filename.lower().endswith(".txt")]
        if len(members) != 1:
            raise ValueError("每个压缩包必须恰有一个 TXT；此包需要人工选择条目")
        info = members[0]
        if info.file_size > MAX_ARCHIVE_BYTES:
            raise ValueError("TXT 超过 100 MiB 安全上限")
        raw = zf.read(info)
    if len(raw) != info.file_size or len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("解压大小与声明不符或超过安全上限")
    text, encoding = _decode(raw)
    return text, encoding, info.filename, _sha256(archive.read_bytes()), len(raw)


def _lines(text: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Remove only recognizable site wrappers; retain raw source line numbers."""
    result = []
    removed = Counter()
    for number, line in enumerate(text.splitlines(), 1):
        clean = line.strip(" \t\u3000\r\n")
        if AD_LINE.search(clean):
            removed["ad"] += 1
            continue
        if SEPARATOR.fullmatch(clean):
            removed["separator"] += 1
            continue
        result.append({"line": number, "text": clean})
    return result, dict(removed)


def _chapters(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    starts = [index for index, row in enumerate(lines) if HEADING.fullmatch(row["text"])]
    if not starts:
        return [{"no": 0, "title": "未识别章节", "lines": lines, "heading_status": "needs_review"}]
    if starts[0] > 0 and any(row["text"] for row in lines[:starts[0]]):
        starts.insert(0, 0)
    chapters = []
    for ordinal, (begin, end) in enumerate(zip(starts, starts[1:] + [len(lines)]), 1):
        section = lines[begin:end]
        title = section[0]["text"] if HEADING.fullmatch(section[0]["text"]) else "正文前段"
        body = section[1:] if title != "正文前段" else section
        if not any(item["text"] for item in body):
            continue
        chapters.append({"no": ordinal, "title": title, "lines": body, "heading_status": "heuristic"})
    return chapters


def _breaks(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Only split on explicit scene markers; a blank paragraph is not a scene."""
    parts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for row in lines:
        if SCENE_MARK.fullmatch(row["text"]):
            if current:
                parts.append(current)
                current = []
            continue
        current.append(row)
    if current:
        parts.append(current)
    return parts


def _units(book_id: str, chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units = []
    for chapter in chapters:
        for scene_no, lines in enumerate(_breaks(chapter["lines"]), 1):
            body = "\n".join(item["text"] for item in lines if item["text"])
            if not body.strip():
                continue
            units.append({
                "unit_id": f"{book_id}-c{chapter['no']:05d}-s{scene_no:02d}",
                "chapter_no": chapter["no"],
                "chapter_title": chapter["title"],
                "scene_no": scene_no,
                "start_line": lines[0]["line"],
                "end_line": lines[-1]["line"],
                "characters": len(body),
                "dialogue_quote_count": body.count("“"),
                "paragraphs": sum(bool(item["text"]) for item in lines),
                "explicit_scene_boundary": len(_breaks(chapter["lines"])) > 1,
                "text": body,
            })
    return units


def _book_profile(archive: Path, book_id: str, text: str, chapters: list[dict[str, Any]], units: list[dict[str, Any]], removed: dict[str, int]) -> dict[str, Any]:
    lengths = [len("".join(line["text"] for line in chapter["lines"])) for chapter in chapters]
    return {
        "book_id": book_id,
        "source_name": archive.name,
        "characters_raw": len(text),
        "chapters_detected": len(chapters),
        "chapter_title_examples": [chapters[i]["title"] for i in sorted({0, len(chapters) // 2, len(chapters) - 1})],
        "chapter_characters_median": int(statistics.median(lengths)) if lengths else 0,
        "chapter_characters_max": max(lengths, default=0),
        "explicit_scene_units": sum(unit["explicit_scene_boundary"] for unit in units),
        "units": len(units),
        "removed_lines": removed,
        "understanding_status": "structural_only",
        "interpretation_to_review": ["人物目标与阻力", "叙述层级与信息差", "转折与因果", "场景结尾效果", "表达技巧及适用条件"],
        "note": "章节与明确分场符号由程序定位；情节和写法尚未完成语义判读。",
    }


def _select_batches(books: list[dict[str, Any]], *, count: int, target_chars: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Round-robin through works and positions without duplicating units."""
    held_out = sorted(books, key=lambda book: book["book_id"])[-2:]
    train = [book for book in books if book not in held_out]
    cursors = {book["book_id"]: 0 for book in train}
    selected_ids: set[str] = set()
    batches = []
    for number in range(count):
        book = train[number % len(train)]
        units = book["units"]
        if not units:
            continue
        # Two visits per work sample the earlier and later halves.
        visit = cursors[book["book_id"]]
        pivot = min(len(units) - 1, round(((visit % 2) + 0.5) * len(units) / 2))
        cursor = pivot
        while cursor < len(units) and units[cursor]["unit_id"] in selected_ids:
            cursor += 1
        if cursor == len(units):
            cursor = next((i for i, unit in enumerate(units) if unit["unit_id"] not in selected_ids), len(units))
        chosen = []
        total = 0
        for unit in units[cursor:]:
            if unit["unit_id"] in selected_ids:
                break
            if chosen and total + unit["characters"] > target_chars:
                break
            chosen.append(unit)
            total += unit["characters"]
            selected_ids.add(unit["unit_id"])
            if total >= target_chars:
                break
        cursors[book["book_id"]] += 1
        if chosen:
            batches.append({
                "batch_no": len(batches) + 1,
                "book_id": book["book_id"],
                "characters": total,
                "unit_ids": [unit["unit_id"] for unit in chosen],
                "source_chapters": sorted({unit["chapter_no"] for unit in chosen}),
                "status": "source_candidates_only",
                "training_status": "not_started",
                "checkpoint": None,
            })
    return batches, [{"book_id": book["book_id"], "reason": "whole_work_holdout"} for book in held_out]


def prepare(source_dir: Path, output_dir: Path, *, batch_count: int = 24, target_chars: int = 25000) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if not 20 <= batch_count <= 30 or not 10000 <= target_chars <= 40000:
        raise ValueError("批次数须为20～30，每批目标字数须为1～4万")
    if not source_dir.is_dir():
        raise ValueError(f"素材目录不存在：{source_dir}")
    if source_dir == output_dir or source_dir in output_dir.parents:
        raise ValueError("输出目录不得位于素材目录内")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("输出目录非空；请使用新的 D 盘目录以保留旧结果")
    archives = sorted(source_dir.glob("*.zip"))
    if not archives:
        raise ValueError("素材目录没有 ZIP 文件")
    output_dir.mkdir(parents=True, exist_ok=True)
    books = []
    errors = []
    for archive in archives:
        try:
            text, encoding, member, sha, uncompressed = _read_book(archive)
            book_id = sha[:16]
            lines, removed = _lines(text)
            chapters = _chapters(lines)
            units = _units(book_id, chapters)
            profile = _book_profile(archive, book_id, text, chapters, units, removed)
            profile.update({"source_sha256": sha, "source_member": member, "encoding": encoding, "uncompressed_bytes": uncompressed})
            folder = output_dir / book_id
            folder.mkdir(exist_ok=True)
            atomic_write_json(folder / "profile.json", profile)
            atomic_write_text(folder / "chapters.jsonl", _jsonl([{
                "chapter_no": chapter["no"], "title": chapter["title"],
                "start_line": chapter["lines"][0]["line"], "end_line": chapter["lines"][-1]["line"],
                "heading_status": chapter["heading_status"],
            } for chapter in chapters]))
            # Prose stays on D:, outside the public source tree.
            atomic_write_text(folder / "units.jsonl", _jsonl(units))
            books.append({"book_id": book_id, "profile": profile, "units": units})
        except (OSError, UnicodeError, zipfile.BadZipFile, ValueError) as exc:
            errors.append({"source_name": archive.name, "error": str(exc)})
    batches, held_out = _select_batches(books, count=batch_count, target_chars=target_chars) if len(books) >= 3 else ([], [])
    atomic_write_text(output_dir / "batches.jsonl", _jsonl(batches))
    unit_index = {unit["unit_id"]: unit for book in books for unit in book["units"]}
    candidates_dir = output_dir / "candidates"
    candidates_dir.mkdir(exist_ok=True)
    for batch in batches:
        rows = []
        for unit_id in batch["unit_ids"]:
            unit = unit_index[unit_id]
            rows.append({
                "source_unit_ids": [unit_id], "book_id": batch["book_id"],
                "chapter_no": unit["chapter_no"], "source_start_line": unit["start_line"],
                "source_end_line": unit["end_line"], "task": "", "context": "",
                "completion": unit["text"], "scene_understanding": {
                    "viewpoint": "", "character_goal": "", "obstacle": "", "causal_turn": "",
                    "outcome": "", "technique": "", "limits": "",
                },
                "reviewed_by": "", "quality_accepted": False, "training_rights_confirmed": False,
                "status": "needs_human_semantic_review",
            })
        atomic_write_text(candidates_dir / f"batch-{batch['batch_no']:04d}.jsonl", _jsonl(rows))
    manifest = {
        "schema": "inkflow-writer-materials-v1", "source_directory": str(source_dir),
        "batch_count_requested": batch_count, "target_chars_per_batch": target_chars,
        "books": [book["profile"] for book in books], "holdout": held_out,
        "batches_created": len(batches), "errors": errors,
        "state": "structurally_prepared_not_training_ready",
        "next": "逐批核对候选目录，补写任务/必要前文/人物目标/阻力/因果/合格正文及使用权利；合格批次复制到 reviewed 目录后才可训练。",
    }
    atomic_write_json(output_dir / "manifest.json", manifest)
    return {"manifest": str(output_dir / "manifest.json"), "books": len(books), "batches": len(batches), "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description="只在本地拆解小说素材，生成 Writer 训练候选清单")
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--batches", type=int, default=24)
    parser.add_argument("--chars", type=int, default=25000)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source_dir, args.output_dir, batch_count=args.batches, target_chars=args.chars), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
