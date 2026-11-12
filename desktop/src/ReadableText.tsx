import type { ReactNode } from "react";
import { readableRecord } from "./readableRecords";

/** 仅把规划中独立的结构化段落转为阅读说明；不写回文件，不参与版本与恢复哈希。 */
function planningReadingText(text: string): string {
  const lines = text.split(/\r?\n/), output: string[] = [];
  const titles = new Map<string, string>();
  // 每条分别整理，避免共用摘要去重吞掉其他伏笔相同但仍必要的条件。
  const renderRecords = (value: unknown): string => {
    const fields = new Set(["thread_id", "kind", "horizon", "title", "question", "purpose", "causal_basis", "trigger_conditions", "completion_criteria", "milestones", "window_start", "window_end", "open_reason", "parent_thread_ids", "action", "expected_change"]);
    // 只解释现行伏笔结构；未知或将来的字段整段保留，不能被摘要选择器静默省略。
    if (!Array.isArray(value) || !value.length || value.some(item => !item || typeof item !== "object"
      || typeof item.thread_id !== "string" || Object.keys(item).some(key => !fields.has(key)))) return JSON.stringify(value, null, 2) ?? String(value);
    for (const item of value) if (item && typeof item === "object" && typeof item.thread_id === "string" && typeof item.title === "string") titles.set(item.thread_id, item.title);
    return value.map(item => {
      const title = item.title || titles.get(item.thread_id);
      return [title ? `### ${title}` : "### 线索安排", readableRecord({ ...item, title: undefined })].join("\n\n");
    }).join("\n\n");
  };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line === "[") {
      const end = lines.findIndex((item, candidate) => candidate > index && item === "]");
      if (end >= 0) {
        try {
          output.push(renderRecords(JSON.parse(lines.slice(index, end + 1).join("\n"))));
          index = end;
          continue;
        } catch { /* 非完整结构化段落按原文保留，不能用空摘要遮住内容。 */ }
      }
    }
    const beats = line.match(/^(- 伏笔与钩子动作：)(\[.*\])$/);
    if (beats) {
      try { output.push("### 本章伏笔安排", renderRecords(JSON.parse(beats[2]))); continue; }
      catch { /* 旧版或手改格式不能解析时仍显示原文。 */ }
    }
    output.push(line.replace(/^> 规划协议 v2(?: · |；)/, "> ").replace(/^(## .*?)伏笔与钩子规划合同$/, "$1伏笔与悬念安排"));
  }
  return output.join("\n");
}

/** 只支持文字强调，不执行模型输出的 HTML、脚本或链接。 */
function inlineText(text: string): ReactNode[] {
  return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g).map((part, index) => part.startsWith("**") && part.endsWith("**")
    ? <strong key={index}>{part.slice(2, -2)}</strong> : part.startsWith("`") && part.endsWith("`") ? part.slice(1, -1) : part);
}

/** 对话与规划共用轻量阅读排版；保留全部段落，未知格式按普通文字显示。 */
export function ReadableText({ text, planning = false }: { text: string; planning?: boolean }) {
  const lines = (planning ? planningReadingText(text) : text).split(/\r?\n/);
  const blocks: ReactNode[] = [];
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (!line.trim()) continue;
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const Heading = heading[1].length === 1 ? "h2" : heading[1].length === 2 ? "h3" : "h4";
      blocks.push(<Heading key={index}>{inlineText(heading[2])}</Heading>); continue;
    }
    const list = line.match(/^\s*(?:[-*+] |\d+[.)、]\s+)(.+)$/);
    if (list) {
      const start = index, ordered = /^\s*\d/.test(line), items: ReactNode[] = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*(?:[-*+] |\d+[.)、]\s+)(.+)$/);
        if (!item || /^\s*\d/.test(lines[index]) !== ordered) break;
        items.push(<li key={index}>{inlineText(item[1])}</li>); index++;
      }
      index--;
      blocks.push(ordered ? <ol key={start} start={Number(line.trim().match(/^\d+/)?.[0] || 1)}>{items}</ol> : <ul key={start}>{items}</ul>);
      continue;
    }
    if (line.startsWith("> ")) { blocks.push(<blockquote key={index}>{inlineText(line.slice(2))}</blockquote>); continue; }
    blocks.push(<p key={index}>{inlineText(line)}</p>);
  }
  return <div className={`readable-text${planning ? " planning-reading" : ""}`}>{blocks}</div>;
}
