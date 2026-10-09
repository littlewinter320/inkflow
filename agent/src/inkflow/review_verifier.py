"""Reviewer evidence and semantic verification gates."""
from __future__ import annotations

import re
import json
from typing import Any

from .schemas import ContextPacket, ReviewClaimDecision, ReviewFinding, ReviewReport
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


def packet_sources(packet: ContextPacket) -> dict[str, str]:
    sources: dict[str, str] = {}
    for section in packet.sections:
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
    return sources


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
    sources = packet_sources(packet)
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
        canon_refs = [] if finding.rule_id == "internal_chapter_conflict" else list(finding.canon_refs)
        if finding.reference_evidence.strip():
            matching_refs = [
                ref for ref, source_text in sources.items()
                if evidence_matches(finding.reference_evidence, source_text)
            ]
            if matching_refs and not any(ref in matching_refs for ref in canon_refs):
                canon_refs = matching_refs[:4]
                rebound_note = "引用编号已按逐字证据自动重绑"
        if not finding.evidence.strip() or not evidence_matches(finding.evidence, content):
            reasons.append("引用无法逐字定位到当前正文")
        if any(ref not in sources for ref in canon_refs):
            reasons.append("引用来源不在本次上下文内")
        hard = finding.severity in {"major", "blocking"}
        editorial = finding.category in {"style", "pacing"}
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
