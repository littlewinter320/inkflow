from __future__ import annotations

import json
from typing import Any

from .schemas import MemoryPatch, PlanBundle, ReviewReport


def render_plan(bundle: PlanBundle) -> str:
    book = bundle.book
    volume = bundle.current_volume
    arc = bundle.current_arc
    lines = [
        "# 小说规划",
        "",
        "> 本文件是四级规划的唯一人类可读视图。当前篇章精细，远期保持弹性。",
        "",
        "## 一、全书罗盘",
        "",
        f"- 书名：{book.title}",
        f"- 故事前提：{book.premise}",
        f"- 读者承诺：{book.reader_promise}",
        f"- 长期叙事发动机：{book.narrative_engine}",
        f"- 主冲突：{book.main_conflict}",
        f"- 主角起点：{book.protagonist_start}",
        f"- 主角终点：{book.protagonist_end}",
        f"- 主题问题：{book.theme_question}",
        f"- 结局方向：{book.ending_direction}",
        f"- 预计规模：{book.estimated_volumes} 卷 / {book.estimated_chapters} 章",
        "",
        "### 卷级罗盘",
        "",
    ]
    for item in book.volume_compass:
        lines.extend(
            [
                f"#### 第 {item.volume_no} 卷 · {item.title}",
                "",
                f"- 阶段承诺：{item.promise}",
                f"- 起点：{item.start_state}",
                f"- 终点：{item.end_state}",
                f"- 预计章节：{item.estimated_chapters}",
                "",
            ]
        )
    lines.extend(
        [
            f"## 二、当前卷：第 {volume.volume_no} 卷 · {volume.title}",
            "",
            f"- 章节范围：{volume.chapter_start}～{volume.chapter_end}",
            f"- 本卷承诺：{volume.promise}",
            f"- 卷首状态：{volume.start_state}",
            f"- 卷末状态：{volume.end_state}",
            f"- 对手压力：{volume.antagonist_pressure}",
            f"- 中段转折：{volume.midpoint_turn}",
            f"- 卷末高潮：{volume.climax}",
            f"- 代价与结果：{volume.cost_and_result}",
            f"- 下一卷桥：{volume.next_volume_bridge}",
            "",
            "### 本卷篇章",
            "",
        ]
    )
    for item in volume.arcs:
        lines.append(
            f"- `{item.arc_id}` {item.title}（第 {item.chapter_start}～{item.chapter_end} 章）："
            f"{item.promise} → {item.end_state}"
        )
    lines.extend(
        [
            "",
            f"## 三、当前篇章：{arc.title}",
            "",
            f"- ID：`{arc.arc_id}`",
            f"- 章节范围：{arc.chapter_start}～{arc.chapter_end}",
            f"- 篇章承诺：{arc.promise}",
            f"- 中心矛盾：{arc.central_conflict}",
            f"- 开始状态：{arc.start_state}",
            f"- 结束状态：{arc.end_state}",
            f"- 中段转折：{arc.midpoint_turn}",
            f"- 高潮：{arc.climax}",
            f"- 余波：{arc.aftermath}",
            f"- 出口桥：{arc.exit_bridge}",
            "",
            "### 公开推理摘要",
            "",
            *(
                [f"- {item}" for item in arc.public_reasoning_summary]
                or ["- 当前规划来自已批准的章节卡；旧版本未提供公开推理摘要。"]
            ),
            "",
            "### 后续核对点",
            "",
            *([f"- {item}" for item in arc.public_risks_to_verify] or ["- 当前无额外核对点。"]),
            "",
            "### 升级阶梯",
            "",
            *[f"- {item}" for item in arc.escalation],
            "",
            "### 重新规划触发器",
            "",
            *([f"- {item}" for item in arc.replan_triggers] or ["- 当前无额外触发器"]),
            "",
            "## 四、当前篇章章节卡",
            "",
        ]
    )
    for card in arc.chapter_cards:
        lines.extend(
            [
                f"### 第 {card.chapter_no} 章 · {card.title_working}",
                "",
                f"- 状态：`{card.status}`",
                f"- POV / 时空：{card.pov} / {card.time_location}",
                f"- 章节功能：{card.function}",
                f"- 目标：{card.goal}",
                f"- 阻力：{card.obstacle}",
                f"- 决定：{card.decision}",
                f"- 后果：{card.consequence}",
                f"- 不可逆变化：{card.irreversible_delta}",
                f"- 信息释放：{card.information_release}",
                f"- 钩子：{card.hook_type}｜{card.hook_question}",
                f"- 钩子强度：{card.hook_strength}",
                f"- 正文锚点：{card.hook_anchor or '由 Writer 在正文版本中落实'}",
                f"- 留白边界：{card.withholding_boundary or '答案可延后，但本章因果与人物行动必须清楚'}",
                f"- 预计回应：{card.payoff_window}",
                f"- 目标字数：{card.target_words}",
                f"- 依赖：{', '.join(card.dependencies) if card.dependencies else '无'}",
                "- 场景：",
                *[f"  - {scene}" for scene in card.scenes],
                f"- 推进伏笔：{', '.join(card.foreshadow_advance) if card.foreshadow_advance else '无'}",
                f"- 兑现承诺：{', '.join(card.payoff) if card.payoff else '无'}",
                "",
            ]
        )
    lines.extend(
        [
            "> 修改本文件后应让墨流生成并验证计划补丁；不要绕过门禁直接让宿主 Agent 写下一章。",
            "",
        ]
    )
    return "\n".join(lines)


def render_review(chapter_no: int, report: ReviewReport, code_metrics: dict[str, Any]) -> str:
    hard_findings = [item for item in report.findings if item.severity in {"major", "blocking"}]
    pending_findings = [
        item for item in report.findings
        if item.verification_status in {"unsupported", "uncertain"}
        or item.semantic_status == "uncertain"
    ]
    if report.verdict == "unknown":
        gate_summary = "当前审核尚未形成可放行结论，需要核实以下依据。"
        if not hard_findings:
            gate_summary += "核验后未保留 major/blocking 级硬问题，不等于正文已证实存在硬矛盾。"
    elif report.verdict == "pass" and not hard_findings:
        gate_summary = "未发现 major/blocking 级、且有证据支撑的问题；本章可以通过。"
    elif hard_findings:
        gate_summary = "核验后保留的 major/blocking 级问题需要处理，当前不能放行。"
    else:
        gate_summary = "当前审查要求修订或重新规划，具体依据见分项记录。"
    lines = [
        f"# 第 {chapter_no} 章审查报告",
        "",
        f"> 结论：`{report.verdict}`｜{'审查符合度（规则计算）' if report.confidence_basis else '旧版模型自评（未校准）'}：{report.confidence:.2%}｜有效跨来源对照：{len(report.source_comparisons)} 条",
        "",
        "## 把握度依据",
        "",
        *([f"- {item}" for item in report.confidence_basis] or ["- 旧版报告：尚无证据化计算明细。"]),
        "- 自动通过采用设置中的底线（至少80%），不是固定得分。资料缺失的0%表示未完成评定，不代表正文质量为零；符合度也不是事实正确的概率。",
        "",
        "## 必需项与加权项的证据",
        "",
        *[f"- {item.criterion} · {item.status} · 等级{item.grade}/4：{item.reason}\n  - 正文：{item.chapter_evidence}\n  - 来源：{item.source_id} {item.source_evidence}\n  - 另一种解读：{item.alternative}" for item in report.assessments],
        "",
        "## 核验后的当前结论",
        "",
        gate_summary,
        *(
            [f"- 待核实依据：{item.verification_note or '尚未得到完整核验结果。'}" for item in pending_findings]
            if report.verdict == "unknown"
            else []
        ),
        "",
        "## 模型摘要（保留记录）",
        "",
        "> 以下概括可能仍含后续核验未支持的原始断言；放行或阻断以上方当前结论为准。",
        f"> 模型自评把握度：{(report.model_self_confidence if report.model_self_confidence is not None else report.confidence):.0%}。未经校准，不用于自动放行。",
        "",
        report.summary,
        "",
        "## 本章实际内容观察",
        "",
        f"- 人物目标：{report.focus_observation.observed_goal or '未定位'}",
        f"- 目标原文：{report.focus_observation.goal_evidence or '未定位'}",
        f"- 决定或变化：{report.focus_observation.observed_change or '未定位'}",
        f"- 变化原文：{report.focus_observation.change_evidence or '未定位'}",
        f"- 与章节卡关系：{report.focus_observation.plan_alignment}；{report.focus_observation.alignment_reason or '未说明'}",
        "",
        "## 确定性指标",
        "",
        "```json",
        json.dumps(code_metrics, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 阅读体验与钩子",
        "",
        *(
            [
                f"- 清晰度：`{report.hook_assessment.clarity}`",
                f"- 钩子依据概括：{report.hook_assessment.actual_anchor or '未单独记录'}",
                f"- 读者期待：{report.hook_assessment.reader_expectation or '未单独记录'}",
                f"- 重复风险：{report.hook_assessment.repetition_risk or '未发现'}",
                f"- 回应风险：{report.hook_assessment.payoff_risk or '未发现'}",
                f"- 建议：{report.hook_assessment.suggestion or '无需额外调整'}",
            ]
            if report.hook_assessment
            else ["- 当前报告未包含单独的钩子判断；正史安全结论仍按下方证据门禁计算。"]
        ),
        "",
        "## 上下文使用核对（模型判断）",
        "",
        f"- 结论：{report.context_use_audit.summary or '未发现需要单独说明的上下文使用问题。'}",
        f"- 已实际采用：{', '.join(report.context_use_audit.used_source_ids) or '未单独标记'}",
        f"- 应用但遗漏：{', '.join(report.context_use_audit.missing_required_source_ids) or '无'}",
        f"- 发生冲突：{', '.join(report.context_use_audit.conflicting_source_ids) or '无'}",
        "",
        "## 与规划和前章的逐字对照",
        "",
        *([line for item in report.source_comparisons for line in (
            f"- 来源 `{item.source_id}`｜关系：{item.relation}｜{item.reason}",
            f"  - 来源原文：{item.source_evidence}",
            f"  - 本章原文：{item.chapter_evidence}",
        )] or ["- 本次报告未保存逐字对照；不能据此声称已完成跨章核对。"]),
        "",
        "## 多视角阅读画像",
        "",
        "- 各维度独立呈现，不汇总成单一总分，也不把某一种文风当成标准答案。",
        "- 放行规则：已核实的 `major` / `blocking` 硬问题须处理；`unknown` 须先核实，不能按通过处理；`minor` 只是可选编辑建议。",
        "",
    ]
    if report.verdict == "unknown":
        lines.extend(["### 审查未完成：不计算质量分", "", "- 未核实不等于没有问题，也不能显示满分。", ""])
    for item in report.scorecard if report.verdict != "unknown" else []:
        severities = {deduction.severity for deduction in item.deductions}
        status = (
            "必须修复"
            if "blocking" in severities
            else "需要修补"
            if "major" in severities
            else "可保留，有编辑建议"
            if "minor" in severities
            else "未见问题"
        )
        lines.extend(
            [
                f"### {item.dimension}：{status}",
                "",
            ]
        )
        if not item.deductions:
            lines.append("- 当前文本中没有找到需要提出的证据化问题。")
        for deduction in item.deductions:
            lines.extend(
                [
                    f"- [{deduction.severity}] {deduction.category}：{deduction.evidence}",
                    f"  - 原因：{deduction.explanation}",
                    f"  - 依据：{', '.join(deduction.canon_refs) if deduction.canon_refs else '本章正文证据'}",
                ]
            )
        lines.append("")
    lines.extend(
        [
            "## 放行或拦截依据",
            "",
            f"- {gate_summary}",
            *(
                [f"  - [{item.severity}] {item.evidence}：{item.explanation}" for item in hard_findings]
                if hard_findings
                else []
            ),
            "",
            "## 做得好的地方",
            "",
            *([f"- {item}" for item in report.strengths] or ["- 暂无单独记录"]),
            "",
            "## 问题",
            "",
        ]
    )
    if not report.findings:
        lines.append("- 未发现需要修改的问题。")
    for index, item in enumerate(report.findings, 1):
        lines.extend(
            [
                f"### {index}. [{item.severity}] {item.category}",
                "",
                f"- 证据：{item.evidence}",
                f"- 正史引用：{', '.join(item.canon_refs) if item.canon_refs else '无'}",
                f"- 说明：{item.explanation}",
                f"- 证据核验：{item.verification_note or '旧报告未记录核验结果'}",
                f"- 引文定位：{item.verification_status} / 语义状态：{item.semantic_status} / " + (f"语义复核自评：{item.verification_confidence:.0%}" if item.semantic_status != "unchecked" else "尚无独立语义复核分数（引文存在不等于判断成立）"),
                f"- 修复：{item.repair_instruction}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_memory_conflict(chapter_no: int, patch: MemoryPatch) -> str:
    lines = [
        f"# 第 {chapter_no} 章记忆冲突",
        "",
        "> 本文件是未提交的候选 MemoryPatch 投影，不属于 SQLite 正史。",
        "",
        "## 阻塞原因",
        "",
        *[f"- {item}" for item in patch.unresolved_conflicts],
        "",
        "## 候选补丁",
        "",
        "```json",
        json.dumps(patch.model_dump(mode="json"), ensure_ascii=False, indent=2),
        "```",
        "",
        "> 需要由用户判断应修正文、修规划还是让 记忆服务 重新提取；不要直接编辑 SQLite。",
        "",
    ]
    return "\n".join(lines)


def render_state(facts: list[dict[str, Any]], threads: list[dict[str, Any]], status: dict[str, Any]) -> str:
    lines = [
        "# 当前正史状态",
        "",
        "> 由已接受章节和 SQLite 正史生成；草稿、被拒版本和模型推理不进入本页。",
        "",
        "## 项目进度",
        "",
        f"- 章节：{json.dumps(status.get('chapters', {}), ensure_ascii=False)}",
        f"- 当前事实：{status.get('active_facts', 0)}",
        f"- 未结线索：{status.get('open_threads', 0)}",
        "",
        "## 当前事实",
        "",
    ]
    if facts:
        for fact in facts:
            lines.append(
                f"- `{fact['fact_id']}` {fact['subject']} / {fact['predicate']} = "
                f"{json.dumps(fact['value'], ensure_ascii=False)}（第 {fact['source_chapter']} 章）"
            )
    else:
        lines.append("- 暂无已提交事实。")
    lines.extend(["", "## 未结线索与承诺", ""])
    if threads:
        for thread in threads:
            due = f"，预计第 {thread['due_chapter']} 章前处理" if thread.get("due_chapter") else ""
            lines.append(
                f"- `{thread['thread_id']}` [{thread['status']}] {thread['title']}：{thread['description']}{due}"
            )
    else:
        lines.append("- 暂无未结线索。")
    return "\n".join(lines).rstrip() + "\n"
