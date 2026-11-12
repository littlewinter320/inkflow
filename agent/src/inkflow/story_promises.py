"""Planning contracts and source-bound lifecycle records for promises and hooks."""
from __future__ import annotations

import json
import re
from typing import Any

from .schemas import MemoryPatch, PromisePlan, StrictModel, ThreadMutation
from .utils import content_hash, json_dumps
from pydantic import Field


_BOOK_ENDING = r"(?:全书(?:终章|结局|完结|收尾)|整(?:本|部)(?:书|小说)(?:的)?(?:终章|结局|完结)|完结(?:这部|这本|本次|整个|整部)?(?:小说|本书|全书))"
_CHAPTER = r"第\s*([1-9]\d{0,4})\s*章"


def requested_book_ending(instruction: str) -> tuple[bool, int | None]:
    """从肯定的作者要求识别全书收尾，章号只能来自直接指定的终点。"""
    requested, targets = False, set()
    for clause in re.split(r"[。！？；，\n]", instruction):
        if re.search(r"(?:不想|不必|不应|不算|还没|不是|不要|暂不|先别|不能|无需|不再|别)[^。！？；，\n]{0,18}(?:终章|结局|完结|收尾)", clause):
            continue
        direct = re.search(_CHAPTER + r"\s*(?:作为|就是|是|为|改成|改为|修成|写成|写作|完成)?\s*(?:这本书的?|本书的?|小说的?)?(?:全书)?(?:终章|结局|完结)(?!后|前)", clause)
        location = re.search(r"(?:终章|结局|完结)(?:设在|定在|安排在|落在|是|为|在|到|于)\s*" + _CHAPTER, clause)
        if match := (direct or location):
            targets.add(int(match.group(1)))
        requested = requested or bool(direct or location or re.search(_BOOK_ENDING, clause))
    return requested and len(targets) < 2, next(iter(targets)) if len(targets) == 1 else None


def chapter_ending_contract(project, chapter_no: int, task: str, *, temporary_goal: str = "",
                            prior: list[dict[str, Any]] | None = None, planned: bool = True) -> dict[str, Any]:
    """续接从同版 Writer 任务或已核验临时目标恢复作者终点。"""
    _, author_final = requested_book_ending(task)
    if author_final is None:
        chapter = project.db.get_chapter(chapter_no)
        if chapter:
            saved = next((item["data"] for item in project.db.list_agent_artifacts(
                chapter_no=chapter_no, artifact_type="writer_context_manifest", limit=20)
                if item["status"] == "current" and item["chapter_version"] <= chapter["version"]), {})
            # 审查微改不重建 Writer 资料，当前写作目标仍可沿用；审核证据始终按新版另核。
            author_final = saved.get("author_final") or requested_book_ending(saved.get("task", ""))[1]
    if author_final is None and temporary_goal:
        _, author_final = requested_book_ending(temporary_goal)
    if not planned and author_final is None:
        return {}
    from .planning_pipeline import load_active_planning
    active = load_active_planning(project)
    return book_ending_contract(active[1] if active else None, chapter_no, prior or [], author_final=author_final)


def book_ending_contract(outline, chapter_no: int, prior: list[dict[str, Any]], *, author_final: int | None = None) -> dict[str, Any]:
    """从作者指定终点或生效全书大纲识别最后十章，规划只提供有来源的承诺。"""
    final_volume = max(outline.volumes, key=lambda volume: volume.chapter_end) if outline and outline.volumes else None
    final_chapter = author_final or (final_volume.chapter_end if final_volume else 0)
    remaining = final_chapter - chapter_no + 1
    if not 1 <= remaining <= 10:
        return {}
    history = {item['thread_id']: item for item in prior}
    promises = []
    for plan in outline.promises if outline else ():
        if plan.horizon != 'book' or not plan.completion_criteria:
            continue
        previous = history.get(plan.thread_id, {})
        promises.append({'thread_id': plan.thread_id, 'title': plan.title, 'question': plan.question,
                         'criteria': plan.completion_criteria, 'intentional_open': plan.open_reason,
                         'prior_status': previous.get('status', 'unknown')})
    if not promises and final_volume and final_chapter == final_volume.chapter_end:
        # 只有终点一致时，旧大纲最后一篇的目标才作为终局判据。
        promises.append({'thread_id': 'book-outcome', 'title': '全书主冲突与结局',
                         'question': final_volume.central_conflict, 'criteria': [final_volume.outcome],
                         'intentional_open': '', 'prior_status': 'unknown'})
    if remaining == 1:
        # 结构化伏笔表可能缺项。正文仍须回答全书在实际叙事中提出的问题。
        for slug, title, criteria in (
            ('payoff', '主线兑现与人物归宿', '根据前文实际提出的主问题和关键伏笔，交代结果或最终处置、人物选择的后果和关系的落点；超出证据的细节可以有明确边界，不凭空解谜，也不把主要结果留给后续。'),
            ('last-scene', '终幕收束与余韵', '核对章末实际停点，最后一幕展示结果发生后的人物或世界。可以用日常、动作或意象呼应前文；不新开危机、谜团或下一段任务，不以仍约好追查、等信或来日揭晓代替结局。'),
        ):
            thread_id = f'@ending/{slug}'
            while any(item['thread_id'] == thread_id for item in promises):
                thread_id += '/'
            promises.append({'thread_id': thread_id, 'title': title, 'criteria': [criteria],
                             'current_evidence': 'last_scene' if slug == 'last-scene' else 'chapter'})
    return {'final_chapter': final_chapter, 'remaining': remaining,
            **({'author_final': author_final} if author_final else {}),
            'progression_weight': 35 + 2 * (11 - remaining),
            'promises': promises}


def packet_ending_contract(packet) -> dict[str, Any]:
    """由同一资料包恢复专项范围，旧审查资料包沿用原评分。"""
    section = next((part for part in packet.sections if part.key == 'ENDING'), None)
    return json.loads(section.content) if section else {}


def _plan(value: Any) -> dict[str, Any]:
    return PromisePlan.model_validate(value).model_dump(mode="json")


def _binding(plan: dict[str, Any]) -> tuple[str, str]:
    return "promise-plan:" + plan["thread_id"], content_hash(json_dumps(plan, indent=None))


def planning_promises(bundle: Any) -> list[dict[str, Any]]:
    """Merge actual planning definitions without inventing a canonical lifecycle."""
    if bundle is None:
        return []
    found: dict[str, dict[str, Any]] = {}
    levels = [("book", bundle.book), ("volume", bundle.current_volume), ("arc", bundle.current_arc)]
    levels.extend(("chapter", card) for card in bundle.current_arc.chapter_cards)
    for level, owner in levels:
        for value in getattr(owner, "promises", []):
            plan = _plan(value)
            identity, digest = _binding(plan)
            previous = found.get(plan["thread_id"])
            if previous and previous["source_hash"] != digest:
                raise ValueError(f"承诺 {plan['thread_id']} 在不同规划层存在冲突定义")
            if not previous:
                found[plan["thread_id"]] = {**plan, "source_id": identity,
                    "source_hash": digest, "source_level": level}
    for card in bundle.current_arc.chapter_cards:
        for beat in getattr(card, "promise_beats", []):
            if beat.thread_id not in found:
                raise ValueError(f"章节承诺动作引用未声明编号：{beat.thread_id}")
    for identity, plan in found.items():
        if any(parent not in found for parent in plan["parent_thread_ids"]):
            raise ValueError(f"承诺 {identity} 引用了未声明的前置承诺")
    visited: dict[str, int] = {}
    for identity in found:
        stack = [(identity, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                visited[current] = 2
            elif visited.get(current) == 1:
                raise ValueError("承诺前置关系存在循环")
            elif visited.get(current) != 2:
                visited[current] = 1
                stack.append((current, True))
                stack.extend((parent, False) for parent in found[current]["parent_thread_ids"])
    return list(found.values())


def thread_patch_gaps(patch: MemoryPatch, content: str, *, prior_threads: list[dict[str, Any]],
                      plans: list[dict[str, Any]], sources: dict[int, str],
                      source_bindings: dict[int, dict[str, Any]] | None = None) -> list[str]:
    """Verify lifecycle evidence; an estimated chapter window is never a completion test."""
    known_plans = {item["thread_id"]: item.get("plan") for item in prior_threads if item.get("plan")}
    prior = {item["thread_id"]: item for item in prior_threads
             if item.get("authority") not in {"stale", "planned"}
             and item.get("source_status") not in {"stale", "provisional"}
             and item.get("status") != "unknown"}
    planned = {item["thread_id"]: item for item in plans}
    errors, seen = [], set()
    for mutation in patch.threads:
        item = mutation.model_dump(mode="json")
        identity = item["thread_id"]
        if identity in seen:
            errors.append(f"承诺 {identity} 在同章补丁重复变更")
        seen.add(identity)
        old = prior.get(identity, {})
        candidate = item.get("plan") or old.get("plan")
        modern = bool(identity in planned or (plans and identity not in prior) or candidate or known_plans.get(identity) or item.get("evidence") or item.get("reason")
                      or item.get("condition_checks") or item.get("plan_source_id") or item.get("plan_source_hash"))
        # Legacy recovery retains its old shape and is displayed as unverified in the catalog.
        if not modern:
            continue
        evidence = str(item.get("evidence") or "")
        if not evidence.strip() or evidence not in content:
            errors.append(f"承诺 {identity} 的本次正文原句未定位")
        if not candidate:
            legacy_source = (not prior_threads and not plans) or (
                old.get("authority") == "accepted" and old.get("source_status") == "accepted"
                and old.get("kind") == item["kind"] and old.get("status") in {"open", "advanced", "delayed"})
            legacy_advance = (legacy_source and identity not in planned and not known_plans.get(identity)
                and item["status"] in {"open", "advanced", "delayed"}
                and (item["status"] != "delayed" or bool(str(item.get("reason") or "").strip()))
                and not item.get("plan_source_id") and not item.get("plan_source_hash"))
            if not legacy_advance:
                errors.append(f"承诺 {identity} 缺少可核验的规划定义")
            else:
                # An evidenced delay keeps an accepted legacy thread unresolved; it invents no completion contract.
                checks = item.get("condition_checks") or []
                if len({check["condition"] for check in checks}) != len(checks):
                    errors.append(f"承诺 {identity} 的推进条件重复")
                for check in checks:
                    number, quote = check["source_chapter"], str(check.get("quote") or "")
                    source = content if number == patch.chapter_no else sources.get(number, "") if number < patch.chapter_no else ""
                    if ((check["status"] == "met" and not quote.strip()) or (quote and (not source or quote not in source))):
                        errors.append(f"承诺 {identity} 的推进条件引文未定位：{check['condition']}")
                    if quote and number < patch.chapter_no:
                        binding = (source_bindings or {}).get(number, {})
                        if (not check.get("source_version") or not check.get("source_hash")
                                or source_bindings is not None and (
                                    check["source_version"] != binding.get("version", binding.get("source_version"))
                                    or check["source_hash"] != binding.get("content_hash", binding.get("source_hash")))):
                            errors.append(f"承诺 {identity} 的推进条件来源版本或哈希缺失、变化：{check['condition']}")
            continue
        try:
            plan = _plan(candidate)
        except ValueError:
            errors.append(f"承诺 {identity} 的规划定义无效")
            continue
        if plan["thread_id"] != identity or plan["kind"] != item["kind"]:
            errors.append(f"承诺 {identity} 的规划身份或类型不一致")
        source_id, digest = _binding(plan)
        original = known_plans.get(identity)
        try:
            original_matches = bool(original and _plan(original) == plan)
        except ValueError:
            original_matches = False
        binding_id = item.get("plan_source_id") or (old.get("plan_source_id") if original_matches else "")
        binding_hash = item.get("plan_source_hash") or (old.get("plan_source_hash") if original_matches else "")
        active = planned.get(identity)
        active_matches = bool(active and active.get("source_id") == source_id and active.get("source_hash") == digest)
        historical_matches = bool(original_matches and old.get("plan_source_id") == source_id
                                  and old.get("plan_source_hash") == digest)
        if (binding_id != source_id or binding_hash != digest
                or not (active_matches or historical_matches)):
            errors.append(f"承诺 {identity} 未绑定当前有效规划或已接受的同版原规划")
        if original and not original_matches:
            errors.append(f"承诺 {identity} 不得以新规划悄悄覆盖已接受的承诺定义")
        conditions = list(dict.fromkeys([*plan.get("trigger_conditions", []), *plan.get("completion_criteria", [])]))
        checks = item.get("condition_checks") or []
        checked: dict[str, dict[str, Any]] = {}
        for check in checks:
            condition = check["condition"]
            if condition not in conditions or condition in checked:
                errors.append(f"承诺 {identity} 的条件检查对象未知或重复：{condition}")
            checked[condition] = check
            number = check["source_chapter"]
            source = content if number == patch.chapter_no else sources.get(number, "") if number < patch.chapter_no else ""
            quote = str(check.get("quote") or "")
            if check["status"] == "met" and (not quote.strip() or not source or quote not in source):
                errors.append(f"承诺 {identity} 的已满足条件缺少可定位原文：{condition}")
            elif quote and (not source or quote not in source):
                errors.append(f"承诺 {identity} 的条件引文未定位：{condition}")
            if check["status"] == "met" and number < patch.chapter_no:
                if not check.get("source_version") or not check.get("source_hash"):
                    errors.append(f"承诺 {identity} 的前章条件缺少版本与哈希绑定：{condition}")
                elif source_bindings is not None:
                    binding = source_bindings.get(number, {})
                    if (check["source_version"] != binding.get("version", binding.get("source_version"))
                            or check["source_hash"] != binding.get("content_hash", binding.get("source_hash"))):
                        errors.append(f"承诺 {identity} 的前章条件来源版本或哈希已变化：{condition}")
        if item["status"] == "paid":
            if not plan.get("completion_criteria"):
                errors.append(f"承诺 {identity} 没有明确完成判据，不能证明全部兑现")
            if any(checked.get(condition, {}).get("status") != "met" for condition in conditions):
                errors.append(f"承诺 {identity} 的触发条件与完成判据尚未全部有据满足")
        if item["status"] in {"delayed", "abandoned"} and not str(item.get("reason") or "").strip():
            errors.append(f"承诺 {identity} 的延期或放弃缺少理由")
    return list(dict.fromkeys(errors))



def partition_thread_patch(patch: MemoryPatch, content: str, *, prior_threads: list[dict[str, Any]],
                           plans: list[dict[str, Any]], sources: dict[int, str],
                           source_bindings: dict[int, dict[str, Any]] | None = None) -> tuple[MemoryPatch, list[dict[str, Any]]]:
    """仅隔离未核验的可选线索候选；不伪造状态、不改原候选，事实与事件另经原门禁。"""
    from collections import Counter
    counts = Counter(item.thread_id for item in patch.threads)
    verified, deferred = [], []
    for item in patch.threads:
        single = patch.model_copy(update={"threads": [item]})
        gaps = thread_patch_gaps(single, content, prior_threads=prior_threads, plans=plans,
            sources=sources, source_bindings=source_bindings)
        if counts[item.thread_id] > 1:
            gaps.append(f"承诺 {item.thread_id} 在同章补丁重复变更")
        if gaps:
            deferred.append({"thread_id": item.thread_id, "candidate": item.model_dump(mode="json"),
                "gaps": list(dict.fromkeys(gaps))})
        else:
            verified.append(item)
    return patch.model_copy(update={"threads": verified}), deferred


def thread_catalog(project: Any, bundle: Any = None, *, before_chapter: int | None = None,
                   include_plans: bool = True) -> list[dict[str, Any]]:
    """Read all accepted lifecycle stages and planning candidates without mutating books."""
    if bundle is None:
        bundle = project.db.get_current_plan_bundle()
    plans = planning_promises(bundle) if include_plans else []
    catalog: dict[str, dict[str, Any]] = {}
    for item in plans:
        plan = _plan({key: value for key, value in item.items() if key not in {"source_id", "source_hash", "source_level"}})
        catalog[item["thread_id"]] = {**plan, "plan": plan, "status": "planned", "authority": "planned",
            "source_status": "planned", "plan_source_id": item["source_id"],
            "plan_source_hash": item["source_hash"], "source_level": item["source_level"], "history": []}
    accepted = project.db.accepted_chapters()
    boundary = max((int(item["chapter_no"]) for item in accepted), default=0)
    upper = boundary + 1 if before_chapter is None else min(boundary + 1, before_chapter)
    rows = [row for row in project.db.narrative_history_rows(upper) if int(row["chapter_no"]) < upper]
    bindings = {int(row["chapter_no"]): row for row in rows
                if row.get("content_text") and content_hash(str(row["content_text"])) == row["content_hash"]}
    with project.db.connect() as connection:
        derived_rows = connection.execute("SELECT value_json FROM metadata WHERE key LIKE 'promise_thread_index:%'").fetchall()
    derived = []
    for saved in derived_rows:
        try:
            value = json.loads(saved["value_json"])
            if isinstance(value, dict) and value.get("status") == "verified":
                derived.append(value)
        except (ValueError, TypeError):
            continue  # 派生缓存损坏不改变原记忆与已接受正文。
    for row in rows:
        number, version, digest = int(row["chapter_no"]), int(row["version"]), str(row["content_hash"])
        if not row.get("data_json"):
            for pending in catalog.values():
                if pending.get("authority") == "planned":
                    pending["coverage_reason"] = "部分已接受章节缺少记忆补丁；规划候选不能据此认定已埋设"
            continue
        text = str(row.get("content_text") or "")
        source_valid = bool(text and content_hash(text) == digest
                            and (version == 1 or row.get("committed_at") == row.get("updated_at")))
        try:
            patch = MemoryPatch.model_validate_json(row["data_json"])
            if patch.chapter_no != number:
                raise ValueError("记忆补丁章节身份不一致")
            threads = [item.model_dump(mode="json") for item in patch.threads]
        except (ValueError, TypeError):
            source_valid = False
            try:
                raw = json.loads(row.get("data_json") or "{}")
                threads = [item for item in raw.get("threads", [])
                           if isinstance(item, dict) and isinstance(item.get("thread_id"), str)]
            except (ValueError, TypeError, AttributeError):
                threads = []
        if source_valid:
            original_ids = {item["thread_id"] for item in threads}
            for saved in derived:
                basis = saved.get("basis", {})
                if (saved.get("chapter_no") != number or basis.get("version") != version
                        or basis.get("content_hash") != digest):
                    continue
                try:
                    proposed = MemoryPatch(chapter_no=number, chapter_summary="派生线索附件", threads=saved["threads"])
                    ids = {item.thread_id for item in proposed.threads}
                    if (ids & original_ids or basis.get("plan_hash") != content_hash(json_dumps(
                            [item for item in plans if item["thread_id"] in ids]))
                            or basis.get("prior_threads_hash") != content_hash(json_dumps(
                                [item for item in catalog.values() if item["thread_id"] in ids]))):
                        continue
                    source_texts = {n: str(value.get("content_text") or "") for n, value in bindings.items()}
                    if any(int(n) not in bindings or expected["hash"] != content_hash(source_texts[int(n)])
                            or expected["binding"].get("version", expected["binding"].get("source_version")) != bindings[int(n)]["version"]
                            for n, expected in basis.get("sources", {}).items()):
                        continue
                    if thread_patch_gaps(proposed, text, prior_threads=list(catalog.values()), plans=plans,
                            sources=source_texts, source_bindings=bindings):
                        continue
                except (ValueError, TypeError, KeyError):
                    continue
                threads.extend({**item.model_dump(mode="json"), "derived_work_id": saved["work_id"]} for item in proposed.threads)
                original_ids.update(ids)
        for mutation in threads:
            identity = mutation["thread_id"]
            previous = catalog.get(identity, {})
            history = list(previous.get("history", []))
            history.append({"chapter_no": number, "version": version, "source_hash": digest,
                "status": mutation.get("status", "unknown"), "reason": mutation.get("reason", ""),
                "evidence": mutation.get("evidence", ""), "condition_checks": mutation.get("condition_checks", []),
                "source_valid": source_valid})
            # A planned definition is not retrospective proof that an old detail was intentional.
            inherited = previous if previous.get("authority") == "accepted" else {}
            current = {**mutation, "history": history, "last_advanced_chapter": number,
                "source_version": version, "source_hash": digest,
                "authority": "accepted" if source_valid else "stale",
                "source_status": "accepted" if source_valid else "stale"}
            if source_valid and inherited.get("plan") and not current.get("plan"):
                for key in ("plan", "plan_source_id", "plan_source_hash"):
                    current[key] = inherited.get(key)
            if source_valid and inherited.get("planted_chapter") is not None:
                current["planted_chapter"] = inherited["planted_chapter"]
            current["legacy_unverified"] = not bool(current.get("plan") and current.get("evidence"))
            condition_sources_valid = True
            if source_valid and current.get("status") == "paid" and current.get("plan"):
                for check in current.get("condition_checks", []):
                    source_no = check["source_chapter"]
                    if check["status"] != "met" or source_no == number:
                        continue
                    binding = bindings.get(source_no, {})
                    if (source_no > number or not check.get("source_version") or not check.get("source_hash")
                            or check["source_version"] != binding.get("version")
                            or check["source_hash"] != binding.get("content_hash")
                            or not check.get("quote") or check["quote"] not in str(binding.get("content_text") or "")):
                        condition_sources_valid = False
                history[-1]["condition_sources_valid"] = condition_sources_valid
            if not source_valid:
                current.update(status="unknown", coverage_reason="原记忆补丁与已接受正文缺失、损坏或版本不一致，生命周期待核")
            elif not condition_sources_valid:
                current.update(status="unknown", authority="stale", source_status="stale",
                    coverage_reason="兑现条件引用的前章版本、哈希或原文已失效，不能沿用原完成结论")
            elif current["legacy_unverified"]:
                current["coverage_reason"] = "旧记录未提供完整规划条件与原文核验，历史状态不等于新标准验证通过"
            catalog[identity] = current
    return list(catalog.values())



def thread_attachment_index_valid(project, index: dict[str, Any]) -> bool:
    """只读核对后台回执的原任务、当前原文、规划及实际前史，供调度和复用共用。"""
    try:
        if not isinstance(index, dict) or index.get("status") != "verified":
            return False
        work = project.db.get_metadata("pending_work:" + index["work_id"], {})
        original = work.get("source", {})
        if (original.get("attachment_kind") != "promise_threads"
                or any(index.get(key) != original.get(key) for key in ("task_id", "snapshot_hash", "role", "chapter_no"))):
            return False
        number, basis = index["chapter_no"], index["basis"]
        row = project.db.get_chapter(number)
        body = project.db.canonical_chapter_content(number)
        visible = project.root / row["path"] if row else None
        if (not row or row["status"] != "accepted" or basis["version"] != row["version"]
                or basis["content_hash"] != row["content_hash"] or not body or content_hash(body) != row["content_hash"]
                or not visible.is_file() or content_hash(visible.read_text(encoding="utf-8")) != row["content_hash"]):
            return False
        requested = {item["thread_id"] for item in original["candidates"]}
        if not requested:
            return False
        patch = MemoryPatch(chapter_no=number, chapter_summary="派生线索附件", threads=index["threads"])
        if {item.thread_id for item in patch.threads} != requested:
            return False
        bundle = project.db.get_current_plan_bundle()
        plans = planning_promises(bundle)
        prior = thread_catalog(project, bundle, before_chapter=number)
        if (basis["plan_hash"] != content_hash(json_dumps([p for p in plans if p["thread_id"] in requested]))
                or basis["prior_threads_hash"] != content_hash(json_dumps([p for p in prior if p["thread_id"] in requested]))):
            return False
        sources, bindings = {}, {}
        for identity, expected in basis.get("sources", {}).items():
            n = int(identity)
            row = project.db.get_chapter(n)
            text = project.db.canonical_chapter_content(n)
            bound = expected["binding"]
            if (not 0 < n < number or not row or row["status"] != "accepted" or not text
                    or content_hash(text) != expected["hash"] or row["content_hash"] != expected["hash"]
                    or row["version"] != bound.get("version", bound.get("source_version"))):
                return False
            sources[n], bindings[n] = text, row
        return not thread_patch_gaps(patch, body, prior_threads=prior, plans=plans,
            sources=sources, source_bindings=bindings)
    except (KeyError, TypeError, ValueError, OSError):
        return False


class ThreadAttachmentOutput(StrictModel):
    """原记忆责任角色只补指定线索，不返回正文、事实或规划写入。"""
    threads: list[ThreadMutation] = Field(max_length=24)
    source_chapters: list[int] = Field(default_factory=list, max_length=6)
    remaining: list[str] = Field(default_factory=list, max_length=16)


async def complete_thread_attachment(engine, project, work_id: str, *, resume: bool = False) -> dict[str, Any]:
    """沿原任务补齐可选线索派生索引；原章和原记忆不改，未知返回及用户停止不重放。"""
    import asyncio
    from types import SimpleNamespace
    from .engine import _saved_comparison_repair
    from .errors import ValidationGateError
    from .model_usage import UsageLedger
    from .narrative_memory import event_history
    from .project_lock import project_write_lock
    from .runtime import active_generation_attempt_limit, active_generation_request_id, active_runtime
    from .provider import _DraftFieldRepair
    from .task_settings import active_task_settings
    from .utils import utc_now

    work = project.db.get_metadata("pending_work:" + work_id)
    source = work.get("source", {}) if isinstance(work, dict) else {}
    scope, runtime = active_task_settings.get(), active_runtime.get()
    if (source.get("attachment_kind") != "promise_threads" or not scope or not runtime
            or source.get("task_id") != scope.task_id or source.get("snapshot_hash") != scope.snapshot_hash
            or runtime.task_id != scope.task_id or source.get("role") not in {"editor", "memory_keeper"}):
        raise ValidationGateError("附件补全缺少原任务、配置或记忆责任人的绑定，未发送请求。")
    key, number, role = "promise_attachment:" + work_id, source["chapter_no"], source["role"]
    state = project.db.get_metadata(key, {})
    if state.get("status") == "cancelled" and not resume:
        return {"status": "cancelled", "summary": "作者已停止这项补全；原候选和次数保留。"}
    requested = {item["thread_id"] for item in source["candidates"]}
    if not requested:
        raise ValidationGateError("附件工作没有明确的待补线索，未发送请求。")
    request_id = "thread-attachment:" + work_id
    selected = {number - 1} | set(state.get("source_chapters", []))
    for item in source["candidates"]:
        selected.update(check["source_chapter"] for check in (item.get("candidate") or {}).get("condition_checks", []))
    system = ("你是本章原记忆责任角色，只补给定线索的派生记录，不写或修改正文、事实、规划和原记忆。"
        "对照当前已接受正文、有效规划及给出的前章原文；逐字证据与条件版本必须真实。"
        "只返回requested_thread_ids中的线索，缺少原文就在source_chapters申请具体前章；不编造证据或兑现。"
        "保留未完成状态，只有全部完成条件有原句证明才能paid。其他候选和旧报告不是正史。")

    def read_sources():
        """只读当前版本与本次需要的来源；后章新增不改变本章附件身份。"""
        chapter = project.db.get_chapter(number)
        body = project.db.canonical_chapter_content(number)
        if not chapter or chapter["status"] != "accepted" or not body or content_hash(body) != chapter["content_hash"]:
            raise ValidationGateError("本章尚未接受或原文版本待核，附件保留，待正式收稿后补齐。")
        visible = project.root / chapter["path"]
        if not visible.is_file() or content_hash(visible.read_text(encoding="utf-8")) != chapter["content_hash"]:
            raise ValidationGateError("本章有尚未同步的手改，后台未覆盖或沿用旧依据。")
        bundle = project.db.get_current_plan_bundle()
        plans = planning_promises(bundle)
        history = event_history(project.db.narrative_history_rows(number))
        texts = {n: history["sources"][n] for n in sorted(selected) if 0 < n < number and n in history["sources"]}
        bindings = {n: history["source_bindings"][n] for n in texts}
        prior = thread_catalog(project, bundle, before_chapter=number)
        basis = {"version": chapter["version"], "content_hash": chapter["content_hash"],
            "prior_threads_hash": content_hash(json_dumps([p for p in prior if p["thread_id"] in requested])),
            "plan_hash": content_hash(json_dumps([p for p in plans if p["thread_id"] in requested])),
            "sources": {n: {"hash": content_hash(texts[n]), "binding": bindings[n]} for n in texts}}
        return body, plans, prior, texts, bindings, json.loads(json_dumps(basis))

    async def save_status(status, *, locked: bool = False, **details):
        """状态与原work同身份保存，次数不因恢复或换版本清零。"""
        nonlocal state
        state = {**state, **details, "status": status, "updated_at": utc_now()}
        def persist():
            """复用外层写锁时不再次等待同一锁；原work和计数一起更新。"""
            project.db.set_metadata(key, state)
            project.update_pending_work(work_id, status=status, resolution=details.get("summary", ""))
        if locked:
            persist()
        else:
            async with project_write_lock(project.root):
                persist()

    def new_sources(output):
        """只接纳原角色新指出的有效前章，缺失原文仍由证据门禁检查。"""
        return {n for n in output.source_chapters if type(n) is int and 0 < n < number} - selected

    def attachment_gaps(output, body, plans, prior, texts, bindings):
        """核对派生记录本身；尚未兑现的条件留在候选说明中。"""
        patch = MemoryPatch(chapter_no=number, chapter_summary="派生线索附件", threads=output.threads)
        ids = [item.thread_id for item in output.threads]
        gaps = thread_patch_gaps(patch, body, prior_threads=prior, plans=plans, sources=texts, source_bindings=bindings)
        if set(ids) != requested or len(ids) != len(set(ids)):
            gaps.append("只补指定线索，不能漏项、重复或扩展其他线索。")
        if output.remaining and output.threads and all(item.status == "paid" for item in output.threads):
            gaps.append("角色仍报告未核条件，本次不能把全部线索标为兑现。")
        return gaps

    def compatible_prior(output, prior, previous_basis, basis):
        """只重绑定可重新验证的推进；其他结论必须逐条沿用请求时的前史。"""
        if previous_basis == basis:
            return True
        if not isinstance(previous_basis, dict) or previous_basis != {**basis, "prior_threads_hash": previous_basis.get("prior_threads_hash")}:
            return False
        try:
            original, _ = json.JSONDecoder().raw_decode(state.get("request_prompt") or "")
            earlier = {item["thread_id"]: item for item in original["prior_threads"]}
        except (KeyError, TypeError, ValueError):
            return False
        current = {item["thread_id"]: item for item in prior}
        for item in output.threads:
            bound = current.get(item.thread_id, {})
            if earlier.get(item.thread_id, {}) == bound:
                continue
            if (item.status != "advanced" or bound.get("authority") != "accepted"
                    or bound.get("status") not in {"open", "advanced"}):
                return False
        return True

    token = active_generation_request_id.set(request_id)
    try:
        for _ in range(10):
            body, plans, prior, texts, bindings, basis = await asyncio.to_thread(read_sources)
            saved_index = project.db.get_metadata("promise_thread_index:" + work_id, {})
            if saved_index.get("basis") == basis and thread_attachment_index_valid(project, saved_index):
                await save_status("completed", summary="已复用同版本完成回执，未重新请求。")
                return {"status": "completed", "chapter_no": number, "summary": f"第{number}章伏笔记录已补全。"}
            prompt = json_dumps({"current_body": body, "sources": texts, "source_bindings": bindings,
                "source_hashes": {str(n): content_hash(v) for n, v in texts.items()},
                "plans": [p for p in plans if p["thread_id"] in requested],
                "prior_threads": [p for p in prior if p["thread_id"] in requested],
                "requested_thread_ids": sorted(requested), "candidates": source["candidates"],
                "previous_candidate": state.get("candidate"), "remaining": state.get("gaps", [])})
            original_prompt = state.get("request_prompt") if state.get("request_pending") else prompt
            original_prompt = original_prompt or prompt
            contract = SimpleNamespace(system=system, prompt=original_prompt, output_model=ThreadAttachmentOutput)
            stored = None
            previous_basis = state.get("request_basis") or {}
            if not state.get("request_pending") and isinstance(state.get("candidate"), dict):
                # 旧版本可能已拿到合格的阶段性记录，只因未来条件仍开放而停住。复核同源回执即可。
                try:
                    candidate = ThreadAttachmentOutput.model_validate(state["candidate"])
                except ValueError:
                    candidate = None
                if (candidate is not None and compatible_prior(candidate, prior, previous_basis, basis)
                        and not new_sources(candidate) and not attachment_gaps(candidate, body, plans, prior, texts, bindings)):
                    stored = SimpleNamespace(data=candidate)
            saved = ((stored, None) if stored is not None else
                _saved_comparison_repair(project, scope, engine.settings, role, contract, prepare_missing_fields=True))
            fields = saved[0] if saved and isinstance(saved[0], _DraftFieldRepair) else None
            if saved and fields is None:
                result = saved[0]
            else:
                with UsageLedger()._connect() as db:
                    rows = db.execute("SELECT request_sent,outcome,error_type,diagnostic_json FROM model_attempts WHERE task_id=? AND request_id=? AND role=? ORDER BY rowid",
                        (scope.task_id, request_id, role)).fetchall()
                if any(row["request_sent"] is None or row["outcome"] == "pending" for row in rows):
                    raise ValidationGateError("附件请求账本仍有未知结果，未重放付费请求。")
                if state.get("request_pending") and fields is None:
                    closed = rows and rows[-1]["outcome"] == "failed" and rows[-1]["error_type"] in {"ValidationError", "JSONDecodeError"}
                    diagnostic = json.loads(rows[-1]["diagnostic_json"] or "{}") if rows else {}
                    stream = diagnostic.get("stream", {})
                    if not closed or stream.get("finish_reason") != "stop":
                        raise ValidationGateError("原附件返回尚不能证明完整闭合，已保留断点，未重放付费请求。")
                    # 已闭合但无法缩为字段的格式失败带明确反馈再交原角色，实际累计请求上限不重置。
                    prompt = original_prompt + "\n上次完整返回格式未通过，请按原Schema补齐；不要修改正文或扩大线索范围。"
                attempts = sum(row["request_sent"] == 1 for row in rows)
                if attempts >= 10:
                    raise ValidationGateError("同一附件已累计尝试10次，保留原候选及缺口。")
                await save_status("running", request_pending=True, input_hash=content_hash(prompt), attempts=attempts,
                    request_prompt=original_prompt if fields else prompt, request_basis=basis,
                    summary=f"正在后台补全第{number}章伏笔记录。")
                runtime.publish({"type": "memory.attachments", "summary": f"正在补全第{number}章伏笔记录，前台创作继续。"})
                limit = active_generation_attempt_limit.set(min(active_generation_attempt_limit.get() or 10, 10 - attempts))
                try:
                    result = await engine.provider.generate_json(system_prompt=fields.system if fields else system,
                        user_prompt=fields.prompt if fields else prompt,
                        output_model=fields.output_model if fields else ThreadAttachmentOutput,
                        agent_role=role, max_tokens=6000, thinking=False)
                    if fields:
                        from dataclasses import replace
                        result = replace(result, data=fields.merge(result.data, ThreadAttachmentOutput))
                finally:
                    active_generation_attempt_limit.reset(limit)
            output = ThreadAttachmentOutput.model_validate(result.data.model_dump(mode="json"))
            if (state.get("request_pending") and state.get("request_basis")
                    and state["request_basis"] != json.loads(json_dumps(basis))):
                # 先收回旧付费答复作参考，再交原角色核新版；字面校验不能替代变更后的语义判断。
                await save_status("running", request_pending=False, candidate=output.model_dump(mode="json"),
                    gaps=["原请求期间来源已变化，参考旧答复，只重新核对改变的来源。"])
                continue
            await save_status("running", request_pending=False)
            additions = new_sources(output)
            if additions:
                selected.update(additions)
                await save_status("running", source_chapters=sorted(selected), candidate=output.model_dump(mode="json"),
                    gaps=["已补读原角色申请的前章原文，继续原附件。"])
                continue
            gaps = attachment_gaps(output, body, plans, prior, texts, bindings)
            if gaps:
                # 同一实际缺项仍未补齐时，理由措辞变化不能重新触发付费请求。
                unchanged = state.get("gaps") == gaps
                await save_status("waiting_condition" if unchanged else "running", candidate=output.model_dump(mode="json"), gaps=gaps,
                    summary="附加记录仍有依据待补，原正史和后续创作保持；后台保留具体缺口。")
                if unchanged:
                    return {"status": "waiting_condition", "summary": "附件没有新依据，已保留断点，未重复相同请求。"}
                continue
            async with project_write_lock(project.root):
                # 写入前重新核原文、手改、规划及读过的前史，迟到结果不覆盖新版本。
                if (await asyncio.to_thread(read_sources))[-1] != basis:
                    await save_status("running", locked=True, request_pending=False, request_basis=basis,
                                      candidate=output.model_dump(mode="json"), gaps=["来源变化，重读对应来源后续接。"])
                    continue
                project.db.set_metadata("promise_thread_index:" + work_id, {"status": "verified", "work_id": work_id,
                    "chapter_no": number, "basis": basis, "task_id": scope.task_id, "snapshot_hash": scope.snapshot_hash,
                    "role": role, "original_artifact_id": source.get("artifact_id"), "threads": [t.model_dump(mode="json") for t in output.threads]})
                await save_status("completed", locked=True, request_pending=False, request_basis=basis,
                                  candidate=output.model_dump(mode="json"), gaps=[], summary="本章伏笔派生记录已补全，正文和原记忆保持。")
            return {"status": "completed", "chapter_no": number, "summary": f"第{number}章伏笔记录已补全。"}
        raise ValidationGateError("附件补全已达到本次处理上限，原进度和缺口保留。")
    except asyncio.CancelledError:
        await save_status("cancelled", summary="你已停止这项补全；原候选、返回和尝试次数保留。")
        raise
    except Exception as exc:
        await save_status("waiting_condition", summary=str(exc))
        raise
    finally:
        active_generation_request_id.reset(token)
