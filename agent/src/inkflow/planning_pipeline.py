"""Versioned whole-book -> volume -> rolling-window planning for existing novels.

Candidates remain in the run directory until a separate editorial verdict passes.
Only the engine publishes active Markdown and SQLite execution projections;
no candidate is canon.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import sqlite3
from uuid import uuid4
from pathlib import Path
from typing import Any

from .errors import InkFlowError, ValidationGateError
from .project import InkFlowProject, render_book_brief
from .project_lock import project_write_lock, project_write_lock_sync
from .schemas import (ArcPlan, ArcSummary, BookOutlineV2, BookPlan, ChapterCard,
                      PlanBundle, PlanningReviewV2, RollingPlanV2, VolumeCompass,
                      VolumeDetailV2, VolumePlan)
from .trace import TraceRecorder
from .task_settings import active_task_settings, capture_task_settings, validate_task_settings_snapshot
from .utils import SOURCE_RECOVERY_TOKEN_LIMIT, atomic_write_text, content_hash, estimate_tokens, utc_now


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
_EDITOR_SYSTEM += """\n交卷前专门核对全篇关键物件与人物的终态：同一物件若写成仍失踪/不归还，又写成主角拿到或被人放回，须解释是否确为不同物件，否则判 revise；“最后一次出现”若在不同卷重复，也须判 revise 并引用前后原句。不能只审核每段局部合理性。"""


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


def _accepted_text(project: InkFlowProject, row: dict[str, Any]) -> str:
    """Read the accepted authority; old databases may have only a hash-bound projection."""
    try:
        text = project.db.canonical_chapter_content(int(row["chapter_no"]))
        if text is None:
            path = (project.root / str(row["path"])).resolve()
            text = _read(path) if path.is_relative_to(project.root.resolve()) else ""
    except (OSError, UnicodeError):
        return ""
    return text if text and content_hash(text) == row["content_hash"] else ""


def _planning_thread_evidence(project: InkFlowProject, accepted: list[dict[str, Any]], *,
                              anchor: int, end: int, included: set[int],
                              token_limit: int) -> tuple[str, dict[str, Any]]:
    """Attach bounded original-text leads for early unresolved promises, never invented proof."""
    from .retrieval import HybridRetriever

    early = [item for item in project.db.threads_as_of(anchor)
             if any(0 < int(item.get(key) or 0) < anchor - 2
                    for key in ("planted_chapter", "last_advanced_chapter"))]
    early.sort(key=lambda item: (not (item.get("due_chapter") and int(item["due_chapter"]) <= end),
        item.get("kind") not in {"promise", "foreshadow", "mystery"},
        int(item.get("due_chapter") or 999999), str(item["thread_id"])))
    selected = early[:4]
    queries = [f"{item['title']} {item['description']}"[:160] for item in selected]
    thread_records = [{"thread_id": item["thread_id"], "title": str(item["title"])[:160],
        "source_chapters": [int(item[key]) for key in ("planted_chapter", "last_advanced_chapter") if item.get(key)],
        "status": "资料不足：尚未读到可定位原文，不证明伏笔不存在"} for item in selected]
    intro = ("\n【早期未结伏笔的原文补读】\n索引只负责指路；下面的连续原文须核对对象、时间和叙事视角，"
             "检索相关或原文存在不等于支持结论。未选中、未命中、节选外内容都不能据此判不存在。"
             "新未来设想无需旧章预先出现；只有关键过去前提仍缺必要来源才判资料不足。\n")
    excerpt_limit = max(0, token_limit - estimate_tokens(intro + json.dumps(thread_records, ensure_ascii=False) + "\n\n\n" * 6))
    retriever = HybridRetriever(project)
    hits, diagnostics = retriever.recover_review_sources(queries, chapter_no=anchor + 1) if queries and excerpt_limit else ([], {})
    rows = {int(item["chapter_no"]): item for item in accepted}
    # Resolve known planting/advance identities before using fuzzy-search leads.
    requested = list(dict.fromkeys(
        int(item.get(key) or 0) for key in ("planted_chapter", "last_advanced_chapter")
        for item in selected if int(item.get(key) or 0) in rows))
    requested.extend(int(item["chapter_no"]) for item in hits)
    requested = list(dict.fromkeys(number for number in requested if number not in included))[:6]
    loaded, missing, excerpts = [], [], []
    spent = 0
    query = " ".join(queries)
    for number in requested:
        row = rows[number]
        text = _accepted_text(project, row)
        if not text:
            missing.append({"chapter_no": number, "reason": "已接受原文缺失或哈希不符"})
            continue
        parts = [{"source_id": str(offset), "title": "", "body": text[offset:offset + 5000]}
                 for offset in range(0, len(text), 1000)]
        ranking = retriever._bm25_ranking(query, parts)
        start = int(ranking[0][0]) if ranking else 0
        excerpt = text[start:start + 5000]
        label = f"【第 {number} 章已接受原文；版本 {row['version']}；位置 {start}:{start + len(excerpt)}；哈希 {row['content_hash']}】\n"
        if spent + estimate_tokens(label + excerpt) > excerpt_limit:
            missing.append({"chapter_no": number, "reason": "补读上下文预算不足，未作为已读来源"})
            continue
        spent += estimate_tokens(label + excerpt)
        excerpts.append(label + excerpt)
        loaded.append({"chapter_no": number, "version": int(row["version"]),
            "content_hash": row["content_hash"], "start": start, "end": start + len(excerpt),
            "excerpt": excerpt, "status": "原文已定位，是否支持该伏笔仍由当前审核角色核对"})
    available = included | {item["chapter_no"] for item in loaded}
    for record in thread_records:
        if any(number in available for number in record["source_chapters"]):
            record["status"] = "有原文可核对"
    record = {"policy": "planning-thread-evidence-v1", "queries": queries, "threads": thread_records,
        "loaded_sources": loaded, "missing_sources": missing, "search": diagnostics,
        "unselected_thread_ids": [item["thread_id"] for item in early[4:]],
        "added_tokens": spent, "token_limit": token_limit}
    if not selected or not excerpt_limit:
        return "", record
    context = intro + json.dumps(thread_records, ensure_ascii=False) + "\n" + "\n\n".join(excerpts)
    record["added_tokens"] = estimate_tokens(context)
    return context, record


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
        for field, label in (("time_location", "时地"), ("goal", "目标"), ("obstacle", "阻力"),
                             ("decision", "选择"), ("consequence", "后果"), ("hook_question", "钩子")):
            if getattr(chapter, field):
                lines += [f"- {label}：{getattr(chapter, field)}"]
        if chapter.scenes:
            lines += ["- 场景：" + "；".join(chapter.scenes)]
        if any((chapter.goal, chapter.scenes, chapter.hook_question, chapter.time_location)):
            lines.append("")
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


def recover_planning_publication(project: InkFlowProject, *, user_retry: bool = False) -> str:
    """Replay only a hash-bound, already authorized publication; never call a model."""
    path = project.root / "planning/publication-recovery.json"
    if not path.is_file():
        return ""
    journal: dict[str, Any] = {}
    with project_write_lock_sync(project.root):
        try:
            journal = json.loads(_read(path))
            if not isinstance(journal, dict):
                journal = {}
                raise ValueError("恢复记录不是有效对象")
            if journal.get("status") == "completed":
                return ""
            if journal.get("attempts", 0) >= 2 and not user_retry:
                return str(journal.get("last_error") or "规划恢复次数已用尽；修复后请明确继续原任务。")
            journal.update(status="recovering", attempts=journal.get("attempts", 0) + 1, stage="source_preflight")
            atomic_write_text(path, json.dumps(journal, ensure_ascii=False, indent=2))
            payload = journal["payload"]
            if not isinstance(payload, dict) or content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True)) != journal["payload_hash"]:
                raise ValueError("恢复记录哈希不符")
            validate_task_settings_snapshot(payload["snapshot"], novel_id=project.project_id)
            if payload.get("snapshot_linked"):
                from .studio import StudioService
                with StudioService(project).db.connect() as connection:
                    row = connection.execute("SELECT snapshot_json FROM task_settings_snapshots WHERE task_id=?",
                                             (payload["snapshot"]["task_id"],)).fetchone()
                if row is None or json.loads(row["snapshot_json"]) != payload["snapshot"]:
                    raise ValueError("原任务配置快照缺失或变化")
            # Preflight every source and target before making any recovery write.
            for relative, value in payload["files"].items():
                target = project.resolve_user_path(relative)
                current = _read(target) if target.is_file() else None
                if current not in (value["before"], value["after"]):
                    raise ValueError(f"{relative} 在中断后另有修改，未覆盖")
            for relative, digest in payload["sources"].items():
                if content_hash(_read(project.resolve_user_path(relative, allow_internal=True))) != digest:
                    raise ValueError(f"来源 {relative} 已变化，须定点重新审核")
            accepted = [[row["chapter_no"], row["version"], row["content_hash"]]
                        for row in project.db.accepted_chapters()]
            if accepted != payload["accepted"]:
                raise ValueError("已接受正史已变化，不能重放旧发布")
            from .story_settings import StorySettingsService
            from .preferences import preference_prompt
            if (StorySettingsService(project).source_fingerprint() != payload["settings_source_hash"]
                    or content_hash(preference_prompt(project.db)) != payload["preferences_hash"]):
                raise ValueError("设定或偏好来源已变化，须定点重新审核")
            bundle = PlanBundle.model_validate(payload["bundle"])
            desired = bundle.model_dump(mode="json")
            current_bundle = project.db.get_current_plan_bundle()
            accepted_numbers = {row[0] for row in accepted}
            db_ready = bool(current_bundle and current_bundle.model_dump(mode="json") == desired
                            and all((existing := project.db.get_chapter_card(card.chapter_no)) == card.model_dump(mode="json")
                                    or (card.chapter_no in accepted_numbers and existing is not None)
                                    for card in bundle.current_arc.chapter_cards))
            if not db_ready:
                with project.db.connect() as connection:
                    rows = [dict(row) for row in connection.execute("SELECT * FROM plans ORDER BY kind,plan_key")]
                if rows != payload["old_plans"]:
                    raise ValueError("数据库规划已被其他任务改变，未覆盖")
            brief = project.db.get_brief().model_dump(mode="json")
            if brief not in (payload["prior_brief"], payload["brief"]):
                raise ValueError("书籍简报已被另一个任务修改，未覆盖")
            def stage(name: str) -> None:
                journal["stage"] = name
                atomic_write_text(path, json.dumps(journal, ensure_ascii=False, indent=2))

            for name, value in payload["files"].items():
                if name == "planning/active-v2.json":
                    continue
                stage(name)
                target = project.resolve_user_path(name)
                current_text = _read(target) if target.is_file() else None
                if current_text not in (value["before"], value["after"]):
                    raise ValueError(f"{name} 在恢复期间被修改，未覆盖")
                if current_text != value["after"]:
                    atomic_write_text(target, value["after"])
            stage("database.execution_projection")
            if brief != payload["brief"]:
                from .schemas import BookBrief
                project.db.set_brief(BookBrief.model_validate(payload["brief"]))
            if not db_ready:
                project.db.save_plan_bundle(bundle, supersede_after_chapter=payload["manifest"]["accepted_anchor"])
            stage("planning/active-v2.json")
            for relative, digest in payload["sources"].items():
                if content_hash(_read(project.resolve_user_path(relative, allow_internal=True))) != digest:
                    raise ValueError(f"来源 {relative} 在恢复期间变化，尚未发布清单")
            manifest_text = payload["files"]["planning/active-v2.json"]["after"]
            manifest_path = project.resolve_user_path("planning/active-v2.json")
            current_manifest_text = _read(manifest_path) if manifest_path.is_file() else None
            if current_manifest_text not in (payload["files"]["planning/active-v2.json"]["before"], manifest_text):
                raise ValueError("生效清单在恢复期间被修改，未覆盖")
            if current_manifest_text != manifest_text:
                atomic_write_text(manifest_path, manifest_text)
            stage("publication.finalize")
            from .manual_edits import ManualEditsService
            manual = ManualEditsService(project)
            if payload["focus"] != "chapter-window":
                manual.reconcile_planning(payload["source_hashes"], payload["manifest"]["trace_id"])
            for name in ("OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md", "BOOK.md"):
                manual.note_engine_write(name, payload["files"][name]["after"])
            pending = project.db.get_metadata("pending_planning_publication", {})
            if isinstance(pending, dict) and pending.get("run_id") in {payload["approved_run_id"], payload["manifest"]["trace_id"]}:
                project.db.set_metadata("pending_planning_publication", {})
            journal.update(status="completed", last_error="", stage="completed")
            audit_path = project.internal / "runs" / payload["manifest"]["trace_id"] / "publication-record.json"
            atomic_write_text(audit_path, json.dumps(journal, ensure_ascii=False, indent=2))
            # Keep startup reads small; the complete recovery record remains auditable in its run.
            atomic_write_text(path, json.dumps({"status": "completed", "manifest": payload["manifest"],
                "task_id": payload["snapshot"]["task_id"], "history_choice": payload["history_choice"],
                "audit_record": str(audit_path.relative_to(project.root)), "payload_hash": journal["payload_hash"]},
                ensure_ascii=False, indent=2))
            for item in project.pending_work():
                if item["kind"] == "planning_publication" and item["source"].get("run_id") == payload["manifest"]["trace_id"]:
                    project.update_pending_work(item["id"], status="completed", resolution_run_id=payload["manifest"]["trace_id"])
            project.planning_recovery_result = {"status": "planned", "trace_id": payload["manifest"]["trace_id"],
                "revision_no": payload["manifest"]["revision_no"], "recovered": True,
                "chapter_range": payload["manifest"]["chapter_window"],
                "pending_history_choice": bool(payload["history_choice"])}
            return ""
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, InkFlowError) as exc:
            message = f"规划发布未完成：步骤 {journal.get('stage', '恢复记录读取')}；{type(exc).__name__}：{exc}。"
            journal.update(status="failed", last_error=message)
            if journal.get("payload"):
                try:
                    atomic_write_text(path, json.dumps(journal, ensure_ascii=False, indent=2))
                except OSError:
                    pass  # The original durable journal still records the interrupted stage.
            failed_payload = journal.get("payload")
            failed_manifest = failed_payload.get("manifest") if isinstance(failed_payload, dict) else None
            try:
                project.record_pending_work(kind="planning_publication", reason=message,
                    next_action="核对上述位置并修复文件、权限或来源；明确继续原任务后重核，再续接未完成步骤。",
                    source={"run_id": failed_manifest.get("trace_id", "unknown") if isinstance(failed_manifest, dict) else "unknown"},
                    status="failed")
            except (sqlite3.Error, OSError):
                pass  # Return the original cause even if diagnostic storage is also unavailable.
            return message


def load_active_planning(project: InkFlowProject, *, allow_document_edits: bool = False) -> tuple[dict[str, Any], BookOutlineV2, VolumeDetailV2, RollingPlanV2] | None:
    """Resolve the active v2 hierarchy by manifest and content, never by file age."""
    manifest_path = project.root / "planning" / "active-v2.json"
    journal_path = project.root / "planning" / "publication-recovery.json"
    if journal_path.is_file():
        try:
            journal = json.loads(_read(journal_path))
            if not isinstance(journal, dict):
                raise ValueError("恢复记录不是有效对象")
        except (OSError, ValueError, TypeError) as exc:
            raise PlanningNeedsAttention(f"规划恢复记录无法读取：{exc}", "请先恢复 planning/publication-recovery.json，再核对发布步骤。") from exc
        if journal.get("status") != "completed":
            raise PlanningNeedsAttention(
                "规划发布仍有未完成恢复步骤：" + str(journal.get("last_error") or journal.get("stage")),
                "修复提示中的文件或环境后继续原规划任务；不会使用半套规划写后章。")
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(_read(manifest_path))
        source_id = str(manifest["trace_id"])
        if manifest.get("status") != "active" or Path(source_id).name != source_id:
            raise ValueError("invalid planning manifest")
        revision_no = manifest.get("revision_no", 1)
        if not isinstance(revision_no, int) or isinstance(revision_no, bool) or revision_no < 1:
            raise ValueError("invalid formal planning revision")
        for filename, key in (("OUTLINE.md", "outline_hash"),
                              ("STORY_DETAIL.md", "volume_detail_hash"),
                              ("RECENT_PLAN.md", "recent_plan_hash")):
            if content_hash(_read(project.root / filename)) != manifest[key]:
                if allow_document_edits:
                    # Explicit full redesign may replace edited projections; they are never returned as active plans.
                    return None
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
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        reason = f"生效规划的来源版本不一致：{exc}。原规划和正文均未改。"
        observed: dict[str, str | None] = {}
        for name in ("planning/active-v2.json", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"):
            try:
                observed[name] = content_hash(_read(project.root / name))
            except (OSError, UnicodeError):
                observed[name] = None
        try:
            project.record_pending_work(kind="planning_source", reason=reason,
                next_action="核对具体来源文件，恢复版本一致后从受影响层继续；采用手改内容须重新审核。",
                source={"manifest_hash": observed.pop("planning/active-v2.json"), "files": observed})
        except (OSError, sqlite3.Error):
            pass  # The original source failure must remain visible when diagnostics cannot be saved.
        raise PlanningNeedsAttention(
            reason,
            "在规划工作区核对修改或缺失的来源文件，恢复版本一致后只从受影响层继续。",
        ) from exc


def v2_chapter_card(item: Any, brief: Any) -> ChapterCard:
    return ChapterCard(
        chapter_no=item.chapter_no, title_working=item.title, pov=brief.protagonist,
        time_location=item.time_location or item.body, function=item.body,
        goal=item.goal or item.body, obstacle=item.obstacle or item.body,
        decision=item.decision or item.body, consequence=item.consequence or item.body,
        irreversible_delta=item.consequence or "不得撤销前章已发生事实；本章变化以近期规划原文为准",
        scenes=item.scenes or [item.body], information_release="只使用角色有途径获得的信息",
        hook_type="问题", hook_question=item.hook_question or item.body,
        target_words=brief.target_chapter_words,
        dependencies=[f"v2近期规划第{item.chapter_no}章", "已接受正史"],
    )


def v2_execution_bundle(project: InkFlowProject, manifest: dict[str, Any], outline: BookOutlineV2,
                        detail: VolumeDetailV2, window: RollingPlanV2) -> PlanBundle:
    """Project the reviewed v2 hierarchy into the existing chapter-card contract."""
    brief = project.db.get_brief()
    direction = next(item for item in outline.volumes if item.volume_no == detail.volume_no)
    first, last = window.chapters[0].chapter_no, window.chapters[-1].chapter_no
    key = f"v2:{manifest['trace_id']}:{first}-{last}"
    cards = [v2_chapter_card(item, brief) for item in window.chapters]
    arc = ArcPlan(
        arc_id=key, volume_no=detail.volume_no, title=f"第{first}—{last}章执行视图",
        chapter_start=first, chapter_end=last, promise=direction.central_conflict,
        central_conflict=direction.central_conflict, start_state=window.anchor_summary,
        end_state=direction.outcome,
        escalation=["依据生效近期规划推进", "让人物选择产生可见后果"],
        midpoint_turn="随正文自然展开", climax=direction.outcome,
        aftermath="保留已发生结果", exit_bridge=direction.outcome, chapter_cards=cards,
    )
    volume = VolumePlan(
        volume_no=detail.volume_no, title=detail.title, chapter_start=detail.chapter_start,
        chapter_end=detail.chapter_end, promise=direction.central_conflict,
        start_state=window.anchor_summary, end_state=direction.outcome,
        antagonist_pressure=direction.central_conflict,
        midpoint_turn=detail.rough_chapter_beats[len(detail.rough_chapter_beats) // 2],
        climax=direction.outcome, cost_and_result=direction.outcome,
        next_volume_bridge=direction.outcome,
        arcs=[ArcSummary(arc_id=key, title=arc.title, chapter_start=first,
                         chapter_end=last, promise=arc.promise, end_state=arc.end_state)],
    )
    book = BookPlan(
        title=outline.title, premise=outline.body[:1200], reader_promise=outline.body[:1200],
        narrative_engine=direction.central_conflict, main_conflict=direction.central_conflict,
        protagonist_start=window.anchor_summary, protagonist_end=outline.volumes[-1].outcome,
        theme_question=direction.central_conflict, ending_direction=outline.volumes[-1].outcome,
        estimated_chapters=outline.volumes[-1].chapter_end,
        estimated_volumes=len(outline.volumes),
        volume_compass=[VolumeCompass(
            volume_no=item.volume_no, title=item.title, promise=item.central_conflict,
            start_state=item.central_conflict, end_state=item.outcome,
            estimated_chapters=item.chapter_end - item.chapter_start + 1,
        ) for item in outline.volumes],
    )
    return PlanBundle(book=book, current_volume=volume, current_arc=arc)


def synchronize_v2_projection(project: InkFlowProject) -> bool:
    """Bring pre-upgrade projects onto their already reviewed active hierarchy."""
    active = load_active_planning(project)
    if active is None:
        return False
    manifest, outline, detail, window = active
    current = project.db.get_current_plan_bundle()
    expected = v2_execution_bundle(project, manifest, outline, detail, window)
    accepted_numbers = {int(row["chapter_no"]) for row in project.db.accepted_chapters()}
    if (current is not None and current.model_dump(mode="json") == expected.model_dump(mode="json")
            and all((existing := project.db.get_chapter_card(card.chapter_no)) == card.model_dump(mode="json")
                    or (card.chapter_no in accepted_numbers and existing is not None)
                    for card in expected.current_arc.chapter_cards)):
        return False
    archive_id = f"{manifest['trace_id']}-db-sync"
    archive = project.root / "planning" / "history" / f"database-{archive_id}.json"
    with project.db.connect() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM plans")]
        metadata = [dict(row) for row in connection.execute("SELECT * FROM metadata")]
    atomic_write_text(archive, json.dumps({"plans": rows, "metadata": metadata}, ensure_ascii=False, indent=2))
    project.db.save_plan_bundle(expected,
                                supersede_after_chapter=window.anchor_chapter)
    if not manifest.get("revision_no"):
        atomic_write_text(project.root / "planning" / "active-v2.json",
                          json.dumps({**manifest, "revision_no": 1}, ensure_ascii=False, indent=2))
    record = project.root / "planning" / "history" / f"revision-{archive_id}.json"
    atomic_write_text(record, json.dumps({
        "run_id": archive_id, "revision_no": 0, "superseded_by_revision": manifest.get("revision_no") or 1,
        "created_at": utc_now(), "status": "pending", "previous_run_id": None,
        "files": [{"source": "旧版数据库规划", "path": str(archive.relative_to(project.root)),
                   "content_hash": content_hash(_read(archive))}],
    }, ensure_ascii=False, indent=2))
    return True


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
        with project.db.connect() as connection:
            old_rows = [dict(row) for row in connection.execute("SELECT * FROM plans")]
            old_metadata = [dict(row) for row in connection.execute("SELECT * FROM metadata")]
        targets = [*prior, "planning/cleanup-suggestions.json", "planning/active-v2.json"]
        before = {name: _read(project.root / name) for name in targets}
        restored = {**active[0], "published_at": utc_now(), "trace_id": previous_run_id,
                    "revision_no": int(active[0].get("revision_no") or 1) + 1,
                    "previous_run_id": current_run_id,
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
            project.db.save_plan_bundle(v2_execution_bundle(project, restored, outline, detail, window),
                                        supersede_after_chapter=window.anchor_chapter)
            atomic_write_text(project.root / "planning/cleanup-suggestions.json", json.dumps(
                {"generated_at": utc_now(), "candidates": _cleanup_suggestions(project)},
                ensure_ascii=False, indent=2,
            ))
            atomic_write_text(project.root / "planning/active-v2.json", json.dumps(
                restored, ensure_ascii=False, indent=2,
            ))
            load_active_planning(project)
            from .manual_edits import ManualEditsService
            manual_edits = ManualEditsService(project)
            for name, content in prior.items():
                manual_edits.note_engine_write(name, content)
        except Exception:
            with project.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM plans")
                connection.executemany(
                    "INSERT INTO plans(kind,plan_key,parent_key,version,status,data_json,updated_at) "
                    "VALUES (:kind,:plan_key,:parent_key,:version,:status,:data_json,:updated_at)", old_rows)
                connection.execute("DELETE FROM metadata")
                connection.executemany(
                    "INSERT INTO metadata(key,value_json,updated_at) VALUES (:key,:value_json,:updated_at)", old_metadata)
                connection.commit()
            for name, content in before.items():
                atomic_write_text(project.root / name, content)
            project.db.set_brief(prior_brief)
            raise
    return {"restored_run_id": previous_run_id, "chapter_window": restored["chapter_window"]}


def restore_kept_planning_publication(root: str | Path, revision_no: int) -> dict[str, Any]:
    """Reapply a user-selected kept revision as a new formal revision."""
    from .planning_history import kept_record

    project = InkFlowProject(root, recover_on_open=False)
    with project_write_lock_sync(project.root):
        record = kept_record(project, revision_no)
        if any(not (project.root / str(item.get("path") or "")).is_file()
               for item in record.get("files", []) if isinstance(item, dict)):
            raise ValidationGateError("所选历史版已有部分副本被删除，不能直接恢复；可引用仍保留的部分重新规划。")
        previous = record.get("previous_manifest") or {}
        source_id = str(record.get("previous_run_id") or "")
        if (not source_id or Path(source_id).name != source_id or previous.get("trace_id") != source_id
                or previous.get("status") != "active"):
            raise ValidationGateError("所选历史版没有完整的正式发布来源，不能直接恢复；可指定部分供重新规划参考。")
        current = load_active_planning(project)
        if current is None:
            raise ValidationGateError("当前三层规划未形成可核对的生效记录，未恢复旧版。")
        current_manifest = current[0]
        if (previous.get("accepted_anchor") != project.db.latest_accepted_chapter_no()
                or previous.get("accepted_anchor") != current_manifest.get("accepted_anchor")
                or previous.get("source_canon_hash") != current_manifest.get("source_canon_hash")):
            raise ValidationGateError("正史锚点或来源已变化，旧规划不能直接恢复；请明确要求参考旧版重新规划。")
        source = project.internal / "runs" / source_id
        outline = BookOutlineV2.model_validate_json(_read(source / "outline-candidate.json"))
        detail = VolumeDetailV2.model_validate_json(_read(source / "volume-detail-candidate.json"))
        window = RollingPlanV2.model_validate_json(_read(source / "chapter-window-candidate.json"))
        rendered = {"OUTLINE.md": _render_outline(outline), "STORY_DETAIL.md": _render_detail(detail),
                    "RECENT_PLAN.md": _render_window(window)}
        for filename, key in (("OUTLINE.md", "outline_hash"), ("STORY_DETAIL.md", "volume_detail_hash"),
                              ("RECENT_PLAN.md", "recent_plan_hash")):
            if content_hash(rendered[filename]) != previous.get(key):
                raise ValidationGateError("历史候选与原发布记录不一致，未恢复。")
        if all(content_hash(content) == current_manifest.get(key) for (filename, content), key in zip(
                rendered.items(), ("outline_hash", "volume_detail_hash", "recent_plan_hash"))):
            return {"status": "unchanged", "revision_no": current_manifest.get("revision_no", 1),
                    "next_action": "所选历史版内容与当前生效版相同，未创建空修订。"}
        now = utc_now()
        new_revision = int(current_manifest.get("revision_no") or 1) + 1
        restored = {**previous, "published_at": now, "revision_no": new_revision,
                    "previous_run_id": current_manifest["trace_id"], "legacy_plan_status": "replaced_in_database"}
        book_path = project.root / "BOOK.md"
        old_book = _read(book_path)
        revised_book = re.sub(r"(?m)^- 预计规模：\d+ 卷 / \d+ 章$",
                              f"- 预计规模：{len(outline.volumes)} 卷 / {outline.volumes[-1].chapter_end} 章", old_book)
        restored["book_hash"] = content_hash(revised_book)
        revision_id = uuid4().hex
        stamp = now.replace(":", "-")
        targets = [*rendered, "BOOK.md", "planning/active-v2.json", "planning/cleanup-suggestions.json",
                   f"planning/history/revision-{revision_id}.json"]
        before = {name: ((project.root / name).is_file(), _read(project.root / name)) for name in targets}
        with project.db.connect() as connection:
            old_rows = [dict(row) for row in connection.execute("SELECT * FROM plans")]
            old_metadata = [dict(row) for row in connection.execute("SELECT * FROM metadata")]
        prior_brief = project.db.get_brief()
        database_changed = False
        try:
            files = []
            for stage, filename in (("outline", "OUTLINE.md"), ("volume-detail", "STORY_DETAIL.md"),
                                    ("chapter-window", "RECENT_PLAN.md")):
                prior = before[filename][1]
                if prior and prior != rendered[filename]:
                    path = project.root / "planning" / "history" / f"{stage}-{stamp}-{content_hash(prior)[:10]}.md"
                    atomic_write_text(path, prior)
                    files.append({"source": filename, "path": str(path.relative_to(project.root)),
                                  "content_hash": content_hash(prior)})
                atomic_write_text(project.root / filename, rendered[filename])
            if revised_book != old_book:
                atomic_write_text(book_path, revised_book)
            archive = project.root / "planning" / "history" / f"database-{revision_id}.json"
            atomic_write_text(archive, json.dumps({"plans": old_rows, "metadata": old_metadata},
                                                  ensure_ascii=False, indent=2))
            files.append({"source": "旧版数据库规划", "path": str(archive.relative_to(project.root)),
                          "content_hash": content_hash(_read(archive))})
            project.db.set_brief(prior_brief.model_copy(update={
                "estimated_chapters": outline.volumes[-1].chapter_end,
                "estimated_volumes": len(outline.volumes),
            }))
            project.db.save_plan_bundle(v2_execution_bundle(project, restored, outline, detail, window),
                                        supersede_after_chapter=window.anchor_chapter)
            database_changed = True
            atomic_write_text(project.root / f"planning/history/revision-{revision_id}.json",
                              json.dumps({"run_id": revision_id, "revision_no": new_revision - 1,
                                          "superseded_by_revision": new_revision,
                                          "previous_run_id": current_manifest["trace_id"],
                                          "previous_manifest": current_manifest,
                                          "created_at": now, "status": "pending", "files": files},
                                         ensure_ascii=False, indent=2))
            atomic_write_text(project.root / "planning/cleanup-suggestions.json", json.dumps(
                {"generated_at": now, "candidates": _cleanup_suggestions(project)},
                ensure_ascii=False, indent=2))
            atomic_write_text(project.root / "planning/active-v2.json", json.dumps(restored, ensure_ascii=False, indent=2))
            load_active_planning(project)
            from .manual_edits import ManualEditsService
            manual_edits = ManualEditsService(project)
            for name, content in rendered.items():
                manual_edits.note_engine_write(name, content)
            if revised_book != old_book:
                manual_edits.note_engine_write("BOOK.md", revised_book)
        except Exception:
            if database_changed:
                with project.db.connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM plans")
                    connection.executemany(
                        "INSERT INTO plans(kind,plan_key,parent_key,version,status,data_json,updated_at) "
                        "VALUES (:kind,:plan_key,:parent_key,:version,:status,:data_json,:updated_at)", old_rows)
                    connection.execute("DELETE FROM metadata")
                    connection.executemany(
                        "INSERT INTO metadata(key,value_json,updated_at) VALUES (:key,:value_json,:updated_at)", old_metadata)
                    connection.commit()
            project.db.set_brief(prior_brief)
            for name, (existed, content) in before.items():
                path = project.root / name
                if existed:
                    atomic_write_text(path, content)
                elif path.is_file():
                    path.unlink()
            raise
        return {"status": "restored", "revision_no": new_revision,
                "restored_from_revision": revision_no,
                "next_action": "历史版已作为新的正式版本生效；被替换的版本等待你选择保留或删除。"}


async def redesign_existing_story(engine: Any, root: str | Path, *, anchor: int, end: int,
                                  instruction: str, focus: str = "",
                                  approved_run_id: str = "") -> dict[str, Any]:
    """Complete an authorized planning chain, resuming only the failed stage on retry."""
    if end <= anchor or not instruction.strip():
        raise ValidationGateError("请说清以哪一章正史为锚点、要规划到第几章，以及后续剧情方向。")
    project = InkFlowProject(root)
    recovery_path = project.root / "planning/publication-recovery.json"
    continuing = bool(re.search(r"继续|恢复|重试|断点|续修", instruction))
    if (continuing or getattr(project, "planning_recovery_result", None)) and recovery_path.is_file():
        try:
            recovery = json.loads(_read(recovery_path))
            if not isinstance(recovery, dict):
                raise ValueError("恢复记录不是有效对象")
            if recovery.get("status") == "completed":
                audit = json.loads(_read(project.resolve_user_path(recovery["audit_record"], allow_internal=True)))
                if (audit.get("status") != "completed" or audit["payload_hash"] != recovery["payload_hash"]
                        or content_hash(json.dumps(audit["payload"], ensure_ascii=False, sort_keys=True)) != recovery["payload_hash"]
                        or audit["payload"]["manifest"] != recovery["manifest"]
                        or audit["payload"]["snapshot"]["task_id"] != recovery["task_id"]):
                    raise ValueError("完成回执与原发布审计不一致")
                recovery = audit
            recovered_payload = recovery["payload"]
            if (not isinstance(recovered_payload, dict) or not isinstance(recovered_payload.get("snapshot"), dict)
                    or not isinstance(recovered_payload.get("manifest"), dict)
                    or content_hash(json.dumps(recovered_payload, ensure_ascii=False, sort_keys=True)) != recovery["payload_hash"]):
                raise ValueError("恢复记录哈希或来源身份不符")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, InkFlowError) as exc:
            raise PlanningNeedsAttention(f"规划恢复记录无法核验：{exc}",
                                         "先恢复原任务的发布审计与回执，再继续；不会重新发布或递增修订号。") from exc
        task_scope = active_task_settings.get()
        same_task = bool(task_scope and recovered_payload.get("snapshot", {}).get("task_id") == task_scope.task_id)
        if same_task and recovered_payload["snapshot"].get("snapshot_hash") != task_scope.snapshot_hash:
            raise PlanningNeedsAttention("原发布任务的配置快照哈希不一致。",
                                         "先恢复原任务配置；不会用当前设置重放已完成发布。")
        if same_task and recovery.get("status") != "completed":
            warning = recover_planning_publication(project, user_retry=True)
            if warning:
                raise PlanningNeedsAttention(warning, "请先修复列出的来源或环境，再继续原任务并核验。")
        if (same_task and (getattr(project, "planning_recovery_result", None) or recovery.get("status") == "completed")
                and recovered_payload.get("manifest", {}).get("chapter_window") == [anchor, end]):
            manifest = recovered_payload["manifest"]
            active = load_active_planning(project)
            if active is None or active[0] != manifest:
                raise PlanningNeedsAttention("原任务的发布结果已经被新版本替代。", "核对当前正式版，不能将旧结果当作本次任务完成。")
            return {"status": "planned", "trace_id": manifest["trace_id"],
                    "revision_no": manifest["revision_no"], "recovered": True,
                    "chapter_range": manifest["chapter_window"],
                    "pending_history_choice": recovered_payload["history_choice"]}
    # A formal revision may only advance from a valid active publication.
    load_active_planning(project, allow_document_edits=focus != "chapter-window")
    active_start_hashes = {name: content_hash(_read(project.root / name))
                           for name in ("BOOK.md", "PLAN.md", "OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md",
                                        "planning/active-v2.json")}
    accepted = project.db.accepted_chapters()
    if not accepted or int(accepted[-1]["chapter_no"]) != anchor:
        raise ValidationGateError("规划起点必须是当前最后一章已接受正文；不会改写或跳过正史。")
    trace = TraceRecorder(project.root, "planning-v2", engine.settings.trace_level)
    scope = active_task_settings.get()
    review_role = "reviewer" if scope and scope.collaboration_mode in {"review_boost", "full_specialist"} else "editor"
    book = _read(project.root / "BOOK.md")
    state = _read(project.root / "STATE.md")
    from .story_settings import StorySettingsService
    story_settings = StorySettingsService(project)
    settings_fingerprint = story_settings.source_fingerprint()
    settings_context = story_settings.context(chapter_no=anchor + 1, actor="writer", max_chars=16000)
    review_settings_context = story_settings.context(chapter_no=anchor + 1, actor=review_role, max_chars=16000)
    state_context = _recent_planning_state(state, anchor) if focus == "chapter-window" else state
    canon = []
    for item in accepted[-3:]:
        content = _accepted_text(project, item)
        if not content:
            raise ValidationGateError(f"第 {item['chapter_no']} 章正史文件缺失或版本不符，暂不重设计剧情。")
        canon.append(content)
    cited_canon = []
    from .context import named_prior_chapters
    cited_order = named_prior_chapters(instruction, anchor + 1, set())
    cited_numbers = set(cited_order)
    accepted_by_number = {int(item["chapter_no"]): item for item in accepted}
    recent_numbers = {int(item["chapter_no"]) for item in accepted[-3:]}
    for number in cited_order:
        item = accepted_by_number.get(number)
        if item is None:
            reason = f"本次规划点名的第 {number} 章没有已接受正文，缺少必要过去依据。"
            project.record_pending_work(kind="context_source", reason=reason,
                source={"chapter_no": number, "boundary_chapter": anchor}, run_id=trace.run_id,
                next_action="确认点名章与已接受来源后从规划资料节点续接；不让 Writer 为缺资料改稿。",
                status="waiting_condition")
            raise ValidationGateError(reason)
        if number in recent_numbers:
            continue
        content = _accepted_text(project, item)
        if not content:
            reason = f"规划点名的第 {number} 章正史文件缺失或版本不符。"
            project.record_pending_work(kind="context_source", reason=reason,
                source={"chapter_no": number, "version": item["version"],
                        "source_hash": item["content_hash"], "boundary_chapter": anchor}, run_id=trace.run_id,
                next_action="恢复点名来源后从规划资料节点续接；不重写正文或默默跳过该章。",
                status="waiting_condition")
            raise ValidationGateError(reason)
        cited_canon.append(f"【第 {item['chapter_no']} 章已接受正文】\n{content}")
    summaries = "\n".join(f"第 {item['chapter_no']} 章：{item.get('summary') or (project.db.rebuilt_chapter_summary(item['chapter_no']) or {}).get('chapter_summary') or item.get('title') or ''}" for item in accepted)
    stable_sources = f"【书籍设定】\n{book}\n【已接受章节摘要】\n{summaries}\n【时序化事实与伏笔】\n{state_context}\n【最近三章全文】\n" + "\n\n".join(canon)
    from .manual_edits import ManualEditsService
    manual_edits = ManualEditsService(project)
    if focus != "chapter-window":
        for job in manual_edits.jobs().values():
            if job["kind"] == "setting_document" and job["relative_path"] in {"OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md", "PLAN.md"} and job["status"] not in {"superseded", "discarded", "reconciled"}:
                edited_text = _read(project.root / job["relative_path"])
                if content_hash(edited_text) == job["current_hash"]:
                    stable_sources += f"\n【用户手动规划候选：{job['relative_path']}，尚非生效结构】\n" + edited_text
    if cited_canon:
        stable_sources += "\n【本次点名的已接受前章】\n" + "\n\n".join(cited_canon)
    stable_sources += settings_context
    from .preferences import preference_prompt
    frozen_preferences = preference_prompt(project.db)
    stable_sources += frozen_preferences
    hard_limit = min(engine.settings.context_budget_for(role)[1] for role in ("writer", review_role))
    base_tokens = estimate_tokens(stable_sources) + max(0, estimate_tokens(review_settings_context) - estimate_tokens(settings_context))
    source_input_limit = max(0, hard_limit - engine.settings.max_output_tokens - 4096)
    if base_tokens > source_input_limit:
        named = [{"chapter_no": number, "version": accepted_by_number[number]["version"],
                  "source_hash": accepted_by_number[number]["content_hash"]} for number in cited_order]
        reason = (f"规划必要来源约 {base_tokens} token，超过当前角色输入余量 {source_input_limit}；"
                  + ("点名旧章：" + "、".join(f"第{number}章" for number in cited_order) if cited_order else "必要正史与设定均保留。"))
        project.record_pending_work(kind="context_source", reason=reason,
            source={"required_chapters": named, "boundary_chapter": anchor}, run_id=trace.run_id,
            next_action="缩小必要点名范围或提高当前角色上下文容量后，从规划资料节点续接；不静默删去指定来源。",
            status="waiting_condition")
        raise ValidationGateError(reason)
    thread_context, thread_evidence = await asyncio.to_thread(_planning_thread_evidence,
        project, accepted, anchor=anchor, end=end,
        included={int(item["chapter_no"]) for item in accepted[-3:]} | cited_numbers,
        token_limit=max(0, min(SOURCE_RECOVERY_TOKEN_LIMIT, hard_limit - base_tokens
                               - engine.settings.max_output_tokens - 4096)))
    stable_sources += thread_context
    atomic_write_text(trace.run_dir / "thread-evidence.json", json.dumps(thread_evidence, ensure_ascii=False, indent=2))
    trace.record("planning.thread-evidence", "completed", "早期未结伏笔先定位已接受原文，未定位项明确保留资料缺口",
                 metadata={"loaded_chapters": [item["chapter_no"] for item in thread_evidence["loaded_sources"]],
                           "missing_sources": thread_evidence["missing_sources"],
                           "unselected_count": len(thread_evidence["unselected_thread_ids"])})
    frozen = content_hash(stable_sources)
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
    ) or re.search(r"断点.{0,8}续修", instruction))
    previous_run: Path | None = None
    previous_review_identity_matches = False
    recheck_saved_dependencies = False
    active_outline: BookOutlineV2 | None = None
    active_detail: VolumeDetailV2 | None = None
    active_source_run: Path | None = None
    preserved_candidates: dict[str, Any] = {}
    preserved_outline: BookOutlineV2 | None = None
    new_window = False
    if focus in {"volume-detail", "outline"}:
        active = load_active_planning(project)
        if active is None or end != active[3].chapters[-1].chapter_no or anchor != active[3].anchor_chapter:
            raise PlanningNeedsAttention("单层规划修改的正式锚点或窗口已变化。",
                                         "先核对现行规划与正史，再明确受影响范围；不会自动重生三层。")
        preserved_outline = active[1]
        preserved_candidates["chapter-window"] = active[3]
        if focus == "volume-detail":
            active_outline = active[1]
        else:
            preserved_candidates["volume-detail"] = active[2]
    if focus == "chapter-window":
        active = load_active_planning(project)
        new_window = bool(active and anchor == project.db.latest_accepted_chapter_no()
                          and anchor >= active[0].get("accepted_anchor", 0)
                          and end > active[3].chapters[-1].chapter_no
                          and end <= active[2].chapter_end)
        if active is None or (not new_window and (active[0].get("accepted_anchor") != anchor
                                                    or active[0].get("chapter_window") != [anchor, end])):
            raise ValidationGateError("当前三层规划的锚点或范围已变化；请先核对受影响章节，不会改写旧稿。")
        manifest, active_outline, active_detail, _active_window = active
        source_run = project.root / ".inkflow" / "runs" / str(manifest["trace_id"])
        if not new_window:
            active_source_run = source_run
            previous_run = source_run
    focus_range = re.search(
        r"(?<!不)(?<!别)(?<!勿)(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)(?:当前|现行|生效)?第\s*(\d+)\s*(?:到|至|～|~|—|-)\s*(\d+)\s*章",
        instruction,
    )
    focus_single = re.search(r"(?<!不)(?<!别)(?<!勿)(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)(?:当前|现行|生效)?第\s*(\d+)\s*章", instruction)
    focus_chapters = (set(range(anchor + 1, end + 1)) if new_window else
                      set(range(int(focus_range.group(1)), int(focus_range.group(2)) + 1))
                      if focus == "chapter-window" and focus_range else
                      {int(focus_single.group(1))} if focus == "chapter-window" and focus_single else set())
    replan_window = (focus == "chapter-window" and len(focus_chapters) > 1
                     and bool(re.search(r"重排|重构|重新规划|重新安排", instruction)))
    if focus == "chapter-window" and (not focus_chapters or min(focus_chapters) <= anchor or max(focus_chapters) > end):
        raise ValidationGateError("请指出要修订的未来章节范围；已接受正文和其他规划不会被猜测改动。")
    requested_instruction = instruction
    resume_basis = {
        "snapshot_hash": scope.snapshot_hash if scope else "",
        "collaboration_mode": scope.collaboration_mode if scope else "",
        "review_role": review_role,
        "canon_hash": frozen,
        "settings_source_hash": settings_fingerprint,
        "accepted_versions": [[item["chapter_no"], item["version"], item["content_hash"]]
                              for item in accepted],
        "anchor": anchor,
        "end": end,
        "focus": focus or "",
        "focus_chapters": sorted(focus_chapters),
        "active_manifest_hash": active_start_hashes["planning/active-v2.json"],
    }
    if approved_run_id:
        if Path(approved_run_id).name != approved_run_id:
            raise ValidationGateError("待发布规划的运行标识不合法。")
        approved_dir = project.internal / "runs" / approved_run_id
        pending = project.db.get_metadata("pending_planning_publication", {})
        if (not isinstance(pending, dict) or pending.get("run_id") != approved_run_id
                or pending.get("source_manifest_hash") != content_hash(_read(project.root / "planning/active-v2.json"))
                or json.loads(_read(approved_dir / "resume-base.json")) != resume_basis):
            raise ValidationGateError("待确认规划的来源、范围或生效版本已变化，不能直接采用旧候选。")
        for stage in ("outline", "volume-detail", "chapter-window"):
            required = ("candidate.json",) if ((focus == "chapter-window" and stage != "chapter-window")
                                              or (focus == "volume-detail" and stage == "outline")) else ("candidate.json", "review.json")
            for suffix in required:
                name = f"{stage}-{suffix}"
                content = _read(approved_dir / name)
                if not content:
                    raise ValidationGateError(f"待发布规划缺少 {name}，未采用。")
                if content_hash(content) != pending.get("artifact_hashes", {}).get(name):
                    raise ValidationGateError("已审核候选或报告在等待确认期间变化，未采用。")
                review = PlanningReviewV2.model_validate_json(content) if suffix == "review.json" else None
                if review is not None and (review.verdict != "pass" or review.confidence < min_review_confidence):
                    raise ValidationGateError("待发布规划仍有未通过的审核，未采用。")
                atomic_write_text(trace.run_dir / name, content)
        trace.record("planning.reviewed_candidate", "completed", "用户明确采用同来源已审核候选；不重复调用模型",
                     metadata={"source_run_id": approved_run_id})
    if resume_requested:
        candidate_only_run: Path | None = None
        source_changed_run: Path | None = None
        nearby_saved_runs: list[str] = []
        identity_fields = {"snapshot_hash", "collaboration_mode", "review_role"}
        for prior in prior_runs:
            try:
                prior_basis = json.loads(_read(prior / "resume-base.json"))
            except (ValueError, TypeError):
                continue
            has_candidate = any((prior / f"{name}-candidate.json").is_file()
                                for name in ("outline", "volume-detail", "chapter-window"))
            same_range = isinstance(prior_basis, dict) and all(prior_basis.get(key) == resume_basis[key]
                for key in ("anchor", "end", "focus", "focus_chapters"))
            same_snapshot = bool(scope and isinstance(prior_basis, dict)
                                 and prior_basis.get("snapshot_hash") == scope.snapshot_hash)
            if has_candidate and same_range:
                nearby_saved_runs.append(prior.name)
            if prior_basis == resume_basis and has_candidate:
                previous_run = prior
                previous_review_identity_matches = True
                break
            if (candidate_only_run is None and has_candidate and same_snapshot and isinstance(prior_basis, dict)
                    and {key: value for key, value in prior_basis.items() if key not in identity_fields}
                    == {key: value for key, value in resume_basis.items() if key not in identity_fields}):
                candidate_only_run = prior
            if source_changed_run is None and has_candidate and same_snapshot and same_range:
                source_changed_run = prior
        if previous_run is None and candidate_only_run is not None:
            previous_run = candidate_only_run
            trace.record("planning.candidate-recheck", "completed",
                "来源与范围相同，保留已生成候选；旧配置或审核责任缺失/变化，按本次角色定点补审，不重生成上游",
                metadata={"source_run_id": previous_run.name, "review_role": review_role,
                          "prior_review_reusable": False})
        if previous_run is None and source_changed_run is not None:
            previous_run = source_changed_run
            recheck_saved_dependencies = True
            trace.record("planning.source-recheck", "completed",
                "同原任务与授权范围的候选保留；来源已变化，旧通过状态失效，先逐层定点核对，不重生成上游",
                metadata={"source_run_id": previous_run.name, "snapshot_hash": scope.snapshot_hash,
                          "prior_review_reusable": False})
        if previous_run is None and nearby_saved_runs:
            reason = "找到同范围保存候选，但无法核验它属于本任务原配置；未读取另次旧规划来替代，也未重做Writer上游。"
            action = "先恢复原任务配置与候选来源身份，再从该规划层继续审核。"
            atomic_write_text(trace.run_dir / "planning-checkpoint.json", json.dumps({
                "status": "waiting_condition", "stage": "sources", "phase": "candidate_identity",
                "reason": reason, "next_action": action, "candidate_run_ids": nearby_saved_runs,
                "snapshot": scope.public_summary() if scope else None}, ensure_ascii=False, indent=2))
            trace.finish(status="waiting_condition", summary=reason)
            raise PlanningNeedsAttention(reason, action)
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

    current_node = {"stage": "sources", "phase": "source_check"}
    def verify_canon() -> None:
        for filename, original_hash in active_start_hashes.items():
            if content_hash(_read(project.root / filename)) != original_hash:
                raise PlanningNeedsAttention(
                    f"{filename} 的来源版本在规划期间已变化；候选与返回结果保留，未发布旧候选。",
                    f"先对照当前 {filename}，从 {current_node['stage']} 的受影响来源核对继续，不重放已完成写稿。")
        if story_settings.source_fingerprint() != settings_fingerprint:
            raise PlanningNeedsAttention("规划期间设定合集已变化，候选保留，未覆盖现行规划。",
                                         "对照当前设定及其证据，从受影响的规划层继续。")
        if preference_prompt(project.db) != frozen_preferences:
            raise PlanningNeedsAttention("规划期间作者习惯或本书偏好已变化，候选保留。", "按最新偏好核对受影响规划后继续。")
        current = project.db.accepted_chapters()
        if len(current) != len(accepted) or any((a["content_hash"], a["version"]) != (b["content_hash"], b["version"])
                                              for a, b in zip(accepted, current)):
            raise PlanningNeedsAttention(
                "规划期间正史已更新；候选已保存，未覆盖新版本。",
                "以最新已接受正文为依据，从受影响的规划层继续；已通过且未受影响的上游内容保留。",
            )
        by_number = {int(item["chapter_no"]): item for item in current}
        for number in {int(item["chapter_no"]) for item in accepted[-3:]} | (cited_numbers & by_number.keys()):
            if not _accepted_text(project, by_number[number]):
                raise PlanningNeedsAttention("规划已读取的正史正文缺失或版本不符，候选保留。",
                                             "恢复这一章的已接受来源后，从受影响层继续。")
        for source in thread_evidence["loaded_sources"]:
            row = by_number.get(source["chapter_no"])
            text = _accepted_text(project, row) if row else ""
            if (not row or int(row["version"]) != source["version"] or row["content_hash"] != source["content_hash"]
                    or text[source["start"]:source["end"]] != source["excerpt"]):
                raise PlanningNeedsAttention("早期伏笔的补读来源已变化，旧依据不再用于发布。",
                                             "重新定位这一来源章的已接受原文，从受影响层核对。")
        if content_hash(_read(project.root / "BOOK.md") + _read(project.root / "STATE.md")) != content_hash(book + state):
            raise PlanningNeedsAttention(
                "规划期间设定或事实账已变化；候选保留，未覆盖当前规划。",
                "核对新增设定影响的范围，只重审受影响规划，不重发整项任务。",
            )

    async def generate_and_review(stage: str, model_type: Any, source: str, task: str) -> Any:
        current_node.update(stage=stage, phase="source_check")
        verify_canon()
        candidate_path = trace.run_dir / f"{stage}-candidate.json"
        review_path = trace.run_dir / f"{stage}-review.json"
        if candidate_path.is_file() and review_path.is_file():
            review = PlanningReviewV2.model_validate_json(_read(review_path))
            if review.verdict == "pass" and review.confidence >= min_review_confidence:
                return model_type.model_validate_json(_read(candidate_path))
        feedback = ""
        previous_candidate = ""
        resumed_candidate = None
        if stage in preserved_candidates:
            resumed_candidate = preserved_candidates[stage]
            previous_candidate = _stage_text(resumed_candidate)
        if stage not in preserved_candidates and previous_run is not None and not (focus == "chapter-window" and previous_run == active_source_run):
            dependencies = ("outline",) if stage == "volume-detail" else (("outline", "volume-detail") if stage == "chapter-window" else ())
            matching_dependencies = all(
                content_hash(_read(previous_run / f"{name}-candidate.json"))
                == content_hash(_read(trace.run_dir / f"{name}-candidate.json"))
                for name in dependencies
            )
            prior_candidate = previous_run / f"{stage}-candidate.json"
            if (matching_dependencies or recheck_saved_dependencies) and prior_candidate.is_file():
                try:
                    resumed_candidate = model_type.model_validate_json(_read(prior_candidate))
                    previous_candidate = _stage_text(resumed_candidate)
                    trace.record(f"{stage}.resume", "completed",
                                 "复用上次候选作修订底稿" if creative_revision and stage == "chapter-window"
                                 else "复用上次保存的候选，先重新审核；不重复生成整层")
                except ValueError:
                    resumed_candidate = None
        if (focus == "chapter-window" and stage == "chapter-window"
                and active_source_run is not None and previous_run == active_source_run):
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
            if (previous_review_identity_matches and not creative_revision and len(focus_chapters) == 1
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
        if stage == "chapter-window" and resumed_candidate is not None and creative_revision and stage not in preserved_candidates:
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
                chapter_body_note = ("\n【逐章交稿长度】每章 body 至少200个汉字，后面的章节也要写全；"
                                     "交代具体场景、人物目标、阻力、选择、可见后果及承接下一章的钩子。"
                                     "同时填写 time_location、goal、obstacle、decision、consequence、scenes、hook_question，"
                                     "这些结构字段与 body 一致，写具体动作因果，不填通用口号。不要用重复记纸凑字数。"
                                     if stage == "chapter-window" else "")
                focused_note = ("\n【定点返回】只返回第 " + "、".join(str(number) for number in sorted(focus_chapters))
                                + " 章的章节卡；其他章节由程序从生效版保留，不要复写。"
                                if stage == "chapter-window" and active_source_run is not None
                                and len(focus_chapters) < end - anchor else "")
                current_node.update(phase="writer")
                writer = await engine.provider.generate_json(
                    system_prompt=_WRITER_SYSTEM,
                    user_prompt=f"{source}\n\n【用户最新授权】\n{instruction}\n\n【本层任务】\n{task}{replan_note}{chapter_body_note}{focused_note}{revision_context}",
                    output_model=model_type, effort="high", thinking=False,
                    max_tokens=engine.settings.max_output_tokens,
                    timeout_seconds=engine.settings.planning_timeout_seconds, agent_role="writer",
                )
                candidate = writer.data
                atomic_write_text(trace.run_dir / f"{stage}-writer-return-{attempt + 1}.json", candidate.model_dump_json(indent=2))
                trace.record_model(f"{stage}.writer.{attempt + 1}", writer, "Writer 返回隔离规划候选；尚未发布")
                verify_canon()
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
            atomic_write_text(candidate_path, candidate.model_dump_json(indent=2))
            if stage == "chapter-window" and stage not in preserved_candidates and resumed_candidate is None:
                if any(not all((item.goal, item.obstacle, item.decision, item.consequence, item.scenes))
                       for item in candidate.chapters):
                    feedback = "本次近期规划缺少具体目标、阻力、选择、后果或场景；请同次补齐结构字段，并与 body 一致。"
                    if attempt == 2:
                        raise PlanningNeedsAttention(feedback, "从当前近期规划候选补齐缺失细节，再审核；不重做上游。")
                    continue
            pattern_hint = _repeated_user_rejected_pattern(latest_instruction, candidate) if stage == "chapter-window" else ""
            if stage == "chapter-window" and replan_window:
                pattern_issue = _repeated_user_rejected_pattern(latest_instruction, candidate)
                if pattern_issue:
                    trace.record(f"{stage}.user-constraint.{attempt + 1}", "needs_review",
                                 pattern_issue, metadata={"source": "explicit_user_request"})
            source_for_review = (f"【用户当前要求】\n{instruction}\n【已接受锚点正文】\n{canon[-1]}\n"
                                 + ("【本次点名的已接受前章】\n" + "\n\n".join(cited_canon) + "\n" if cited_canon else "")
                                 + f"【时序化事实与伏笔】\n{state_context}\n"
                                 + review_settings_context + thread_context
                                 + f"【已生效全书大纲】\n{_render_outline(active_outline)}\n"
                                 f"【已生效当前卷细纲】\n{_render_detail(active_detail)}"
                                 if focus == "chapter-window" and stage == "chapter-window"
                                 and active_outline is not None and active_detail is not None
                                 else (source.replace(settings_context, review_settings_context, 1)
                                       if settings_context else source + review_settings_context))
            if frozen_preferences not in source_for_review:
                source_for_review += frozen_preferences
            quote_bank = []
            original_text = "\n".join([*(item["excerpt"] for item in thread_evidence["loaded_sources"]),
                                        canon[-1], *cited_canon, *canon[:-1]])
            for segment in re.split(r"(?<=[。！？；\n])", original_text):
                excerpt = segment.strip()
                if 12 <= len(excerpt) <= 300:
                    quote_bank.append(excerpt)
                if len(quote_bank) >= 24:
                    break
            quotes = "\n".join(f"- {item}" for item in quote_bank)
            review_prompt = (f"【来源】\n{source_for_review}\n\n【可逐字复制的来源短句，引用时不要带项目符号】\n{quotes}"
                             f"\n\n【用户要求】\n{instruction}\n\n【当前层任务】\n{task}\n\n【候选】\n{previous_candidate}")
            if stage == "chapter-window":
                review_prompt += ("\n【用户拒绝的重复与本窗口推进】按原话识别行动组合，包括不同措辞的同类行动；"
                                  "比较起点与终点的目标、选择、关系、风险和实际后果。词频只能提供线索，"
                                  "若有真实变化允许慢热和再次行动；若未覆盖明确要求，用问题章原句与用户原话定位，"
                                  "说明最小重组范围。结构字段与 body 必须相容。\n窄规则线索：" + (pattern_hint or "无，仍按语义审核"))
            verify_canon()
            current_node.update(phase="review")
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
            verify_canon()
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
                # Any planning layer can contradict itself. Pass still needs
                # an independent canon/outline anchor.
                return review.verdict == "revise" and item.source_excerpt in candidate_text
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
                         (review.verdict == "revise" and item.source_excerpt in candidate_text)}
                        for item in review.evidence
                        if not grounded_item(item)
                    ]
                    candidate_spans = [part.strip() for part in re.split(r"(?<=[。！？；\n])", candidate_text)
                                       if 12 <= len(part.strip()) <= 300]
                    source_spans = [part.strip() for part in re.split(r"(?<=[。！？；\n])", source_for_review)
                                    if 12 <= len(part.strip()) <= 300]
                    candidate_examples = list(dict.fromkeys(
                        span for item in review.evidence
                        for span in difflib.get_close_matches(item.candidate_excerpt, candidate_spans, n=2, cutoff=0.25)
                    ))
                    source_examples = list(dict.fromkeys(
                        span for item in review.evidence
                        for span in difflib.get_close_matches(item.source_excerpt, source_spans, n=2, cutoff=0.25)
                    ))
                    verify_canon()
                    current_node.update(phase="review_evidence")
                    audit = await engine.provider.generate_json(
                        system_prompt=_EDITOR_SYSTEM,
                        user_prompt=(f"【来源】\n{source_for_review}\n\n【可逐字复制的来源短句，引用时不要带项目符号】\n{quotes}\n\n【用户要求】\n{instruction}\n\n【当前层任务】\n{task}"
                                     f"\n\n【候选】\n{candidate_text}\n\n【你上一份审核】\n{review.model_dump_json()}"
                                     f"\n\n【程序核出的无效引文】\n{json.dumps(invalid, ensure_ascii=False)}"
                                     f"\n\n【候选可逐字复制的相近原句】\n" + "\n".join(candidate_examples)
                                     + f"\n\n【外部来源可逐字复制的相近原句】\n" + "\n".join(source_examples)
                                     + f"\n自动放行的把握度底线为 {min_review_confidence:.0%}。"
                                     "请只重核依据与判断；引文格式或把握度问题不能要求 Writer 重写。"
                                     "若候选含无来源的关键事实，判 revise 并给最小修改指令；"
                                     "若仍可通过，只给 1～3 条强证据，每条两个 excerpt 都必须是"
                                     "来源与候选中的一整段连续原文；revise 可引用候选内部互斥的两处原句，"
                                     "pass 仍须外部来源。不加事实 ID、引号、删节号，也不要拼接。"),
                        output_model=PlanningReviewV2, effort="high", thinking=False,
                        max_tokens=min(engine.settings.max_output_tokens, 8192),
                        timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
                    )
                    review = audit.data
                    atomic_write_text(review_path, review.model_dump_json(indent=2))
                    trace.record_model(f"{stage}.editor-citation-repair.{attempt + 1}.{citation_attempt + 1}", audit,
                                       f"仅重核引用：{review.verdict}")
                    verify_canon()
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
            if (review.verdict == "revise" and grounded
                    and any(re.search(r"未(?:说明|交代|写明|明确)|可能|容易|建议|风险|模糊|已明确|已写|倒灌|缺少", item.finding)
                            for item in review.evidence)):
                verify_canon()
                current_node.update(phase="review_coexistence")
                challenge = await engine.provider.generate_json(
                    system_prompt=_EDITOR_SYSTEM,
                    user_prompt=(review_prompt + "\n\n【同一审核的共存复核】\n" + review.model_dump_json()
                                 + "\n逐项判断引用的两个事实能否同时成立；日常动作或先后发生的新事件无需额外证明。"
                                 "来源引文若只记录了部分观察，不得说未记录的细节当时也已写下。"
                                 "工作人员被主动询问后说明查询方向，不等于无代价送来原始证据。"
                                 "若改判 pass，至少一条 evidence.source_excerpt 必须逐字取自【来源】的已接受正文或当前生效规划，不能全引候选内部句子。"
                                 "若仍判 revise，指出排他性矛盾或缺失的必需因果链；"
                                 "若只是可能误读、少一句说明，改判 pass 并将建议留在 summary。"
                                 "不要让 Writer 为审核措辞问题重写。"),
                    output_model=PlanningReviewV2, effort="high", thinking=False,
                    max_tokens=min(engine.settings.max_output_tokens, 8192),
                    timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
                )
                challenged = challenge.data
                atomic_write_text(trace.run_dir / f"{stage}-coexistence-return-{attempt + 1}.json", challenged.model_dump_json(indent=2))
                trace.record_model(f"{stage}.editor-coexistence.{attempt + 1}", challenge,
                                   f"复核同真性：{challenged.verdict}")
                verify_canon()
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
            if stage in preserved_candidates:
                raise PlanningNeedsAttention(f"单层修改影响了下游 {stage}：{review.summary}\n{findings}",
                    "已保留单层候选和当前正式版；请确认是否同时修订受影响下游，原请求不会自动扩大。")
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

    superseded_files: list[dict[str, str]] = []

    try:
        outline_task = (f"重设计全书大纲约5000～25000字，必须有主线、背景设定、重要场景、人物变化、主要高潮、大致结局和连续的卷级方向。"
                        f"前 {anchor} 章已经发生，不得更改；后续卷数与总章数可随剧情合理调整。第一卷包含第1～{anchor}章，后续每卷至少10章。body 不写逐章计划。")
        if focus == "outline" and preserved_outline is not None:
            outline_task = ("只修改生效全书大纲，保留既有卷号与各卷起止章，不改正史；"
                            "下游细纲和近期规划本轮只做必要相容性审核，不自动重写。\n生效大纲："
                            + _stage_text(preserved_outline))
        if active_outline is not None:
            outline = active_outline
            atomic_write_text(trace.run_dir / "outline-candidate.json", outline.model_dump_json(indent=2))
            trace.record("outline.reuse", "completed", "沿用已生效大纲；本轮不重新生成")
        else:
            outline = await generate_and_review("outline", BookOutlineV2, stable_sources, outline_task)
        if (focus == "outline" and preserved_outline is not None
                and [(v.volume_no, v.chapter_start, v.chapter_end) for v in outline.volumes]
                != [(v.volume_no, v.chapter_start, v.chapter_end) for v in preserved_outline.volumes]):
            raise PlanningNeedsAttention("单独大纲修改改变了卷边界，候选保留但未发布。", "请确认是否扩大为完整三层重设计。")
        if (active_outline is None and focus != "outline" and outline.volumes[0].chapter_end != anchor) or len(outline.volumes) < 2:
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
        current_publication = load_active_planning(project, allow_document_edits=focus != "chapter-window")
        if current_publication is not None and all(
            content_hash(content) == current_publication[0][key]
            for content, key in ((outline_text, "outline_hash"), (detail_text, "volume_detail_hash"),
                                 (plan_text, "recent_plan_hash"))
        ):
            trace.finish(summary="审核后的三层规划与当前正式版相同，未创建空修订")
            return {"status": "unchanged", "revision_no": current_publication[0].get("revision_no", 1),
                    "next_action": "候选与当前正式版相同；修订号保持不变。"}
        publication_mode = scope.settings.planning_publication_mode if scope else engine.settings.planning_publication_mode
        current_node.update(stage="publication", phase="confirmation" if publication_mode == "confirm_after_review" else "publish")
        if publication_mode == "confirm_after_review" and not approved_run_id:
            async with project_write_lock(project.root):
                verify_canon()
                for filename, original_hash in active_start_hashes.items():
                    if content_hash(_read(project.root / filename)) != original_hash:
                        raise PlanningNeedsAttention("审核期间生效规划已变化，候选保留但不能请求采用。",
                                                     "先核对新旧版本，再从受影响层续接。")
                project.db.set_metadata("pending_planning_publication", {
                    "run_id": trace.run_id, "anchor": anchor, "end": end,
                    "instruction": requested_instruction, "focus": focus,
                    "source_manifest_hash": active_start_hashes["planning/active-v2.json"],
                    "artifact_hashes": {f"{stage}-{suffix}": content_hash(_read(
                        trace.run_dir / f"{stage}-{suffix}"))
                        for stage in ("outline", "volume-detail", "chapter-window")
                        for suffix in (("candidate.json",) if ((focus == "chapter-window" and stage != "chapter-window")
                                                               or (focus == "volume-detail" and stage == "outline"))
                                       else ("candidate.json", "review.json"))},
                })
            trace.finish(status="waiting_user", summary="三层候选审核通过，等待用户确认正式采用")
            return {"status": "waiting_user", "decision": "awaiting_confirmation",
                    "candidate_run_id": trace.run_id,
                    "chapter_range": [anchor, end], "revision_no":
                    (load_active_planning(project, allow_document_edits=focus != "chapter-window") or ({"revision_no": 0},))[0].get("revision_no", 0),
                    "next_action": "三层候选均已审核通过，当前正式版未改变。请明确说‘采用刚才审核通过的规划’，才会发布新版并递增修订号。"}
        async with project_write_lock(project.root):
            verify_canon()
            for filename, original_hash in active_start_hashes.items():
                if content_hash(_read(project.root / filename)) != original_hash:
                    raise PlanningNeedsAttention(
                        f"{filename} 在规划期间被修改；候选保留，未覆盖新版本。",
                        "先比较新旧规划的受影响章节，再从相应层续接；不会静默覆盖用户编辑。",
                    )
            prior_brief = project.db.get_brief()
            from .manual_edits import _book_brief_from_document
            source_brief = _book_brief_from_document(book) if book != render_book_brief(prior_brief) else prior_brief
            last_chapter = outline.volumes[-1].chapter_end
            revised_brief = source_brief.model_copy(update={
                "estimated_chapters": last_chapter, "estimated_volumes": len(outline.volumes),
            })
            revised_book, scale_matches = re.subn(
                r"(?m)^- 预计规模：\d+ 卷 / \d+ 章$",
                f"- 预计规模：{len(outline.volumes)} 卷 / {last_chapter} 章", book,
            )
            previous_manifest_text = _read(project.root / "planning" / "active-v2.json")
            try:
                previous_manifest = json.loads(previous_manifest_text) if previous_manifest_text else {}
            except (ValueError, TypeError):
                previous_manifest = {}
            manifest = {
                "protocol_version": 2, "status": "active", "published_at": utc_now(),
                "revision_no": int(previous_manifest.get("revision_no") or (1 if previous_manifest else 0)) + 1,
                "previous_run_id": previous_manifest.get("trace_id"),
                "source_canon_hash": frozen,
                "accepted_anchor": anchor, "volume_no": volume.volume_no,
                "chapter_window": [anchor, end],
                "outline_hash": content_hash(outline_text),
                "volume_detail_hash": content_hash(detail_text),
                "recent_plan_hash": content_hash(plan_text),
                "book_hash": content_hash(revised_book),
                "trace_id": trace.run_id,
                "book_scale_updated": scale_matches == 1,
                "legacy_plan_status": "replaced_in_database",
            }
            stamp = manifest["published_at"].replace(":", "-")
            # Stage the complete authorized write before the first mutation.
            contents = {"OUTLINE.md": outline_text, "STORY_DETAIL.md": detail_text,
                        "RECENT_PLAN.md": plan_text, "BOOK.md": revised_book if scale_matches == 1 else book}
            for stage_name, name in (("outline", "OUTLINE.md"), ("volume-detail", "STORY_DETAIL.md"),
                                     ("chapter-window", "RECENT_PLAN.md"), ("book-scale", "BOOK.md")):
                previous = _read(project.root / name)
                if previous and previous != contents[name]:
                    history_name = f"planning/history/{stage_name}-{stamp}-{content_hash(previous)[:10]}.md"
                    contents[history_name] = previous
                    if name != "BOOK.md":
                        superseded_files.append({"source": name, "path": history_name,
                                                 "content_hash": content_hash(previous)})
            with project.db.connect() as connection:
                old_plan_rows = [dict(row) for row in connection.execute("SELECT * FROM plans ORDER BY kind,plan_key")]
                old_plan_metadata = [dict(row) for row in connection.execute("SELECT * FROM metadata")]
            if superseded_files or old_plan_rows:
                archive_name = f"planning/history/database-{trace.run_id}.json"
                contents[archive_name] = json.dumps({"plans": old_plan_rows, "metadata": old_plan_metadata},
                                                   ensure_ascii=False, indent=2)
                superseded_files.append({"source": "旧版数据库规划", "path": archive_name,
                                         "content_hash": content_hash(contents[archive_name])})
            cleanup = _cleanup_suggestions(project)
            contents["planning/cleanup-suggestions.json"] = json.dumps(
                {"generated_at": utc_now(), "candidates": cleanup}, ensure_ascii=False, indent=2)
            if superseded_files:
                contents[f"planning/history/revision-{trace.run_id}.json"] = json.dumps({
                    "run_id": trace.run_id, "revision_no": manifest["revision_no"] - 1,
                    "superseded_by_revision": manifest["revision_no"],
                    "previous_run_id": previous_manifest.get("trace_id"), "previous_manifest": previous_manifest,
                    "created_at": manifest["published_at"], "status": "pending", "files": superseded_files,
                }, ensure_ascii=False, indent=2)
            contents["planning/active-v2.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)
            if scope:
                from .studio import StudioService
                with StudioService(project).db.connect() as connection:
                    row = connection.execute("SELECT snapshot_json FROM task_settings_snapshots WHERE task_id=?",
                                             (scope.task_id,)).fetchone()
                if row is None:
                    raise ValidationGateError("发布所关联的原任务配置快照缺失，未开始写入。")
                snapshot = json.loads(row["snapshot_json"])
                if snapshot["snapshot_hash"] != scope.snapshot_hash:
                    raise ValidationGateError("发布的原任务配置哈希不一致，未开始写入。")
            else:
                snapshot = capture_task_settings(engine.settings, novel_id=project.project_id, task_id=trace.run_id)
            payload = {
                "snapshot": snapshot, "snapshot_linked": scope is not None, "manifest": manifest,
                "files": {name: {"before": _read(project.root / name) if (project.root / name).is_file() else None,
                                 "after": text} for name, text in contents.items()},
                "sources": {**{name: active_start_hashes[name] for name in ("PLAN.md", "STATE.md")
                               if name in active_start_hashes}, "STATE.md": content_hash(state),
                            **{row["path"]: row["content_hash"] for row in accepted},
                            **{str(path.relative_to(project.root)): content_hash(_read(path))
                               for path in trace.run_dir.glob("*-candidate.json")},
                            **{str(path.relative_to(project.root)): content_hash(_read(path))
                               for path in trace.run_dir.glob("*-review.json")}},
                "accepted": [[row["chapter_no"], row["version"], row["content_hash"]] for row in accepted],
                "settings_source_hash": settings_fingerprint, "preferences_hash": content_hash(frozen_preferences),
                "bundle": v2_execution_bundle(project, manifest, outline, detail, plan).model_dump(mode="json"),
                "old_plans": old_plan_rows, "prior_brief": prior_brief.model_dump(mode="json"),
                "brief": revised_brief.model_dump(mode="json"), "source_hashes": active_start_hashes,
                "focus": focus, "approved_run_id": approved_run_id, "history_choice": bool(superseded_files),
            }
            recovery_record = {"status": "prepared", "attempts": 0, "stage": "prepared", "payload": payload,
                               "payload_hash": content_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True))}
            atomic_write_text(project.root / "planning/publication-recovery.json",
                              json.dumps(recovery_record, ensure_ascii=False, indent=2))
            warning = recover_planning_publication(project)
            if warning:
                trace.record("planning-v2.publish", "recovering", "首次发布失败，定位后在原节点重试", warning)
                warning = recover_planning_publication(project)
            if warning:
                raise PlanningNeedsAttention(warning, "修复报告中的具体位置后继续原规划任务；原候选与审核保留。")
            trace.record("planning-v2.publish", "completed", "三层文件与执行投影核验一致，整组生效",
                         metadata={"manifest": "planning/active-v2.json", "outline_hash": manifest["outline_hash"]})
        trace.finish(summary=f"大纲、卷细纲和第 {anchor}～{end} 章规划完成")
        return {"status": "planned", "chapter_range": [anchor, end], "future_range": [anchor + 1, end],
                "revision_no": manifest["revision_no"],
                "volume_no": volume.volume_no, "outline_path": str(project.root / "OUTLINE.md"),
                "detail_path": str(project.root / "STORY_DETAIL.md"), "plan_path": str(project.root / "RECENT_PLAN.md"),
                "trace_id": trace.run_id, "cleanup_candidates": cleanup,
                "pending_history_choice": bool(superseded_files),
                "next_action": f"第 {manifest['revision_no']} 版规划已正式生效；请在项目中心选择保留或删除被替换的旧版。"}
    except asyncio.CancelledError:
        trace.record("planning-v2", "cancelled", "已停止；本阶段候选保留，未发布未审核结果")
        trace.finish(status="cancelled", summary="规划已停止，现有正史不变")
        raise
    except PlanningNeedsAttention as exc:
        exc.next_action += (f" 当前断点：{current_node['stage']} / {current_node['phase']}；"
                            f"来源与保留成果见 {trace.run_dir / 'planning-checkpoint.json'}。")
        checkpoint = {"status": "waiting_condition", **current_node,
            "reason": str(exc), "next_action": exc.next_action, "candidate_run_id": trace.run_id,
            "snapshot": scope.public_summary() if scope else None,
            "source_basis_path": str(trace.run_dir / "resume-base.json"),
            "preserved_outputs": [str(path) for path in trace.run_dir.glob("*.json")
                if "candidate" in path.name or "review" in path.name or "return" in path.name]}
        atomic_write_text(trace.run_dir / "planning-checkpoint.json", json.dumps(checkpoint, ensure_ascii=False, indent=2))
        trace.record("planning-v2", "waiting_condition", "候选和上游成果已保留，等待定点处理", str(exc), metadata=checkpoint)
        trace.finish(status="waiting_condition", summary=str(exc))
        raise
    except Exception as exc:
        trace.record("planning-v2", "failed", "规划停在受影响阶段；候选保留", str(exc))
        trace.finish(status="failed", summary=str(exc))
        raise
