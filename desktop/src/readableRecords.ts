import { readableSourceText } from "./readableNames";

const states: Record<string, string> = {
  pass: "审查通过", patch: "需要局部修订", unknown: "依据不足，尚未确定", completed: "已完成",
  failed: "未完成", interrupted: "已中断", cancelled: "已停止", running: "正在处理",
  waiting_user: "等待你的选择", waiting_condition: "等待必要条件", needs_input: "需要补充信息",
  accepted: "已收入正史", draft: "尚未接受的草稿", open: "尚未解决", advanced: "已有进展",
  paid: "已经回应", delayed: "延后回应", abandoned: "不再推进", verified: "已经核验",
  unresolved: "仍未解决", no_change: "正文没有变化", satisfied: "明确认可", revised: "已经修订",
};
const roles: Record<string, string> = { coordinator: "任务协调", writer: "写作", editor: "综合审读", reviewer: "专项审查", memory_keeper: "记忆整理", engine: "本地引擎" };
const groups: Record<string, string> = {
  facts: "事实候选", threads: "线索变化", setting_updates: "设定候选", findings: "发现的问题",
  issues: "需要处理的问题", source_comparisons: "正文与来源对照", assessments: "审查依据",
  evidence_refs: "原文依据", operations: "记忆调整", warnings: "需要留意的情况",
  selected: "本次选用", omitted: "本次未选用", discarded: "没有取回的资料", questions: "需要你选择的问题",
  scene_summaries: "本章场景", unresolved_conflicts: "尚未消解的冲突", decision_summary: "处理依据",
  change_summary: "修改说明", strengths: "做得合适的部分", public_reasoning_summary: "公开判断依据",
};
const prose: Record<string, string> = {
  reply: "", summary: "", chapter_summary: "本章内容", description: "", text: "", message: "",
  reason: "原因", explanation: "原因", next_action: "接下来", suggestion: "建议", instruction: "本次要求",
  evidence: "正文依据", quote: "引用原文", source_quote: "对照来源", chapter_quote: "本章原文", setting_quote: "设定原文",
  resolution: "处理结果", remaining_question: "尚需确定", requested_response: "本步需要", answer: "回应",
  task: "本次任务", before: "修改前", after: "修改后", inserted_text: "补充的内容", replacement: "替换内容",
};

export function readableValue(value: unknown): string {
  if (value === null || value === undefined) return "尚未记录";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value !== "object") return readableSourceText(String(value));
  if (Array.isArray(value)) return value.map(readableValue).join("；");
  const labels: Record<string, string> = { name: "名称", status: "状态", location: "位置", owner: "持有人", time: "时间", content: "内容", value: "内容", notes: "说明", count: "数量", amount: "数额" };
  return Object.entries(value as Record<string, unknown>).filter(([key]) => !/(?:^|_)id$|Id$|hash|fingerprint|secret|api.?key|reasoning|thought/i.test(key))
    .map(([key, item]) => `${labels[key] || (/[^\x00-\x7f]/.test(key) ? key : "内容")}：${readableValue(item)}`).join("；");
}

export function readableRecord(value: unknown): string {
  const lines: string[] = [], seen = new Set<unknown>();
  const add = (text: string) => { const clean = readableSourceText(text).trim(); if (clean && !lines.includes(clean)) lines.push(clean); };
  const visit = (item: unknown, heading = "", depth = 0) => {
    if (item === null || item === undefined || depth > 8) return;
    if (typeof item === "string") { add(`${heading ? heading + "：" : ""}${item}`); return; }
    if (typeof item !== "object" || seen.has(item)) return;
    seen.add(item);
    if (Array.isArray(item)) { item.forEach(entry => visit(entry, heading, depth + 1)); return; }
    const record = item as Record<string, unknown>;
    if (record.subject && record.predicate && "value" in record) {
      const kind = record.epistemic_kind === "belief" ? "人物相信的事" : record.epistemic_kind === "rumor" ? "传闻" : heading || "事实候选";
      add(`${kind}：${String(record.subject)} · ${String(record.predicate)}——${readableValue(record.value)}`);
    } else {
      const title = String(record.title || record.name || "");
      const actor = roles[String(record.actor || record.role || "")] || "";
      const state = states[String(record.verdict || record.status || "")] || "";
      const chapter = typeof record.chapter_no === "number" ? `第${record.chapter_no}章` : "";
      const label = [heading, title, chapter, actor, state].filter(Boolean).join(" · ");
      if (label) add(label);
    }
    for (const [key, label] of Object.entries(prose)) if (typeof record[key] === "string" && record[key]) {
      add(`${label ? label + "：" : ""}${String(record[key])}`);
    }
    if (record.values && typeof record.values === "object") add(readableValue(record.values));
    for (const [key, label] of Object.entries(groups)) if (Array.isArray(record[key]) && (record[key] as unknown[]).length) visit(record[key], label, depth + 1);
    if (record.query_terms !== undefined) add(`本次查找提取了${String(record.query_terms)}个关键词，匹配${String(record.entity_count ?? "尚未统计")}个人物或对象；候选中有${String(record.open_thread_count ?? "尚未统计")}条未结线索，参考跨度${String(record.time_span ?? "尚未统计")}章。`);
    if (typeof record.changed === "boolean") add(record.changed ? "内容已有实际改动。" : "内容没有发生变化。");
    for (const key of ["result", "data", "output", "report", "memory_patch", "preview", "steps", "reports", "events", "impact", "automatic_acceptance"]) {
      if (record[key] && typeof record[key] === "object") visit(record[key], "", depth + 1);
    }
  };
  visit(value);
  return lines.join("\n\n") || "这份记录尚无可读摘要；来源身份和运行参数保存在后台，不能据此判断任务已经完成。";
}

export function readableRecordText(text: string): string {
  const clean = text.trim();
  if (/^[\[{]/.test(clean)) {
    try { return readableRecord(JSON.parse(clean)); } catch { /* Ordinary prose keeps its original punctuation. */ }
  }
  return readableSourceText(text.replace(/```(?:json)?\s*\n([\s\S]*?)\n```/g, (block, body: string) => {
    try { return readableRecord(JSON.parse(body)); } catch { return block; }
  }));
}
