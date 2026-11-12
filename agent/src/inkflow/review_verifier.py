"""Reviewer evidence and semantic verification gates."""
from __future__ import annotations

import re
import json
from typing import Any

from .schemas import ContextPacket, EndingCheck, ReviewClaimDecision, ReviewFinding, ReviewReport
from .utils import content_hash

HARD_RULES = {
    "canon_conflict": {"world", "knowledge", "character"},
    "internal_chapter_conflict": {"world", "causality", "timeline", "knowledge"},
    "impossible_time": {"timeline"},
    "impossible_causality": {"causality"},
    "core_function_missing": {"planning"},
    "truncation": {"format"},
    "severe_repetition": {"originality", "format"},
}
_SAME_CHAPTER_CONFLICT_NOTE = "本章双处逐字引文待核"


def review_quote_choices(sources: dict[str, str]) -> dict[str, dict[str, str]]:
    """将已读原文列为可复制选项；只截连续片段，来源身份和语义判断仍由原责任角色核对。"""
    choices = {}
    for source_id, body in sources.items():
        for line in body.splitlines():
            line = line.strip()
            # 与已保存合同保持相同顺序及400字窗口，不能改变旧编号或截断后漏掉段尾。
            for start in range(0, len(line) if len(line) > 400 else 1, 200):
                quote = line[start:start + 400]
                if len(quote) >= 4:
                    choices[f"@quote:{len(choices)}"] = {"source_id": source_id, "quote": quote}
    return choices


def resolve_review_quote_refs(output: Any, quotes: dict[str, dict[str, str]], *, chapter_no: int | None = None) -> Any:
    """Resolve explicit choices from this call's source-bound excerpts, never fuzzy-match claims."""
    data = output.model_dump(mode="json")
    def resolve(value: str, source_id: str) -> str:
        entry = quotes.get(value)
        return entry["quote"] if entry and entry["source_id"] == source_id else value
    focus = data.get("focus_observation")
    if focus:
        for field in ("goal_evidence", "change_evidence"):
            focus[field] = resolve(focus[field], "当前正文")
    for item in [*data.get("source_comparisons", []), *data.get("assessments", [])]:
        # 引文选项属于别的来源时保留未解析编号供补核，不能悄悄改写本项核对对象。
        item["chapter_evidence"] = resolve(item["chapter_evidence"], "当前正文")
        item["source_evidence"] = resolve(item["source_evidence"], item["source_id"])
    for item in [*data.get("findings", []), *data.get("deviations", [])]:
        item["evidence"] = resolve(item["evidence"], "当前正文")
        entry = quotes.get(item.get("reference_evidence", ""))
        # 同章矛盾的第二处原句仍属本章，旧canon_refs不能把合法本章选句误当作外部来源。
        allowed = ["当前正文"] if item.get("rule_id") == "internal_chapter_conflict" else item.get("canon_refs") or ["当前正文"]
        if entry and entry["source_id"] in allowed:
            item["reference_evidence"] = entry["quote"]
    for check in data.get("context_use_audit", {}).get("setting_checks", []):
        check["setting_quote"] = resolve(check["setting_quote"], check["record_id"])
        check["chapter_quote"] = resolve(check["chapter_quote"], "当前正文")
    patch = data.get("memory_patch")
    boundary = chapter_no or (patch or {}).get("chapter_no")
    for proposal in [*data.get("setting_updates", []), *(patch or {}).get("setting_updates", [])]:
        for reference in proposal.get("evidence_refs", []):
            entry = quotes.get(reference.get("quote", ""))
            number = reference.get("chapter_no")
            if not entry or not isinstance(number, int) or not boundary or number > boundary:
                continue
            source = entry["source_id"]
            if ((number == boundary and source == "当前正文")
                    or source == f"chapter:{number:05d}"
                    or (source.startswith("batch:") and source.endswith(f":chapter:{number:05d}"))):
                reference["quote"] = entry["quote"]
    if patch:
        def resolve_reference(item: dict[str, Any]) -> None:
            entry = quotes.get(item.get("quote", ""))
            chapter = item.get("source_chapter")
            if not entry or not isinstance(chapter, int) or chapter > patch["chapter_no"]:
                return
            source = entry["source_id"]
            if ((chapter == patch["chapter_no"] and source == "当前正文")
                    or source == f"chapter:{chapter:05d}"
                    or (source.startswith("batch:") and source.endswith(f":chapter:{chapter:05d}"))):
                item["quote"] = entry["quote"]
        for item in patch.get("facts", []):
            item["evidence"] = resolve(item["evidence"], "当前正文")
            for reference in item.get("evidence_refs", []):
                resolve_reference(reference)
        for operation in patch.get("operations", []):
            for reference in operation.get("evidence", []):
                resolve_reference(reference)
        for thread in patch.get("threads", []):
            if thread.get("evidence"):
                thread["evidence"] = resolve(thread["evidence"], "当前正文")
            for check in thread.get("condition_checks", []):
                resolve_reference(check)
        for event in patch.get("narrative_events", []):
            event["evidence"] = resolve(event["evidence"], "当前正文")
            for reference in event.get("time_evidence", []):
                resolve_reference(reference)
            for relation in event.get("relations", []):
                relation["evidence"] = resolve(relation["evidence"], "当前正文")
    return type(output).model_validate(data)


def restore_review_quote_choices(registry: dict[str, dict[str, str]], sources: dict[str, str], *,
                                 known_quotes: tuple[str, ...] = ()) -> dict[str, dict[str, str]]:
    """Rebuild only hash-identical excerpts; caller must verify the saved task and source identity."""
    wanted = {(entry["source_id"], entry["quote_hash"]) for entry in registry.values()}
    found: dict[tuple[str, str], str] = {}
    for source_id, body in sources.items():
        if not any(source == source_id for source, _ in wanted):
            continue
        for line in body.splitlines():
            line = line.strip()
            for start in range(0, len(line) if len(line) > 400 else 1, 200):
                quote = line[start:start + 400]
                identity = (source_id, content_hash(quote))
                if identity in wanted:
                    found[identity] = quote
    # 来源经过合法排版投影后，旧短引文可能仍逐字存在却不再落在固定切片边界。
    # 只用已存候选的原句回填：来源、完整SHA及逐字包含三者须同时成立，不能模糊匹配或补造引文。
    by_hash = {content_hash(quote): quote for quote in known_quotes if isinstance(quote, str) and quote}
    for source_id, digest in wanted - found.keys():
        quote = by_hash.get(digest)
        if quote and quote in sources.get(source_id, ""):
            found[(source_id, digest)] = quote
    return {key: {"source_id": entry["source_id"], "quote": found[identity]}
            for key, entry in registry.items()
            if (identity := (entry["source_id"], entry["quote_hash"])) in found}


def repair_review_quote_representation(
    output: Any, content: str, packet: ContextPacket, registry: dict[str, dict[str, str]],
    *, repair_source_numbering: bool = True, repair_missing_ascii_quotes: bool = False,
) -> tuple[Any, list[dict[str, Any]]]:
    """只恢复可唯一定位的原句表示；来源、结论和语义仍由调用方门禁核验。"""
    data = output.model_dump(mode="json")
    original_hash = content_hash(json.dumps(data, ensure_ascii=False, sort_keys=True))
    audit: list[dict[str, Any]] = []
    sources = packet_sources(packet)
    def unique_keys(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("重复的JSON键不能作为逐字引文恢复")
        return result
    # A book-wide quote choice can name one specific requirement only when its
    # exact JSON-string hash and the loaded record revision prove that mapping.
    for section in packet.sections:
        if section.key != "CSET":
            continue
        try:
            payload = json.loads(section.content)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        all_records = [record for collection in payload.get("collections", [])
                       for record in collection.get("records", [])
                       if record.get("record_id") in section.source_ids]
        records = [record for collection in payload.get("collections", [])
                   if collection.get("collection_id") == "book-required-reading"
                   for record in collection.get("records", [])
                   if record.get("record_id") in section.source_ids]
        for index, check in enumerate(data.get("context_use_audit", {}).get("setting_checks", [])):
            matching = [record for record in all_records if record.get("record_id") == check["record_id"]]
            if len(matching) == 1:
                record = matching[0]
                value_source = json.dumps(record.get("values"), ensure_ascii=False)
                if (record.get("revision") == check["record_revision"] and record.get("status") in {"current", "verified_reference"}
                        and isinstance(record.get("values"), dict) and len(value_source) <= 400
                        and not evidence_matches(check["setting_quote"], value_source)):
                    try:
                        quoted = json.loads(check["setting_quote"], object_pairs_hook=unique_keys)
                    except (ValueError, TypeError):
                        quoted = None
                    # Reordered complete objects are identical data; subsets and altered values still need their owner.
                    if (isinstance(quoted, dict) and json.dumps(quoted, ensure_ascii=False, sort_keys=True)
                            == json.dumps(record["values"], ensure_ascii=False, sort_keys=True)):
                        audit.append({"field": f"context_use_audit.setting_checks[{index}].setting_quote",
                            "method": "exact_setting_json_order", "original_quote": check["setting_quote"],
                            "repaired_quote": value_source, "source_id": check["record_id"],
                            "record_revision": check["record_revision"], "source_hash": content_hash(value_source)})
                        check["setting_quote"] = value_source
            entry = registry.get(check["setting_quote"], {})
            matching = [record for record in records if record.get("record_id") == check["record_id"]]
            if entry.get("source_id") != "book:requirements" or len(matching) != 1:
                continue
            record = matching[0]
            values = record.get("values", {})
            rule = values.get("requirement")
            if (set(values) != {"requirement"} or not isinstance(rule, str) or not 1 <= len(rule) <= 400
                    or record.get("revision") != check["record_revision"]
                    or record.get("status") != "current"):
                continue
            encoded = json.dumps(rule, ensure_ascii=False)
            value_source = json.dumps(values, ensure_ascii=False)
            if (entry.get("quote_hash") != content_hash(encoded)
                    or sources.get("book:requirements", "").count(encoded) != 1
                    or not evidence_matches(rule, value_source)):
                continue
            audit.append({"field": f"context_use_audit.setting_checks[{index}].setting_quote",
                "method": "exact_book_rule_json_choice", "original_quote": check["setting_quote"],
                "repaired_quote": rule, "choice_source_id": "book:requirements",
                "source_id": check["record_id"], "record_revision": check["record_revision"],
                "choice_quote_hash": entry["quote_hash"], "source_hash": content_hash(value_source)})
            check["setting_quote"] = rule

    if repair_source_numbering:
        # 列表编号的“第七：/七、”可指向同一原句；只允许编号完全相同、正文后缀逐字相同的整行。
        # 不做近似匹配，也不删改事实文字。旧付费输入重建时显式关闭此新增表示恢复，保留原输入哈希。
        numbered = re.compile(r"(?:第([一二三四五六七八九十百零〇0-9]{1,4})(?:条|项|点)?|([一二三四五六七八九十百零〇0-9]{1,4}))\s*[、.．:：)）]\s*(\S[\s\S]*)")
        for index, item in enumerate(data.get("source_comparisons", [])):
            source_id = resolve_packet_source_id(item["source_id"], sources)
            source = sources.get(source_id, "")
            original = item["source_evidence"]
            quoted = numbered.fullmatch(original.strip())
            if (not source or evidence_matches(original, source) or not quoted
                    or len(re.sub(r"\s+", "", quoted[3])) < 16):
                continue
            number = quoted[1] or quoted[2]
            suffix = re.sub(r"\s+", "", quoted[3])
            if re.sub(r"\s+", "", source).count(suffix) != 1:
                continue
            matches = []
            for line in re.finditer(r"(?m)^[^\r\n]+", source):
                exact = line[0].strip()
                parsed = numbered.fullmatch(exact)
                if (parsed and (parsed[1] or parsed[2]) == number
                        and re.sub(r"\s+", "", parsed[3]) == suffix):
                    start = line.start() + len(line[0]) - len(line[0].lstrip())
                    matches.append((exact, start, start + len(exact)))
            if len(matches) != 1:
                continue
            repaired, start, end = matches[0]
            audit.append({"field": f"source_comparisons[{index}].source_evidence",
                "method": "unique_same_source_numbering", "original_quote": original,
                "repaired_quote": repaired, "source_id": source_id, "source_hash": content_hash(source),
                "source_spans": [[start, end]]})
            item["source_evidence"] = repaired

    # Only typography and whitespace may differ during lookup. Return the
    # original source spans, never normalized prose or a fuzzy approximation.
    quote_map = str.maketrans({"“": '"', "”": '"', "「": '"', "」": '"',
                               "『": '"', "』": '"', "‘": "'", "’": "'"})
    def normalize_quote(value: str) -> str:
        # Quote delimiters may change style; apostrophes inside English words may not.
        return re.sub(r"(?<![A-Za-z0-9])'|'(?![A-Za-z0-9])", '"', value.translate(quote_map))
    positions = [index for index, char in enumerate(content) if not char.isspace()]
    normalized_content = normalize_quote(content)
    normalized = "".join(normalized_content[index] for index in positions)
    choices = restore_review_quote_choices(registry, {"当前正文": content})
    targets = [(f"{kind}[{index}].evidence", finding, "evidence")
               for kind in ("findings", "deviations") for index, finding in enumerate(data.get(kind, []))]
    targets.extend((f"assessments[{index}].chapter_evidence", assessment, "chapter_evidence")
                   for index, assessment in enumerate(data.get("assessments", [])))
    contiguous, boundaries = set(), set()
    patch = data.get("memory_patch") or {}
    if patch.get("chapter_no") == packet.chapter_no:
        for kind in ("facts", "narrative_events", "threads"):
            for index, item in enumerate(patch.get(kind, [])):
                for key in ("evidence", "scene_start", "scene_end") if kind == "narrative_events" else ("evidence",):
                    if not isinstance(item.get(key), str) or not item[key]:
                        continue
                    field = f"memory_patch.{kind}[{index}].{key}"
                    targets.append((field, item, key))
                    contiguous.add(field)
                    if key != "evidence":
                        boundaries.add(field)
                references = (item.get("condition_checks", []) if kind == "threads" else
                    item.get("time_evidence", []) if kind == "narrative_events" else [])
                for number, reference in enumerate(references):
                    if reference.get("source_chapter") == packet.chapter_no:
                        targets.append((f"memory_patch.{kind}[{index}].references[{number}].quote", reference, "quote"))
                if kind == "narrative_events":
                    for number, relation in enumerate(item.get("relations", [])):
                        targets.append((f"memory_patch.{kind}[{index}].relations[{number}].evidence", relation, "evidence"))
    for field, target, key in targets:
        original = target[key]
        if field not in contiguous and evidence_matches(original, content):
            continue
        if field in contiguous and original in content:
            start = content.index(original)
            end = start + len(original)
            if field not in boundaries or (not content[:start].rsplit("\n", 1)[-1].strip()
                    and not content[end:].split("\n", 1)[0].strip()):
                continue
        if (repair_missing_ascii_quotes and field.startswith("assessments[")
                and field.endswith(".chapter_evidence") and len(original) >= 16 and '"' not in original):
            # 仅在调用方已证明同任务完整返回+已付费定向补丁之后启用。缺失引号不能全局
            # 视为匹配；只恢复唯一连续整行中成对ASCII双引号，除此之外每个字符均须相同。
            matches = []
            for line in re.finditer(r"(?m)^[^\r\n]+", content):
                exact = line[0].strip()
                count = exact.count('"')
                if (count < 2 or count % 2 or exact.replace('"', '') != original
                        or len(re.findall(r'"[^"\r\n]+"', exact)) * 2 != count):
                    continue
                start = line.start() + len(line[0]) - len(line[0].lstrip())
                matches.append((exact, start, start + len(exact)))
            if len(matches) == 1:
                exact, start, end = matches[0]
                audit.append({"field": field, "method": "unique_same_source_missing_ascii_quote_pairs",
                    "original_quote": original, "repaired_quote": exact, "source_id": "当前正文",
                    "source_hash": content_hash(content), "source_spans": [[start, end]]})
                target[key] = exact
                continue
        aliases = re.fullmatch(r"\s*@quote:\d+(?:\s*[；;|]\s*@quote:\d+)*\s*", original)
        if aliases:
            ids = re.findall(r"@quote:\d+", original)
            if any(identity not in choices for identity in ids):
                continue
            pieces = [choices[identity]["quote"] for identity in ids]
        else:
            pieces = [piece.strip() for piece in (re.split(r"…{2,}|\.{3,}|\|", original)
                      if field in contiguous else original.split("|"))]
        if not 1 <= len(pieces) <= 8:
            continue
        spans = []
        terminal_punctuation = False
        for piece in pieces:
            needle = "".join(normalize_quote(piece).split())
            start = normalized.find(needle)
            # A quoted clause may end with a model-added full stop; restore only its unique, source-exact comma.
            if (start < 0 and field not in contiguous and len(pieces) == 1
                    and len(needle) >= 16 and needle.endswith("。")):
                prefix = needle[:-1]
                position = normalized.find(prefix)
                if (position >= 0 and normalized.find(prefix, position + 1) < 0
                        and normalized[position + len(prefix):position + len(needle)] in {"，", ","}):
                    start = position
                    needle = normalized[start:start + len(needle)]
                    terminal_punctuation = True
            if (len(needle) < 8 or start < 0 or normalized.find(needle, start + 1) >= 0):
                break
            span = (positions[start], positions[start + len(needle) - 1] + 1)
            if spans and span[0] < spans[-1][1]:
                break
            spans.append(span)
        if len(spans) != len(pieces):
            continue
        if field in contiguous:
            start, end = spans[0][0], spans[-1][1]
            if field in boundaries:
                start = content.rfind("\n", 0, start) + 1
                line_end = content.find("\n", end)
                end = len(content) if line_end < 0 else line_end
            repaired = content[start:end]
        else:
            repaired = "……".join(content[start:end] for start, end in spans)
        if not evidence_matches(repaired, content):
            continue
        if (repaired == original or field in boundaries and len(repaired) > 200
                or field.startswith("memory_patch.narrative_events") and key == "evidence" and len(repaired) > 2000):
            continue
        audit.append({"field": field, "method": "unique_scene_boundary_paragraphs" if field in boundaries else
            "unique_same_source_terminal_punctuation" if terminal_punctuation else "unique_same_source_typography_spans",
            "original_quote": original, "repaired_quote": repaired, "source_id": "当前正文",
            "source_hash": content_hash(content), "source_spans": [[start, end]] if field in contiguous else [list(span) for span in spans],
            **({"selected_spans": [list(span) for span in spans]} if field in contiguous else {}),
            **({"quote_choices": {identity: dict(registry[identity]) for identity in ids}} if aliases else {})})
        target[key] = repaired
    for item in audit:
        item["original_report_hash"] = original_hash
        item["semantic_verification"] = "unchanged_required"
    if not audit:
        return output, []
    try:
        return type(output).model_validate(data), audit
    except ValueError:
        return output, []  # Source-exact expansion must still fit the original report contract.


AUTHOR_REQUIREMENT_SOURCE = "task:author-requirement"
ENDING_SOURCE_PREFIX = "ending:"


def ending_requirements(packet: ContextPacket) -> dict[str, dict]:
    """终章的主线约束供审核引用，不构成已发生事件的证据。"""
    from .story_promises import packet_ending_contract
    ending = packet_ending_contract(packet)
    return {ENDING_SOURCE_PREFIX + item["thread_id"]: item for item in ending["promises"]} if ending.get("remaining") == 1 else {}


def ending_check_matches(check: EndingCheck, requirement: dict, content: str, sources: dict[str, str]) -> bool:
    """检查终局回执引用本版正文。终幕另需引用实际停点附近的文字。"""
    if check.status != "resolved" or not check.evidence or any(
            (ref.source_id != "current" and not ref.source_id.startswith(("chapter:", "batch:")))
            or not evidence_matches(ref.quote, content if ref.source_id == "current" else sources.get(ref.source_id, ""))
            for ref in check.evidence):
        return False
    scope = requirement.get("current_evidence")
    if not scope:
        return True
    if scope == "last_scene":
        lines = [line for line in content.splitlines() if line.strip() and not re.fullmatch(
            r"\s*(?:#+.*|[-*—…]+|[（(]?\s*(?:全书|全文)?\s*完[。！）)]*)\s*", line)]
        content = "\n".join(lines[-3:])
    return any(ref.source_id == "current" and evidence_matches(ref.quote, content) for ref in check.evidence)


def ending_report_gaps(requirements, checks: list[EndingCheck], content: str, sources: dict[str, str]) -> list[str]:
    """完成终章必须逐项取得独立审读的正文依据，重复或缺失的检查不能复用。"""
    missing = []
    for requirement in requirements:
        matches = [check for check in checks if check.thread_id == requirement["thread_id"]]
        if len(matches) != 1 or not ending_check_matches(matches[0], requirement, content, sources):
            missing.append(requirement["title"])
    return missing


def packet_sources(packet: ContextPacket, *, review_requirements: bool = False) -> dict[str, str]:
    """解析可逐字核对的来源；当次作者要求只在审核任务目标时加入。"""
    sources: dict[str, str] = {}
    for section in packet.sections:
        if section.key == "P0":
            try:
                payload = json.loads(section.content)
            except ValueError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("plans"), list):
                for plan in payload["plans"]:
                    if isinstance(plan, dict) and plan.get("source_id") in section.source_ids:
                        sources[plan["source_id"]] = json.dumps(plan, ensure_ascii=False)
            continue
        if section.key == "F1":
            try:
                payload = json.loads(section.content)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            # Derived event labels and inferred times cannot prove literal chapter evidence.
            entries = payload.get("sources", [])
            if not isinstance(entries, list):
                continue
            for item in entries:
                if (not isinstance(item, dict) or not isinstance(item.get("body"), str)
                        or type(item.get("chapter_no")) is not int or item["chapter_no"] < 1):
                    continue
                if item.get("source_status") == "provisional":
                    if not isinstance(item.get("batch_id"), str) or not item["batch_id"]:
                        continue
                    chapter_identity = f"batch:{item['batch_id']}:chapter:{item['chapter_no']:05d}"
                elif item.get("source_status", "accepted") == "accepted":
                    chapter_identity = f"chapter:{item['chapter_no']:05d}"
                else:
                    continue
                if item.get("chapter_source_id", chapter_identity) != chapter_identity:
                    continue
                if not re.fullmatch(re.escape(chapter_identity) + r":scene:\d+:\d+", str(item.get("source_id", ""))):
                    continue
                for identity in (item.get("source_id"), chapter_identity):
                    if identity in section.source_ids:
                        sources[identity] = sources.get(identity, "") + "\n" + item["body"]
            continue
        if section.key == "CSET":
            try:
                payload = json.loads(section.content)
            except ValueError:
                continue
            for collection in payload.get("collections", []):
                for record in collection.get("records", []):
                    if record["record_id"] in section.source_ids:
                        sources[record["record_id"]] = json.dumps(record["values"], ensure_ascii=False)
            continue
        if section.key in {"B2", "E0"}:
            try:
                payload = json.loads(section.content)
            except ValueError:
                continue
            indexed = {item["chapter_no"]: item for item in payload
                       if isinstance(item, dict) and isinstance(item.get("chapter_no"), int)} if isinstance(payload, list) else {}
            for ref in section.source_ids:
                match = re.fullmatch(r"batch-memory:.+:chapter:(\d+)", ref)
                if match and int(match.group(1)) in indexed:
                    sources[ref] = json.dumps(indexed[int(match.group(1))], ensure_ascii=False, indent=2)
            continue
        if section.title in {"不可违反的硬正史", "当前人物状态与知识边界", "相关历史索引"}:
            try:
                payload = json.loads(section.content)
            except ValueError:
                continue
            indexed = {str(item["事实编号"]): item for item in payload
                       if isinstance(item, dict) and item.get("事实编号")} if isinstance(payload, list) else {}
            for ref in section.source_ids:
                if ref in indexed:
                    sources[ref] = sources.get(ref, "") + "\n" + json.dumps(indexed[ref], ensure_ascii=False, indent=2)
            continue
        if section.key == "E1":
            # Derived ending summaries cannot supply a chapter's literal prose evidence.
            continue
        if section.key == "F0" or (section.key == "F" and section.title == "分层混合检索结果"):
            try:
                payload = json.loads(section.content)
            except ValueError:
                # A compressed or damaged index is not evidence for every ID it once listed.
                continue
            entries = payload.get("自适应结果", []) if isinstance(payload, dict) else payload
            indexed = {str(item["source_id"]): item for item in entries if isinstance(item, dict)
                       and item.get("source_id") and isinstance(item.get("body"), str)}
            for ref in section.source_ids:
                if ref in indexed:
                    sources[ref] = sources.get(ref, "") + "\n" + str(indexed[ref]["body"])
            continue
        if section.key.startswith("reference-recovery-"):
            payload = json.loads(section.content)
            for ref in section.source_ids:
                if ref == payload.get("source_id"):
                    sources[ref] = sources.get(ref, "") + "\n" + str(payload["body"])
            continue
        chapter_chunks: dict[int, str] = {}
        matches = list(re.finditer(r"(?m)^### .*?第\s*(\d+)\s*章[^\n]*\n", section.content))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(section.content)
            chapter_chunks[int(match.group(1))] = section.content[match.start():end]
        for ref in section.source_ids:
            chapter_match = re.search(r"chapter:(\d+)$", ref)
            scoped = chapter_chunks.get(int(chapter_match.group(1))) if chapter_match else None
            sources[ref] = sources.get(ref, "") + "\n" + (scoped or section.content)
    # 保留此编号给调用方绑定的请求，不能从书稿或引用目录取得同名授权。
    sources = {key: value for key, value in sources.items()
               if key != AUTHOR_REQUIREMENT_SOURCE and not key.startswith(ENDING_SOURCE_PREFIX)}
    if review_requirements and packet.review_instruction.strip():
        sources[AUTHOR_REQUIREMENT_SOURCE] = packet.review_instruction
    if review_requirements:
        sources.update({key: json.dumps(item, ensure_ascii=False) for key, item in ending_requirements(packet).items()})
    return sources


def setting_review_gaps(output: Any, content: str, packet: ContextPacket) -> list[str]:
    """Require a source-bound readback, never infer that a tool call read a setting."""
    section = next((item for item in packet.sections if item.key == "CSET"), None)
    if section is None:
        return []
    try:
        payload = json.loads(section.content)
    except ValueError:
        return ["设定资料格式无法读取，尚不能确认核对范围"]
    records = {record["record_id"]: record for collection in payload.get("collections", [])
               for record in collection.get("records", [])}
    checks = getattr(output.context_use_audit, "setting_checks", [])
    seen, gaps = set(), []
    for check in checks:
        record = records.get(check.record_id)
        if record is None or check.record_id in seen:
            gaps.append("设定核对包含未装入或重复的条目；空模板不能记作已审核")
            continue
        seen.add(check.record_id)
        if check.record_revision != record.get("revision"):
            gaps.append(f"设定《{record['title']}》核对的修订号与本次读取不一致")
        values = json.dumps(record["values"], ensure_ascii=False)
        if not check.setting_quote.strip() or not evidence_matches(check.setting_quote, values):
            gaps.append(f"设定《{record['title']}》的核对原句未定位")
        if check.disposition in {"compatible", "conflict"} and (
                not check.chapter_quote.strip() or not evidence_matches(check.chapter_quote, content)):
            gaps.append(f"设定《{record['title']}》缺少本章对照原句")
        if check.disposition in {"conflict", "needs_evidence"}:
            gaps.append(f"设定《{record['title']}》仍待核对：{check.reason}")
        if record.get("status") in {"stale_evidence", "configuration_changed"} and check.disposition == "compatible":
            gaps.append(f"设定《{record['title']}》来源已变化，不能沿用旧的通过结论")
        if record.get("classification_issues") and check.disposition == "compatible":
            gaps.append(f"设定《{record['title']}》存在归类问题：" + "；".join(record["classification_issues"]))
    gaps.extend(f"尚未核对设定《{record['title']}》的本章适用性" for key, record in records.items() if key not in seen)
    for collection in payload.get("coverage", []):
        gaps.extend(f"用户约定《{item['title']}》未装入：{item['reason']}" for item in collection.get("omitted", []) if item.get("required"))
    return gaps


def mode_output_requirements(packet: ContextPacket) -> dict:
    """Cover the references actually supplied, including the same temporary goal as Writer."""
    sources = packet_sources(packet)
    section = next((item for item in packet.sections if item.key == "CSET"), None)
    payload = json.loads(section.content) if section else {}
    planning = [key for key in ("OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md") if key in sources]
    planning += [key for key in sources if key.startswith("task-goal:")]
    if not planning:
        planning = [key for key in sources if key.startswith("plan:chapter:")]
    return {"source_comparisons": (["book:requirements"] if "book:requirements" in sources else []) + planning,
        "setting_checks": [{"record_id": record["record_id"], "record_revision": record["revision"], "title": record["title"]}
            for collection in payload.get("collections", []) for record in collection.get("records", [])],
        "instruction": "每份原文分别对照；设定条目逐项核对，不适用时明确说明，不省略。编号和修订号照抄，结论和引文由实际阅读判断，不编造已读或通过。"}


def mode_output_model(packet: ContextPacket, *, primary: bool, criteria: tuple[str, ...] = ()) -> type:
    """Require structural coverage in the existing Provider format retry, before a second review."""
    from pydantic import Field, create_model, model_validator
    from .schemas import ContextUseAudit, EndingCheck, ModeCheckOutput, ReviewAssessment, ReviewSourceComparison, SettingUseCheck
    if not primary and not criteria:
        return ModeCheckOutput
    sources = packet_sources(packet)
    requirements = mode_output_requirements(packet)
    required = set(requirements["source_comparisons"])
    ending_ids = {item["thread_id"] for item in ending_requirements(packet).values()} if primary else set()
    records = {item["record_id"]: item["record_revision"] for item in requirements["setting_checks"]}
    def coverage(output):
        if criteria:
            actual = [item.criterion for item in output.assessments]
            if set(actual) != set(criteria) or len(actual) != len(criteria):
                raise ValueError("本次分派的assessments须每项恰好一次：" + "、".join(criteria))
        if not primary:
            return output
        if ending_ids:
            checked = [item.thread_id for item in output.ending_checks]
            if set(checked) != ending_ids or len(checked) != len(ending_ids):
                raise ValueError("终章 ending_checks 必须逐条覆盖本次收尾主线，每条恰好一次：" + "、".join(sorted(ending_ids)))
        seen = [check.record_id for check in output.context_use_audit.setting_checks]
        missing = required - {resolve_packet_source_id(item.source_id, sources) for item in output.source_comparisons}
        if missing or set(records) - set(seen) or len(seen) != len(set(seen)):
            raise ValueError("本次结构覆盖不完整：source_comparisons须分别包含 " + "、".join(sorted(required))
                + "；setting_checks须逐条包含且不重复 " + "、".join(records))
        if any(records.get(check.record_id) != check.record_revision for check in output.context_use_audit.setting_checks):
            raise ValueError("设定核对编号与修订号须复制本次实际读取条目，不能串用或补造。")
        return output
    fields = {"assessments": (list[ReviewAssessment], Field(min_length=1, max_length=6,
        description="只填写本次任务实际分派的检查项，各恰好一次"))} if criteria else {}
    if ending_ids:
        fields["ending_checks"] = (list[EndingCheck], Field(min_length=len(ending_ids), max_length=len(ending_ids)))
    if primary and packet.cache_layout_version >= 26:
        from typing import Annotated, Literal
        from .schemas import ReviewFocusObservation
        # 与目标/变化门禁同一长度要求；空值仍可表示未找到证据，不能用短句伪造足够支持。
        quote = Literal[""] | Annotated[str, Field(min_length=8, max_length=400)]
        focus_model = create_model("ReviewFocusObservation", __base__=ReviewFocusObservation,
            goal_evidence=(quote, Field(description="支持实际目标的本章连续原句，至少8字；找不到留空并保留未知")),
            change_evidence=(quote, Field(description="支持实际变化的另一处本章连续原句，至少8字且不与目标引文重叠")))
        fields["focus_observation"] = (focus_model, Field())
    if primary and required:
        fields["source_comparisons"] = (list[ReviewSourceComparison], Field(min_length=1, max_length=8,
            description="逐份核对本次实际读取的原文，来源编号照抄任务资料"))
    if primary and records:
        audit_model = create_model("ContextUseAudit", __base__=ContextUseAudit,
            setting_checks=(list[SettingUseCheck], Field(min_length=1,
                description="逐项核对本次实际读取的设定，编号与修订号照抄任务资料")))
        fields["context_use_audit"] = (audit_model, Field(description="本次已读资料及逐项设定核对"))
    model = create_model("ModeCheckOutput", __base__=ModeCheckOutput, **fields,
        __validators__={"current_coverage": model_validator(mode="after")(coverage)})
    model.__doc__ = ModeCheckOutput.__doc__
    return model


def evidence_matches(evidence: str, content: str) -> bool:
    """Match exact excerpts and conservative ellipsis-compressed excerpts."""

    if evidence in content:
        return True
    # Paragraph layout is not part of a factual claim; preserve every other character.
    if evidence.strip() and re.sub(r"\s+", "", evidence) in re.sub(r"\s+", "", content):
        return True
    if len(evidence) > 2 and (evidence[0], evidence[-1]) in {('“', '”'), ('「', '」'), ('"', '"')}:
        return evidence_matches(evidence[1:-1], content)
    parts = [part.strip() for part in re.split(r"(?:…{2,}|\.{3,})", evidence) if part.strip()]
    if len(parts) < 2:
        return False
    # A model can collapse two Markdown paragraphs into one quoted excerpt.
    # For an ellipsis quote only, ignore paragraph whitespace while preserving
    # the order of every quoted character.
    compact_content = re.sub(r"\s+", "", content)
    compact_parts = [re.sub(r"\s+", "", part) for part in parts]
    cursor = 0
    for part in compact_parts:
        position = compact_content.find(part, cursor)
        if position < 0:
            return False
        cursor = position + len(part)
    return True


def resolve_packet_source_id(raw: str, sources: dict[str, str]) -> str | None:
    """Accept an unambiguous display suffix, never a different source."""

    if raw in sources:
        return raw
    if re.fullmatch(r"chapter:\d{5}", raw):
        provisional = [source_id for source_id in sources if source_id.startswith("batch:") and source_id.endswith(":" + raw)]
        if len(provisional) == 1:
            return provisional[0]
    for source_id in sorted(sources, key=len, reverse=True):
        if raw.startswith(source_id) and raw[len(source_id):len(source_id) + 1] in {" ", "#", "·", "（"}:
            return source_id
    return None


def _same_chapter_conflict_excerpt(finding: ReviewFinding, content: str) -> str:
    """Find a second exact in-chapter quote for an alleged hard state conflict."""
    if (
        finding.severity not in {"major", "blocking"}
        or finding.category not in {"world", "causality", "timeline", "knowledge"}
        or finding.category not in HARD_RULES.get(finding.rule_id, set())
        or not finding.evidence.strip()
        or finding.evidence not in content
    ):
        return ""
    if finding.rule_id == "internal_chapter_conflict":
        second = finding.reference_evidence.strip()
        first_at = content.find(finding.evidence)
        second_at = content.find(second) if len(second) >= 8 else -1
        distinct_spans = (
            second_at >= first_at + len(finding.evidence)
            or first_at >= second_at + len(second)
        ) if second_at >= 0 else False
        return second if distinct_spans else ""
    if not re.search(r"无法同时|自相矛盾|前后矛盾|互斥|位置矛盾|状态矛盾|时间矛盾|认知倒退|因果断裂", finding.explanation):
        return ""
    for match in re.finditer(r"[“「『]([^”」』\n]{8,160})[”」』]", finding.explanation):
        excerpt = match.group(1)
        if excerpt not in finding.evidence and excerpt in content:
            return excerpt
    return ""


def verify_review(report: ReviewReport, content: str, packet: ContextPacket) -> tuple[list[ReviewFinding], str]:
    """Anchor every finding to exact source text before it can affect prose."""
    sources = packet_sources(packet, review_requirements=True)
    findings: list[ReviewFinding] = []
    seen: set[tuple[str, str, str]] = set()
    # A harmless editorial note cannot erase an explicitly unresolved review.
    # Unsupported claims still cannot authorize a prose edit; the existing
    # bounded full-evidence recheck can resolve uncertainty with a new report.
    # A model's failed patch/replan claim is not evidence that the chapter is
    # sound.  Recheck the claim instead of silently converting it to pass.
    disputed = report.verdict == "unknown" or (report.verdict in {"patch", "replan"} and not report.findings)
    for finding in report.findings:
        key = (finding.category, finding.evidence, finding.explanation)
        if key in seen:
            continue
        seen.add(key)
        reasons: list[str] = []
        rebound_note = ""
        goal_missing = finding.rule_id == "core_function_missing"
        def allowed_source(ref: str) -> bool:
            """把作者要求与终局计划限定在结果缺失核对，防止作为正史引用。"""
            return goal_missing or (ref != AUTHOR_REQUIREMENT_SOURCE and not ref.startswith(ENDING_SOURCE_PREFIX))
        canon_refs = [] if finding.rule_id == "internal_chapter_conflict" else list(finding.canon_refs)
        if finding.reference_evidence.strip():
            matching_refs = [
                ref for ref, source_text in sources.items()
                if allowed_source(ref)
                and evidence_matches(finding.reference_evidence, source_text)
            ]
            if matching_refs and not any(ref in matching_refs for ref in canon_refs):
                canon_refs = matching_refs[:4]
                rebound_note = "引用编号已按逐字证据自动重绑"
        if not finding.evidence.strip() or not evidence_matches(finding.evidence, content):
            reasons.append("引用无法逐字定位到当前正文")
        if any(ref not in sources or not allowed_source(ref) for ref in canon_refs):
            reasons.append("引用来源不在本次上下文内")
        hard = finding.severity in {"major", "blocking"}
        editorial = finding.category in {"style", "pacing"}
        # 跨章表达意见也须有真实对照，不能只定位本章一句就把错误的前章指控交给Writer。
        if editorial and canon_refs and (not finding.reference_evidence.strip() or not any(
                evidence_matches(finding.reference_evidence, sources.get(ref, "")) for ref in canon_refs)):
            reasons.append("编辑意见的对照原文无法定位到来源")
        same_chapter_excerpt = _same_chapter_conflict_excerpt(finding, content)
        if hard and not editorial:
            if finding.category not in HARD_RULES.get(finding.rule_id, set()):
                reasons.append("缺少适用的硬门禁规则编号")
            if finding.rule_id == "internal_chapter_conflict" and not same_chapter_excerpt:
                reasons.append("同章第二处引文无法逐字定位到当前正文，或与第一处重叠")
            if finding.rule_id in {"canon_conflict", "core_function_missing"}:
                if not canon_refs or not finding.reference_evidence.strip():
                    reasons.append("缺少正史或章节卡对照原文")
                elif not any(
                    evidence_matches(finding.reference_evidence, sources.get(ref, ""))
                    for ref in canon_refs
                ):
                    reasons.append("对照原文无法定位到来源")
                # Future design alone cannot prove a past fact or an author's mandatory result.
                matched_refs = [ref for ref in canon_refs if evidence_matches(finding.reference_evidence, sources.get(ref, ""))]
                if matched_refs and all(ref in {"OUTLINE.md", "STORY_DETAIL.md", "RECENT_PLAN.md"}
                                        or ref.startswith(("plan:", "setting-record-", "promise-plan:"))
                                        or (finding.rule_id == "canon_conflict" and ref.startswith("task-goal:")) for ref in matched_refs):
                    reasons.append("这里只引用了设计参考；须回到已接受正文或作者明确要求，不能仅因偏离规划判硬错")
        # A valid first draft quote plus a second exact in-chapter quote is a
        # live state-conflict question even when an unrelated canon citation
        # fails. It still lacks a verified hard finding, so keep it unknown.
        same_chapter_uncertain = bool(
            same_chapter_excerpt
            and "引用无法逐字定位到当前正文" not in reasons
            and (reasons or finding.rule_id == "internal_chapter_conflict")
        )
        disputed = disputed or same_chapter_uncertain or (hard and not editorial and bool(reasons))
        # Unsupported hard claims are demoted to visible informational notes
        # below. They must never edit prose or block a batch by themselves;
        # only a hard finding anchored to the current text can produce a patch
        # verdict. An explicitly unknown report with no findings remains
        # unknown through the initialization above.
        findings.append(finding.model_copy(update={
            "canon_refs": canon_refs,
            "severity": "info" if reasons or same_chapter_uncertain else "minor" if hard and editorial else finding.severity,
            "verification_status": "uncertain" if same_chapter_uncertain else "unsupported" if reasons else "anchored",
            "semantic_status": "uncertain" if same_chapter_uncertain else "unchecked",
            "verification_note": (
                "；".join([*reasons, f"{_SAME_CHAPTER_CONFLICT_NOTE}：{same_chapter_excerpt}"] if same_chapter_uncertain else reasons)
                if reasons
                else "；".join(filter(None, [rebound_note, "正文和来源引用已定位；语义结论等待核验", _SAME_CHAPTER_CONFLICT_NOTE if same_chapter_excerpt else ""]))
            ),
            "verification_confidence": 0.0 if reasons else 1.0,
            "proposed_severity": finding.proposed_severity or finding.severity,
            "repair_instruction": (
                "需核实本章两处引文之间是否有物件转移或因果动作；未核实前勿自动修改正文。"
                if same_chapter_uncertain else "待核实，勿据此自动修改正文。" if reasons else finding.repair_instruction
            ),
        }))
    return findings, _verdict(findings, disputed)


def findings_for_semantic_check(findings: list[ReviewFinding]) -> list[dict[str, Any]]:
    return [{
        "finding_index": index,
        "rule_id": item.rule_id,
        "category": item.category,
        "claim": item.claim or item.explanation,
        "draft_evidence": item.evidence,
        "reference_evidence": item.reference_evidence,
    } for index, item in enumerate(findings)
        if item.verification_status == "anchored"
        and item.severity in {"major", "blocking"}
        and item.semantic_status == "unchecked"
        # A cross-chapter repetition finding already carries two exact excerpts:
        # one from the current draft and one from a scoped source chapter.  A
        # second model pass used to erase this concrete repair request.  Keep
        # the evidence-backed Editor decision and let Writer repair it.
        and not (item.rule_id == "severe_repetition" and item.reference_evidence.strip())]


def same_chapter_disputes(findings: list[ReviewFinding], content: str) -> list[dict[str, Any]]:
    """Only concrete two-quote state conflicts warrant a focused follow-up."""

    targets: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        if finding.verification_status != "uncertain" or _SAME_CHAPTER_CONFLICT_NOTE not in finding.verification_note:
            continue
        proposed = finding.proposed_severity or finding.severity
        second = _same_chapter_conflict_excerpt(finding.model_copy(update={"severity": proposed}), content)
        if not second:
            continue
        targets.append({
            "finding_index": index,
            "category": finding.category,
            "claim": finding.claim or finding.explanation,
            "first_evidence": finding.evidence,
            "second_evidence": second,
        })
    return targets


def legacy_accepted_conflict(
    report: ReviewReport, content: str, *, require_source_hash: bool = True,
) -> dict[str, str] | None:
    """Flag a previously accepted two-quote conflict, without rewriting canon."""

    if report.verdict != "pass" or (require_source_hash and report.source_hash != content_hash(content)):
        return None
    for finding in report.findings:
        if (
            finding.severity != "info"
            or finding.proposed_severity not in {"major", "blocking"}
            or finding.verification_status not in {"unsupported", "uncertain"}
        ):
            continue
        proposed = finding.model_copy(update={"severity": finding.proposed_severity})
        second = _same_chapter_conflict_excerpt(proposed, content)
        if second:
            return {
                "reason": "旧版审核把本章两处可定位的剧情硬疑点降为通过；需先复核并修正这一章。",
                "detail": finding.explanation,
                "first_evidence": finding.evidence,
                "second_evidence": second,
            }
    return None


def checked_claim_decisions(decisions: list[ReviewClaimDecision]) -> list[ReviewClaimDecision]:
    """Classification/verdict disagreement is a review error, never a prose edit."""
    counts: dict[int, int] = {}
    for item in decisions:
        counts[item.finding_index] = counts.get(item.finding_index, 0) + 1
    result = []
    for item in decisions:
        invalid = (counts[item.finding_index] != 1
            or (item.verdict == "supported" and item.conflict_type not in {"direct", "exclusive_conflict"})
            or (item.verdict in {"not_blocking", "contradicted"}
                and item.conflict_type in {"unchecked", "exclusive_conflict", "insufficient", "irrelevant"}))
        if invalid:
            item = item.model_copy(update={"verdict": "uncertain",
                "reason": item.reason + "；冲突分类未完成、与结论矛盾或重复返回同一编号，保留待核。"})
        result.append(item)
    return result


def apply_same_chapter_decisions(
    findings: list[ReviewFinding],
    decisions: list[ReviewClaimDecision],
    content: str,
) -> tuple[list[ReviewFinding], str]:
    """Resolve a two-quote dispute without trusting an unanchored model acquittal."""

    targets = {item["finding_index"]: item for item in same_chapter_disputes(findings, content)}
    by_index = {item.finding_index: item for item in checked_claim_decisions(decisions)}
    result: list[ReviewFinding] = []
    disputed = False
    for index, finding in enumerate(findings):
        target = targets.get(index)
        if target is None:
            result.append(finding)
            disputed = disputed or finding.verification_status == "uncertain"
            continue
        decision = by_index.get(index)
        if decision and decision.verdict == "supported" and decision.conflict_type == "exclusive_conflict":
            result.append(finding.model_copy(update={
                "severity": finding.proposed_severity or "major",
                "rule_id": "internal_chapter_conflict",
                "canon_refs": [],
                "reference_evidence": target["second_evidence"],
                "verification_status": "anchored",
                "semantic_status": "supported",
                "conflict_type": decision.conflict_type,
                "verification_confidence": decision.confidence,
                "verification_note": f"同章两处原文已定位；局部复核：{decision.reason}",
                "repair_instruction": "请补足或改正两处引文之间的时间、状态、认知或因果交代，再重新审查。",
            }))
            continue
        resolution = decision.resolution_evidence.strip() if decision else ""
        first_at = content.find(target["first_evidence"])
        second_at = content.find(target["second_evidence"])
        earlier_at, later_at = sorted((first_at, second_at))
        later_quote = target["second_evidence"] if second_at == later_at else target["first_evidence"]
        paragraph_start = content.rfind("\n\n", 0, earlier_at)
        paragraph_end = content.find("\n\n", later_at + len(later_quote))
        nearby_text = content[paragraph_start + 2 if paragraph_start >= 0 else 0:
                              paragraph_end if paragraph_end >= 0 else len(content)]
        if (
            decision and decision.verdict in {"not_blocking", "contradicted"}
            and 8 <= len(resolution) <= 200 and resolution in nearby_text
        ):
            result.append(finding.model_copy(update={
                "severity": "info",
                "rule_id": "internal_chapter_conflict",
                "canon_refs": [],
                "reference_evidence": target["second_evidence"],
                "verification_status": "anchored",
                "semantic_status": "not_blocking",
                "conflict_type": decision.conflict_type,
                "verification_confidence": decision.confidence,
                "verification_note": f"同章消解原文已定位：{resolution}；局部复核：{decision.reason}",
                "repair_instruction": "已有正文交代，不需因这一项修改。",
            }))
            continue
        disputed = True
        result.append(finding.model_copy(update={
            "semantic_status": "uncertain",
            "conflict_type": decision.conflict_type if decision else "insufficient",
            "verification_note": (
                f"{_SAME_CHAPTER_CONFLICT_NOTE}；"
                + (f"局部复核：{decision.reason}" if decision else "局部复核没有返回该问题")
                + ("；未给出两处引文附近可逐字定位的消解依据" if decision and decision.verdict in {"not_blocking", "contradicted"} else "")
            ),
            "verification_confidence": decision.confidence if decision else 0.0,
        }))
    return result, _verdict(result, disputed)


def carry_unresolved_same_chapter_recheck(
    previous: ReviewReport,
    findings: list[ReviewFinding],
    content: str,
) -> tuple[list[ReviewFinding], str]:
    """A second Editor response cannot clear a concrete conflict by omission."""

    result = list(findings)
    carried = False
    for target in same_chapter_disputes(previous.findings, content):
        old = previous.findings[target["finding_index"]]
        matching = [item for item in result if item.category == old.category and item.evidence == old.evidence]
        if any(
            item.verification_status == "uncertain"
            or item.severity in {"major", "blocking"}
            or (item.semantic_status == "not_blocking" and any(
                marker in item.verification_note for marker in ("同章消解原文已定位", "同章过渡原文已定位")
            ))
            for item in matching
        ):
            continue
        result.append(old.model_copy(update={
            "verification_note": old.verification_note + "；重新审读未给出可逐字定位的消解依据",
            "semantic_status": "uncertain",
        }))
        carried = True
    return result, _verdict(result, carried or any(item.verification_status == "uncertain" for item in result))


def apply_semantic_decisions(findings: list[ReviewFinding], decisions: list[ReviewClaimDecision], *, source: str) -> tuple[list[ReviewFinding], str]:
    by_index = {item.finding_index: item for item in checked_claim_decisions(decisions)}
    targets = {item["finding_index"] for item in findings_for_semantic_check(findings)}
    result: list[ReviewFinding] = []
    disputed = False
    for index, finding in enumerate(findings):
        if index not in targets:
            result.append(finding)
            disputed = disputed or finding.verification_status == "uncertain"
            continue
        decision = by_index.get(index)
        if decision is None or decision.verdict != "supported":
            status = decision.verdict if decision else "uncertain"
            unresolved = status == "uncertain" or _SAME_CHAPTER_CONFLICT_NOTE in finding.verification_note
            disputed = disputed or unresolved
            reason = decision.reason if decision else "核验器没有返回该问题"
            result.append(finding.model_copy(update={
                "severity": "info",
                "verification_status": "uncertain" if unresolved else "anchored",
                "semantic_status": "uncertain" if unresolved else status,
                "conflict_type": decision.conflict_type if decision else "insufficient",
                "verification_confidence": decision.confidence if decision else 0.0,
                "verification_note": f"{source}：{reason}" + (f"；{_SAME_CHAPTER_CONFLICT_NOTE}" if unresolved and _SAME_CHAPTER_CONFLICT_NOTE in finding.verification_note else ""),
                "repair_instruction": (
                    "待核实，勿据此自动修改正文。" if unresolved
                    else "该指控不构成硬问题，无需据此强制修改正文。"
                ),
                "proposed_severity": finding.proposed_severity or finding.severity,
            }))
        else:
            result.append(finding.model_copy(update={
                "semantic_status": "supported",
                "conflict_type": decision.conflict_type,
                "verification_confidence": decision.confidence,
                "verification_note": f"{source}：{decision.reason}",
            }))
    return result, _verdict(result, disputed)


def apply_dispute_decisions(findings: list[ReviewFinding], decisions: list[ReviewClaimDecision], *, source: str) -> tuple[list[ReviewFinding], str]:
    by_index = {item.finding_index: item for item in checked_claim_decisions(decisions)}
    result: list[ReviewFinding] = []
    disputed = False
    for index, finding in enumerate(findings):
        if finding.verification_status != "uncertain":
            result.append(finding)
            continue
        decision = by_index.get(index)
        if decision and decision.verdict == "supported" and finding.proposed_severity:
            result.append(finding.model_copy(update={
                "severity": finding.proposed_severity,
                "verification_status": "anchored",
                "semantic_status": "supported",
                "conflict_type": decision.conflict_type,
                "verification_confidence": decision.confidence,
                "verification_note": f"{source}：{decision.reason}",
            }))
        elif decision and decision.verdict in {"not_blocking", "contradicted"} and _SAME_CHAPTER_CONFLICT_NOTE not in finding.verification_note:
            result.append(finding.model_copy(update={
                "severity": "info",
                "verification_status": "anchored",
                "semantic_status": decision.verdict,
                "conflict_type": decision.conflict_type,
                "verification_confidence": decision.confidence,
                "verification_note": f"{source}：{decision.reason}",
                "repair_instruction": "该指控不构成硬问题，无需据此强制修改正文。",
            }))
        else:
            disputed = True
            result.append(finding.model_copy(update={
                "semantic_status": "uncertain",
                "verification_note": f"{source}：{decision.reason if decision else '裁判没有返回该问题'}",
                "verification_confidence": decision.confidence if decision else 0.0,
            }))
    return result, _verdict(result, disputed)


def local_nli_decisions(model_name: str, findings: list[ReviewFinding]) -> list[ReviewClaimDecision]:
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("本地 NLI 需要安装 review 可选依赖。") from exc
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSequenceClassification.from_pretrained(model_name)
    model.eval()
    decisions: list[ReviewClaimDecision] = []
    for item in findings_for_semantic_check(findings):
        premise = "\n".join(value for value in (item["draft_evidence"], item["reference_evidence"]) if value)
        encoded = tokenizer(premise, item["claim"], return_tensors="pt", truncation=True)
        with torch.no_grad():
            probabilities = torch.softmax(model(**encoded).logits[0], dim=-1)
        label_index = int(probabilities.argmax().item())
        label = str(model.config.id2label.get(label_index, "uncertain")).casefold()
        verdict = "supported" if "entail" in label else "contradicted" if "contrad" in label else "uncertain"
        decisions.append(ReviewClaimDecision(
            finding_index=item["finding_index"], verdict=verdict,
            confidence=float(probabilities[label_index].item()), reason=f"本地 NLI 标签 {label}",
        ))
    return decisions


def _verdict(findings: list[ReviewFinding], disputed: bool) -> str:
    if any(item.severity in {"major", "blocking"} for item in findings):
        return "patch"
    return "unknown" if disputed else "pass"
