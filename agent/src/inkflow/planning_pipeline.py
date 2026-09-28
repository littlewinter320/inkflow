"""Versioned whole-book -> volume -> rolling-window planning for existing novels.

Candidates remain in the run directory until a separate editorial verdict passes.
Only the engine publishes active Markdown projections; no candidate is canon.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from .errors import ValidationGateError
from .project import InkFlowProject
from .project_lock import project_write_lock, project_write_lock_sync
from .schemas import BookOutlineV2, PlanningReviewV2, RollingPlanV2, VolumeDetailV2
from .trace import TraceRecorder
from .task_settings import active_task_settings
from .utils import atomic_write_text, content_hash, utc_now


_WRITER_SYSTEM = """你是墨流 Writer。依照任务指明的层级写小说规划，只输出要求的 JSON。
已接受正文是发生过的事实，旧大纲和旧未来计划只是待替换方案。人物不能提前知道未发现的线索，物件不能无行动回到旧位置。新线索必须有来源、行动和代价。用户最新明确要求优先于旧规模估计。大纲、卷细纲、逐章规划不得混写；本轮不写小说正文。
对未来剧情可以创造，对过去事实不能脑补。大纲是方向、细纲是卷级因果、近期规划是可调整的写作假设，不是对未来每件物品和每句对话的承诺。特别核对人物是否受伤、物件最后位置、谁已经知道什么；来源未写明的状态不要当作既成事实。若写“上一次”“第二次”“又”“早已”等承接过去的判断，核对同一个人物、动作与前因确实存在；没有来源就改为首次发生或写成推测，不补造旧事。看整个近期窗口的主线和关键伏笔如何推进，暂时不展开的线索可说明留待何时，不要求每章平均分配。交稿前自己从前章结尾顺到后章开头：关键决定若反转，写出促成改变的行动或代价；已知失物不能在下一处又当成首次发现。每章可保留记录、问话等生活动作，但它们不能反复代替剧情变化，尤其当用户明确要求突破重复时。收到审查意见时，在上一版候选上做最小范围修订，不要整篇重写而引入新矛盾。"""

_EDITOR_SYSTEM = """你是墨流当前模式负责规划审核的角色，只输出 PlanningReviewV2 JSON。
审核的是未来方向，不是正文逐句验收。先判断它是否接得上已接受章节、基本符合大纲主线与本卷因果、关键伏笔在整个窗口有推进或合理暂缓、没有明显跑题；不要求每章处理所有线索。允许悬念、暂未揭晓的人物动机和有衔接的未来变化。不要把旧未来提案、每件物品的预想位置、文风或某章尚未写明的细节当成正史硬约束。对近期窗口，还须从前章结尾顺读到后章开头：关键决定突然反转却无促成改变的桥接、已知状态被当成新发现、整段重复同一种行动而没有用户要求的推进，都属于需要 Writer 定点修订的因果或任务覆盖问题，不能在 summary 指出来却仍判 pass。同一地点、相同调查主题或两章都记录信息不等于重复；只有目标、阻力和结果几乎不变且占据整个窗口推进时才据此拦截。未来新线索不要求旧正文预先出现，但重要证据须有合理取得路径，不能恰巧送到主角手里就解决难题。规划中纸张从工具箱取到台面再放回等日常动作可以省略，不是物件瞬移；只有关键物件位置造成剧情不可能、持有人无合理取得途径或直接违背已接受事实，才拦截，普通收纳细节只给建议。另须核对“上一次”“第二次”“又”“早已”等关于过去的断言：同一个人是否真做过所称的事，不能把甲的动作归给乙，也不能把未来新设想倒灌成已发生的旧事。若这类无来源断言影响关键因果，判 revise 并要求最小修改；不影响因果的模糊措辞只给建议。已接受事实明确冲突、关键人物无途径获知信息、关键物件凭空回归，或违背用户明确硬要求且影响主线，也判 revise。审美分歧和可合理补足的小留白只写建议，不拦截。pass 至少给一条能逐字定位到外部来源和候选的主线/衔接依据；revise 可以用来源对照候选，或用同一候选中前后两章的原句证明内部矛盾。最严重的阻断问题必须在 evidence 中有对应原句；无法定位的疑虑只能留在 summary 作建议。evidence 最多三条，每个 excerpt 是对应文本里的连续原文，不拼接，不加解释。若判 revise，summary 和 repair_instruction 说明具体矛盾及最小修法，不代写规划。"""


# Planning is a proposal. A missing transition is not a contradiction when
# the cited events can coexist; this rule prevents advisory notes from
# consuming two Writer rewrites and leaving a sound candidate unpublished.
_EDITOR_SYSTEM += """\n判事实矛盾前做共存检验：逐字引用的两件事若发生在不同日期、不同纸张或不同人物视角，能够同时为真，就不是事实矛盾。读者可能误解、缺一句交代、线索暂未解释或人物对动作的主观解读，也不等于剧情不可能；只在 summary 给出改善建议。事实类 revise 须有已接受事实冲突、关键人物确无途径获知信息、关键物件确实凭空回归等排他性依据；在 finding 中说明为何不能同时成立。另有独立的用户任务覆盖检查：用户明确要求打破某种反复出现的行动结构，而候选多数章节仍照旧循环，也判 revise，即使没有事实矛盾；须引用用户原话与多个候选章节的原句，说明最小重组范围。若仅能说“可能”“容易”“建议明确”“未说明”，但无事实冲突或明确要求未兑现的证据，应判 pass 并保留建议。"""

_WRITER_SYSTEM += """\n近期规划要让前章行动造成后章的新选择或代价。安静的生活章节可以保留，但若用户明确要求打破重复，不能只更换问话对象、公告或纸张，再让人物回到原处记录；在整个窗口里改变至少一条行动路径、人物关系、风险或可证实的局面。不要为制造变化凭空送来决定性证据，优先让人物以已有材料作不同选择并承担后果。"""
_EDITOR_SYSTEM += """\n事实共存不等于任务完成。另从窗口起点到终点比较人物目标、障碍、实际选择与后果；当用户明确要求突破重复，而连续数章只是换人问话、看通知、回铺整理且没有推进目标或改变局面时，可判 revise，并引用这些章节的原句说明重复模式及最小重组范围。不要要求每章都有高潮，也不要因为单章安静就否决慢热叙事。"""
_EDITOR_SYSTEM += """\n若判 pass，summary 要说清窗口收尾相比起点发生了什么由人物行动造成的局面变化或代价。单纯多得一条消息、又记了一张纸、把材料换地方藏，不自动等于任务要求的剧情推进；人物仍可保持谨慎，关键是谨慎带来新关系、新阻力或新的可执行选择。找不到这种变化而用户明确要打破重复时，应判 revise 并指出最小受影响章节段，不要用“线索逐步增多”代替实际后果。"""


class PlanningNeedsAttention(ValidationGateError):
    """A saved candidate needs targeted follow-up; the whole task has not failed."""

    def __init__(self, reason: str, next_action: str):
        super().__init__(reason)
        self.next_action = next_action


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _recent_planning_state(state: str, anchor: int) -> str:
    """Keep current-state context temporal for local future planning.

    STATE.md includes historical facts that were true when written but have
    since changed. Recent canon and open threads are safer than flattening all
    earlier possession and decision records into one apparent present tense.
    """
    recent = [line for line in state.splitlines()
              if (match := re.search(r"`fact-ch(\d+)-", line))
              and int(match.group(1)) >= anchor - 2]
    thread_section = state.split("## 未结线索与承诺", 1)
    threads = thread_section[1].split("\n## ", 1)[0].strip() if len(thread_section) == 2 else ""
    if not recent or not threads:
        return state
    return (f"## 截至第 {anchor} 章的近期事实\n" + "\n".join(recent)
            + "\n\n## 未结线索与承诺\n" + threads
            + "\n\n较早章节状态按发生时点理解；物件当前持有和人物最新决定以已接受的最近章节正文为准。")


def _stage_text(value: Any) -> str:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _repeated_user_rejected_pattern(instruction: str, plan: RollingPlanV2) -> str:
    """Catch an explicit anti-repetition request that a model review waved through.

    This narrow check does not judge style or demand action in every chapter.
    It only catches a whole-window recurrence of the very actions the user
    named, so the Writer can recompose the window before publication.
    """
    if len(plan.chapters) < 5 or not re.search(r"别再|不要再|别沿用|不要沿用|避免反复|不想再", instruction):
        return ""
    motifs = {
        "问话": (r"问话|问一句|问人|逐户问|换人问", r"问|询问|打听"),
        "回铺": (r"回铺|回到铺|回店|回去记", r"回铺|回到铺|回了铺|回店"),
        "记纸": (r"记纸|交货单|反复记录|整理线索", r"旧交货单|空白交货单|写进.*纸|记在.*纸"),
    }
    rejected = [(label, pattern) for label, (named, pattern) in motifs.items()
                if re.search(named, instruction)]
    if len(rejected) < 2:
        return ""
    repeated = [(label, sum(bool(re.search(pattern, chapter.body)) for chapter in plan.chapters))
                for label, pattern in rejected]
    pervasive = [(label, count) for label, count in repeated
                 if count >= max(4, (len(plan.chapters) * 3 + 3) // 4)]
    if len(pervasive) < 2:
        return ""
    counts = "、".join(f"{label} {count}/{len(plan.chapters)} 章" for label, count in pervasive)
    return (f"用户明确要求摆脱的行动组合仍贯穿多数章节（{counts}）。"
            "请重组目标窗口的行动和后果，让选择真正改变人物关系、风险或局面；"
            "安静章节可以保留，不要求每章都有高潮。")


def _render_outline(value: BookOutlineV2) -> str:
    lines = [f"# {value.title}", "", "> 规划协议 v2 · 全书大纲；未来构想，不是已发生正史。", "", value.body.strip(), "", "## 卷级方向", ""]
    for volume in value.volumes:
        lines += [f"### 第 {volume.volume_no} 卷 · {volume.title}（第 {volume.chapter_start}～{volume.chapter_end} 章）", "", f"核心冲突：{volume.central_conflict}", "", f"阶段结果：{volume.outcome}", ""]
    return "\n".join(lines).strip() + "\n"


def _render_detail(value: VolumeDetailV2) -> str:
    lines = [f"# 第 {value.volume_no} 卷 · {value.title}", "", f"> 规划协议 v2 · 卷细纲 · 第 {value.chapter_start}～{value.chapter_end} 章；未来构想，不是正史。", "", value.body.strip(), "", "## 粗略章节节拍", ""]
    lines.extend(f"- {item}" for item in value.rough_chapter_beats)
    return "\n".join(lines).strip() + "\n"


def _render_window(value: RollingPlanV2) -> str:
    lines = [f"# 第 {value.anchor_chapter}～{value.chapters[-1].chapter_no} 章近期规划", "", "> 规划协议 v2；已接受的衔接章只读，以下未来章节尚不是正史。", "", f"## 第 {value.anchor_chapter} 章 · 已接受正文衔接", "", value.anchor_summary.strip(), ""]
    for chapter in value.chapters:
        lines += [f"## 第 {chapter.chapter_no} 章 · {chapter.title}", "", chapter.body.strip(), ""]
    return "\n".join(lines).strip() + "\n"


def _cleanup_suggestions(project: InkFlowProject) -> list[dict[str, str]]:
    """Identify obsolete planning candidates, never delete them on a model verdict."""
    active_outline = content_hash(_read(project.root / "OUTLINE.md"))
    suggestions: list[dict[str, str]] = []
    source_dir = project.root / "planning" / "outlines"
    if source_dir.is_dir():
        for path in sorted(source_dir.glob("outline_*.md")):
            content = _read(path)
            if not content or content_hash(content) == active_outline:
                continue
            suggestions.append({"path": str(path.relative_to(project.root)),
                                "content_hash": content_hash(content),
                                "reason": "旧大纲候选已不再是生效版本；删除前须核对引用和影响",
                                "action": "review_before_delete"})
    return suggestions


def load_active_planning(project: InkFlowProject) -> tuple[dict[str, Any], BookOutlineV2, VolumeDetailV2, RollingPlanV2] | None:
    """Resolve the active v2 hierarchy by manifest and content, never by file age."""
    manifest_path = project.root / "planning" / "active-v2.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(_read(manifest_path))
        source_id = str(manifest["trace_id"])
        if manifest.get("status") != "active" or Path(source_id).name != source_id:
            raise ValueError("invalid planning manifest")
        for filename, key in (("OUTLINE.md", "outline_hash"),
                              ("STORY_DETAIL.md", "volume_detail_hash"),
                              ("RECENT_PLAN.md", "recent_plan_hash")):
            if content_hash(_read(project.root / filename)) != manifest[key]:
                raise ValueError(f"{filename} source changed")
        source = project.root / ".inkflow" / "runs" / source_id
        outline = BookOutlineV2.model_validate_json(_read(source / "outline-candidate.json"))
        detail = VolumeDetailV2.model_validate_json(_read(source / "volume-detail-candidate.json"))
        window = RollingPlanV2.model_validate_json(_read(source / "chapter-window-candidate.json"))
        if ([window.anchor_chapter, window.chapters[-1].chapter_no] != manifest["chapter_window"]
                or detail.volume_no != manifest["volume_no"]
                or content_hash(_render_outline(outline)) != manifest["outline_hash"]
                or content_hash(_render_detail(detail)) != manifest["volume_detail_hash"]
                or content_hash(_render_window(window)) != manifest["recent_plan_hash"]):
            raise ValueError("planning range changed")
        return manifest, outline, detail, window
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PlanningNeedsAttention(
            "生效规划的来源版本不一致；原规划和正文均未改。",
            "在规划工作区核对修改或缺失的来源文件，恢复版本一致后只从受影响层继续。",
        ) from exc


def restore_previous_planning_publication(
    root: str | Path, *, current_run_id: str, previous_run_id: str,
) -> dict[str, Any]:
    """Compensate one mistaken publication using its exact, verified prior run.

    This only restores the active planning projections. Accepted prose, draft
    chapters and historical candidates are never changed or deleted.
    """
    project = InkFlowProject(root, recover_on_open=False)
    if any(Path(value).name != value for value in (current_run_id, previous_run_id)):
        raise ValidationGateError("规划恢复来源无效，文件未修改。")
    source = project.internal / "runs" / previous_run_id
    outline = BookOutlineV2.model_validate_json(_read(source / "outline-candidate.json"))
    detail = VolumeDetailV2.model_validate_json(_read(source / "volume-detail-candidate.json"))
    window = RollingPlanV2.model_validate_json(_read(source / "chapter-window-candidate.json"))
    prior = {"OUTLINE.md": _render_outline(outline), "STORY_DETAIL.md": _render_detail(detail),
             "RECENT_PLAN.md": _render_window(window)}
    events = _read(source / "events.jsonl")
    if '"stage": "planning-v2.publish", "status": "completed"' not in events:
        raise ValidationGateError("指定来源不是已发布的三层规划，文件未修改。")
    with project_write_lock_sync(project.root):
        active = load_active_planning(project)
        if active is None or active[0]["trace_id"] != current_run_id:
            raise ValidationGateError("现行规划版本已变化，未覆盖后续修改。")
        stamp = str(active[0]["published_at"]).replace(":", "-")
        history = project.root / "planning" / "history"
        for stage, filename in (("outline", "OUTLINE.md"), ("volume-detail", "STORY_DETAIL.md"),
                                ("chapter-window", "RECENT_PLAN.md")):
            matches = list(history.glob(f"{stage}-{stamp}-*.md"))
            if len(matches) != 1 or _read(matches[0]) != prior[filename]:
                raise ValidationGateError(f"{stage} 的发布前备份与来源不符，未执行恢复。")
        book_matches = list(history.glob(f"book-scale-{stamp}-*.md"))
        if len(book_matches) != 1:
            raise ValidationGateError("找不到本次误改前的书籍规模，未执行恢复。")
        prior["BOOK.md"] = _read(book_matches[0])
        prior_brief = project.db.get_brief()
        targets = [*prior, "planning/cleanup-suggestions.json", "planning/active-v2.json"]
        before = {name: _read(project.root / name) for name in targets}
        restored = {**active[0], "published_at": utc_now(), "trace_id": previous_run_id,
                    "chapter_window": [window.anchor_chapter, window.chapters[-1].chapter_no],
                    "volume_no": detail.volume_no,
                    "outline_hash": content_hash(prior["OUTLINE.md"]),
                    "volume_detail_hash": content_hash(prior["STORY_DETAIL.md"]),
                    "recent_plan_hash": content_hash(prior["RECENT_PLAN.md"]),
                    "book_hash": content_hash(prior["BOOK.md"])}
        try:
            for name, content in prior.items():
                atomic_write_text(project.root / name, content)
            project.db.set_brief(prior_brief.model_copy(update={
                "estimated_chapters": outline.volumes[-1].chapter_end,
                "estimated_volumes": len(outline.volumes),
            }))
            atomic_write_text(project.root / "planning/cleanup-suggestions.json", json.dumps(
                {"generated_at": utc_now(), "candidates": _cleanup_suggestions(project)},
                ensure_ascii=False, indent=2,
            ))
            atomic_write_text(project.root / "planning/active-v2.json", json.dumps(
                restored, ensure_ascii=False, indent=2,
            ))
            load_active_planning(project)
        except Exception:
            for name, content in before.items():
                atomic_write_text(project.root / name, content)
            project.db.set_brief(prior_brief)
            raise
    return {"restored_run_id": previous_run_id, "chapter_window": restored["chapter_window"]}


async def redesign_existing_story(engine: Any, root: str | Path, *, anchor: int, end: int,
                                  instruction: str, focus: str = "") -> dict[str, Any]:
    """Complete an authorized planning chain, resuming only the failed stage on retry."""
    if end <= anchor or not instruction.strip():
        raise ValidationGateError("请说清以哪一章正史为锚点、要规划到第几章，以及后续剧情方向。")
    project = InkFlowProject(root)
    accepted = project.db.accepted_chapters()
    if not accepted or int(accepted[-1]["chapter_no"]) != anchor:
        raise ValidationGateError("规划起点必须是当前最后一章已接受正文；不会改写或跳过正史。")
    trace = TraceRecorder(project.root, "planning-v2", engine.settings.trace_level)
    book = _read(project.root / "BOOK.md")
    state = _read(project.root / "STATE.md")
    state_context = _recent_planning_state(state, anchor) if focus == "chapter-window" else state
    canon = []
    for item in accepted[-3:]:
        path = project.root / str(item["path"])
        content = _read(path)
        if not content or content_hash(content) != item["content_hash"]:
            raise ValidationGateError(f"第 {item['chapter_no']} 章正史文件缺失或版本不符，暂不重设计剧情。")
        canon.append(content)
    summaries = "\n".join(f"第 {item['chapter_no']} 章：{item.get('summary') or item.get('title') or ''}" for item in accepted)
    stable_sources = f"【书籍设定】\n{book}\n【已接受章节摘要】\n{summaries}\n【时序化事实与伏笔】\n{state_context}\n【最近三章全文】\n" + "\n\n".join(canon)
    frozen = content_hash(stable_sources)
    active_start_hashes = {name: content_hash(_read(project.root / name))
                           for name in ("OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md")}
    scope = active_task_settings.get()
    review_role = "reviewer" if scope and scope.collaboration_mode in {"review_boost", "deep", "full_specialist"} else "editor"
    min_review_confidence = max(0.80, engine.settings.review_min_confidence)
    prior_runs = sorted(
        (path for path in (project.root / ".inkflow" / "runs").glob("*-planning-v2-*")
        if path.is_dir() and path != trace.run_dir),
        key=lambda path: path.stat().st_mtime, reverse=True,
    )
    # Mentioning "刚才这版" is feedback on the active publication, not consent
    # to reuse an arbitrary older, unpublished candidate.
    resume_requested = bool(re.search(
        r"(?:继续|恢复|接着|重试|复核).{0,18}(?:未完成|没做完|中断|断点|保存的候选|失败|停在)",
        instruction,
    ))
    previous_run: Path | None = None
    active_outline: BookOutlineV2 | None = None
    active_detail: VolumeDetailV2 | None = None
    active_source_run: Path | None = None
    if focus == "chapter-window":
        active = load_active_planning(project)
        if active is None or active[0].get("accepted_anchor") != anchor or active[0].get("chapter_window") != [anchor, end]:
            raise ValidationGateError("当前三层规划的锚点或范围已变化；请先核对受影响章节，不会改写旧稿。")
        manifest, active_outline, active_detail, _active_window = active
        source_run = project.root / ".inkflow" / "runs" / str(manifest["trace_id"])
        active_source_run = source_run
        previous_run = source_run
    focus_range = re.search(
        r"(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)第\s*(\d+)\s*(?:到|至|～|~|—|-)\s*(\d+)\s*章",
        instruction,
    )
    focus_single = re.search(r"(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)第\s*(\d+)\s*章", instruction)
    focus_chapters = (set(range(int(focus_range.group(1)), int(focus_range.group(2)) + 1))
                      if focus == "chapter-window" and focus_range else
                      {int(focus_single.group(1))} if focus == "chapter-window" and focus_single else set())
    replan_window = (focus == "chapter-window" and len(focus_chapters) > 1
                     and bool(re.search(r"重排|重构|重新规划|重新安排", instruction)))
    if focus == "chapter-window" and (not focus_chapters or min(focus_chapters) <= anchor or max(focus_chapters) > end):
        raise ValidationGateError("请指出要修订的未来章节范围；已接受正文和其他规划不会被猜测改动。")
    resume_basis = {
        "canon_hash": frozen,
        "anchor": anchor,
        "end": end,
        "focus": focus or "",
        "focus_chapters": sorted(focus_chapters),
        "active_manifest_hash": content_hash(_read(project.root / "planning" / "active-v2.json"))
        if focus == "chapter-window" else "",
    }
    if resume_requested:
        for prior in prior_runs:
            try:
                prior_basis = json.loads(_read(prior / "resume-base.json"))
            except (ValueError, TypeError):
                continue
            if (prior_basis == resume_basis
                    and any((prior / f"{name}-candidate.json").is_file()
                            for name in ("outline", "volume-detail", "chapter-window"))):
                previous_run = prior
                break
    atomic_write_text(trace.run_dir / "resume-base.json", json.dumps(resume_basis, ensure_ascii=False))
    # A short follow-up such as "continue the saved candidate" does not erase
    # the creative goal that started the task. Keep one immutable root intent
    # across retries instead of nesting every previous prompt (and growing the
    # input/cache prefix on each attempt). The newest message still wins.
    latest_instruction = instruction.strip()
    creative_revision = bool(re.search(
        r"(?:别再|不要再|不能再|换一种|改成|重排|重新安排).{0,70}"
        r"(?:剧情|人物|章节|规划|行动|节奏|走向|选择|问话|记录|重复)",
        latest_instruction,
    ))
    root_intent = (
        _read(previous_run / "task-root-intent.txt").strip()
        if resume_requested and previous_run is not None and previous_run != active_source_run
        else ""
    ) or instruction.strip()
    atomic_write_text(trace.run_dir / "task-root-intent.txt", root_intent)
    if resume_requested and root_intent != instruction.strip():
        instruction = (f"【原任务仍有效的目标】\n{root_intent}\n"
                       f"【本次最新补充；如与原目标冲突，以本次为准】\n{instruction}")
        trace.record("planning.intent-resume", "completed", "续接时保留原创作目标，并让本次补充优先")

    def verify_canon() -> None:
        current = project.db.accepted_chapters()
        if len(current) != len(accepted) or any(a["content_hash"] != b["content_hash"] for a, b in zip(accepted, current)):
            raise PlanningNeedsAttention(
                "规划期间正史已更新；候选已保存，未覆盖新版本。",
                "以最新已接受正文为依据，从受影响的规划层继续；已通过且未受影响的上游内容保留。",
            )
        if content_hash(_read(project.root / "BOOK.md") + _read(project.root / "STATE.md")) != content_hash(book + state):
            raise PlanningNeedsAttention(
                "规划期间设定或事实账已变化；候选保留，未覆盖当前规划。",
                "核对新增设定影响的范围，只重审受影响规划，不重发整项任务。",
            )

    async def generate_and_review(stage: str, model_type: Any, source: str, task: str) -> Any:
        candidate_path = trace.run_dir / f"{stage}-candidate.json"
        review_path = trace.run_dir / f"{stage}-review.json"
        if candidate_path.is_file() and review_path.is_file():
            review = PlanningReviewV2.model_validate_json(_read(review_path))
            if review.verdict == "pass" and review.confidence >= min_review_confidence:
                return model_type.model_validate_json(_read(candidate_path))
        feedback = ""
        previous_candidate = ""
        resumed_candidate = None
        if previous_run is not None and not (focus == "chapter-window" and previous_run == active_source_run):
            dependencies = ("outline",) if stage == "volume-detail" else (("outline", "volume-detail") if stage == "chapter-window" else ())
            matching_dependencies = all(
                content_hash(_read(previous_run / f"{name}-candidate.json"))
                == content_hash(_read(trace.run_dir / f"{name}-candidate.json"))
                for name in dependencies
            )
            prior_candidate = previous_run / f"{stage}-candidate.json"
            if matching_dependencies and prior_candidate.is_file():
                try:
                    resumed_candidate = model_type.model_validate_json(_read(prior_candidate))
                    previous_candidate = _stage_text(resumed_candidate)
                    trace.record(f"{stage}.resume", "completed",
                                 "复用上次候选作修订底稿" if creative_revision and stage == "chapter-window"
                                 else "复用上次保存的候选，先重新审核；不重复生成整层")
                except ValueError:
                    resumed_candidate = None
        if focus == "chapter-window" and stage == "chapter-window" and previous_run == active_source_run:
            # A whole-window replan starts from canon and approved upper layers,
            # not from the old sequence that the user explicitly rejected.
            resumed_candidate = None
            previous_candidate = ("" if replan_window else _stage_text(RollingPlanV2.model_validate_json(
                _read(active_source_run / "chapter-window-candidate.json"))))
        if (focus == "chapter-window" and stage == "chapter-window"
                and resumed_candidate is not None and active_source_run is not None):
            original = RollingPlanV2.model_validate_json(_read(active_source_run / "chapter-window-candidate.json"))
            revised = {item.chapter_no: item for item in resumed_candidate.chapters}
            if not focus_chapters.issubset(revised):
                raise ValidationGateError("修订候选缺少目标章节，原规划保持不变。")
            resumed_candidate = resumed_candidate.model_copy(update={
                "anchor_chapter": original.anchor_chapter,
                "anchor_summary": original.anchor_summary,
                "chapters": [revised[item.chapter_no] if item.chapter_no in focus_chapters else item
                             for item in original.chapters],
            })
            previous_candidate = _stage_text(resumed_candidate)
            prior_review_path = previous_run / "chapter-window-review.json" if previous_run else None
            if (not creative_revision and len(focus_chapters) == 1
                    and prior_review_path is not None and prior_review_path.is_file()):
                prior_review = PlanningReviewV2.model_validate_json(_read(prior_review_path))
                focus_body = revised[next(iter(focus_chapters))].body
                exact = [item for item in prior_review.evidence
                         if item.candidate_excerpt in previous_candidate
                         and item.source_excerpt in (instruction + canon[-1] + state + _render_detail(active_detail))]
                # An extra paraphrased citation is a report-format flaw, not
                # proof that the whole edited chapter failed. Retain only real
                # exact evidence when the focused correction itself is cited.
                if (prior_review.verdict == "pass"
                        and prior_review.confidence >= min_review_confidence
                        and len(exact) >= 2
                        and any(item.candidate_excerpt in focus_body
                                and item.source_excerpt in (instruction + canon[-1]) for item in exact)):
                    atomic_write_text(candidate_path, resumed_candidate.model_dump_json(indent=2))
                    atomic_write_text(review_path, prior_review.model_copy(update={"evidence": exact}).model_dump_json(indent=2))
                    trace.record(f"{stage}.review-reuse", "completed",
                                 "保留已通过的定点审查与两条逐字依据；舍弃无法定位的附加引文",
                                 metadata={"exact_evidence": len(exact),
                                           "discarded_paraphrases": len(prior_review.evidence) - len(exact)})
                    return resumed_candidate
        if stage == "chapter-window" and resumed_candidate is not None and creative_revision:
            # A new creative requirement invalidates review of the old text.
            # Preserve the saved candidate as context, then let Writer revise
            # before Editor judges the changed objective.
            previous_candidate = _stage_text(resumed_candidate)
            resumed_candidate = None
            trace.record(f"{stage}.resume-revise", "completed",
                         "本次提出新的剧情或节奏要求；先定点修订旧候选，再审核")
        for attempt in range(3):
            verify_canon()
            if attempt == 0 and resumed_candidate is not None:
                candidate = resumed_candidate
            else:
                revision_context = (
                    (f"\n\n【上一版候选；整段结构仍有问题，可重组目标章节的场景和行动，"
                     f"只保留已成立的事实与上层约束】\n{previous_candidate}\n{feedback}")
                    if replan_window else
                    f"\n\n【上一版候选，作为定点修订底稿】\n{previous_candidate}\n{feedback}"
                ) if previous_candidate else ""
                replan_note = ("\n【范围说明】这是用户授权的多章重排，可以重新分配目标窗口的行动、场景和转折；"
                               "不必保留旧规划的逐章流程。不得改已接受正文、大纲和卷细纲，也不得凭空送来关键证据。"
                               if replan_window else "")
                writer = await engine.provider.generate_json(
                    system_prompt=_WRITER_SYSTEM,
                    user_prompt=f"{source}\n\n【用户最新授权】\n{instruction}\n\n【本层任务】\n{task}{replan_note}{revision_context}",
                    output_model=model_type, effort="high", thinking=False,
                    max_tokens=engine.settings.max_output_tokens,
                    timeout_seconds=engine.settings.planning_timeout_seconds, agent_role="writer",
                )
                candidate = writer.data
                if focus == "chapter-window" and stage == "chapter-window" and active_source_run is not None:
                    original = RollingPlanV2.model_validate_json(_read(active_source_run / "chapter-window-candidate.json"))
                    changed = {item.chapter_no: item for item in candidate.chapters}
                    if not focus_chapters.issubset(changed):
                        raise ValidationGateError("Writer 未返回完整目标章节修订，原规划保持不变。")
                    candidate = candidate.model_copy(update={
                        "anchor_chapter": original.anchor_chapter,
                        "anchor_summary": original.anchor_summary,
                        "chapters": [changed[item.chapter_no] if item.chapter_no in focus_chapters else item
                                     for item in original.chapters],
                    })
                previous_candidate = _stage_text(candidate)
                trace.record_model(f"{stage}.writer.{attempt + 1}", writer, "Writer 生成隔离规划候选")
            atomic_write_text(candidate_path, candidate.model_dump_json(indent=2))
            if stage == "chapter-window" and replan_window:
                pattern_issue = _repeated_user_rejected_pattern(latest_instruction, candidate)
                if pattern_issue:
                    trace.record(f"{stage}.user-constraint.{attempt + 1}", "requires_revision",
                                 pattern_issue, metadata={"source": "explicit_user_request"})
                    if attempt == 2:
                        raise PlanningNeedsAttention(
                            f"近期规划仍重复用户明确拒绝的行动结构；候选已保留，未覆盖生效规划。{pattern_issue}",
                            "从近期规划这一层调整 Writer 的章节行动与后果；大纲、细纲和已接受正文不重做。",
                        )
                    feedback = f"【用户明确要求的结构未兑现】\n{pattern_issue}"
                    continue
            source_for_review = (f"【用户当前要求】\n{instruction}\n【已接受锚点正文】\n{canon[-1]}\n【时序化事实与伏笔】\n{state_context}\n"
                                 f"【已生效全书大纲】\n{_render_outline(active_outline)}\n"
                                 f"【已生效第二卷细纲】\n{_render_detail(active_detail)}"
                                 if focus == "chapter-window" and stage == "chapter-window"
                                 and active_outline is not None and active_detail is not None
                                 else source)
            quote_bank = []
            for segment in re.split(r"(?<=[。！？；\n])", source_for_review):
                excerpt = segment.strip()
                if 12 <= len(excerpt) <= 180 and any(word in excerpt for word in ("名单", "交货单", "钥匙", "铁盒")):
                    quote_bank.append(excerpt)
                if len(quote_bank) >= 24:
                    break
            quotes = "\n".join(f"- {item}" for item in quote_bank)
            review_prompt = (f"【来源】\n{source_for_review}\n\n【可逐字复制的来源短句，引用时不要带项目符号】\n{quotes}"
                             f"\n\n【用户要求】\n{instruction}\n\n【当前层任务】\n{task}\n\n【候选】\n{previous_candidate}")
            reviewer = await engine.provider.generate_json(
                system_prompt=_EDITOR_SYSTEM,
                user_prompt=review_prompt,
                output_model=PlanningReviewV2, effort="high", thinking=False,
                max_tokens=min(engine.settings.max_output_tokens, 8192),
                timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
            )
            review = reviewer.data
            atomic_write_text(review_path, review.model_dump_json(indent=2))
            trace.record_model(f"{stage}.editor.{attempt + 1}", reviewer, f"规划审核：{review.verdict}")
            if review.verdict == "insufficient_context":
                raise PlanningNeedsAttention(
                    f"{stage} 缺少审核所必需的来源；候选已保留。审核说明：{review.summary}",
                    "补齐审核指出的具体来源后从本层继续；已通过的大纲或细纲不必重做。",
                )
            candidate_text = _stage_text(candidate)
            review_source = source_for_review + "\n" + instruction + "\n" + task
            def grounded_item(item: Any) -> bool:
                if item.candidate_excerpt not in candidate_text:
                    return False
                if item.source_excerpt in review_source:
                    return True
                # A rolling plan can contradict its own preceding chapter.
                # Pass still requires an independent canon/outline anchor.
                return (stage == "chapter-window" and review.verdict == "revise"
                        and item.source_excerpt in candidate_text)
            exact_evidence = [
                item for item in review.evidence
                if grounded_item(item)
            ]
            # Only grounded findings may affect the candidate. One malformed
            # extra citation must not hide a separate, well-located defect.
            grounded = bool(exact_evidence)
            if grounded and len(exact_evidence) != len(review.evidence):
                review = review.model_copy(update={"evidence": exact_evidence})
                atomic_write_text(review_path, review.model_dump_json(indent=2))
                trace.record(f"{stage}.review-evidence", "completed",
                             "仅保留逐字可定位的审核依据；无效附加引文不掩盖已定位的问题",
                             metadata={"exact_evidence": len(exact_evidence)})
            if not grounded or (review.verdict == "pass" and review.confidence < min_review_confidence):
                # A citation or confidence problem belongs to Editor, not Writer.
                # Do not rewrite a sound candidate merely to fix a review report.
                for citation_attempt in range(1):
                    invalid = [
                        {"candidate_excerpt": item.candidate_excerpt,
                         "candidate_found": item.candidate_excerpt in candidate_text,
                         "source_excerpt": item.source_excerpt,
                         "source_found": item.source_excerpt in review_source or
                         (stage == "chapter-window" and review.verdict == "revise"
                          and item.source_excerpt in candidate_text)}
                        for item in review.evidence
                        if not grounded_item(item)
                    ]
                    audit = await engine.provider.generate_json(
                        system_prompt=_EDITOR_SYSTEM,
                        user_prompt=(f"【来源】\n{source_for_review}\n\n【可逐字复制的来源短句，引用时不要带项目符号】\n{quotes}\n\n【用户要求】\n{instruction}\n\n【当前层任务】\n{task}"
                                     f"\n\n【候选】\n{candidate_text}\n\n【你上一份审核】\n{review.model_dump_json()}"
                                     f"\n\n【程序核出的无效引文】\n{json.dumps(invalid, ensure_ascii=False)}"
                                     f"\n自动放行的把握度底线为 {min_review_confidence:.0%}。"
                                     "请只重核依据与判断；引文格式或把握度问题不能要求 Writer 重写。"
                                     "若候选含无来源的关键事实，判 revise 并给最小修改指令；"
                                     "若仍可通过，只给 1～3 条强证据，每条两个 excerpt 都必须是"
                                     "来源与候选中的一整段连续原文；近期规划的 revise 也可引用候选内前后两章原句，"
                                     "pass 仍须外部来源。不加事实 ID、引号、删节号，也不要拼接。"),
                        output_model=PlanningReviewV2, effort="high", thinking=False,
                        max_tokens=min(engine.settings.max_output_tokens, 8192),
                        timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
                    )
                    review = audit.data
                    atomic_write_text(review_path, review.model_dump_json(indent=2))
                    trace.record_model(f"{stage}.editor-citation-repair.{attempt + 1}.{citation_attempt + 1}", audit,
                                       f"仅重核引用：{review.verdict}")
                    exact_evidence = [
                        item for item in review.evidence
                        if grounded_item(item)
                    ]
                    grounded = bool(exact_evidence)
                    if grounded and len(exact_evidence) != len(review.evidence):
                        review = review.model_copy(update={"evidence": exact_evidence})
                        atomic_write_text(review_path, review.model_dump_json(indent=2))
                    if grounded and (review.verdict != "pass" or review.confidence >= min_review_confidence):
                        break
            if review.verdict == "insufficient_context":
                raise PlanningNeedsAttention(
                    f"{stage} 定点复核后仍缺少必要来源；候选已保留。审核说明：{review.summary}",
                    "补齐审核指出的具体来源后从本层继续；不要重复生成已经通过的上游内容。",
                )
            # A proposed future plan can omit ordinary transitions. Ask the
            # same Editor to challenge an omission-based rejection before it
            # spends another Writer call. Keep the candidate and source prefix
            # unchanged so the follow-up can benefit from provider caching.
            if (stage == "chapter-window" and review.verdict == "revise" and grounded
                    and any(re.search(r"未(?:说明|交代|写明|明确)|可能|容易|建议|风险|模糊", item.finding)
                            for item in review.evidence)):
                challenge = await engine.provider.generate_json(
                    system_prompt=_EDITOR_SYSTEM,
                    user_prompt=(review_prompt + "\n\n【同一审核的共存复核】\n" + review.model_dump_json()
                                 + "\n逐项判断引用的两个事实能否同时成立；日常动作或先后发生的新事件无需额外证明。"
                                 "若仍判 revise，指出排他性矛盾或缺失的必需因果链；"
                                 "若只是可能误读、少一句说明，改判 pass 并将建议留在 summary。"
                                 "不要让 Writer 为审核措辞问题重写。"),
                    output_model=PlanningReviewV2, effort="high", thinking=False,
                    max_tokens=min(engine.settings.max_output_tokens, 8192),
                    timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
                )
                challenged = challenge.data
                trace.record_model(f"{stage}.editor-coexistence.{attempt + 1}", challenge,
                                   f"复核同真性：{challenged.verdict}")
                challenged_evidence = [item for item in challenged.evidence
                                       if item.candidate_excerpt in candidate_text
                                       and item.source_excerpt in review_source]
                if (challenged.verdict == "pass" and challenged.confidence >= min_review_confidence
                        and challenged_evidence):
                    review = challenged.model_copy(update={"evidence": challenged_evidence})
                    grounded = True
                    atomic_write_text(review_path, review.model_dump_json(indent=2))
                elif challenged.verdict == "revise":
                    # Preserve only grounded new findings; an unsupported
                    # challenge cannot replace the original located report.
                    revised_evidence = [item for item in challenged.evidence if grounded_item(item)]
                    if revised_evidence:
                        review = challenged.model_copy(update={"evidence": revised_evidence})
                        atomic_write_text(review_path, review.model_dump_json(indent=2))
            if review.verdict == "pass" and review.confidence >= min_review_confidence and grounded:
                return candidate
            if not grounded or review.verdict == "pass":
                raise PlanningNeedsAttention(
                    f"{stage} 的审核依据仍无法逐字定位或把握度不足；"
                    "候选已保留，未让 Writer 为审核报告问题重写。",
                    "先核对审核报告引用的实际来源与把握度，从本层定点复核；不重做已通过的上游规划。",
                )
            findings = "\n".join(
                f"候选原句：{item.candidate_excerpt}\n对照原句：{item.source_excerpt}\n问题：{item.finding}"
                for item in review.evidence
            )
            feedback = (f"【审核对整体推进的观察，仅下列逐字证据作为阻断依据】\n{review.summary}\n"
                        f"【已定位的需修问题】\n{findings}\n"
                        "只处理上面有原句依据的问题，并顺读前后章节确认因果；"
                        "未定位的附加建议不是硬要求。若重复动作跨越整个目标窗口，可重组这些章节，"
                        "不要只替换几句收尾；其他已成立的事实保持原样。")
        leading_issue = review.evidence[0].finding if review.evidence else review.summary
        raise PlanningNeedsAttention(
            f"{stage} 两轮定向修订后仍未通过独立审核；当前具体问题：{leading_issue}"
            f"。候选与审核记录已保留在 {trace.run_dir}，未覆盖生效规划。",
            "先核对这条问题是关键因果矛盾还是未来规划可合理补足的细节；"
            "误读只重核审核，真矛盾只修本层，不重跑已通过的大纲和细纲。",
        )

    async def publish(stage: str, filename: str, rendered: str, source_hash: str) -> None:
        active = project.root / filename
        previous = _read(active)
        if previous and content_hash(previous) != content_hash(rendered):
            history = project.root / "planning" / "history" / f"{stage}-{utc_now().replace(':', '-')}-{content_hash(previous)[:10]}.md"
            atomic_write_text(history, previous)
        atomic_write_text(active, rendered)
        trace.record(f"{stage}.publish", "prepared", f"{stage} 已写入，等待整组三层提交", metadata={"path": filename, "source_hash": source_hash})

    try:
        outline_task = (f"重设计全书大纲约5000～25000字，必须有主线、背景设定、重要场景、人物变化、主要高潮、大致结局和连续的卷级方向。"
                        f"前 {anchor} 章已经发生，不得更改；后续卷数与总章数可随剧情合理调整。第一卷包含第1～{anchor}章，后续每卷至少10章。body 不写逐章计划。")
        if active_outline is not None:
            outline = active_outline
            atomic_write_text(trace.run_dir / "outline-candidate.json", outline.model_dump_json(indent=2))
            trace.record("outline.reuse", "completed", "沿用已生效大纲；本轮不重新生成")
        else:
            outline = await generate_and_review("outline", BookOutlineV2, stable_sources, outline_task)
        if outline.volumes[0].chapter_end != anchor or len(outline.volumes) < 2:
            raise ValidationGateError("大纲没有保留已接受第一卷边界并规划后续卷；候选未发布。")
        outline_text = _render_outline(outline)
        volume = next((item for item in outline.volumes if item.chapter_start <= anchor + 1 <= item.chapter_end), None)
        if volume is None or end > volume.chapter_end:
            raise ValidationGateError("用户要求的近期范围跨越或超出本次卷边界；大纲已保存，需按涉及卷逐个细化。")
        detail_task = (f"只细化第 {volume.volume_no} 卷第 {volume.chapter_start}～{volume.chapter_end} 章，body 约5000～25000字。"
                       "写主支线、前因后果、场景和新人物特征、冲突升级、伏笔种收与高潮；rough_chapter_beats 是粗略章程，不写逐章完整执行卡。")
        if active_detail is not None:
            detail = active_detail
            atomic_write_text(trace.run_dir / "volume-detail-candidate.json", detail.model_dump_json(indent=2))
            trace.record("volume-detail.reuse", "completed", "沿用已生效卷细纲；本轮不重新生成")
        else:
            detail = await generate_and_review("volume-detail", VolumeDetailV2, stable_sources + "\n【已审核大纲候选】\n" + outline_text, detail_task)
        if (detail.volume_no, detail.chapter_start, detail.chapter_end) != (volume.volume_no, volume.chapter_start, volume.chapter_end):
            raise ValidationGateError("卷细纲改变了大纲卷边界，候选未发布。")
        detail_text = _render_detail(detail)
        plan_task = (f"生成第 {anchor}～{end} 章规划；第 {anchor} 章已是正史，只写100～500字衔接摘要，不改它。"
                     f"chapters 仅含第 {anchor + 1}～{end} 章，每章 body 约250～500字，含人物认知、场景、核心行动、阻力选择与后果、伏笔、钩子和收尾。"
                     "与大纲主线和本卷细纲基本吻合、接住已发生事实即可；未来细节允许有因果衔接的变化和留白，不能把旧提案当成不可变正史。"
                     "让这段规划有实质推进：相邻章节不要反复去同一处问同一件事、回铺抄一遍再等待；安排新的选择、阻力或后果，不重复已经发生的事件。")
        plan = await generate_and_review("chapter-window", RollingPlanV2, stable_sources + "\n【已审核大纲候选】\n" + outline_text + "\n【已审核卷细纲候选】\n" + detail_text, plan_task)
        expected = list(range(anchor + 1, end + 1))
        if plan.anchor_chapter != anchor or [item.chapter_no for item in plan.chapters] != expected:
            raise ValidationGateError("近期规划没有准确覆盖用户范围，候选未发布。")
        plan_text = _render_window(plan)
        async with project_write_lock(project.root):
            verify_canon()
            for filename, original_hash in active_start_hashes.items():
                if content_hash(_read(project.root / filename)) != original_hash:
                    raise PlanningNeedsAttention(
                        f"{filename} 在规划期间被修改；候选保留，未覆盖新版本。",
                        "先比较新旧规划的受影响章节，再从相应层续接；不会静默覆盖用户编辑。",
                    )
            prior_brief = project.db.get_brief()
            last_chapter = outline.volumes[-1].chapter_end
            revised_brief = prior_brief.model_copy(update={
                "estimated_chapters": last_chapter, "estimated_volumes": len(outline.volumes),
            })
            revised_book, scale_matches = re.subn(
                r"(?m)^- 预计规模：\d+ 卷 / \d+ 章$",
                f"- 预计规模：{len(outline.volumes)} 卷 / {last_chapter} 章", book,
            )
            manifest = {
                "protocol_version": 2, "status": "active", "published_at": utc_now(),
                "source_canon_hash": frozen,
                "accepted_anchor": anchor, "volume_no": volume.volume_no,
                "chapter_window": [anchor, end],
                "outline_hash": content_hash(outline_text),
                "volume_detail_hash": content_hash(detail_text),
                "recent_plan_hash": content_hash(plan_text),
                "book_hash": content_hash(revised_book),
                "trace_id": trace.run_id,
                "book_scale_updated": scale_matches == 1,
                "legacy_plan_status": "stale_future_only",
            }
            targets = ["OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md", "BOOK.md",
                       "planning/cleanup-suggestions.json", "planning/active-v2.json"]
            before = {name: ((project.root / name).is_file(), _read(project.root / name)) for name in targets}
            try:
                await publish("outline", "OUTLINE.md", outline_text, frozen)
                await publish("volume-detail", "STORY_DETAIL.md", detail_text, content_hash(outline_text))
                await publish("chapter-window", "RECENT_PLAN.md", plan_text, content_hash(outline_text + detail_text))
                if scale_matches == 1:
                    if revised_book != book:
                        atomic_write_text(
                            project.root / "planning" / "history" /
                            f"book-scale-{utc_now().replace(':', '-')}-{content_hash(book)[:10]}.md",
                            book,
                        )
                    atomic_write_text(project.root / "BOOK.md", revised_book)
                cleanup = _cleanup_suggestions(project)
                atomic_write_text(project.root / "planning" / "cleanup-suggestions.json",
                                  json.dumps({"generated_at": utc_now(), "candidates": cleanup}, ensure_ascii=False, indent=2))
                project.db.set_brief(revised_brief)
                # The manifest is written last: absent or mismatched hashes mean
                # that a crashed publication must not be treated as active.
                atomic_write_text(project.root / "planning" / "active-v2.json",
                                  json.dumps(manifest, ensure_ascii=False, indent=2))
                trace.record("planning-v2.publish", "completed", "三层规划与规模设置已整组生效",
                             metadata={"manifest": "planning/active-v2.json", "outline_hash": manifest["outline_hash"]})
            except Exception:
                for name, (existed, old_content) in before.items():
                    path = project.root / name
                    if existed:
                        atomic_write_text(path, old_content)
                    elif path.is_file():
                        path.unlink()
                project.db.set_brief(prior_brief)
                trace.record("planning-v2.publish", "rolled_back", "整组发布遇错，已恢复原生效文件与规模设置")
                raise
        trace.finish(summary=f"大纲、卷细纲和第 {anchor}～{end} 章规划完成")
        return {"status": "planned", "chapter_range": [anchor, end], "future_range": [anchor + 1, end],
                "volume_no": volume.volume_no, "outline_path": str(project.root / "OUTLINE.md"),
                "detail_path": str(project.root / "STORY_DETAIL.md"), "plan_path": str(project.root / "RECENT_PLAN.md"),
                "trace_id": trace.run_id, "cleanup_candidates": cleanup,
                "next_action": "三层规划已审核并生效；旧候选已由软件列为待核对清理项，未删除文件。"}
    except asyncio.CancelledError:
        trace.record("planning-v2", "cancelled", "已停止；本阶段候选保留，未发布未审核结果")
        trace.finish(status="cancelled", summary="规划已停止，现有正史不变")
        raise
    except PlanningNeedsAttention as exc:
        trace.record("planning-v2", "waiting_condition", "候选和上游成果已保留，等待定点处理", str(exc))
        trace.finish(status="waiting_condition", summary=str(exc))
        raise
    except Exception as exc:
        trace.record("planning-v2", "failed", "规划停在受影响阶段；候选保留", str(exc))
        trace.finish(status="failed", summary=str(exc))
        raise
