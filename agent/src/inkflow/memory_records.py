"""Versioned evidence and reversible memory changes, committed with canon."""
from __future__ import annotations

import json
from difflib import SequenceMatcher
from uuid import uuid4

from .preferences import read_meta, write_meta
from .utils import content_hash, utc_now


def evidence_record(connection, reference, chapter_no, version, content):
    number = reference.source_chapter
    if number > chapter_no:
        raise ValueError("记忆证据不能引用未来章节")
    if number == chapter_no:
        text, source_version = content, version
    else:
        row = connection.execute("SELECT content_text,version FROM chapters WHERE chapter_no=? AND status='accepted'",
                                 (number,)).fetchone()
        if row is None or not row["content_text"]:
            raise ValueError("记忆证据缺少已接受正文")
        text, source_version = row["content_text"], row["version"]
    if reference.quote not in text:
        raise ValueError("记忆证据无法在指定章节原文定位")
    result = {"source_chapter": number, "source_version": source_version, "source_hash": content_hash(text),
              "quote": reference.quote, "offset": text.index(reference.quote), "relation": reference.relation}
    result["evidence_id"] = content_hash(json.dumps(result, sort_keys=True))[:24]
    result["recorded_at_chapter"] = chapter_no
    return result


def journal(connection, action, fact_id, before, after, reason, chapter_no):
    event = {"event_id": uuid4().hex, "action": action, "fact_id": fact_id, "before": before,
             "after": after, "reason": reason, "chapter_no": chapter_no, "created_at": utc_now()}
    write_meta(connection, "memory.event:" + event["event_id"], event)


def commit_evidence(connection, patch, chapter_no, version, content):
    from .schemas import MemoryEvidence
    for fact in patch.facts:
        refs = [MemoryEvidence(source_chapter=chapter_no, quote=fact.evidence), *fact.evidence_refs]
        records = [evidence_record(connection, ref, chapter_no, version, content) for ref in refs]
        key = "memory.evidence:" + fact.fact_id
        old = read_meta(connection, key, [])
        unique = {json.dumps(item, sort_keys=True): item for item in records}
        write_meta(connection, key, list(unique.values()))
        journal(connection, "evidence_replace" if old else "evidence_add", fact.fact_id, old,
                list(unique.values()), "随已审核正文提交证据", chapter_no)
    seen = set()
    for operation in patch.operations:
        if operation.target_fact_id in seen or any(f.fact_id == operation.target_fact_id for f in patch.facts):
            raise ValueError("同一事实不能同时新增、补证据或撤销")
        seen.add(operation.target_fact_id)
        row = connection.execute("SELECT * FROM facts WHERE fact_id=? AND status='active'",
                                 (operation.target_fact_id,)).fetchone()
        if row is None or row["source_chapter"] > chapter_no:
            raise ValueError("记忆操作目标不存在、过期或来自未来章节")
        records = [evidence_record(connection, ref, chapter_no, version, content) for ref in operation.evidence]
        if operation.action == "retract":
            if not any(ref["source_chapter"] == chapter_no and ref["relation"] == "counter" for ref in records):
                raise ValueError("撤销事实须有当前已审核章的明确反证；证据缺失不能当成反证")
            connection.execute("UPDATE facts SET status='superseded',valid_to_chapter=? WHERE fact_id=?",
                               (chapter_no - 1, operation.target_fact_id))
        key = "memory.evidence:" + operation.target_fact_id
        old = read_meta(connection, key, [])
        before_evidence = old
        if operation.action in {"replace_evidence", "retire_evidence"}:
            target = next((ref for ref in old if ref.get("evidence_id") == operation.target_evidence_id
                           and not ref.get("retired")), None)
            if target is None:
                raise ValueError("待替换或停用的证据不存在，请按当前证据编号重核")
            if target["source_chapter"] == row["source_chapter"] and target["quote"] == row["evidence"]:
                raise ValueError("主证据须通过正文修订与记忆重核替换，不能只停用主证据")
            old = [ref | {"retired": True, "retired_reason": operation.reason,
                          "retired_at_chapter": chapter_no}
                   if ref.get("evidence_id") == operation.target_evidence_id else ref for ref in old]
            if any(ref["evidence_id"] == operation.target_evidence_id for ref in records):
                raise ValueError("不能将同一证据同时停用和重新补入")
        additions = [] if operation.action == "retire_evidence" else records
        combined = {item.get("evidence_id", json.dumps(item, sort_keys=True)): item for item in old}
        for item in additions:
            previous = combined.get(item["evidence_id"])
            if previous and previous.get("retired"):
                raise ValueError("已停用的旧证据不能被补证据操作静默恢复")
            combined.setdefault(item["evidence_id"], item)
        write_meta(connection, key, list(combined.values()))
        journal(connection, operation.action, operation.target_fact_id, dict(row) | {"evidence_refs": before_evidence},
                {"evidence": list(combined.values()), "target_evidence_id": operation.target_evidence_id,
                 "justification_evidence": records,
                 "valid_to_chapter": chapter_no - 1 if operation.action == "retract" else None},
                operation.reason, chapter_no)


def memory_overview(database):
    with database.connect() as connection:
        events = [json.loads(row[0]) for row in connection.execute(
            "SELECT value_json FROM metadata WHERE key LIKE 'memory.event:%' ORDER BY updated_at DESC,key DESC")]
        facts = [dict(row) for row in connection.execute("SELECT * FROM facts ORDER BY source_chapter,fact_id")]
        for fact in facts:
            refs = read_meta(connection, "memory.evidence:" + fact["fact_id"], [])
            if not refs:
                refs = [{"source_chapter": fact["source_chapter"], "source_version": fact["source_version"],
                         "source_hash": fact["source_hash"], "quote": fact["evidence"], "relation": "support"}]
            for ref in refs:
                source = connection.execute("SELECT version,content_hash,content_text,status FROM chapters WHERE chapter_no=?",
                                            (ref["source_chapter"],)).fetchone()
                ref["status"] = "retired" if ref.get("retired") else "valid" if (source and source["status"] == "accepted" and
                    source["version"] == ref.get("source_version") and source["content_hash"] == ref.get("source_hash") and
                    ref["quote"] in (source["content_text"] or "")) else "needs_review"
            fact["evidence_refs"] = refs
        artifacts = [dict(row) for row in connection.execute(
            "SELECT artifact_id,chapter_no,artifact_type,data_json FROM agent_artifacts WHERE artifact_type='writer_context_manifest'")]
        dependencies = {}
        for item in artifacts:
            for ref in {ref for section in json.loads(item["data_json"]).get("sections", [])
                        for ref in section.get("source_ids", [])}:
                dependencies.setdefault(ref, []).append(item["artifact_id"])
        for fact in facts:
            # Only recorded direct dependencies; never claim this is a semantic whole-book audit.
            fact["dependent_artifacts"] = dependencies.get(fact["fact_id"], [])
    return {"facts": facts, "events": events, "notice": "过期证据待核对，不等于事实错误；仅显示已记录的直接引用。"}


def affected_fact_ids(facts, before, after):
    old, new = before.splitlines(), after.splitlines()
    changed = []
    for tag, a, b, c, d in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            changed.append("\n".join(old[max(0, a - 2):b + 2] + new[max(0, c - 2):d + 2]))
    return {fact.fact_id for fact in facts if fact.evidence not in after or
            any(fact.evidence in part or fact.subject in part for part in changed)}


def attach_evidence(database, facts, boundary=None):
    with database.connect() as connection:
        for fact in facts:
            refs = read_meta(connection, "memory.evidence:" + fact["fact_id"], [])
            usable = []
            for ref in refs:
                if boundary is not None and max(ref["source_chapter"], ref.get("recorded_at_chapter", 0)) > boundary:
                    continue
                if ref.get("retired") and (boundary is None or boundary >= ref.get("retired_at_chapter", 0)):
                    continue
                if boundary is not None and ref.get("retired_at_chapter", 0) > boundary:
                    ref = {key: value for key, value in ref.items() if key not in {"retired", "retired_reason", "retired_at_chapter"}}
                row = connection.execute("SELECT version,content_hash,content_text,status FROM chapters WHERE chapter_no=?",
                                         (ref["source_chapter"],)).fetchone()
                if row and row["status"] == "accepted" and row["version"] == ref["source_version"] and row["content_hash"] == ref["source_hash"] and ref["quote"] in (row["content_text"] or ""):
                    usable.append(ref)
            fact["evidence_refs"] = usable
    return facts
