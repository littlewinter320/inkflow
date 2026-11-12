"""Versioned whole-book -> volume -> rolling-window planning for existing novels.

Candidates remain in the run directory until a separate editorial verdict passes.
Only the engine publishes active Markdown and SQLite execution projections;
no candidate is canon.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import sqlite3
import re
import sqlite3
from uuid import uuid4
from pathlib import Path
from typing import Any

from .errors import InkFlowError, ValidationGateError
from .project import InkFlowProject, render_book_brief
from .prompts import PUBLIC_RESULT_LANGUAGE
from .project_lock import project_write_lock, project_write_lock_sync
from .schemas import (ArcPlan, ArcSummary, BookOutlineV2, BookPlan, ChapterCard,
                      PlanBundle, PlanningReviewV2, RollingPlanV2, VolumeCompass,
                      VolumeDetailV2, VolumePlan, PromiseAlignmentPatch, PromiseAlignmentReviewV2,
                      AcceptedContinuityPatch)
from .trace import TraceRecorder
from .runtime import COMPLETION_ATTEMPT_LIMIT
from .task_settings import active_task_settings, capture_task_settings, validate_task_settings_snapshot
from .utils import SOURCE_RECOVERY_TOKEN_LIMIT, atomic_write_text, content_hash, estimate_tokens, utc_now
from .prompts import PROMISE_PLANNING_CONTRACT


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
_WRITER_SYSTEM += PROMISE_PLANNING_CONTRACT
_WRITER_SYSTEM += "\n同一thread_id跨大纲、卷细纲和章节窗口代表同一份完整PromisePlan，所有定义字段必须逐字一致。下层的具体推进写入promise_beats.expected_change，不得另写一份同ID但条件或里程碑不同的合同；新承诺才使用新编号。"
_EDITOR_SYSTEM += PROMISE_PLANNING_CONTRACT
_EDITOR_SYSTEM += """\n事实共存不等于任务完成。另从窗口起点到终点比较人物目标、障碍、实际选择与后果；当用户明确要求突破重复，而连续数章只是换人问话、看通知、回铺整理且没有推进目标或改变局面时，可判 revise，并引用这些章节的原句说明重复模式及最小重组范围。不要要求每章都有高潮，也不要因为单章安静就否决慢热叙事。"""
_EDITOR_SYSTEM += """\n若判 pass，summary 要说清窗口收尾相比起点发生了什么由人物行动造成的局面变化或代价。单纯多得一条消息、又记了一张纸、把材料换地方藏，不自动等于任务要求的剧情推进；人物仍可保持谨慎，关键是谨慎带来新关系、新阻力或新的可执行选择。找不到这种变化而用户明确要打破重复时，应判 revise 并指出最小受影响章节段，不要用“线索逐步增多”代替实际后果。"""
_EDITOR_SYSTEM += """\n交卷前专门核对全篇关键物件与人物的终态：同一物件若写成仍失踪/不归还，又写成主角拿到或被人放回，须解释是否确为不同物件，否则判 revise；“最后一次出现”若在不同卷重复，也须判 revise 并引用前后原句。不能只审核每段局部合理性。"""
_EDITOR_SYSTEM += """\n区分已完成状态的回述与再次首次发生：“已经写上”等状态表述不能仅凭叙述所在时段推断成刚发生。须引用实际重复行动或排他的发生时间；普通拿取、携带、收纳的省略仍按前述共存规则判断。修订意见只要求改变有据问题及必要衔接，不能为了单处问题重写整层。"""

# 三层规划复用公开说明要求；原始字段、内容版本与发布核验继续保持原合同。
_WRITER_SYSTEM += PUBLIC_RESULT_LANGUAGE
_EDITOR_SYSTEM += PUBLIC_RESULT_LANGUAGE


class PlanningNeedsAttention(ValidationGateError):
    """A saved candidate needs targeted follow-up; the whole task has not failed."""

    def __init__(self, reason: str, next_action: str):
        super().__init__(reason)
        self.next_action = next_action


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _apply_planning_patch(candidate: Any, patch: AcceptedContinuityPatch) -> Any:
    """复用唯一原句替换，仅派生当前候选；完整Schema及规划对象身份仍须通过。"""
    from .engine import _apply_unique_replacements
    updated = type(candidate).model_validate_json(_apply_unique_replacements(
        candidate.model_dump_json(indent=2), [(edit.target_excerpt, edit.replacement) for edit in patch.edits]))
    identities = ("volume_no", "chapter_start", "chapter_end", "anchor_chapter")
    if any(getattr(candidate, key, None) != getattr(updated, key, None) for key in identities):
        raise ValidationGateError("局部规划补丁改变了卷号、正史锚点或范围，原候选保留。")
    if ([item.chapter_no for item in getattr(candidate, "chapters", [])]
            != [item.chapter_no for item in getattr(updated, "chapters", [])]
            or [item.thread_id for item in candidate.promises] != [item.thread_id for item in updated.promises]):
        raise ValidationGateError("局部规划补丁改变了章节或承诺身份，原候选保留。")
    return updated


def outline_promise_update_requested(text: str) -> bool:
    """识别作者明确同步大纲伏笔定义的窄范围；查看、讨论和否定不授予修改权限。"""
    if re.search(r"先只看|只查看|讨论|不要执行|不执行|是否|能不能|要不要", text):
        return False
    verb = r"(?:同步|统一|校正|修订|修改|对齐)"
    return any("大纲" in clause and re.search(r"(?:伏笔|承诺).{0,8}(?:定义|合同|条件)", clause)
        and (re.search(verb + r".{0,20}大纲", clause) or re.search(r"大纲.{0,30}" + verb, clause))
        and not re.search(r"(?:不|别|无需|不用|不必|暂不)(?:要|再)?" + verb, clause)
        for clause in re.split(r"[，,。！？；\n]", text))


def _planning_effective_instruction(instruction: str) -> str:
    """去掉终端内部恢复标记，保留用户已累计的完整有效要求。"""
    text = instruction.strip()
    marker = "恢复未完成的规划。"
    return text[len(marker):].strip() if text.startswith(marker) else text


def _planning_instruction_delta(instruction: str, previous: str) -> tuple[str, str]:
    """只把本次追加的要求视作修订；失败重进时不重演已经绑定的创意。"""
    effective = _planning_effective_instruction(instruction)
    previous = previous.strip()
    ordinary_resume = not effective or bool(re.fullmatch(
        r"(?:请)?(?:继续|恢复|重试|续接)(?:刚才|原来|原|这个|该)?(?:未完成的|中断的)?(?:规划|任务|断点)?[。！!]?",
        effective,
    ))
    if ordinary_resume:
        return previous, ""
    if effective == previous:
        return effective, ""
    # generate_plan 会在用户累计要求之后补固定范围句，新增要求因此位于尾句之前。
    # 只在两边尾句完全相同且符合该执行器格式时排除它，范围本身仍保留在完整身份中。
    comparable, previous_comparable = effective, previous
    tail = previous.rsplit("\n", 1)[-1]
    if (re.fullmatch(
        r"(?:重排第 \d+～\d+ 章近期规划，以第 \d+ 章正史为锚；沿用生效大纲，进入下一卷时仅补齐该卷细纲并审核，不重写正史或全书方向。"
        r"|重排第 \d+ 到 \d+ 章的生效近期规划。)", tail,
    ) and effective.endswith("\n" + tail) and previous.endswith("\n" + tail)):
        comparable, previous_comparable = effective[:-(len(tail) + 1)], previous[:-(len(tail) + 1)]
    # 终端只在旧要求后追加这条显式标记；不按关键词删用户原话，未知写法仍交原修订门禁。
    prefix = previous_comparable + "\n本次继续时新增要求："
    if previous_comparable and comparable.startswith(prefix):
        return effective, comparable[len(prefix):].strip()
    return effective, effective


def _planning_saved_instruction(run: Path) -> str:
    """新运行使用有效要求快照，旧运行仅回退它实际保存的根目标。"""
    return (_read(run / "task-effective-intent.txt") or _read(run / "task-root-intent.txt")).strip()


def _complete_planning_resume_source(
    selected: Path, prior_runs: list[Path], basis: dict[str, Any], instruction: str,
    *, min_review_confidence: float,
) -> tuple[Path, dict[str, Any]]:
    """同要求的半次续接可回到唯一完整来源链，绝不拼接不同运行的层级。"""
    stages = ("outline", "volume-detail", "chapter-window")
    selected_intent = _planning_saved_instruction(selected)
    effective, delta = _planning_instruction_delta(instruction, selected_intent)
    existing = {f"{stage}-candidate.json": content_hash(_read(selected / f"{stage}-candidate.json"))
                for stage in stages if (selected / f"{stage}-candidate.json").is_file()}
    if delta or len(existing) == len(stages) or not selected_intent:
        return selected, {}
    root = _read(selected / "task-root-intent.txt").strip()
    matches: list[tuple[Path, dict[str, str]]] = []
    # 必须整条候选链都在同一个旧运行中，且最新半成品已有的全部依赖逐字相同。
    for prior in prior_runs:
        if prior == selected or _planning_saved_instruction(prior) != effective:
            continue
        try:
            if (json.loads(_read(prior / "resume-base.json")) != basis
                    or _read(prior / "task-root-intent.txt").strip() != root):
                continue
            hashes = {f"{stage}-candidate.json": content_hash(_read(prior / f"{stage}-candidate.json"))
                      for stage in stages if (prior / f"{stage}-candidate.json").is_file()}
            if len(hashes) != len(stages) or any(hashes.get(name) != value for name, value in existing.items()):
                continue
            # 保留的大纲不必重新审；生成过的卷细纲与窗口须各自有真实通过回执。
            for stage in ("volume-detail", "chapter-window"):
                checked = PlanningReviewV2.model_validate_json(_read(prior / f"{stage}-review.json"))
                if checked.verdict != "pass" or checked.confidence < min_review_confidence:
                    break
            else:
                matches.append((prior, hashes))
        except (ValueError, TypeError, OSError):
            continue
    if not matches:
        # 已有回执证明它曾承接过窗口，但本目录未落盘完整窗口时，缺少唯一祖先便停下。
        # 不能把丢失的承接关系当作“从未生成”，再次花费 Writer 次数。
        events = _read(selected / "events.jsonl").splitlines()
        resumed_window = False
        for line in events:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if event.get("stage") in {"chapter-window.resume", "chapter-window.review-reuse"}:
                resumed_window = True
                break
        if resumed_window:
            raise PlanningNeedsAttention("上次已承接的章节窗口缺少唯一完整来源证明。",
                                         "核对原候选依赖后续接；保留已用次数，不重新生成章节窗口。")
        return selected, {}
    # 两套不同的完整窗口不能仅凭“较新/较旧”猜选；须先由原任务核清来源。
    identities = {json.dumps(hashes, sort_keys=True) for _, hashes in matches}
    if len(identities) != 1:
        raise PlanningNeedsAttention("找到多条不同的完整规划来源，无法唯一续接。",
                                     "核对原任务的完整候选链；保留现有结果，不拼接或重新生成章节。")
    source, hashes = matches[0]
    return source, {"partial_run_id": selected.name, "source_run_id": source.name,
                    "basis_hash": content_hash(json.dumps(basis, ensure_ascii=False, sort_keys=True)),
                    "effective_instruction_hash": content_hash(effective),
                    "partial_candidate_hashes": existing, "source_candidate_hashes": hashes,
                    "source_review_hashes": {f"{stage}-review.json": content_hash(_read(source / f"{stage}-review.json"))
                                             for stage in ("volume-detail", "chapter-window")}}


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


def _restore_planning_review_quotes(review: PlanningReviewV2, candidate: str, source: str) -> tuple[PlanningReviewV2, list[dict[str, Any]]]:
    """唯一连续原句仅差引号或中文句末分段显示时取回原文；不接受省略、改字或拼接。"""
    display = str.maketrans({'“': '"', '”': '"'})
    def display_text(text: str) -> tuple[str, list[int]]:
        """只折叠中文句末与下一汉字间的纯换行，保留原始字符位置供逐字取回。"""
        breaks = {position for match in re.finditer(r"(?<=[。！？；])\r?\n(?:\r?\n)*(?=[\u3400-\u9fff])", text)
                  for position in range(match.start(), match.end())}
        positions = [position for position in range(len(text)) if position not in breaks]
        return "".join(text[position] for position in positions).translate(display), positions
    evidence, proofs = [], []
    for index, item in enumerate(review.evidence):
        changes = {}
        for field, texts in (("candidate_excerpt", [candidate]),
                             ("source_excerpt", [source, candidate] if review.verdict == "revise" else [source])):
            quote = getattr(item, field)
            if not quote or any(quote in text for text in texts):
                continue
            matches = []
            target, _ = display_text(quote)
            for text in texts:
                normalized, positions = display_text(text)
                start = normalized.find(target)
                while start >= 0:
                    matches.append((text, positions[start], positions[start + len(target) - 1] + 1))
                    start = normalized.find(target, start + 1)
            # 取回的是含分段的完整连续原文；空格、字词、其他标点、重复位置和拼接仍拒绝。
            if len(matches) != 1:
                continue
            text, start, end = matches[0]
            actual = text[start:end]
            changes[field] = actual
            proofs.append({"evidence_index": index, "field": field, "source_hash": content_hash(text),
                           "start_offset": start, "end_offset": start + len(actual),
                           "original_quote": quote, "source_quote": actual})
        evidence.append(item.model_copy(update=changes) if changes else item)
    return review.model_copy(update={"evidence": evidence}), proofs


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
    if value.promises:
        lines += ["## 伏笔与钩子规划合同", "", json.dumps([item.model_dump(mode="json") for item in value.promises], ensure_ascii=False, indent=2), ""]
    return "\n".join(lines).strip() + "\n"


def _render_detail(value: VolumeDetailV2) -> str:
    lines = [f"# 第 {value.volume_no} 卷 · {value.title}", "", f"> 规划协议 v2 · 卷细纲 · 第 {value.chapter_start}～{value.chapter_end} 章；未来构想，不是正史。", "", value.body.strip(), "", "## 粗略章节节拍", ""]
    lines.extend(f"- {item}" for item in value.rough_chapter_beats)
    if value.promises:
        lines += ["", "## 本卷伏笔与钩子规划合同", "", json.dumps([item.model_dump(mode="json") for item in value.promises], ensure_ascii=False, indent=2)]
    return "\n".join(lines).strip() + "\n"


def _render_window(value: RollingPlanV2) -> str:
    lines = [f"# 第 {value.anchor_chapter}～{value.chapters[-1].chapter_no} 章近期规划", "", "> 规划协议 v2；已接受的衔接章只读，以下未来章节尚不是正史。", "", f"## 第 {value.anchor_chapter} 章 · 已接受正文衔接", "", value.anchor_summary.strip(), ""]
    if value.promises:
        lines += ["## 当前窗口新增承诺（未来规划）", "", json.dumps([item.model_dump(mode="json") for item in value.promises], ensure_ascii=False, indent=2), ""]
    for chapter in value.chapters:
        lines += [f"## 第 {chapter.chapter_no} 章 · {chapter.title}", "", chapter.body.strip(), ""]
        for field, label in (("time_location", "时地"), ("goal", "目标"), ("obstacle", "阻力"),
                             ("decision", "选择"), ("consequence", "后果"), ("hook_question", "钩子")):
            if getattr(chapter, field):
                lines += [f"- {label}：{getattr(chapter, field)}"]
        if chapter.scenes:
            lines += ["- 场景：" + "；".join(chapter.scenes)]
        if chapter.promise_beats:
            lines += ["- 伏笔与钩子动作：" + json.dumps([item.model_dump(mode="json") for item in chapter.promise_beats], ensure_ascii=False)]
        if any((chapter.goal, chapter.scenes, chapter.hook_question, chapter.time_location)):
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _promise_definition_conflicts(outline: BookOutlineV2, detail: VolumeDetailV2, window: RollingPlanV2) -> dict[str, list[dict[str, Any]]]:
    """收集跨层同编号的真实定义冲突，不选择上层或下层，也不丢弃细化信息。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for level, owner in (("outline",outline),("volume-detail",detail),("chapter-window",window)):
        for index, plan in enumerate(owner.promises):
            groups.setdefault(plan.thread_id, []).append({"level":level,"index":index,"plan":plan.model_dump(mode="json")})
    return {key:items for key,items in groups.items()
            if len({json.dumps(item["plan"],ensure_ascii=False,sort_keys=True) for item in items}) > 1}


def _apply_promise_alignment(outline: BookOutlineV2, detail: VolumeDetailV2, window: RollingPlanV2,
        patch: PromiseAlignmentPatch, *, lock_volume: bool = False,
        update_outline: bool = False) -> tuple[BookOutlineV2, VolumeDetailV2, RollingPlanV2, list[list[Any]]]:
    """仅派生授权的冲突定义和节拍说明；大纲正文、范围、未受影响合同与章节正文不改。"""
    from types import SimpleNamespace
    from .story_promises import planning_promises
    conflicts = _promise_definition_conflicts(outline,detail,window)
    plans = {plan.thread_id:plan for plan in patch.plans}
    declared = {plan.thread_id for owner in (outline,detail,window) for plan in owner.promises}
    # 只允许补齐原合同实际引用的缺失父定义；不能借对齐创造无关编号。
    required_parents = {parent for owner in (outline,detail,window) for plan in owner.promises
                        for parent in plan.parent_thread_ids if parent not in declared}
    required_parents.update(parent for plan in patch.plans if plan.thread_id in conflicts
                           for parent in plan.parent_thread_ids if parent not in declared)
    additions = set(plans) - set(conflicts)
    if (len(plans) != len(patch.plans) or not set(conflicts) <= set(plans)
            or not additions <= required_parents):
        raise ValueError("承诺对齐须完整返回原冲突编号，新增定义仅限实际缺失的前置承诺，不得删漏或重复。")
    original_outline_plans = {plan.thread_id:plan for plan in outline.promises}
    if update_outline and lock_volume:
        raise ValueError("同步大纲合同须与本次细纲候选联合核对，不能改锁定的生效细纲。")
    if any(identity in original_outline_plans and plan.kind != original_outline_plans[identity].kind
           for identity,plan in plans.items()):
        raise ValueError("合同对齐不能改变既有承诺身份或类型。")
    fixed = {} if update_outline else original_outline_plans
    if lock_volume:
        fixed.update({plan.thread_id:plan for plan in detail.promises})
    if any(key in fixed and value != fixed[key] for key,value in plans.items()):
        raise ValueError("承诺对齐不能修改全书大纲或已生效卷细纲的合同。")
    outline_data = outline.model_dump(mode="json")
    detail_data, window_data, paths = detail.model_dump(mode="json"), window.model_dump(mode="json"), []
    targets = ([("outline",outline_data)] if update_outline else []) + [("volume-detail",detail_data),("chapter-window",window_data)]
    for level, data in targets:
        for index, plan in enumerate(data.get("promises",[])):
            if plan["thread_id"] in plans:
                replacement = plans[plan["thread_id"]].model_dump(mode="json")
                if plan != replacement:
                    data["promises"][index] = replacement
                    paths.append([level,"promises",index])
    # 新声明留在本次窗口候选，锁定的大纲及已生效细纲保持逐字不变。
    for identity in sorted(additions):
        index = len(window_data["promises"])
        window_data["promises"].append(plans[identity].model_dump(mode="json"))
        paths.append(["chapter-window","promises",index])
    chapters = {item["chapter_no"]:index for index,item in enumerate(window_data["chapters"])}
    seen = set()
    for change in patch.beat_changes:
        key = (change.chapter_no,change.beat_index)
        if key in seen or change.chapter_no not in chapters or change.thread_id not in conflicts:
            raise ValueError("承诺节拍补充超出原冲突范围或重复同一位置。")
        seen.add(key)
        index = chapters[change.chapter_no]
        beats = window_data["chapters"][index].get("promise_beats",[])
        if (change.beat_index >= len(beats) or beats[change.beat_index]["thread_id"] != change.thread_id
                or beats[change.beat_index]["action"] != change.action):
            raise ValueError("承诺节拍的章、索引、编号或动作身份不符，未移动或新增节拍。")
        if beats[change.beat_index]["expected_change"] != change.expected_change:
            beats[change.beat_index]["expected_change"] = change.expected_change
            paths.append(["chapter-window","chapters",index,"promise_beats",change.beat_index,"expected_change"])
    revised_outline = BookOutlineV2.model_validate(outline_data)
    revised_detail, revised_window = VolumeDetailV2.model_validate(detail_data), RollingPlanV2.model_validate(window_data)
    # 沿原严格合同核对统一哈希、引用目标及父承诺循环，不另建宽松的发布解释。
    planning_promises(SimpleNamespace(book=revised_outline,current_volume=revised_detail,
        current_arc=SimpleNamespace(promises=revised_window.promises,chapter_cards=revised_window.chapters)))
    return revised_outline,revised_detail,revised_window,paths


def _alignment_missing_parents(outline: BookOutlineV2, detail: VolumeDetailV2, window: RollingPlanV2,
        patch: PromiseAlignmentPatch, *, lock_volume: bool = False, update_outline: bool = False) -> dict[str, list[str]]:
    """核对完整候选及锁定定义的父合同，不能只检查冲突返回或拿历史索引充数。"""
    effective = {plan.thread_id:plan for owner in (outline,detail,window) for plan in owner.promises}
    effective.update({plan.thread_id:plan for plan in patch.plans})
    if not update_outline:
        effective.update({plan.thread_id:plan for plan in outline.promises})
    if lock_volume:
        effective.update({plan.thread_id:plan for plan in detail.promises})
    return {plan.thread_id:missing for plan in effective.values()
            if (missing := [parent for parent in plan.parent_thread_ids if parent not in effective])}


def _merge_alignment_contract_repair(original: PromiseAlignmentPatch, repair: PromiseAlignmentPatch,
        affected_ids: set[str], *, required_parents: set[str] | None = None) -> PromiseAlignmentPatch:
    """仅替换指定坏定义及补齐已引用父合同，其余定义、节拍锁定并交原差异审核。"""
    replacements = {plan.thread_id:plan for plan in repair.plans}
    original_plans = {plan.thread_id:plan for plan in original.plans}
    additions = required_parents or set()
    if (len(replacements) != len(repair.plans) or set(replacements) != affected_ids | additions
            or additions & set(original_plans)
            or not affected_ids <= set(original_plans) or repair.beat_changes):
        raise ValueError("合同补核只能返回指定原承诺和已引用的缺失父定义，不能改其他定义或节拍。")
    if any(replacements[identity].kind != original_plans[identity].kind for identity in affected_ids):
        raise ValueError("前置关系补核不能改变承诺身份或类型。")
    return PromiseAlignmentPatch(plans=[replacements.get(plan.thread_id,plan) for plan in original.plans]
                                 + [replacements[identity] for identity in sorted(additions)],
                                 beat_changes=original.beat_changes)


def _alignment_artifact_names() -> tuple[str, ...]:
    """联合补审及原版副本是同一覆盖证明，确认发布和恢复必须整组保留。"""
    return ("promise-alignment-candidate.json","promise-alignment-review.json","promise-alignment-proof.json",
        "outline-before-alignment-candidate.json",
        "promise-alignment-review-before-recheck.json","promise-alignment-review-recheck.json",
        "promise-alignment-review-authority-recheck.json",
        "promise-alignment-contract-repair.json","promise-alignment-parent-definitions.json",
        "volume-detail-before-alignment-candidate.json","volume-detail-before-alignment-review.json",
        "chapter-window-before-alignment-candidate.json","chapter-window-before-alignment-review.json") + tuple(
        f"promise-alignment-semantic-{number}-{kind}.json" for number in range(1, COMPLETION_ATTEMPT_LIMIT + 1)
        for kind in ("writer","candidate","review"))


def _alignment_review_gaps(assessment: PromiseAlignmentReviewV2, binding: dict[str, Any],
        patch_text: str, source_text: str, min_confidence: float, *, require_pass: bool = True) -> list[str]:
    """核验差异报告的身份、完整对象及逐字依据；父定义也属于实际审核范围。"""
    gaps: list[str] = []
    identities = assessment.reviewed_thread_ids
    if (any(getattr(assessment, key) != binding[key] for key in ("base_hash", "patch_hash", "result_hash"))
            or len(identities) != len(set(identities)) or set(identities) != set(binding["reviewed_thread_ids"])):
        gaps.append("复核记录与本次规划版本或待核伏笔没有对应上，需要先核对版本和对象")
    review = assessment.review
    if not review.evidence or any(item.candidate_excerpt not in patch_text or item.source_excerpt not in source_text
                                  for item in review.evidence):
        gaps.append("复核引用尚未与原规划和修改内容逐字对应，需要补齐原文依据")
    if require_pass and review.verdict != "pass":
        gaps.append("伏笔的前置条件、因果或回应安排还未通过复核，需要按保留的意见定点处理")
    if review.confidence < min_confidence:
        gaps.append("现有依据还不足以形成可靠结论，需要回到来源补核")
    return gaps


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
        for name,digest in manifest.get("promise_alignment_artifacts",{}).items():
            if name not in _alignment_artifact_names() or content_hash(_read(source / name)) != digest:
                raise ValueError("承诺差异补审覆盖证明缺失或变化")
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
        promise_beats=item.promise_beats,
        foreshadow_advance=[f"{beat.thread_id}：{beat.expected_change}" for beat in item.promise_beats if beat.action != "payoff"],
        payoff=[f"{beat.thread_id}：{beat.expected_change}" for beat in item.promise_beats if beat.action == "payoff"],
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
        promises=window.promises,
        planted_threads=[beat.thread_id for item in window.chapters for beat in item.promise_beats if beat.action == "plant"],
        advanced_threads=[beat.thread_id for item in window.chapters for beat in item.promise_beats if beat.action == "advance"],
        payoff_threads=[beat.thread_id for item in window.chapters for beat in item.promise_beats if beat.action == "payoff"],
    )
    volume = VolumePlan(
        volume_no=detail.volume_no, title=detail.title, chapter_start=detail.chapter_start,
        chapter_end=detail.chapter_end, promise=direction.central_conflict,
        start_state=window.anchor_summary, end_state=direction.outcome,
        antagonist_pressure=direction.central_conflict,
        midpoint_turn=detail.rough_chapter_beats[len(detail.rough_chapter_beats) // 2],
        climax=direction.outcome, cost_and_result=direction.outcome,
        next_volume_bridge=direction.outcome,
        promises=detail.promises,
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
        promises=outline.promises,
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
        "created_at": utc_now(), "status": "kept", "previous_run_id": None,
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
                                          "created_at": now, "status": "kept", "files": files},
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
                                  approved_run_id: str = "", total_chapters: int | None = None,
                                  volume_count: int | None = None) -> dict[str, Any]:
    """Complete an authorized planning chain, resuming only the failed stage on retry."""
    if end <= anchor or not instruction.strip():
        raise ValidationGateError("请说清以哪一章正史为锚点、要规划到第几章，以及后续剧情方向。")
    if total_chapters is not None and (type(total_chapters) is not int or total_chapters < end):
        raise ValidationGateError("全书总章数必须是整数且覆盖本次近期规划范围。")
    if volume_count is not None and (type(volume_count) is not int or not 1 <= volume_count <= 20):
        raise ValidationGateError("规划卷数必须是1至20的整数。")
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
    review_role = "reviewer" if scope and scope.effective_collaboration_mode in {"review_boost", "full_specialist"} else "editor"
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
            next_action="缩小必要点名范围，使完整材料符合模型窗口与软件上限后，从规划资料节点续接；不静默删去指定来源。",
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
    update_outline_promises = focus == "future-detail" and outline_promise_update_requested(instruction)
    previous_run: Path | None = None
    previous_review_identity_matches = False
    recheck_saved_dependencies = False
    active_outline: BookOutlineV2 | None = None
    active_detail: VolumeDetailV2 | None = None
    active_source_run: Path | None = None
    preserved_candidates: dict[str, Any] = {}
    preserved_outline: BookOutlineV2 | None = None
    new_window = False
    focus_range = re.search(
        r"(?<!不)(?<!别)(?<!勿)(?:只|仅)?(?:需|要)?(?:改|调整|修|修正|校正|重排|重构|重新规划|重新安排|梳理|优化)(?:当前|现行|生效)?第\s*(\d+)\s*(?:到|至|～|~|—|-)\s*(\d+)\s*章",
        instruction,
    )
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
    if focus == "future-detail":
        # 先同步细纲再续写时沿当前正史重锚近期窗口，只重做受影响的细纲与未来章节安排。
        active = load_active_planning(project)
        if active is None or end > active[1].volumes[-1].chapter_end:
            raise ValidationGateError("同步细纲须有覆盖本次终点的正式大纲；不会自动改写全书规模。")
        preserved_outline = active_outline = active[1]
    if focus == "chapter-window":
        active = load_active_planning(project)
        next_volume = next((volume for volume in active[1].volumes
                            if volume.chapter_start <= anchor + 1 <= volume.chapter_end), None) if active else None
        reanchored_window = bool(active and anchor == project.db.latest_accepted_chapter_no()
                                 and anchor > active[3].anchor_chapter
                                 and end == active[3].chapters[-1].chapter_no)
        new_window = bool(active and anchor == project.db.latest_accepted_chapter_no()
                          and anchor >= active[0].get("accepted_anchor", 0)
                          and end > active[3].chapters[-1].chapter_no
                          and next_volume is not None and end <= next_volume.chapter_end)
        if active is None or (not new_window and not reanchored_window and (active[0].get("accepted_anchor") != anchor
                                                    or active[0].get("chapter_window") != [anchor, end])):
            raise ValidationGateError("当前三层规划的锚点或范围已变化；请先核对受影响章节，不会改写旧稿。")
        manifest, active_outline, active_detail, _active_window = active
        if new_window and next_volume.volume_no != active_detail.volume_no:
            active_detail = None
        source_run = project.root / ".inkflow" / "runs" / str(manifest["trace_id"])
        if not new_window:
            active_source_run = source_run
            previous_run = source_run
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
    scope_constraints = {key: value for key, value in {"total_chapters": total_chapters,
                         "volume_count": volume_count}.items() if value is not None}
    resume_basis = {
        **scope_constraints,
        "snapshot_hash": scope.snapshot_hash if scope else "",
        "collaboration_mode": scope.effective_collaboration_mode if scope else "",
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
            required = ("candidate.json",) if ((focus == "chapter-window" and
                                               (stage == "outline" or stage == "volume-detail" and active_detail is not None))
                                              or (focus in {"volume-detail", "future-detail"} and stage == "outline")) else ("candidate.json", "review.json")
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
        for name in _alignment_artifact_names():
            if name in pending.get("artifact_hashes",{}):
                content = _read(approved_dir / name)
                if not content or content_hash(content) != pending["artifact_hashes"][name]:
                    raise ValidationGateError("待发布承诺差异补审或原版覆盖证明已变化，未采用。")
                atomic_write_text(trace.run_dir / name,content)
    if resume_requested:
        candidate_only_run: Path | None = None
        source_changed_run: Path | None = None
        same_basis_runs: list[Path] = []
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
                same_basis_runs.append(prior)
            if (candidate_only_run is None and has_candidate and same_snapshot and isinstance(prior_basis, dict)
                    and {key: value for key, value in prior_basis.items() if key not in identity_fields}
                    == {key: value for key, value in resume_basis.items() if key not in identity_fields}):
                candidate_only_run = prior
            if source_changed_run is None and has_candidate and same_snapshot and same_range:
                source_changed_run = prior
        if same_basis_runs:
            # 同范围内可能保存过不同追加要求；先找当前有效要求的原组，未命中才视作新要求。
            requested_effective = _planning_effective_instruction(instruction)
            matching_intents = [prior for prior in same_basis_runs
                                if _planning_saved_instruction(prior) == requested_effective]
            previous_run = (matching_intents or same_basis_runs)[0]
            previous_review_identity_matches = True
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
        if previous_review_identity_matches and previous_run is not None:
            # 上次续接可能在尚未复制窗口前停止；仅凭“最新目录”不能丢弃原完整窗口。
            # 精确核对同要求、同来源以及半成品每个既有依赖，再整链选回单一旧运行。
            previous_run, resume_source_proof = _complete_planning_resume_source(
                previous_run, prior_runs, resume_basis, instruction,
                min_review_confidence=min_review_confidence,
            )
            if resume_source_proof:
                atomic_write_text(trace.run_dir / "planning-resume-source.json",
                                  json.dumps(resume_source_proof, ensure_ascii=False, indent=2))
                trace.record("planning.complete-source-resume", "completed",
                             "半次续接的已有依赖与原完整候选链一致；整链承接原来源，不拼接层级",
                             metadata=resume_source_proof)
    atomic_write_text(trace.run_dir / "resume-base.json", json.dumps(resume_basis, ensure_ascii=False))
    # A short follow-up such as "continue the saved candidate" does not erase
    # the creative goal that started the task. Keep one immutable root intent
    # across retries instead of nesting every previous prompt (and growing the
    # input/cache prefix on each attempt). The newest message still wins.
    root_intent = (
        _read(approved_dir / "task-root-intent.txt").strip() if approved_run_id else
        _read(previous_run / "task-root-intent.txt").strip()
        if resume_requested and previous_run is not None and previous_run != active_source_run
        else ""
    ) or instruction.strip()
    # 根目标与累计要求是两个身份：根目标永不改，累计要求记录已交给原任务的新增创意。
    # 普通“继续”包含终端保存的旧目标，不能将其中“重排”等旧措辞再判作一次新修订。
    if resume_requested and previous_run is not None and previous_run != active_source_run:
        effective_instruction, latest_instruction = _planning_instruction_delta(
            instruction, _planning_saved_instruction(previous_run) or root_intent,
        )
    else:
        effective_instruction = _planning_effective_instruction(instruction)
        latest_instruction = effective_instruction
    creative_revision = bool(re.search(
        r"(?:别再|不要再|不能再|换一种|改成|重排|重新安排).{0,70}"
        r"(?:剧情|人物|章节|规划|行动|节奏|走向|选择|问话|记录|重复)",
        latest_instruction,
    ))
    atomic_write_text(trace.run_dir / "task-root-intent.txt", root_intent)
    atomic_write_text(trace.run_dir / "task-effective-intent.txt", effective_instruction)
    if resume_requested and root_intent != instruction.strip():
        instruction = (f"【原任务仍有效的目标】\n{root_intent}\n"
                       f"【本次最新补充；如与原目标冲突，以本次为准】\n{instruction}")
        trace.record("planning.intent-resume", "completed", "续接时保留原创作目标，仅本次新增要求触发修订",
                     metadata={"source_run_id": previous_run.name if previous_run else "",
                               "effective_instruction_hash": content_hash(effective_instruction),
                               "new_instruction_hash": content_hash(latest_instruction),
                               "has_new_instruction": bool(latest_instruction)})

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
        if (previous_review_identity_matches and previous_run is not None and previous_run != active_source_run
                and not creative_revision and not recheck_saved_dependencies and stage not in preserved_candidates
                and stage in {"outline","volume-detail","chapter-window"}):
            prior_candidate,prior_review = previous_run / f"{stage}-candidate.json",previous_run / f"{stage}-review.json"
            if prior_candidate.is_file() and prior_review.is_file():
                checked = PlanningReviewV2.model_validate_json(_read(prior_review))
                retained = model_type.model_validate_json(_read(prior_candidate))
                before = previous_run / f"{stage}-before-alignment-candidate.json"
                reviewed_candidate = model_type.model_validate_json(_read(before)) if before.is_file() else retained
                dependencies = ("outline",) if stage == "volume-detail" else (("outline","volume-detail") if stage == "chapter-window" else ())
                dependency_match = all(content_hash(_read(previous_run / f"{name}-candidate.json"))
                    == content_hash(_read(trace.run_dir / f"{name}-candidate.json")) for name in dependencies)
                returns = list(previous_run.glob(f"{stage}-writer-return-*.json"))
                writer_origins: dict[Path, Path] = {}
                def writer_matches(path: Path, seen: set[Path] | None = None) -> bool:
                    """核验完整返回或补丁派生；旧续接漏带凭据时沿唯一同源复用链回到真实返回。"""
                    seen = set() if seen is None else seen
                    if path in seen:
                        return False
                    seen.add(path)
                    returned = model_type.model_validate_json(_read(path))
                    if returned != reviewed_candidate:
                        return False
                    base = path.with_name(path.name.replace("writer-return-", "writer-base-"))
                    patch = path.with_name(path.name.replace("writer-return-", "writer-patch-"))
                    if base.is_file() or patch.is_file():
                        if not base.is_file() or not patch.is_file():
                            return False
                        rebuilt = _apply_planning_patch(model_type.model_validate_json(_read(base)),
                            AcceptedContinuityPatch.model_validate_json(_read(patch)))
                        if rebuilt != returned:
                            return False
                        writer_origins[path] = path
                        return True
                    # 缺两份sidecar不能伪装旧完整返回：须有实际完整模型输出，或唯一复用来源。
                    number = path.stem.rsplit("-", 1)[-1]
                    for raw in path.parent.glob(f"{stage}.writer.{number}.*.output.json"):
                        try:
                            if model_type.model_validate_json(_read(raw)) == returned:
                                writer_origins[path] = path
                                return True
                        except ValueError:
                            continue
                    sources = {str(event.get("metadata", {}).get("source_run_id"))
                        for line in _read(path.parent / "events.jsonl").splitlines()
                        if (event := json.loads(line)).get("stage") == f"{stage}.review-reuse"
                        and event.get("status") == "completed" and event.get("metadata", {}).get("source_run_id")}
                    if len(sources) != 1:
                        return False
                    identity = sources.pop()
                    if Path(identity).name != identity:
                        return False
                    origin = path.parent.parent / identity / path.name
                    if (origin.parent.resolve().parent != path.parent.parent.resolve() or not origin.is_file()
                            or content_hash(_read(origin)) != content_hash(_read(path))
                            or json.loads(_read(origin.parent / "resume-base.json")) != json.loads(_read(path.parent / "resume-base.json"))
                            or not writer_matches(origin, seen)):
                        return False
                    writer_origins[path] = writer_origins[origin]
                    return True
                writer_match = any(writer_matches(path) for path in returns)
                evidence_source = ((source.replace(settings_context,review_settings_context,1)
                    if settings_context else source + review_settings_context)
                    + instruction + task + _read(previous_run / "task-root-intent.txt"))
                evidence_source += "".join(_read(previous_run / f"{name}-before-alignment-candidate.json") for name in dependencies)
                checked, quote_proofs = _restore_planning_review_quotes(checked, _stage_text(reviewed_candidate), evidence_source)
                # 与新审核共用逐字门禁：无效附加引文不遮住已有有效依据，原报告不改写。
                exact_evidence = [item for item in checked.evidence if item.candidate_excerpt in _stage_text(reviewed_candidate)
                                  and item.source_excerpt in evidence_source]
                grounded = bool(exact_evidence)
                if (checked.verdict == "pass" and checked.confidence >= min_review_confidence
                        and dependency_match and writer_match and grounded):
                    atomic_write_text(candidate_path,_read(prior_candidate))
                    checked = checked.model_copy(update={"evidence": exact_evidence})
                    atomic_write_text(review_path,checked.model_dump_json(indent=2))
                    if quote_proofs:
                        trace.record(f"{stage}.review-quote-display", "completed",
                            "续接时取回唯一连续原文，保留原审核与定位证明", public=False,
                            metadata={"proofs": quote_proofs, "source_review_hash": content_hash(_read(prior_review))})
                    prior_alignment = json.loads(_read(previous_run / "promise-alignment-proof.json") or "{}")
                    contract_scope_changed = update_outline_promises and not prior_alignment.get("base", {}).get("update_outline_promises")
                    # 新的明确合同同步只沿用原层通过结果；旧只读差异节点完整留在原run，不给它改权限。
                    if not contract_scope_changed:
                        for name in _alignment_artifact_names():
                            if (previous_run / name).is_file():
                                atomic_write_text(trace.run_dir / name,_read(previous_run / name))
                    elif prior_alignment:
                        trace.record("planning.contract-scope", "completed", "保留原只读补核记录；作者明确同步冲突大纲合同，另走窄差异节点",
                            metadata={"source_run_id":previous_run.name,"prior_proof_hash":content_hash(_read(previous_run / "promise-alignment-proof.json"))},public=False)
                    for path in returns:
                        if path not in writer_origins:
                            continue
                        atomic_write_text(trace.run_dir / path.name,_read(path))
                        origin = writer_origins[path]
                        for marker in ("writer-base-", "writer-patch-"):
                            sidecar = origin.with_name(origin.name.replace("writer-return-", marker))
                            if sidecar.is_file():
                                atomic_write_text(trace.run_dir / sidecar.name,_read(sidecar))
                    trace.record(f"{stage}.review-reuse","completed",
                        "保留同源已通过候选与原层审核；发布前另核承诺差异覆盖，不重复整层请求",
                        metadata={"source_run_id":previous_run.name})
                    return retained
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
                                 else "复用上次保存的候选，先重新审核；不重复生成整层",
                                 metadata={"source_run_id": previous_run.name,
                                           "candidate_hash": content_hash(_read(prior_candidate))})
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
                "anchor_chapter": anchor,
                "anchor_summary": resumed_candidate.anchor_summary if anchor != original.anchor_chapter else original.anchor_summary,
                "chapters": [revised[item.chapter_no] if item.chapter_no in focus_chapters else item
                             for item in original.chapters if item.chapter_no > anchor],
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
        # 候选生成与独立审查分别成为工具节点；审核结果决定修订、停止或完成。
        # 同一任务本层累计最多十次处理；旧来源链的实际Writer步骤带入，不因新run清零。
        from .orchestration import run_tool_graph
        legacy_attempts = 0
        for prior in prior_runs:
            try:
                basis = json.loads(_read(prior / "resume-base.json"))
                if not all(basis.get(key) == resume_basis.get(key) for key in ("snapshot_hash", "anchor", "end", "focus", "focus_chapters")):
                    continue
                for line in _read(prior / "events.jsonl").splitlines():
                    event = json.loads(line)
                    if re.fullmatch(re.escape(stage) + r"\.writer\.\d+", event.get("stage", "")) and event.get("status") == "completed":
                        legacy_attempts += 1
            except (ValueError, TypeError, AttributeError):
                raise ValidationGateError("本层旧处理记录损坏，未将历史次数清零后重做规划。")
        stage_budget = await engine._native_recovery_attempts(project, "planning." + stage, anchor,
            legacy_attempts=legacy_attempts)
        attempt = -1
        candidate = review = None

        async def write_candidate() -> str:
            """优先保留同源候选，否则只生成当前规划层，再检查明确范围。"""
            nonlocal attempt, candidate, previous_candidate, feedback, stage_budget
            attempt += 1
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
                # 明确单个正文栏的长度，避免长伏笔清单掩盖正文不足并触发整份格式重试。
                chapter_body_note = ("\n【逐章交稿长度】每章 body 至少200个汉字，后面的章节也要写全；"
                                     "交代具体场景、人物目标、阻力、选择、可见后果及承接下一章的钩子。"
                                     "同时填写 time_location、goal、obstacle、decision、consequence、scenes、hook_question，"
                                     "这些结构字段与 body 一致，写具体动作因果，不填通用口号。不要用重复记纸凑字数。"
                                     if stage == "chapter-window" else
                                     "\n【本层交稿长度】body 字段自身须有5000～25000个字符；"
                                     "卷表、伏笔与其他结构字段不计入这个长度。充分交代本层剧情因果、人物选择、"
                                     "谜题证据与收束，不重复堆字；只返回符合既有结构的完整候选。")
                focused_note = ("\n【定点返回】只返回第 " + "、".join(str(number) for number in sorted(focus_chapters))
                                + " 章的章节卡；其他章节由程序从生效版保留，不要复写。"
                                if stage == "chapter-window" and active_source_run is not None
                                and len(focus_chapters) < end - anchor else "")
                current_node.update(phase="writer")
                local_patch = candidate is not None and bool(feedback) and not replan_window and stage != "outline"
                prompt = f"{source}\n\n【用户最新授权】\n{instruction}\n\n【本层任务】\n{task}{replan_note}{chapter_body_note}{focused_note}{revision_context}"
                if local_patch:
                    base_json = candidate.model_dump_json(indent=2)
                    atomic_write_text(trace.run_dir / f"{stage}-writer-base-{attempt + 1}.json", base_json)
                    prompt = (f"{source}\n\n【用户最新授权】\n{instruction}\n\n【本层任务】\n{task}"
                        f"\n【当前候选JSON，补丁逐字定位的唯一底稿】\n{base_json}\n{feedback}"
                        "\n只返回edits与reason，不重发完整候选。最多四处，每处target_excerpt须在上面JSON文本中唯一出现，"
                        "replacement为对应JSON文本的最小替换；JSON内的引号与反斜杠须原样保留。"
                        "只修有据问题与必要衔接，同步涉及的body、时间及承诺条件；不改卷号、章号、范围或承诺编号。")
                stage_budget = await engine._native_recovery_attempts(project, "planning." + stage, anchor,
                    legacy_attempts=legacy_attempts, reserve=True)
                writer = await engine.provider.generate_json(
                    system_prompt=_WRITER_SYSTEM,
                    user_prompt=prompt,
                    output_model=AcceptedContinuityPatch if local_patch else model_type, effort="high", thinking=False,
                    max_tokens=min(engine.settings.max_output_tokens, 8192) if local_patch else engine.settings.max_output_tokens,
                    timeout_seconds=engine.settings.planning_timeout_seconds, agent_role="writer",
                )
                if local_patch:
                    atomic_write_text(trace.run_dir / f"{stage}-writer-patch-{attempt + 1}.json", writer.data.model_dump_json(indent=2))
                    candidate = _apply_planning_patch(candidate, writer.data)
                else:
                    candidate = writer.data
                atomic_write_text(trace.run_dir / f"{stage}-writer-return-{attempt + 1}.json", candidate.model_dump_json(indent=2))
                trace.record_model(f"{stage}.writer.{attempt + 1}", writer,
                    "Writer 返回局部规划补丁，候选已按原句派生；尚未发布" if local_patch else "Writer 返回隔离规划候选；尚未发布")
                verify_canon()
                if focus == "chapter-window" and stage == "chapter-window" and active_source_run is not None:
                    original = RollingPlanV2.model_validate_json(_read(active_source_run / "chapter-window-candidate.json"))
                    changed = {item.chapter_no: item for item in candidate.chapters}
                    if not focus_chapters.issubset(changed):
                        raise ValidationGateError("Writer 未返回完整目标章节修订，原规划保持不变。")
                    candidate = candidate.model_copy(update={
                        "anchor_chapter": anchor,
                        "anchor_summary": candidate.anchor_summary if anchor != original.anchor_chapter else original.anchor_summary,
                        "chapters": [changed[item.chapter_no] if item.chapter_no in focus_chapters else item
                                     for item in original.chapters if item.chapter_no > anchor],
                    })
                previous_candidate = _stage_text(candidate)
            atomic_write_text(candidate_path, candidate.model_dump_json(indent=2))
            if stage == "outline" and ((total_chapters is not None and candidate.volumes[-1].chapter_end != total_chapters)
                                       or (volume_count is not None and len(candidate.volumes) != volume_count)):
                feedback = f"本次用户明确的规模必须保持：{json.dumps(scope_constraints, ensure_ascii=False)}；请在当前候选上修正卷级范围，不改正史。"
                trace.record("outline.scope", "needs_revision", "候选规模不符合用户确认范围，尚未进入审核或发布")
                if stage_budget["attempts"] >= stage_budget["limit"]:
                    raise PlanningNeedsAttention(feedback, "从本层保存候选补齐具体问题；不重做上游。")
                return "write_candidate"
            if stage == "chapter-window" and stage not in preserved_candidates and resumed_candidate is None:
                if any(not all((item.goal, item.obstacle, item.decision, item.consequence, item.scenes))
                       for item in candidate.chapters):
                    feedback = "本次近期规划缺少具体目标、阻力、选择、后果或场景；请同次补齐结构字段，并与 body 一致。"
                    if stage_budget["attempts"] >= stage_budget["limit"]:
                        raise PlanningNeedsAttention(feedback, "从当前近期规划候选补齐缺失细节，再审核；不重做上游。")
                    return "write_candidate"
            return "review_candidate"

        async def review_candidate() -> str | None:
            """由独立角色选择通过或定点修订；无依据指控不能触发 Writer。"""
            nonlocal review, feedback
            pattern_hint = _repeated_user_rejected_pattern(effective_instruction, candidate) if stage == "chapter-window" else ""
            if stage == "chapter-window" and replan_window:
                pattern_issue = _repeated_user_rejected_pattern(effective_instruction, candidate)
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
            if engine.settings.prompt_cache_layout_version < 5:
                # 新布局已在Provider末尾提醒；旧布局也补交付核对，不改冻结的系统合同。
                review_prompt += ("\n【交付核对】判pass或revise至少给一条evidence，同时逐字引用候选和对照来源，"
                    "保留原文换行，不拼接、删节或改写。必要来源确实不足时判insufficient_context，不能用空evidence报通过。")
            verify_canon()
            current_node.update(phase="review")
            reviewer = await engine.provider.generate_json(
                system_prompt=_EDITOR_SYSTEM,
                user_prompt=review_prompt,
                output_model=PlanningReviewV2, effort="high", thinking=False, thinking_purpose="planning_conflict",
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
            def restore_quote_display(value: PlanningReviewV2) -> PlanningReviewV2:
                """原始角色返回已独立留档，只恢复引用显示并记录与当前来源绑定的证明。"""
                restored, proofs = _restore_planning_review_quotes(value, candidate_text, review_source)
                if proofs:
                    trace.record(f"{stage}.review-quote-display", "completed", "已取回仅差引号或分段显示的唯一连续原文，仍按逐字依据核验",
                                 metadata={"proofs": proofs}, public=False)
                return restored
            review = restore_quote_display(review)
            atomic_write_text(review_path, review.model_dump_json(indent=2))
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
                        # 仅引用格式未齐且原把握度达标时按普通核对；实质不确定仍走复杂复核，显式设置照常优先。
                        output_model=PlanningReviewV2, effort="high", thinking=False,
                        thinking_purpose="routine" if review.confidence >= min_review_confidence else "planning_conflict",
                        max_tokens=min(engine.settings.max_output_tokens, 8192),
                        timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
                    )
                    trace.record_model(f"{stage}.editor-citation-repair.{attempt + 1}.{citation_attempt + 1}", audit,
                                       f"仅重核引用：{audit.data.verdict}")
                    review = restore_quote_display(audit.data)
                    atomic_write_text(review_path, review.model_dump_json(indent=2))
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
                    output_model=PlanningReviewV2, effort="high", thinking=False, thinking_purpose="planning_conflict",
                    max_tokens=min(engine.settings.max_output_tokens, 8192),
                    timeout_seconds=engine.settings.planning_timeout_seconds, agent_role=review_role,
                )
                challenged = challenge.data
                atomic_write_text(trace.run_dir / f"{stage}-coexistence-return-{attempt + 1}.json", challenged.model_dump_json(indent=2))
                trace.record_model(f"{stage}.editor-coexistence.{attempt + 1}", challenge,
                                   f"复核同真性：{challenged.verdict}")
                challenged = restore_quote_display(challenged)
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
                return None
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
                        f"【审核的具体修订要求，只采用有据且在授权范围内的部分】\n{review.repair_instruction}\n"
                        "只处理上面有原句依据的问题，并顺读前后章节确认因果；"
                        "未定位的附加建议不是硬要求。若重复动作跨越整个目标窗口，可重组这些章节，"
                        "不要只替换几句收尾；其他已成立的事实保持原样。")
            if stage_budget["attempts"] >= stage_budget["limit"]:
                if stage == "outline" and ((total_chapters is not None and candidate.volumes[-1].chapter_end != total_chapters)
                                           or (volume_count is not None and len(candidate.volumes) != volume_count)):
                    raise ValidationGateError("本层累计处理已达上限，全书规模仍不符合用户确认范围，候选保留，未发布。")
                leading_issue = review.evidence[0].finding if review.evidence else review.summary
                raise PlanningNeedsAttention(
                    f"{stage} 累计{stage_budget['attempts']}次处理后仍未通过独立审核；当前具体问题：{leading_issue}"
                    f"。候选与审核记录已保留在 {trace.run_dir}，未覆盖生效规划。",
                    "先核对这条问题是关键因果矛盾还是未来规划可合理补足的细节；"
                    "误读只重核审核，真矛盾只修本层，不重跑已通过的大纲和细纲。",
                )
            return "write_candidate"

        await run_tool_graph(operations={"write_candidate": write_candidate, "review_candidate": review_candidate},
            first="write_candidate", checkpoint_path=trace.run_dir / "planning-graph.sqlite", max_steps=2 * (COMPLETION_ATTEMPT_LIMIT + 1),
            on_event=lambda name, status: trace.record(f"planning.graph.{stage}.{name}", status,
                "规划工具节点开始" if status == "running" else "规划工具节点已返回；仍按原门禁判断结果"))
        return candidate

    async def align_promises(outline: BookOutlineV2, detail: VolumeDetailV2, window: RollingPlanV2) -> tuple[BookOutlineV2, VolumeDetailV2, RollingPlanV2]:
        """发布前只统一冲突合同并附加一次差异审查；原层审查继续覆盖未改内容。"""
        def digest(value: Any) -> str:
            """用稳定JSON绑定范围、原候选、窄补丁与最终对象，不依赖文件更新时间。"""
            return content_hash(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")))
        directory = trace.run_dir
        proof_path = directory / "promise-alignment-proof.json"
        candidate_path = directory / "promise-alignment-candidate.json"
        review_path = directory / "promise-alignment-review.json"
        outline_before = directory / "outline-before-alignment-candidate.json"
        original_outline = BookOutlineV2.model_validate_json(_read(outline_before)) if outline_before.is_file() else outline
        originals = {}
        for stage, model, current in (("volume-detail",VolumeDetailV2,detail),("chapter-window",RollingPlanV2,window)):
            before = directory / f"{stage}-before-alignment-candidate.json"
            originals[stage] = model.model_validate_json(_read(before)) if before.is_file() else current
        original_detail, original_window = originals["volume-detail"], originals["chapter-window"]
        conflicts = _promise_definition_conflicts(original_outline,original_detail,original_window)
        if not conflicts:
            if proof_path.is_file() or candidate_path.is_file():
                raise PlanningNeedsAttention("承诺补审产物不再对应原冲突，未采用旧覆盖证明。","核对原候选与补审身份，只处理本节点。")
            return outline,detail,window
        current_node.update(stage="promise-alignment",phase="source_check")
        verify_canon()
        # 已生效细纲与全书大纲不能因窗口对齐被顺带修改；本次新卷候选才允许联合统一定义。
        lock_volume = active_detail is not None
        original_hashes = {"outline":digest(original_outline.model_dump(mode="json")),
            **{name:digest(value.model_dump(mode="json")) for name,value in originals.items()}}
        review_hashes = {}
        for stage in originals:
            before_review = directory / f"{stage}-before-alignment-review.json"
            current_review = directory / f"{stage}-review.json"
            text = _read(before_review) if before_review.is_file() else _read(current_review)
            review_hashes[stage] = content_hash(text)
        base = {"source_basis":resume_basis,"source_context_hash":content_hash(stable_sources),
            "root_intent_hash":content_hash(root_intent),"original_hashes":original_hashes,
            "original_review_hashes":review_hashes,"lock_volume":lock_volume,"conflict_ids":sorted(conflicts)}
        if update_outline_promises:
            base["update_outline_promises"] = True
            base["outline_promise_authorization_hash"] = content_hash(_planning_effective_instruction(requested_instruction))
        base_hash = digest(base)
        proof = json.loads(_read(proof_path)) if proof_path.is_file() else None
        if proof is not None and proof.get("base") != base:
            raise PlanningNeedsAttention("原承诺对齐的来源、范围或候选哈希已变化，未复用旧补审。","先核对受影响的承诺来源，不重生成未改章节卡。")
        if proof is None:
            if candidate_path.is_file() or review_path.is_file():
                raise PlanningNeedsAttention("承诺补审缺少原候选绑定，未猜测已有返回的身份。","恢复本节点原绑定记录后继续。")
            for stage,value in originals.items():
                atomic_write_text(directory / f"{stage}-before-alignment-candidate.json",value.model_dump_json(indent=2))
                old_review = _read(directory / f"{stage}-review.json")
                if old_review:
                    atomic_write_text(directory / f"{stage}-before-alignment-review.json",old_review)
            if update_outline_promises:
                atomic_write_text(outline_before,original_outline.model_dump_json(indent=2))
            proof = {"status":"pending","base":base,"base_hash":base_hash}
            atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
        # 原版和返回文件都留存；继续同一节点只读取已有Writer结果，不重新生成十五张章卡。
        affected_beats = [{"chapter_no":chapter.chapter_no,"beat_index":index,**beat.model_dump(mode="json")}
            for chapter in original_window.chapters for index,beat in enumerate(chapter.promise_beats)
            if beat.thread_id in conflicts]
        declared_ids = {plan.thread_id for owner in (original_outline,original_detail,original_window) for plan in owner.promises}
        required_parent_ids = {parent for owner in (original_outline,original_detail,original_window)
            for plan in owner.promises for parent in plan.parent_thread_ids if parent not in declared_ids}
        if candidate_path.is_file():
            patch = PromiseAlignmentPatch.model_validate_json(_read(candidate_path))
        else:
            if proof.get("writer_started"):
                raise PlanningNeedsAttention("承诺Writer请求已有发送记录但未取得完整返回，未自动重复请求。","先核对本节点原模型回执与用量。")
            current_node.update(phase="writer")
            proof["writer_started"] = True
            atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
            writer = await engine.provider.generate_json(system_prompt=_WRITER_SYSTEM +
                "\n本次只做已有跨层承诺定义对齐，不返回大纲、卷细纲正文或章节卡。"
                "完整返回给定冲突ID的统一PromisePlan；实际引用的缺失父定义按required_parent_ids同次补齐，不新增无关编号。"
                "保留上下层有依据的实质信息，不能机械选上层或下层丢弃细化。"
                "优先不改beat；确有必要时只改给定位置的expected_change，不改章号、索引、thread_id或action。"
                "lock_outline为true时大纲合同锁定；为false表示作者只授权同步冲突伏笔定义，保留身份类型、大纲正文、卷章范围及未冲突定义。"
                "lock_volume为true时卷细纲合同锁定，只统一下层副本。"
                "parent_thread_ids只能引用本次完整规划实际声明的PromisePlan；历史伏笔索引不等于合同声明。"
                "保留有依据的历史因果，不能因旧编号未声明便断言相关旧事不存在。",
                user_prompt=stable_sources + "\n原任务：\n" + root_intent + "\n只读规划与冲突：\n" + json.dumps({
                    "outline":original_outline.model_dump(mode="json"),"volume_detail":original_detail.model_dump(mode="json"),
                    "window":original_window.model_dump(mode="json"),"conflicts":conflicts,
                    "affected_beats":affected_beats,"lock_outline":not update_outline_promises,"lock_volume":lock_volume,
                    "required_parent_ids":sorted(required_parent_ids),"current_instruction":instruction},ensure_ascii=False),
                output_model=PromiseAlignmentPatch,effort="high",thinking=False,
                max_tokens=engine.settings.max_output_tokens,timeout_seconds=engine.settings.planning_timeout_seconds,agent_role="writer")
            trace.record_model("promise-alignment.writer",writer,"Writer仅返回冲突承诺与必要节拍说明")
            patch = writer.data
            atomic_write_text(candidate_path,patch.model_dump_json(indent=2))
            verify_canon()
        original_patch = patch
        contract_repair_text = ""
        try:
            revised_outline,revised_detail,revised_window,paths = _apply_promise_alignment(
                original_outline,original_detail,original_window,patch,lock_volume=lock_volume,update_outline=update_outline_promises)
        except ValueError as exc:
            missing = _alignment_missing_parents(original_outline,original_detail,original_window,patch,
                lock_volume=lock_volume,update_outline=update_outline_promises)
            fixed = {} if update_outline_promises else {plan.thread_id:plan for plan in original_outline.promises}
            if lock_volume:
                fixed.update({plan.thread_id:plan for plan in original_detail.promises})
            locked_changes = {plan.thread_id:fixed[plan.thread_id].model_dump(mode="json")
                for plan in patch.plans if plan.thread_id in fixed and plan != fixed[plan.thread_id]}
            affected_ids = (set(missing) & set(conflicts)) | set(locked_changes)
            required_parents = {identity for parents in missing.values() for identity in parents}
            titles = "、".join(plan.title for plan in patch.plans if plan.thread_id in affected_ids) or "本次伏笔"
            trace.record("promise-alignment.contract", "waiting_condition",
                         "伏笔定义或前置关系尚未通过，原返回与次数保留", str(exc),
                         metadata={"missing_parents":missing,"locked_changed_ids":sorted(locked_changes),
                                   "original_patch_hash":digest(patch.model_dump(mode="json"))},public=False)
            repairable_errors = {f"承诺 {identity} 引用了未声明的前置承诺" for identity in missing}
            if locked_changes:
                repairable_errors.add("承诺对齐不能修改全书大纲或已生效卷细纲的合同。")
            if (not (affected_ids or required_parents) or not affected_ids <= set(conflicts)
                    or str(exc) not in repairable_errors):
                raise PlanningNeedsAttention("伏笔定义尚未通过完整关系核对，已保留原规划和返回。",
                    "先补核这处定义关系，再继续原节点；不会重做已通过的章节计划。") from exc

            # 此处只给原Writer一次合同补救；原返回不覆盖，未受影响的定义与全部节拍锁定。
            from .runtime import active_runtime, active_generation_attempt_limit, active_generation_request_id
            from .model_usage import UsageLedger
            runtime = active_runtime.get()
            if not scope or not runtime or runtime.task_id != scope.task_id:
                raise PlanningNeedsAttention(f"“{titles}”依赖的另一条伏笔尚未定义。",
                    "先恢复原任务身份，再由原Writer补核这处关系；保留候选，不重复生成章节。") from exc
            original_patch_hash = digest(original_patch.model_dump(mode="json"))
            binding = {"task_id":scope.task_id,"snapshot_hash":scope.snapshot_hash,"base_hash":base_hash,
                "original_patch_hash":original_patch_hash,"missing_parents":missing,"role":"writer"}
            # 新增锁定定义或父定义时单独绑定实际补核范围；原请求及发送账本继续保留。
            if locked_changes or required_parents:
                binding.update(locked_definitions=locked_changes,required_parent_ids=sorted(required_parents))
            retained_repair = None
            request_affected_ids = affected_ids
            first_grant_key = "planning_contract_repair:" + digest(binding)
            first_grant = project.db.get_metadata(first_grant_key)
            # 原返回若已完整修好指定定义，只缺父定义，就保存这部分并单独绑定一次缺项请求。
            # 不清空旧许可、不覆盖原返回，也不让未知/损坏返回触发额外模型调用。
            if (required_parents and isinstance(first_grant,dict) and first_grant.get("binding") == binding
                    and first_grant.get("status") == "returned" and first_grant.get("repair")):
                first_return = PromiseAlignmentPatch.model_validate(first_grant["repair"])
                first_path = directory / "promise-alignment-contract-repair.json"
                if (digest(first_return.model_dump(mode="json")) == first_grant.get("repair_hash")
                        and (not first_path.is_file() or PromiseAlignmentPatch.model_validate_json(_read(first_path)) == first_return)
                        and {plan.thread_id for plan in first_return.plans} == affected_ids
                        and all(plan.thread_id not in fixed or plan == fixed[plan.thread_id] for plan in first_return.plans)):
                    # 复用原合并门禁检查重复、类型、越界及节拍；通过才开放仅缺项的后续机会。
                    _merge_alignment_contract_repair(original_patch,first_return,affected_ids)
                    retained_repair = first_return
                    request_affected_ids = set()
                    binding = {**binding,"retained_repair_hash":digest(first_return.model_dump(mode="json")),
                               "prior_grant_key":first_grant_key}
            grant_key = "planning_contract_repair:" + digest(binding)
            repair_path = directory / ("promise-alignment-parent-definitions.json" if retained_repair is not None
                                       else "promise-alignment-contract-repair.json")
            verify_canon()
            async with project_write_lock(project.root):
                grant = project.db.get_metadata(grant_key)
                if grant is None:
                    if repair_path.is_file():
                        raise PlanningNeedsAttention(f"“{titles}”的前置关系补核缺少原请求身份。",
                            "核对已有返回的来源，不重新发送Writer。")
                    baseline = UsageLedger().task_role_sent_count(scope.task_id,"writer")
                    grant = {"binding":binding,"status":"reserved","sent_baseline":baseline,"request_id":grant_key}
                    project.db.set_metadata(grant_key,grant)
                elif not isinstance(grant,dict) or grant.get("binding") != binding:
                    raise PlanningNeedsAttention(f"“{titles}”的前置关系补核身份已变化。",
                        "核对原任务及候选来源后继续，不能用另次结果替代。")
                elif not grant.get("repair"):
                    raise PlanningNeedsAttention(f"“{titles}”的一次前置关系补核已请求，完整结果仍待核对。",
                        "先核对原请求回执与用量；不会重复发送或重做章节。")
            proof["contract_repair"] = {"grant_key":grant_key,"binding":binding}
            atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
            if grant.get("repair"):
                try:
                    repair = PromiseAlignmentPatch.model_validate(grant["repair"])
                    repair_changed = (digest(repair.model_dump(mode="json")) != grant.get("repair_hash")
                        or repair_path.is_file() and PromiseAlignmentPatch.model_validate_json(_read(repair_path)) != repair)
                except ValueError as saved_exc:
                    raise PlanningNeedsAttention(f"“{titles}”的已存补核结构损坏。","核对原返回，不重新生成。") from saved_exc
                if repair_changed:
                    raise PlanningNeedsAttention(f"“{titles}”的补核候选与原返回身份不符。","保留两份记录，核对修改后再继续。")
                atomic_write_text(repair_path,repair.model_dump_json(indent=2))
            else:
                # 全网路/格式重试合计一次；新增机会沿原任务发送账本，旧费用与次数不清零。
                verify_canon()
                old_limits = dict(runtime.role_attempt_limits)
                runtime.role_attempt_limits["writer"] = int(grant["sent_baseline"]) + 1
                cap_token = active_generation_attempt_limit.set(1)
                request_token = active_generation_request_id.set(grant_key)
                reserved_grant = dict(grant)
                current_node.update(phase="contract_repair")
                try:
                    repaired = await engine.provider.generate_json(system_prompt=_WRITER_SYSTEM +
                        "\n本次仅补核已有承诺对齐结果中的合同错误。只返回指定affected_ids及required_parent_ids的PromisePlan，"
                        "beat_changes必须为空；其他承诺、所有原节拍、章节卡、卷细纲正文和大纲全部锁定。"
                        "locked_definitions中的定义须完整逐字保留到下层；原缺失父定义只能按来源补齐到窗口，不能删除锁定的依赖。"
                        "若有retained_repair，则这些定义已保存，不重复返回；只返回exact_return_ids指定的缺项。"
                        "自行对照原来源、完整声明目录及坏返回修复依赖和必要定义；不得新增指定范围外的ID，不能将历史索引"
                        "直接当作已声明合同，也不得因编号未声明便否定真实旧事。所有新父定义须以所给前史为据，不能再引入缺失父编号。",
                        user_prompt=stable_sources + "\n原任务：\n" + root_intent + "\n只读原规划与坏返回：\n" + json.dumps({
                            "outline":outline.model_dump(mode="json"),"volume_detail":original_detail.model_dump(mode="json"),
                            "window":original_window.model_dump(mode="json"),"original_patch":original_patch.model_dump(mode="json"),
                            "binding":binding,"contract_error":str(exc),"affected_ids":sorted(request_affected_ids),
                            "required_parent_ids":sorted(required_parents),
                            "exact_return_ids":sorted(request_affected_ids | required_parents),
                            "retained_repair":retained_repair.model_dump(mode="json") if retained_repair is not None else None,
                            "declared_ids":sorted({plan.thread_id for owner in (outline,original_detail,original_window) for plan in owner.promises})},ensure_ascii=False)
                            + "\n【本次必须完整且仅返回的定义编号】\n" + json.dumps(sorted(request_affected_ids | required_parents))
                            + "\nplans条数必须等于上述编号数；beat_changes为空。已保存的其他定义不要重复返回。",
                        output_model=PromiseAlignmentPatch,effort="high",thinking=False,
                        max_tokens=min(engine.settings.max_output_tokens,4096),timeout_seconds=engine.settings.planning_timeout_seconds,
                        agent_role="writer")
                    trace.record_model("promise-alignment.contract-repair",repaired,"原Writer仅补核锁定定义与缺失前置关系，原坏返回保留")
                    repair = repaired.data
                    atomic_write_text(repair_path,repair.model_dump_json(indent=2))
                    verify_canon()
                    grant = {**grant,"status":"returned","repair":repair.model_dump(mode="json"),
                             "repair_hash":digest(repair.model_dump(mode="json"))}
                    async with project_write_lock(project.root):
                        if project.db.get_metadata(grant_key) != reserved_grant:
                            raise PlanningNeedsAttention("这处伏笔补核的请求身份在返回期间变化。","保留返回并核对原请求，不采用迟到结果。")
                        project.db.set_metadata(grant_key,grant)
                finally:
                    active_generation_request_id.reset(request_token)
                    active_generation_attempt_limit.reset(cap_token)
                    runtime.role_attempt_limits = old_limits
            verify_canon()
            if (retained_repair is None and required_parents
                    and {plan.thread_id for plan in repair.plans} == affected_ids
                    and all(plan.thread_id not in fixed or plan == fixed[plan.thread_id] for plan in repair.plans)):
                # 已定位为仅漏父定义时，在同次对话继续一次缺项请求；下一次进入会带retained_repair，不能无限递归。
                # 先沿原合并门禁确认七类有效定义没有重复、越界或类型变化，再复用刚保存的实际回包。
                _merge_alignment_contract_repair(original_patch,repair,affected_ids)
                trace.record("promise-alignment.contract-repair", "info", "已保存有效定义，自动续补缺少的父定义",
                             metadata={"retained_repair_hash":digest(repair.model_dump(mode="json")),
                                       "required_parent_ids":sorted(required_parents)},public=False)
                return await align_promises(outline,detail,window)
            try:
                completion_return = repair
                if retained_repair is not None:
                    if ({plan.thread_id for plan in repair.plans} != required_parents
                            or len(repair.plans) != len(required_parents) or repair.beat_changes):
                        raise ValueError("缺失父定义补充只能完整返回指定父编号，不得重复已保存定义或改节拍。")
                    repair = PromiseAlignmentPatch(plans=retained_repair.plans + repair.plans)
                patch = _merge_alignment_contract_repair(original_patch,repair,affected_ids,required_parents=required_parents)
                revised_outline,revised_detail,revised_window,paths = _apply_promise_alignment(
                    original_outline,original_detail,original_window,patch,lock_volume=lock_volume,update_outline=update_outline_promises)
            except ValueError as repair_exc:
                trace.record("promise-alignment.contract-repair", "waiting_condition", "原Writer的前置关系补核仍未通过完整门禁",
                             str(repair_exc),metadata={"grant_key":grant_key},public=False)
                raise PlanningNeedsAttention(f"“{titles}”的前置关系补核仍未通过，原返回已保留。",
                    "只核对这处伏笔的依据与关系；不会重发Writer或重做章节计划。") from repair_exc
            contract_repair_text = ("\n原坏承诺补丁（未覆盖）：\n" + _stage_text(original_patch)
                + "\n前置关系实际错误：\n" + str(exc)
                + ("\n原Writer已保存的定义修复返回：\n" + _stage_text(retained_repair) if retained_repair is not None else "")
                + "\n原Writer本次实际返回：\n" + _stage_text(completion_return))
        patch_hash = digest(patch.model_dump(mode="json"))
        result_hashes = {"volume-detail":digest(revised_detail.model_dump(mode="json")),
                         "chapter-window":digest(revised_window.model_dump(mode="json"))}
        if update_outline_promises:
            result_hashes["outline"] = digest(revised_outline.model_dump(mode="json"))
        result_hash = digest(result_hashes)
        # 差异审核沿原角色资料权限替换设定上下文；允许引用的只读对照与逐字定位使用同一份文本。
        review_sources = (stable_sources.replace(settings_context,review_settings_context,1)
                          if settings_context else stable_sources + review_settings_context)
        fixed_definitions = {} if update_outline_promises else {plan.thread_id:plan.model_dump(mode="json") for plan in original_outline.promises}
        if lock_volume:
            fixed_definitions.update({plan.thread_id:plan.model_dump(mode="json") for plan in original_detail.promises})
        review_binding = {"base_hash":base_hash,"patch_hash":patch_hash,"result_hash":result_hash,
            "reviewed_thread_ids":sorted(set(conflicts) | {plan.thread_id for plan in patch.plans}),"paths":paths}
        # 明示来源身份，避免把本次候选误当生效合同，或把实际补齐的父定义误判成越界新增。
        authority = {"source_authority_version":2,"lock_outline":not update_outline_promises,"lock_volume":lock_volume,
            "outline_status":"作者本次只授权同步冲突伏笔定义；其余大纲与范围只读" if update_outline_promises else
                "已生效，只读" if active_outline is not None else "本次上层已通过审核，下层对齐阶段只读",
            "source_manifest_hash":active_start_hashes["planning/active-v2.json"],
            "locked_definitions":fixed_definitions,
            "locked_definition_sources":{identity:("volume-detail" if lock_volume and
                any(plan.thread_id == identity for plan in original_detail.promises) else "outline") for identity in fixed_definitions},
            "volume_detail_status":"已生效，只读" if lock_volume else "本次已通过本层审核、尚未发布的候选，可在授权内对齐",
            "window_status":"本次未发布候选", "required_parent_ids":sorted(set(review_binding["reviewed_thread_ids"]) - set(conflicts))}
        source_text = (review_sources + "\n原任务：\n" + root_intent
            + "\n程序核验的来源身份与只读合同：\n" + json.dumps(authority,ensure_ascii=False,indent=2)
            + "\n当前明确授权：\n" + instruction + "\n对齐前大纲：\n" + _stage_text(original_outline)
            + "\n对齐前卷细纲（身份以上表为准）：\n" + _stage_text(original_detail)
            + "\n对齐前窗口候选：\n" + _stage_text(original_window) + "\n原冲突定义：\n" + json.dumps(conflicts,ensure_ascii=False,indent=2)
            + contract_repair_text)
        patch_text = _stage_text(patch)
        review_system = (_EDITOR_SYSTEM +
            "\n返回PromiseAlignmentReviewV2；只审核跨层承诺对齐差异及受影响节拍，原层审核继续覆盖未改部分。"
            "依据程序给定的lock_volume与locked_definitions区分生效只读合同和本次未发布候选，不把候选提升成生效合同。"
            "lock_volume只约束卷细纲；大纲是否允许同步冲突定义由lock_outline及当前明确授权决定，其他大纲字段始终只读。"
            "下层曾通过审核的同ID副本也不能覆盖只读上层合同；在下层恢复统一定义不属于修改上层。"
            "合同可保留已发生的历史里程碑；仅章号早于当前锚点不等于重复执行，须看未来章节卡是否再次安排同一首次行动。"
            "核对原上下层实质信息、因果与触发/完成条件；预计章节窗口不是硬截止。"
            "实际补齐的父定义也须核对前史依据与真实依赖，原合同实际引用的缺失父定义可补齐，不是无关新增。"
            "先前坏返回未采用，当前实际窄补丁才是审核对象；比较补核是否丢掉真实因果，不能只凭编号合法通过。"
            "原样返回base_hash/patch_hash/result_hash及绑定的完整reviewed_thread_ids，不自行增删。"
            "candidate_excerpt逐字取自当前实际窄补丁，source_excerpt取自给定来源或原定义。")
        review_prompt = source_text + "\n当前实际窄补丁：\n" + patch_text + "\n绑定身份：\n" + json.dumps(review_binding)
        if review_path.is_file():
            assessment = PromiseAlignmentReviewV2.model_validate_json(_read(review_path))
        else:
            if proof.get("review_started"):
                raise PlanningNeedsAttention("承诺差异审核已请求但未取得完整返回，未重发审核。","先核对原审核回执和用量，保留Writer结果。")
            current_node.update(phase="review")
            proof.update(review_started=True,patch_hash=patch_hash,result_hash=result_hash,paths=paths)
            atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
            reviewed = await engine.provider.generate_json(system_prompt=review_system, user_prompt=review_prompt,
                output_model=PromiseAlignmentReviewV2,effort="high",thinking=False,
                max_tokens=min(engine.settings.max_output_tokens,8192),timeout_seconds=engine.settings.planning_timeout_seconds,agent_role=review_role)
            trace.record_model("promise-alignment.review",reviewed,"原审核角色补核承诺差异，不重审未改章节卡")
            assessment = reviewed.data
            atomic_write_text(review_path,assessment.model_dump_json(indent=2))
            verify_canon()
        gaps = _alignment_review_gaps(assessment,review_binding,patch_text,source_text,min_review_confidence)
        if gaps:
            # 先让原审核角色核对身份、引文及来源层级；不因报告误读重发Writer，机会沿原节点留痕。
            before_path = directory / "promise-alignment-review-before-recheck.json"
            recheck_path = directory / "promise-alignment-review-recheck.json"
            saved = proof.get("review_recheck")
            if not before_path.is_file():
                if saved is not None:
                    raise PlanningNeedsAttention("原差异复核报告副本缺失，未补造来源。","恢复原报告后从本节点继续。")
                atomic_write_text(before_path,assessment.model_dump_json(indent=2))
            original_assessment = PromiseAlignmentReviewV2.model_validate_json(_read(before_path))
            if saved is None and original_assessment != assessment:
                raise PlanningNeedsAttention("差异复核副本与当前原报告不一致，未采用。","核对原报告与本次候选身份。")
            recheck_binding = {**review_binding,"source_authority_version":2,"source_hash":content_hash(source_text),
                "original_review_hash":digest(original_assessment.model_dump(mode="json"))}
            recheck_key = "review_recheck"
            review_feedback = original_assessment
            if saved is not None and "source_authority_version" not in saved:
                # 旧输入未声明大纲来源归属；核验并保留旧请求后，只为这次协议补正新增一个窄节点。
                # 普通来源变化仍拒绝，不能让每次继续或改措辞领取新的审核次数。
                legacy = PromiseAlignmentReviewV2.model_validate_json(_read(recheck_path)) if recheck_path.is_file() else None
                if (legacy is None or proof.get("review_recheck_result_hash") != digest(legacy.model_dump(mode="json"))
                        or any(saved.get(key) != value for key,value in {**review_binding,
                            "original_review_hash":recheck_binding["original_review_hash"]}.items())):
                    raise PlanningNeedsAttention("旧差异复核的原返回或对象身份未核清，未增加请求。","先恢复旧节点回执，不重做规划。")
                recheck_key = "review_authority_recheck"
                recheck_path = directory / "promise-alignment-review-authority-recheck.json"
                review_feedback = legacy
                recheck_binding["prior_recheck_hash"] = digest(legacy.model_dump(mode="json"))
                saved = proof.get(recheck_key)
            result_hash_key = recheck_key + "_result_hash"
            if saved is not None and saved != recheck_binding:
                raise PlanningNeedsAttention("原差异复核的来源或报告身份已变化，未重复请求。","核对原复核记录与当前候选。")
            if recheck_path.is_file():
                if saved is None:
                    raise PlanningNeedsAttention("已有差异复核缺少原请求绑定，未采用。","恢复本节点的原来源记录。")
                assessment = PromiseAlignmentReviewV2.model_validate_json(_read(recheck_path))
                if proof.get(result_hash_key) != digest(assessment.model_dump(mode="json")):
                    raise PlanningNeedsAttention("差异复核返回与原回执哈希不符，未采用。","核对原角色返回，不重新发送或强写通过。")
            else:
                if saved is not None:
                    raise PlanningNeedsAttention("原差异复核已发起但返回未闭合，未自动重发。","先核对原请求回执及用量。")
                proof[recheck_key] = recheck_binding
                atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
                verify_canon()
                checked = await engine.provider.generate_json(system_prompt=review_system,
                    user_prompt=review_prompt + "\n此前报告，仅供逐项复核，不能当作事实来源：\n" + review_feedback.model_dump_json(indent=2)
                        + "\n本次只重核上述报告的身份、原文及判断：\n" + "；".join(gaps)
                        + "\n先核对所称只读限制是否适用、父定义是否有实际依赖，再判断实质信息是否丢失。"
                        "有据问题保留并说明最小修法，误读或无法定位的指控撤回；不得为了通过省略真实问题。",
                    output_model=PromiseAlignmentReviewV2,effort="high",thinking=False,
                    max_tokens=min(engine.settings.max_output_tokens,8192),
                    timeout_seconds=engine.settings.planning_timeout_seconds,agent_role=review_role)
                assessment = checked.data
                atomic_write_text(recheck_path,assessment.model_dump_json(indent=2))
                proof[result_hash_key] = digest(assessment.model_dump(mode="json"))
                atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
                trace.record_model("promise-alignment.review-recheck",checked,"原审核角色定点重核来源身份与实际差异，原报告保留")
                verify_canon()
            gaps = _alignment_review_gaps(assessment,review_binding,patch_text,source_text,min_review_confidence)
            if not gaps:
                atomic_write_text(review_path,assessment.model_dump_json(indent=2))
        # 有据的语义意见交回原Writer，最多两次局部修订；不重写已通过章节卡，也不把来源复核当成修稿。
        async def semantic_request(number: int, kind: str, model: Any, system: str, prompt: str,
                binding: dict[str, Any], role: str) -> Any:
            """同一补救请求只发送一次；保存完整返回与哈希，未知或迟到结果不能领取新次数。"""
            from .runtime import active_runtime, active_generation_attempt_limit, active_generation_request_id
            from .model_usage import UsageLedger
            runtime = active_runtime.get()
            if not scope or not runtime or runtime.task_id != scope.task_id:
                raise PlanningNeedsAttention("伏笔局部修订缺少原任务身份。","先恢复原任务，不新建创作或重发请求。")
            records = proof.setdefault("semantic_repairs", {})
            key = f"{number}-{kind}"
            path = directory / f"promise-alignment-semantic-{key}.json"
            saved = records.get(key)
            if saved is not None:
                if saved.get("binding") != binding or not path.is_file():
                    raise PlanningNeedsAttention("原伏笔局部修订的来源变化或返回未闭合。","保留原请求，核对回执后续接。")
                result = model.model_validate_json(_read(path))
                if saved.get("return_hash") != digest(result.model_dump(mode="json")):
                    raise PlanningNeedsAttention("伏笔局部修订返回与保存回执不符。","核对原返回，不重复生成。")
                return result
            if path.is_file():
                raise PlanningNeedsAttention("已有局部修订返回缺少原发送身份。","恢复原请求绑定，不另发模型。")
            request_id = "planning_semantic_repair:" + digest({**binding,"kind":kind})
            saved = {"binding":binding,"request_id":request_id,"status":"started",
                "sent_baseline":UsageLedger().task_role_sent_count(scope.task_id,role)}
            records[key] = saved
            atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
            old_limits = dict(runtime.role_attempt_limits)
            runtime.role_attempt_limits[role] = saved["sent_baseline"] + 1
            cap_token = active_generation_attempt_limit.set(1)
            request_token = active_generation_request_id.set(request_id)
            current_node.update(phase=f"semantic_{kind}")
            try:
                verify_canon()
                returned = await engine.provider.generate_json(system_prompt=system,user_prompt=prompt,
                    output_model=model,effort="high",thinking=False,
                    max_tokens=min(engine.settings.max_output_tokens,8192),
                    timeout_seconds=engine.settings.planning_timeout_seconds,agent_role=role)
                trace.record_model(f"promise-alignment.semantic-{number}.{kind}",returned,
                    "原角色按有据意见定点修订伏笔定义" if kind == "writer" else "原审核角色核对局部修订及完整定义关系")
                result = returned.data
                atomic_write_text(path,result.model_dump_json(indent=2))
                verify_canon()
                saved.update(status="returned",return_hash=digest(result.model_dump(mode="json")))
                atomic_write_text(proof_path,json.dumps(proof,ensure_ascii=False,indent=2))
                return result
            finally:
                active_generation_request_id.reset(request_token)
                active_generation_attempt_limit.reset(cap_token)
                runtime.role_attempt_limits = old_limits

        for number in range(1, COMPLETION_ATTEMPT_LIMIT + 1):
            if not gaps:
                break
            # 只按能够逐字回源的意见改稿；附加坏引文留在原报告，不能盖住其余有据问题或授权改别处。
            grounded_evidence = [item for item in assessment.review.evidence
                if item.candidate_excerpt in patch_text and item.source_excerpt in source_text]
            repair_assessment = assessment.model_copy(update={"review":assessment.review.model_copy(
                update={"evidence":grounded_evidence})})
            if (assessment.review.verdict != "revise" or not assessment.review.repair_instruction.strip()
                    or _alignment_review_gaps(repair_assessment,review_binding,patch_text,source_text,
                        min_review_confidence,require_pass=False)):
                break
            matches = [{plan.thread_id for plan in patch.plans if item.candidate_excerpt in _stage_text(plan)}
                for item in grounded_evidence]
            if not matches or any(not identities for identities in matches):
                break
            affected_ids = set().union(*matches)
            if affected_ids & set(fixed_definitions):
                break
            binding = {**review_binding,"source_hash":content_hash(source_text),
                "review_hash":digest(assessment.model_dump(mode="json")),"affected_ids":sorted(affected_ids),
                "grounded_evidence_hash":digest([item.model_dump(mode="json") for item in grounded_evidence]),
                "task_id":scope.task_id if scope else "","snapshot_hash":scope.snapshot_hash if scope else "","round":number}
            repair = await semantic_request(number,"writer",PromiseAlignmentPatch,
                _WRITER_SYSTEM + "\n本次只按有据差异审核修订指定affected_ids的PromisePlan，完整且仅返回这些定义；"
                "beat_changes必须为空。保留各定义身份和类型，其余定义、父定义、所有节拍、章节卡、大纲和细纲正文锁定。"
                "自行逐项核对所给原文与已通过章节安排，校正时间、因果和完成条件；不重复已发生事件、不提前揭示。"
                "不能只改某一处句子而留下同定义内相矛盾的触发条件或完成条件。",
                source_text + "\n当前实际补丁：\n" + patch_text + "\n本轮已定位原文的审核意见：\n" + repair_assessment.model_dump_json(indent=2)
                + "\n仅修订以下定义：\n" + json.dumps(sorted(affected_ids),ensure_ascii=False),binding,"writer")
            try:
                patch = _merge_alignment_contract_repair(patch,repair,affected_ids)
                revised_outline,revised_detail,revised_window,paths = _apply_promise_alignment(
                    original_outline,original_detail,original_window,patch,
                    lock_volume=lock_volume,update_outline=update_outline_promises)
            except ValueError as exc:
                raise PlanningNeedsAttention("伏笔局部修订超出原范围或未通过关系门禁，原返回保留。",
                    "只处理这处修订，不重做已通过章节。") from exc
            merged_path = directory / f"promise-alignment-semantic-{number}-candidate.json"
            merged_text = patch.model_dump_json(indent=2)
            if merged_path.is_file() and _read(merged_path) != merged_text:
                raise PlanningNeedsAttention("局部修订派生候选与原返回不符。","核对原补丁，不覆盖已有产物。")
            atomic_write_text(merged_path,merged_text)
            patch_hash = digest(patch.model_dump(mode="json"))
            result_hashes = {"volume-detail":digest(revised_detail.model_dump(mode="json")),
                "chapter-window":digest(revised_window.model_dump(mode="json"))}
            if update_outline_promises:
                result_hashes["outline"] = digest(revised_outline.model_dump(mode="json"))
            result_hash = digest(result_hashes)
            review_binding = {**review_binding,"patch_hash":patch_hash,"result_hash":result_hash,"paths":paths}
            patch_text = _stage_text(patch)
            assessment = await semantic_request(number,"review",PromiseAlignmentReviewV2,review_system,
                source_text + "\n当前实际窄补丁：\n" + patch_text + "\n绑定身份：\n" + json.dumps(review_binding)
                + "\n上一轮意见与实际局部返回，仅作修订对照：\n" + assessment.model_dump_json(indent=2)
                + "\n" + repair.model_dump_json(indent=2),
                {**binding,"revised_binding":review_binding,"repair_hash":digest(repair.model_dump(mode="json"))},review_role)
            gaps = _alignment_review_gaps(assessment,review_binding,patch_text,source_text,min_review_confidence)
        if gaps:
            titles = "、".join(plan.title for plan in patch.plans if plan.thread_id in conflicts)
            raise PlanningNeedsAttention(f"{titles or '本次伏笔'}的一致性核对尚未完成：{'；'.join(gaps)}。原规划和本次修改均已保留。",
                "可先查看待处理工作中的意见和来源，再安排负责角色补齐本处；已通过的章节安排无需重新生成。")
        # 半写入或已通过候选的恢复仅允许原版与本次最终派生版；范围外手改不得被覆盖。
        checked_objects = [("volume-detail",detail),("chapter-window",window)] + ([("outline",outline)] if update_outline_promises else [])
        if any(digest(value.model_dump(mode="json")) not in {original_hashes[name],result_hashes[name]}
               for name,value in checked_objects):
            raise PlanningNeedsAttention("对齐期间候选发生额外修改，未覆盖当前候选。","对照修改的精确字段后从窄节点继续。")
        verify_canon()
        expected_proof = {**proof,"status":"passed","base_hash":base_hash,"patch_hash":patch_hash,
            "result_hash":result_hash,"result_hashes":result_hashes,"paths":paths,"review_hash":digest(assessment.model_dump(mode="json"))}
        if proof.get("status") == "passed" and any(proof.get(key) != expected_proof[key]
                for key in ("base_hash","patch_hash","result_hash","result_hashes","paths","review_hash")):
            raise PlanningNeedsAttention("已通过的承诺覆盖证明与当前产物不一致，未发布。","回查原补丁与补审，不自动重算旧证明冒充通过。")
        atomic_write_text(proof_path,json.dumps(expected_proof,ensure_ascii=False,indent=2))
        # 只在附加门禁通过后更新本次run的有效候选；原版及原review仍在before副本中。
        atomic_write_text(directory / "volume-detail-candidate.json",revised_detail.model_dump_json(indent=2))
        atomic_write_text(directory / "chapter-window-candidate.json",revised_window.model_dump_json(indent=2))
        if update_outline_promises:
            atomic_write_text(directory / "outline-candidate.json",revised_outline.model_dump_json(indent=2))
        trace.record("promise-alignment.coverage","completed","承诺定义已统一并通过差异补审；原审核继续覆盖未改部分",
            metadata={"base_hash":base_hash,"patch_hash":patch_hash,"result_hash":result_hash,"paths":paths,
                "conflict_ids":sorted(conflicts),"review_role":review_role},public=False)
        return revised_outline,revised_detail,revised_window

    superseded_files: list[dict[str, str]] = []

    try:
        outline_task = (f"重设计全书大纲约5000～25000字，必须有主线、背景设定、重要场景、人物变化、主要高潮、大致结局和连续的卷级方向。"
                        f"前 {anchor} 章已经发生，不得更改；已接受正文可能跨多个卷，不能把它们强行合成第一卷。用户明确卷数和总章数时必须保持，未指定才可合理调整；每卷至少10章。body 不写逐章计划。"
                        f"\n本次代码核验的明确规模：{json.dumps(scope_constraints, ensure_ascii=False)}")
        if focus == "outline" and preserved_outline is not None:
            outline_task = ("只修改生效全书大纲，保留既有卷号与各卷起止章，不改正史；"
                            "下游细纲和近期规划本轮只做必要相容性审核，不自动重写。\n生效大纲："
                            + _stage_text(preserved_outline))
        if active_outline is not None:
            outline = BookOutlineV2.model_validate_json(_read(trace.run_dir / "outline-candidate.json")) if approved_run_id else active_outline
            atomic_write_text(trace.run_dir / "outline-candidate.json", outline.model_dump_json(indent=2))
            trace.record("outline.reuse", "completed", "沿用已生效大纲；本轮不重新生成")
        else:
            outline = await generate_and_review("outline", BookOutlineV2, stable_sources, outline_task)
        if (focus == "outline" and preserved_outline is not None
                and [(v.volume_no, v.chapter_start, v.chapter_end) for v in outline.volumes]
                != [(v.volume_no, v.chapter_start, v.chapter_end) for v in preserved_outline.volumes]):
            raise PlanningNeedsAttention("单独大纲修改改变了卷边界，候选保留但未发布。", "请确认是否扩大为完整三层重设计。")
        if len(outline.volumes) < 2:
            raise ValidationGateError("大纲缺少后续卷级方向；候选未发布。")
        if ((total_chapters is not None and outline.volumes[-1].chapter_end != total_chapters)
                or (volume_count is not None and len(outline.volumes) != volume_count)):
            raise ValidationGateError("全书候选规模与用户确认范围不同，未发布。")
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
        outline,detail,plan = await align_promises(outline,detail,plan)
        outline_text = _render_outline(outline)
        detail_text = _render_detail(detail)
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
                    **scope_constraints,
                    "run_id": trace.run_id, "anchor": anchor, "end": end,
                    "instruction": requested_instruction, "focus": focus,
                    "source_manifest_hash": active_start_hashes["planning/active-v2.json"],
                    "artifact_hashes": {**{f"{stage}-{suffix}": content_hash(_read(
                        trace.run_dir / f"{stage}-{suffix}"))
                        for stage in ("outline", "volume-detail", "chapter-window")
                        for suffix in (("candidate.json",) if ((focus == "chapter-window" and
                                                               (stage == "outline" or stage == "volume-detail" and active_detail is not None))
                                                               or (focus in {"volume-detail", "future-detail"} and stage == "outline"))
                                       else ("candidate.json", "review.json"))},
                        **{name:content_hash(_read(trace.run_dir / name)) for name in _alignment_artifact_names()
                           if (trace.run_dir / name).is_file()}},
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
                "promise_alignment_artifacts":{name:content_hash(_read(trace.run_dir / name))
                    for name in _alignment_artifact_names() if (trace.run_dir / name).is_file()},
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
                    "created_at": manifest["published_at"], "status": "kept", "files": superseded_files,
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
                               for path in trace.run_dir.glob("*-review.json")},
                            **{str((trace.run_dir / name).relative_to(project.root)):content_hash(_read(trace.run_dir / name))
                               for name in _alignment_artifact_names() if (trace.run_dir / name).is_file()}},
                "accepted": [[row["chapter_no"], row["version"], row["content_hash"]] for row in accepted],
                "settings_source_hash": settings_fingerprint, "preferences_hash": content_hash(frozen_preferences),
                "bundle": v2_execution_bundle(project, manifest, outline, detail, plan).model_dump(mode="json"),
                "old_plans": old_plan_rows, "prior_brief": prior_brief.model_dump(mode="json"),
                "brief": revised_brief.model_dump(mode="json"), "source_hashes": active_start_hashes,
                "focus": focus, "approved_run_id": approved_run_id, "history_choice": False,
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
                "pending_history_choice": False,
                "next_action": "当前规划已生效，可以按本次安排继续写作。"}
    except asyncio.CancelledError:
        trace.record("planning-v2", "cancelled", "已停止；本阶段候选保留，未发布未审核结果")
        trace.finish(status="cancelled", summary="规划已停止，现有正史不变")
        raise
    except PlanningNeedsAttention as exc:
        # 阶段代号和绝对路径留在检查点；作者沿界面查看待办即可续接。
        exc.next_action += " 本阶段成果和来源已保留，可在待处理工作中查看缺口，从未完成的步骤继续。"
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
