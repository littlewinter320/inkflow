"""Source-bound event indexes; narrative order never implies story-time order."""
from __future__ import annotations

from collections import deque
import json
from typing import Any

from .schemas import MemoryPatch, NarrativeEventRebuild
from .utils import content_hash


def event_time_evidence(patch: MemoryPatch) -> list:
    return [ref for event in patch.narrative_events for ref in event.time_evidence]


def scene_span(content: str, event: dict[str, Any]) -> tuple[int, int]:
    start_quote, end_quote = event["scene_start"], event["scene_end"]
    if content.count(start_quote) != 1 or content.count(end_quote) != 1:
        raise ValueError("场景首尾锚点必须在当前正文中唯一定位")
    start, end = content.index(start_quote), content.index(end_quote) + len(end_quote)
    if end <= start or event["evidence"] not in content[start:end]:
        raise ValueError("事件引文不在指定的完整场景范围内")
    if content[:start].rsplit("\n", 1)[-1].strip() or content[end:].split("\n", 1)[0].strip():
        raise ValueError("场景锚点须覆盖首尾完整段落，不能从句中截断")
    return start, end


def event_patch_gaps(patch: MemoryPatch, content: str, *,
                     prior_events: list[dict[str, Any]], sources: dict[int, str],
                     source_bindings: dict[int, dict[str, Any]] | None = None) -> list[str]:
    """Validate quotes, identities, source boundaries and strict-order cycles locally."""
    known = {(item["chapter_no"], item["event_id"]): item for item in prior_events
             if item["chapter_no"] < patch.chapter_no and item.get("source_status") != "provisional"}
    current = {(patch.chapter_no, item.event_id): item.model_dump(mode="json")
               for item in patch.narrative_events}
    known.update(current)
    errors = []
    edges: dict[tuple, set[tuple]] = {}
    same: dict[tuple, tuple] = {}

    def root(key: tuple) -> tuple:
        while key in same:
            key = same[key]
        return key

    for key, event in known.items():
        for link in event.get("relations", []):
            target = (link["target_chapter"], link["target_event_id"])
            if target in known and link["relation"] == "same_event":
                left, right = root(key), root(target)
                if left != right:
                    same[left] = right
    for key, event in known.items():
        for link in event.get("relations", []):
            target = (link["target_chapter"], link["target_event_id"])
            if target in known and link["relation"] in {"before", "after"}:
                left, right = (key, target) if link["relation"] == "before" else (target, key)
                edges.setdefault(root(left), set()).add(root(right))
    indegrees = {key: 0 for key in edges}
    for targets in edges.values():
        for target in targets:
            indegrees[target] = indegrees.get(target, 0) + 1
    ready = deque(key for key, degree in indegrees.items() if degree == 0)
    visited = 0
    while ready:
        key = ready.popleft()
        visited += 1
        for target in edges.get(key, ()):
            indegrees[target] -= 1
            if indegrees[target] == 0:
                ready.append(target)
    if visited != len(indegrees):
        errors.append("事件先后关系存在循环或同一事件被排在自身前后")
    for key, event in known.items():
        for link in event.get("relations", []):
            target = (link["target_chapter"], link["target_event_id"])
            if target not in known or link["relation"] != "overlaps":
                continue
            left, right = root(key), root(target)
            for start, end in ((left, right), (right, left)):
                seen, pending = set(), list(edges.get(start, ()))
                while pending:
                    node = pending.pop()
                    if node == end:
                        errors.append("事件重叠关系与严格先后关系矛盾")
                        break
                    if node not in seen:
                        seen.add(node)
                        pending.extend(edges.get(node, ()))
    for key, event in current.items():
        try:
            scene_span(content, event)
        except ValueError as exc:
            errors.append(f"事件 {key[1]}：{exc}")
        for ref in event["time_evidence"]:
            text = content if ref["source_chapter"] == patch.chapter_no else sources.get(ref["source_chapter"], "")
            if ref["source_chapter"] > patch.chapter_no or not text or ref["quote"] not in text:
                errors.append(f"事件 {key[1]} 的时间依据未在有效来源定位：第{ref['source_chapter']}章")
            if ref["source_chapter"] < patch.chapter_no:
                binding = (source_bindings or {}).get(ref["source_chapter"])
                if (not ref["source_version"] or not ref["source_hash"] or
                        (source_bindings is not None and (binding is None or
                         ref["source_version"] != binding["source_version"] or ref["source_hash"] != binding["source_hash"]))):
                    errors.append(f"事件 {key[1]} 的前章时间依据缺少有效版本与哈希绑定")
        for link in event["relations"]:
            target = (link["target_chapter"], link["target_event_id"])
            if target == key or target not in known or link["target_chapter"] > patch.chapter_no:
                errors.append(f"事件 {key[1]} 的关系目标不存在、越界或指向自身")
            elif link["target_chapter"] < patch.chapter_no and (
                    link["target_source_version"] != known[target].get("source_version")
                    or link["target_source_hash"] != known[target].get("source_hash")):
                errors.append(f"事件 {key[1]} 的前章关系目标版本或哈希已变化")
            elif link["target_chapter"] < patch.chapter_no and known[target]["evidence"] not in sources.get(target[0], ""):
                errors.append(f"事件 {key[1]} 的前章关系目标原文未读取")
            elif known[target]["narrative_mode"] != event["narrative_mode"] and link["relation"] == "same_event":
                # Flashbacks may retell an actual event; dreams and hypothetical branches may not.
                if {known[target]["narrative_mode"], event["narrative_mode"]} - {"current", "flashback"}:
                    errors.append(f"事件 {key[1]} 把不同事实层级合并成同一事件")
            if link["evidence"] not in content:
                errors.append(f"事件 {key[1]} 的关系依据未在当前正文定位")
    return list(dict.fromkeys(errors))


def _history_patch(row: dict[str, Any]) -> tuple[MemoryPatch, str]:
    reason = "尚无事件索引；需要时回读原文，不能声称时间线完整"
    base_patch = None
    if row.get("data_json") and row.get("committed_at") == row["updated_at"]:
        try:
            patch = MemoryPatch.model_validate_json(row["data_json"])
            if patch.chapter_no != int(row["chapter_no"]):
                raise ValueError("记忆补丁章节身份不一致")
            if patch.narrative_events:
                base_patch = patch
            else:
                reason = "本章未提供结构化事件；不表示没有事件"
        except ValueError as exc:
            reason = str(exc)
    elif row.get("data_json"):
        reason = "事件索引来源版本已变化，旧索引已隔离"
    if row.get("event_index_json"):
        try:
            cache = json.loads(row["event_index_json"])
            if (not isinstance(cache, dict) or cache.get("actor") not in {"editor", "reviewer", "memory_keeper"}
                    or not cache.get("task_id") or not cache.get("run_id")
                    or cache.get("authority") != "derived_event_index"):
                raise ValueError("派生事件索引缺少责任角色与原任务来源")
            proposal = NarrativeEventRebuild.model_validate({key: cache.get(key) for key in
                ("chapter_no", "source_version", "source_hash", "narrative_events")})
            if (proposal.chapter_no != int(row["chapter_no"]) or proposal.source_version != row["version"]
                    or proposal.source_hash != row["content_hash"]):
                raise ValueError("派生事件索引的正文版本已变化")
            return proposal.as_patch(), "rebuilt_event_index"
        except (ValueError, TypeError) as exc:
            reason += "；" + str(exc)
    if base_patch is not None:
        return base_patch, "accepted_memory"
    raise ValueError(reason)


def event_history(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Read accepted, same-version patches without backfilling or modifying a book."""
    events, gaps, sources, patches, bindings = [], [], {}, [], {}
    for row in rows:
        number = int(row["chapter_no"])
        text = row.get("content_text")
        if isinstance(text, str) and content_hash(text) == row["content_hash"]:
            sources[number] = text
            bindings[number] = {"source_version": int(row["version"]), "source_hash": row["content_hash"]}
        try:
            patch, origin = _history_patch(row)
            if number not in sources:
                raise ValueError("事件索引原文缺失或哈希不匹配")
            patches.append((row, patch, text, origin))
        except (ValueError, TypeError) as exc:
            gaps.append({"chapter_no": number, "reason": str(exc)})
    for row, patch, text, origin in patches:
        errors = event_patch_gaps(patch, text, prior_events=events, sources=sources, source_bindings=bindings)
        if errors:
            gaps.append({"chapter_no": patch.chapter_no, "reason": "；".join(errors)})
            continue
        for event in patch.narrative_events:
            item = event.model_dump(mode="json")
            start, end = scene_span(text, item)
            events.append({**item, "chapter_no": patch.chapter_no,
                "source_id": f"event:{patch.chapter_no:05d}:{event.event_id}",
                "source_version": int(row["version"]), "source_hash": row["content_hash"],
                "start": start, "end": end, "index_origin": origin, "source_status": "accepted"})
    return {"events": events, "gaps": gaps, "sources": sources, "source_bindings": bindings,
        "coverage": {"accepted_chapters": len(rows), "indexed_chapters": len({item["chapter_no"] for item in events}),
            "event_count": len(events), "complete": not gaps,
            "meaning": "完整仅指有效补丁有结构化事件，不保证提取无遗漏；章序与原文位置不是故事时间。"}}


NARRATIVE_COMPARISON_CONTRACT = """
跨场景对照：先沿事件索引、时间关系和已读原文核对人物状态与因果，再比较相似场景的叙事作用和表达。
continuity_sources列出全部已装入的相关原文，核对支持与反证时不可因为相似而省略；expression_sources只选跨章代表场景，供比较感官、动作顺序、视角、节奏及叙事作用，不据此复制或否定必要呼应。
事件摘要与时间推断只是有来源的派生索引，不能替代原文；章节先后、检索排名和写入时间不代表故事时间。
coverage列出未建索引、失效、预算遗漏及仅有索引而本对照未装入原文的事件。先检查本次其他资料是否已有同版原文；必要前提仍未读到时按原补源流程定位读取，不用几个命中或索引里的引文宣称核完完整时间线。
同一人物重复吃早饭不自动矛盾：核对是否次日、同一事件重述、再次进食或视角变化。硬矛盾须同对象、同事件时间、同事实层级且内容排他；相似度不能代判。
时间不明先查事件链与场景原文，可基于可核验前提推断相对顺序或范围，保留依据与其他解释；不得凭空新增一天或精确时刻来消除矛盾。
写作可在本次授权范围内自行改善普通重复、节奏和表达，保留事实、因果、人物声线、有意呼应和伏笔；不能为求不同改历史或新增剧情。只有影响重大因果且仍多解的问题才提示作者。
审查须同时核支持与反证；资料覆盖不全时仅报告已读范围，不把未检索到当作不存在。表达雷同通常为建议，不得冒充硬冲突；只审查时不落稿。
"""
