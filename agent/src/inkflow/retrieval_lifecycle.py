"""Conservative retrieval identity, duplicate and ambiguity policies; never rewrite canon."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .utils import content_hash, json_dumps


def source_fingerprint(item: dict[str, Any]) -> str:
    # Text alone cannot bind feedback to a revised source or a different viewpoint.
    if (item.get("source_type") in {"canon_fact", "canon_thread", "canon_source", "chapter_summary"}
            and (not item.get("version") or not item.get("source_hash"))):
        return ""
    identity = {key: item.get(key) for key in ("source_id", "source_type", "chapter_no", "version",
        "source_hash", "identity", "epistemic_kind", "event_time", "narrative_time", "status")}
    identity["text_hash"] = content_hash(str(item.get("title", "")) + "\n" + str(item.get("body", "")))
    return content_hash(json_dumps(identity, indent=None))


def consolidate_candidates(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse identical, equally scoped evidence; semantic similarity is only a search signal."""
    kept, groups, duplicates = [], {}, []
    for item in items:
        identity = tuple(item.get(key) for key in ("source_type", "identity", "chapter_no", "version",
            "source_hash", "epistemic_kind", "event_time", "narrative_time", "status", "authority_rank"))
        key = (*identity, str(item.get("title", "")), str(item.get("body", "")))
        # Unbound legacy records cannot prove that two IDs represent the same evidence.
        if not item.get("source_hash") or not item.get("body") or key not in groups:
            chosen = dict(item)
            groups[key] = chosen
            kept.append(chosen)
            continue
        chosen = groups[key]
        aliases = chosen.setdefault("duplicate_source_ids", [])
        aliases.append(item["source_id"])
        duplicates.append({"source_id": item["source_id"], "representative_id": chosen["source_id"],
            "reason": "同类型、同章、同版、同来源哈希及相同内容合并展示；原记录保留。"})
    for item in kept:
        item["source_fingerprint"] = source_fingerprint(item)
    return kept, duplicates


def ambiguity_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = defaultdict(list)
    for item in items:
        if item.get("source_type") == "canon_fact" and item.get("subject") and item.get("predicate"):
            groups[(item["subject"], item["predicate"])].append(item)
    result = []
    for (subject, predicate), values in groups.items():
        variants = {json_dumps([item.get(key) for key in
            ("value", "epistemic_kind", "event_time", "narrative_time")], indent=None) for item in values}
        if len(variants) > 1:
            result.append({"subject": subject, "predicate": predicate,
                "source_ids": [item["source_id"] for item in values],
                "state": "needs_comparison",
                "reason": "同一主体关系存在不同内容、认识类型或时点；保留对照，不按排名或新旧直接裁定。"})
    return result


def preserve_alternatives(selected: list[str], groups: list[dict[str, Any]], *, limit: int) -> list[str]:
    """Reserve at most one comparison source without exceeding the existing result budget."""
    # shortcut: reserve one comparison per retrieval; expand after measured missed-comparison rates justify it.
    if limit < 2 or not selected:
        return selected
    for group in groups:
        members = group["source_ids"]
        present = [identity for identity in selected if identity in members]
        missing = [identity for identity in members if identity not in selected]
        if present and missing:
            if len(selected) < limit:
                return selected + missing[:1]
            protected = present[0]
            replace = next((identity for identity in reversed(selected) if identity != protected), None)
            if replace:
                return [identity for identity in selected if identity != replace] + missing[:1]
    return selected


def packet_feedback_manifest(packet, diagnostics: dict[str, Any], *, role: str) -> dict[str, Any]:
    """Bind only complete hits surviving context compression, never merely retrieved candidates."""
    bindings = {item["source_id"]: item.get("source_fingerprint") for item in diagnostics.get("selected", [])
                if item.get("source_fingerprint")}
    included = {}
    for section in packet.sections:
        if section.key not in {"F0", "F"}:
            continue
        try:
            value = json.loads(section.content)
        except (ValueError, TypeError):
            continue
        records = value if isinstance(value, list) else value.get("自适应结果", []) if isinstance(value, dict) else []
        for item in records:
            if (isinstance(item, dict) and item.get("source_id") in section.source_ids
                    and item.get("source_fingerprint") == bindings.get(item.get("source_id"))
                    and item.get("source_fingerprint")):
                included[item["source_id"]] = item["source_fingerprint"]
    return {"query_hash": diagnostics.get("query_hash", ""), "role": role,
        "chapter_no": packet.chapter_no, "sources": included}
