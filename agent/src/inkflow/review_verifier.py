"""Reviewer evidence and semantic verification gates."""
from __future__ import annotations

from typing import Any

from .schemas import ContextPacket, ReviewClaimDecision, ReviewFinding, ReviewReport

HARD_RULES = {
    "canon_conflict": {"world", "knowledge", "character"},
    "impossible_time": {"timeline"},
    "impossible_causality": {"causality"},
    "core_function_missing": {"planning"},
    "truncation": {"format"},
    "severe_repetition": {"originality", "format"},
}


def packet_sources(packet: ContextPacket) -> dict[str, str]:
    sources: dict[str, str] = {}
    for section in packet.sections:
        for ref in section.source_ids:
            sources[ref] = sources.get(ref, "") + "\n" + section.content
    return sources


def verify_review(report: ReviewReport, content: str, packet: ContextPacket) -> tuple[list[ReviewFinding], str]:
    """Anchor every finding to exact source text before it can affect prose."""
    sources = packet_sources(packet)
    findings: list[ReviewFinding] = []
    seen: set[tuple[str, str, str]] = set()
    disputed = report.verdict == "unknown"
    for finding in report.findings:
        key = (finding.category, finding.evidence, finding.explanation)
        if key in seen:
            continue
        seen.add(key)
        reasons: list[str] = []
        if not finding.evidence.strip() or finding.evidence not in content:
            reasons.append("引用无法逐字定位到当前正文")
        if any(ref not in sources for ref in finding.canon_refs):
            reasons.append("引用来源不在本次上下文内")
        hard = finding.severity in {"major", "blocking"}
        editorial = finding.category in {"style", "pacing"}
        if hard and not editorial:
            if finding.category not in HARD_RULES.get(finding.rule_id, set()):
                reasons.append("缺少适用的硬门禁规则编号")
            if finding.rule_id in {"canon_conflict", "core_function_missing"}:
                if not finding.canon_refs or not finding.reference_evidence.strip():
                    reasons.append("缺少正史或章节卡对照原文")
                elif not any(finding.reference_evidence in sources.get(ref, "") for ref in finding.canon_refs):
                    reasons.append("对照原文无法定位到来源")
        if reasons:
            disputed |= hard and not editorial
        findings.append(finding.model_copy(update={
            "severity": "info" if reasons else "minor" if hard and editorial else finding.severity,
            "verification_status": "unsupported" if reasons else "anchored",
            "verification_note": "；".join(reasons) if reasons else "正文和来源引用已定位；语义结论等待核验",
            "verification_confidence": 0.0 if reasons else 1.0,
            "proposed_severity": finding.proposed_severity or finding.severity,
            "repair_instruction": "待核实，勿据此自动修改正文。" if reasons else finding.repair_instruction,
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
        if item.verification_status == "anchored" and item.severity in {"major", "blocking"}]


def apply_semantic_decisions(findings: list[ReviewFinding], decisions: list[ReviewClaimDecision], *, source: str) -> tuple[list[ReviewFinding], str]:
    by_index = {item.finding_index: item for item in decisions}
    result: list[ReviewFinding] = []
    disputed = False
    for index, finding in enumerate(findings):
        if finding.verification_status != "anchored" or finding.severity not in {"major", "blocking"}:
            result.append(finding)
            continue
        decision = by_index.get(index)
        if decision is None or decision.verdict != "supported":
            disputed = True
            status = decision.verdict if decision else "uncertain"
            reason = decision.reason if decision else "核验器没有返回该问题"
            result.append(finding.model_copy(update={
                "severity": "info",
                "verification_status": "uncertain",
                "semantic_status": status,
                "verification_confidence": decision.confidence if decision else 0.0,
                "verification_note": f"{source}：{reason}",
                "repair_instruction": "待核实，勿据此自动修改正文。",
                "proposed_severity": finding.proposed_severity or finding.severity,
            }))
        else:
            result.append(finding.model_copy(update={
                "semantic_status": "supported",
                "verification_confidence": decision.confidence,
                "verification_note": f"{source}：{decision.reason}",
            }))
    return result, _verdict(result, disputed)


def apply_dispute_decisions(findings: list[ReviewFinding], decisions: list[ReviewClaimDecision], *, source: str) -> tuple[list[ReviewFinding], str]:
    by_index = {item.finding_index: item for item in decisions}
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
                "verification_confidence": decision.confidence,
                "verification_note": f"{source}：{decision.reason}",
            }))
        else:
            disputed = True
            result.append(finding.model_copy(update={
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
