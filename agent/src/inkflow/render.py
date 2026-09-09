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
    score_total = sum(item.score for item in report.scorecard)
    max_total = sum(item.maximum_score for item in report.scorecard)
    hard_findings = [item for item in report.findings if item.severity in {"major", "blocking"}]
    lines = [
        f"# 第 {chapter_no} 章审查报告",
        "",
        f"> 结论：`{report.verdict}`｜置信度：{report.confidence:.0%}",
        "",
        "## 摘要",
        "",
        report.summary,
        "",
        "## 确定性指标",
        "",
        "```json",
        json.dumps(code_metrics, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 透明评分（只按已列出的证据扣分）",
        "",
        (
            f"- 总分：{score_total}/{max_total}。评分用于解释完成度，不替代正史门禁。"
            if report.scorecard
            else "- 当前报告来自旧版本，尚未生成分项评分。"
        ),
        "- 放行规则：只有 `major` / `blocking` 的证据化问题会阻止进入正史；`minor` 会扣分并保留修订建议，但不自动拦截。",
        "",
    ]
    for item in report.scorecard:
        lines.extend(
            [
                f"### {item.dimension}：{item.score}/{item.maximum_score}",
                "",
            ]
        )
        if not item.deductions:
            lines.append("- 未见可引用的扣分证据，因此本项满分。")
        for deduction in item.deductions:
            loss = {"minor": 3, "major": 12, "blocking": 25}.get(deduction.severity, 0)
            lines.extend(
                [
                    f"- 扣 {loss} 分（[{deduction.severity}] {deduction.category}）：{deduction.evidence}",
                    f"  - 原因：{deduction.explanation}",
                    f"  - 依据：{', '.join(deduction.canon_refs) if deduction.canon_refs else '本章正文证据'}",
                ]
            )
        lines.append("")
    lines.extend(
        [
            "## 放行或拦截依据",
            "",
            (
                "- 未发现 major/blocking 级、且有证据支撑的问题；本章可以通过。"
                if report.verdict == "pass" and not hard_findings
                else ("- 审核证据不足，等待核实；不等于正文已证实有错。" if report.verdict == "unknown" else "- 以下 major/blocking 问题需要处理：")
            ),
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
                f"- 核验状态：{item.verification_status} / 语义状态：{item.semantic_status} / 置信度：{item.verification_confidence:.0%}",
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
        "> 需要由用户判断应修正文、修规划还是让 Memory Keeper 重新提取；不要直接编辑 SQLite。",
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
