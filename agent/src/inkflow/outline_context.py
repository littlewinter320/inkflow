import re
from pathlib import Path

from .schemas import ContextSection


_CHAPTER_HEADING = re.compile(r"(?m)^##\s*第\s*(\d+)\s*章[^\n]*$")


def outline_neighbor_boundaries(
    existing: str, start: int, end: int, *, neighbor_count: int = 3, include_before: bool = True,
) -> str:
    """Give a revision its adjacent promises without feeding back the old target."""

    matches = list(_CHAPTER_HEADING.finditer(existing))
    blocks: list[str] = []
    for index, match in enumerate(matches):
        chapter_no = int(match.group(1))
        if not (start - neighbor_count <= chapter_no < start or end < chapter_no <= end + neighbor_count):
            continue
        if chapter_no < start and not include_before:
            continue
        following = matches[index + 1].start() if index + 1 < len(matches) else len(existing)
        block = existing[match.start():following]
        # A final planning-rationale section describes the old proposal and
        # must not leak into the supposedly neutral continuation boundary.
        block = re.split(r"(?m)^##\s*第\s*\d+\s*[～~—-]\s*\d+\s*章方向[^\n]*$", block, maxsplit=1)[0]
        block = re.split(r"(?m)^##\s*规划依据\s*$", block, maxsplit=1)[0]
        blocks.append(block.strip())
    return "\n\n".join(blocks)


def replace_outline_range(existing: str, generated: str, start: int, end: int,
                          *, direction: str, character_arc: str, ending: str) -> str:
    """Replace only a future chapter window; keep other volumes byte-for-byte."""
    old = [(int(match.group(1)), match.start()) for match in _CHAPTER_HEADING.finditer(existing)]
    new = [(int(match.group(1)), match.start()) for match in _CHAPTER_HEADING.finditer(generated)]
    expected = list(range(start, end + 1))
    if [number for number, _ in new] != expected:
        raise ValueError("新大纲没有完整覆盖要求的章节范围，原大纲未改动。")
    target = [(number, position) for number, position in old if start <= number <= end]
    if [number for number, _ in target] != expected:
        raise ValueError("原大纲的目标章节不完整，不能安全局部替换；原大纲未改动。")
    start_at = target[0][1]
    end_at = next((position for number, position in old if number > end), len(existing))
    prior_heading = max((position for number, position in old if number < start), default=0)
    marker = re.search(rf"(?m)^## 第\s*{start}\s*[～~—-]\s*{end}\s*章方向[^\n]*\n", existing[prior_heading:start_at])
    if marker:
        start_at = prior_heading + marker.start()
    trailing_rationale = re.search(r"(?m)^## 规划依据\s*$", generated[new[0][1]:])
    generated_end = new[0][1] + trailing_rationale.start() if trailing_rationale else len(generated)
    # Generated documents have a trailing review rationale after the chapter
    # list. Keep it in this range's direction note, not as another global plan.
    sections = generated[new[0][1]:generated_end].strip()
    note = (f"## 第 {start}～{end} 章方向（本轮修订）\n\n"
            f"主线：{direction}\n\n人物变化：{character_arc}\n\n阶段收束：{ending}\n\n")
    return existing[:start_at] + note + sections + "\n\n" + existing[end_at:]


def replace_story_detail_volume(existing: str, replacement: str, volume_no: int) -> str:
    """Replace one explicitly marked volume in a legacy causal detail file."""
    numerals = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
    if volume_no not in numerals:
        raise ValueError("当前只支持有明确卷标题的前六卷细纲局部修订。")
    headings = list(re.finditer(r"(?m)^## [^\n]+$", existing))
    start_label = (f"第{numerals[volume_no]}卷", f"第{volume_no}卷")
    next_label = (f"第{numerals.get(volume_no + 1, '')}卷", f"第{volume_no + 1}卷")
    previous_label = (f"第{numerals.get(volume_no - 1, '')}卷", f"第{volume_no - 1}卷")
    starts = [item for item in headings if any(label in item.group() for label in start_label)
              and not any(label in item.group() for label in previous_label)]
    if not starts:
        raise ValueError("细纲中找不到目标卷起点，未覆盖任何内容。")
    start = starts[0]
    following = [item for item in headings if item.start() > start.start()
                 and any(label in item.group() for label in next_label)]
    if len(following) != 1:
        raise ValueError("细纲中找不到唯一的下一卷边界，未覆盖任何内容。")
    return existing[:start.start()] + replacement.strip() + "\n\n" + existing[following[0].start():]


def outline_sections(root: Path, start: int | None = None, end: int | None = None) -> list[ContextSection]:
    """Read editable foundations, never treat proposed events as accepted canon."""
    result = []
    for key, name, title in (
        ("O0", "OUTLINE.md", "全书大纲（未来构想，非正史）"),
        ("O1", "STORY_DETAIL.md", "剧情细纲（事件因果，不按章节切分）"),
        ("O2", "RECENT_PLAN.md", "近期章节规划（已审核的未来安排，非正史）"),
    ):
        path = root / name
        if path.is_file():
            result.append(ContextSection(
                key=key, title=title,
                content=path.read_text(encoding="utf-8"),
                source_ids=[name], hard=True,
                cache_scope="book",
            ))
    return result
