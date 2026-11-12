from __future__ import annotations

import json
import re
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
    from .story_promises import planning_promises
    promises = planning_promises(bundle)
    if promises:
        lines += ["## 伏笔与钩子规划合同", "", json.dumps(promises, ensure_ascii=False, indent=2), ""]
        lines += [f"- 第{card.chapter_no}章承诺动作：" + json.dumps([beat.model_dump(mode="json") for beat in card.promise_beats], ensure_ascii=False)
                  for card in arc.chapter_cards if card.promise_beats]
    lines.extend(
        [
            "> 修改本文件后应让墨流生成并验证计划补丁；不要绕过门禁直接让宿主 Agent 写下一章。",
            "",
        ]
    )
    return "\n".join(lines)


def _review_readable_text(value: str) -> str:
    """只翻译说明中的接口用语；逐字正文和来源引文不经过此转换。"""
    labels = {"unknown": "待核", "pass": "通过", "patch": "需要修补", "replan": "规划待调整",
        "editor": "编辑", "reviewer": "专项审查", "writer": "写作", "memory_keeper": "记忆整理",
        "expression": "表达检查", "readability": "阅读连贯", "findings": "问题记录",
        "notes_after": "当前写作说明", "notes_before": "此前写作说明", "source_queries": "回源线索",
        "met": "满足", "partial": "部分满足", "data_missing": "依据待补", "direct": "原文直接支持"}
    value = re.sub(r"(?:task-goal:[\w:-]+|chapter:\d+|plan:chapter:[\w:-]+|(?:story-setting:)?setting-record-[\w-]+)",
        lambda item: _review_source_label(item.group()), value)
    value = re.sub(r"\b(?:BOOK|OUTLINE|STORY_DETAIL|RECENT_PLAN)\.md\b", lambda item: _review_source_label(item.group()), value)
    value = re.sub(r"@quote:\d+", "尚未还原的引用", value)
    return re.sub(r"\b(?:" + "|".join(labels) + r")\b", lambda item: labels[item.group()], value)


def _review_source_label(value: str) -> str:
    """把来源身份投影为可读名称，原始标识完整保留在诊断记录。"""
    names = {"book:requirements": "本书要求", "OUTLINE.md": "全书大纲", "STORY_DETAIL.md": "卷细纲",
        "RECENT_PLAN.md": "近期章节规划", "": "本章正文"}
    if value in names:
        return names[value]
    chapter = re.fullmatch(r"chapter:0*(\d+)", value)
    if chapter:
        return f"第{int(chapter[1])}章对照正文"
    if value.startswith("task-goal:"):
        return "本章临时目标"
    if value.startswith("plan:chapter:"):
        return "本章规划"
    if "setting-record-" in value:
        return "设定条目"
    return "对照资料"


def _review_score_incomplete(report: ReviewReport) -> bool:
    """只认实际评分器的缺证哨兵；unknown也可能有有效百分比，完成计算的零分照常保留。"""
    actual_zero = any(re.match(r"加权符合度：0(?:\.0+)?/100(?:；|$)", item) for item in report.confidence_basis)
    return (report.confidence == 0 and not actual_zero and any(item.startswith((
        "审核依据未齐：0%表示无法完成评定", "审核存在未解决的资料、职责或记忆交接缺口，暂不形成有效评分",
        "确定性检查未通过，尚未进行模型评定")) for item in report.confidence_basis))


def _review_quote(value: str) -> str:
    """单独的引用编号尚不是原文；其他实际引句逐字保留，包括句内看似技术词的文字。"""
    return "该引用尚未还原为原文，需要沿原报告核对。" if re.fullmatch(r"@quote:\d+", value.strip()) else value


def _review_pending_points(report: ReviewReport) -> list[str]:
    """把实际补核节点列为待核事项；原指控保持待核身份，不据其措辞确认正文缺陷。"""
    roles = {"editor": "编辑", "reviewer": "专项审查", "memory_keeper": "记忆整理"}
    points = []
    for role, recovery in report.evidence_recovery.get("roles", {}).items():
        if not isinstance(recovery, dict):
            continue
        gaps = recovery.get("remaining_gaps", [])
        for gap in gaps if isinstance(gaps, list) else []:
            item = f"{roles.get(role, '责任检查')}待核：{_review_readable_text(str(gap))}"
            if item not in points:
                points.append(item)
    return points


def _review_readable_details(report: ReviewReport) -> list[str]:
    """把主要审查字段写成可读分组；实际引文和建议替换内容逐字保留，不重新判断。"""
    focus, hook, audit = report.focus_observation, report.hook_assessment, report.context_use_audit
    arithmetic = [item.split("；", 1)[0] for item in report.confidence_basis
        if re.match(r"加权符合度：|.+：等级\d对应[\d.]+% × \d+ = [\d.]+分", item)]
    prefix = "已保存的阶段算式（非有效总分）：" if _review_score_incomplete(report) else ""
    lines = ["### 计分与核验依据", "", *[f"- {prefix}{item}" for item in arithmetic], ""]
    if not arithmetic:
        lines += ["当前记录尚无可独立展示的加权算式，不按未核实等级补造总分。", ""]
    lines += ["### 本章实际内容", "", "人物目标：" + _review_readable_text(focus.observed_goal), "",
        _review_quote(focus.goal_evidence), "", "决定或变化：" + _review_readable_text(focus.observed_change), "", _review_quote(focus.change_evidence), "",
        "与章节安排的关系：" + {"aligned": "相符", "adapted": "有调整", "diverged": "存在偏离", "unclear": "待核"}[focus.plan_alignment]
        + "；" + _review_readable_text(focus.alignment_reason), "", "### 阅读体验与钩子", ""]
    if hook:
        lines += ["清晰度：" + {"clear": "清楚", "intentional_ambiguity": "有意保留悬念", "confusing": "需澄清", "absent": "未单独设置"}[hook.clarity], "",
            "原角色的依据概括：" + hook.actual_anchor, "", "读者期待：" + _review_readable_text(hook.reader_expectation), "",
            "重复风险：" + _review_readable_text(hook.repetition_risk), "", "后续回应风险：" + _review_readable_text(hook.payoff_risk), "",
            "建议：" + _review_readable_text(hook.suggestion), ""]
    else:
        lines += ["没有单独的钩子评定，不能据此推定存在缺陷。", ""]
    lines += ["### 做得合适的部分", "", *[f"- {_review_readable_text(item)}" for item in report.strengths], "",
        "### 与前文、规划及要求的对照", ""]
    for item in report.source_comparisons:
        lines += [f"#### {_review_source_label(item.source_id)}", "", _review_readable_text(item.reason), "",
            "来源原句：", "", _review_quote(item.source_evidence), "", "本章原句：", "", _review_quote(item.chapter_evidence), ""]
    lines += ["### 资料与设定使用核对", "", _review_readable_text(audit.summary), "",
        "实际采用：" + "、".join(_review_source_label(item) for item in audit.used_source_ids), "",
        "应读但未完成对照：" + "、".join(_review_source_label(item) for item in audit.missing_required_source_ids), ""]
    for index, item in enumerate(audit.setting_checks, 1):
        lines += [f"#### 设定条目{index}的原评定", "", _review_readable_text(item.reason), "", "设定原句：", "",
            _review_quote(item.setting_quote), "", "本章对应原句：", "", _review_quote(item.chapter_quote), ""]
    lines += ["### 动态参考观察", ""]
    for item in report.reference_observations:
        lines += [f"#### {_review_readable_text(item.aspect)}", "", _review_readable_text(item.observation), "",
            _review_quote(item.chapter_evidence), "", _review_quote(item.source_evidence), "", "其他解释：" + _review_readable_text(item.alternative), "",
            "参考用途：" + _review_readable_text(item.reference_use), ""]
    lines += ["### 微改建议（尚未执行）", ""]
    for item in report.copyedits:
        lines += ["原句：", "", item.target_excerpt, "", "建议替换：", "", item.replacement, "",
            "原因：" + _review_readable_text(item.reason), ""]
    lines += ["### 本次要求与偏好取舍", ""]
    for item in report.preference_decisions:
        lines += [_review_readable_text(str(item.get("reason", ""))), "",
            *[str(quote) for quote in item.get("source_quotes", [])], ""]
    return lines


def render_review(chapter_no: int, report: ReviewReport, code_metrics: dict[str, Any]) -> str:
    """生成面向作者的四层报告；旧技术明细与完整结构化审计折叠保存，不修改判分或接受门禁。"""
    from .review_rubric import LABELS
    verdict = {"pass": "审查通过", "patch": "需要修补", "replan": "规划待调整", "unknown": "仍需核实"}[report.verdict]
    score = "评定尚未完成" if _review_score_incomplete(report) else f"审查符合度：{report.confidence:.2%}"
    overview = ("当前审查已通过，正式提交仍须核对同版来源与记忆交接。" if report.verdict == "pass" else
        "当前报告要求修补；先核对问题的原句和影响，再决定修改范围。" if report.verdict == "patch" else
        "当前规划依赖需要处理，具体范围以已核实依据为准。" if report.verdict == "replan" else
        "这次审查还没有形成通过结论。先核实审查依据，现有正文保留；待核意见不能当作已证实的正文缺陷。")
    confirmed = [item for item in report.findings if item.verification_status == "anchored"
        and item.semantic_status == "supported" and item.severity in {"major", "blocking"}]
    pending = [item for item in report.findings if item not in confirmed
        and (item.verification_status in {"unchecked", "uncertain", "unsupported"}
             or item.semantic_status in {"unchecked", "uncertain"})]
    pending_points = _review_pending_points(report)
    lines = [f"# 第{chapter_no}章审查报告", "", "## 总结", "", f"**{verdict} · {score}**", "", overview,
        "", "符合度用于说明已核实证据下的规则符合情况，不是正文质量分，也不是事实正确率。", "",
        "## 问题的点", "", f"- 已证实、需要处理的正文问题：{len(confirmed)}项。",
        f"- 仍需核实依据的意见与事项：{len(pending) + len(pending_points)}项。", "",
        *[f"- {item}" for item in pending_points], ""]
    for item in confirmed:
        lines += [f"### {item.category}", "", _review_readable_text(item.explanation), "",
            "原文依据：", "", item.evidence, "", "下一步：" + _review_readable_text(item.repair_instruction), ""]
    lines += ["## 审核要点", ""]
    statuses = {"met": "原评定满足", "partial": "原评定部分满足", "failed": "原评定未满足", "data_missing": "依据待补"}
    for item in report.assessments:
        lines += [f"### {LABELS[item.criterion]} · {statuses[item.status]}", "",
            _review_readable_text(item.reason), "", "正文依据：", "",
            _review_quote(item.chapter_evidence) or "本项尚未提供可定位的正文依据。", "",
            f"对照来源：{_review_source_label(item.source_id)}", "",
            _review_quote(item.source_evidence), "",
            "其他解释：" + _review_readable_text(item.alternative), ""]
    ending_weight = next((item.maximum_score for item in report.scorecard if item.dimension == "剧情推进、伏笔与钩子"), 35)
    if ending_weight > 35:
        lines += [f"剧情推进、伏笔与钩子权重 **{ending_weight}%**", ""]
    if report.ending_checks:
        labels = {"resolved": "报告已找到结果", "unresolved": "收束未完成", "needs_evidence": "结果待核实"}
        lines += ["## 终章主线核对", ""]
        for check in report.ending_checks:
            lines += [f"- **{labels[check.status]}** {_review_readable_text(check.reason)}"]
            for evidence in check.evidence:
                label = "本章" if evidence.source_id == "current" else _review_source_label(evidence.source_id)
                lines.append(f"  - {label} · {evidence.quote}")
        lines.append("")
    lines += ["## 完整报告", "", "下一步：" + ("沿原任务完成正式提交所需的交接。" if report.verdict == "pass" else
        "先核实原报告的待核项目，复用已完成的检查；有确证问题时再按原文处理。"), "",
        *_review_readable_details(report),
        "<details>", "<summary>原始诊断与完整审计（保留原值）</summary>", "",
        _render_review_diagnostics(chapter_no, report, code_metrics), "", "### 原始结构化报告", "", "```json",
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), "```", "", "</details>", ""]
    return "\n".join(lines)


def _render_review_diagnostics(chapter_no: int, report: ReviewReport, code_metrics: dict[str, Any]) -> str:
    """保留既有详细投影作为折叠诊断，不再直接作为作者的首页摘要。"""
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
        "",
        "## 自动补读与复核记录",
        "",
        f"- 证据契约：{report.evidence_policy_version or '旧报告未记录'}",
        f"- Writer说明来源：{report.writer_notes_hash or '无独立说明'}；未来设想不作为正史证据。",
        *(["```json", json.dumps(report.evidence_recovery, ensure_ascii=False, indent=2), "```"]
          if report.evidence_recovery.get("attempted") else ["- 本次没有触发额外检索复核。"]),
        "- 搜索未命中不证明事实不存在；补读记录不代表已经通过，以上方最终门禁为准。",
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
        *(["## 微改建议", "", *[
            f"- 原句：{item.target_excerpt}\n  - 建议替换：{item.replacement}\n  - 原因：{item.reason}"
            for item in report.copyedits], ""] if report.copyedits else []),
        *(["## 本任务偏好取舍", "", *[
            f"- {item['status']} · {item.get('selected_id') or '等待选择或核对'}：{item['reason']}\n  - 来源：{'、'.join(item['preference_ids'])}\n  - 原句：{' / '.join(item['source_quotes'])}"
            for item in report.preference_decisions], ""] if report.preference_decisions else []),
        "## 本章实际内容观察",
        "",
        f"- 人物目标：{report.focus_observation.observed_goal or '未定位'}",
        f"- 目标原文：{report.focus_observation.goal_evidence or '未定位'}",
        f"- 决定或变化：{report.focus_observation.observed_change or '未定位'}",
        f"- 变化原文：{report.focus_observation.change_evidence or '未定位'}",
        f"- 与章节卡关系：{report.focus_observation.plan_alignment}；{report.focus_observation.alignment_reason or '未说明'}",
        "",
        "## 动态参考观察（不单独计分或阻断）",
        "",
        *[f"- {item.aspect}：{item.observation}\n  - 正文：{item.chapter_evidence}\n  - 参考：{item.source_id} {item.source_evidence}\n  - 另一种解读：{item.alternative}\n  - 用途：{item.reference_use}" for item in report.reference_observations],
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
            f"- 来源 `{item.source_id}`｜关系：{item.relation}｜证据归因：{item.evidence_relation}｜{item.reason}",
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
