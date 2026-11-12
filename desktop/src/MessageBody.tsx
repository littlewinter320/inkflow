import "./MessageBody.css";
import { memo } from "react";
import { ReadableText } from "./ReadableText";

type ReviewEvent = { type?: string; stage?: string; status?: string; metadata?: Record<string, unknown> };
type MessageBodyProps = { role: string; text: string; displayText: string; process?: { status?: string; steps?: ReviewEvent[] }; chapterNo?: number };

/** 只为已知旧执行回执收起技术说明；普通讨论、用户要求、代码示例和小说正文保持原样。 */
export function legacyReviewExplanation({ role, text, process, chapterNo }: Omit<MessageBodyProps, "displayText">): { summary: string; groups: string[]; state: string } | null {
  if (role !== "assistant" || /^\s*(?:```|>)/.test(text)) return null;
  // 旧规划失败回执有固定门禁与断点标记，不能仅凭出现字段名就把技术讨论折叠。
  if (text.includes("承诺差异补审未通过或身份/逐字依据不符，原层成果与窄返回均保留")
      && /当前断点[：:]\s*promise-alignment\s*\/\s*review/.test(text)) {
    return { summary: "这条记录返回时，规划的伏笔一致性核对尚未完成，原规划和本次修改结果已保留。",
      groups: ["伏笔前置关系", "剧情因果依据", "复核意见与来源"],
      state: "当时还需核对规划版本、原文依据和已保存意见，再由负责角色定点补齐，无需重新生成已完成的章节安排。现在是否已解决，以最新规划和待办为准。" };
  }
  if (text.length < 450) return null;
  const firstLine = text.trimStart().split("\n", 1)[0];
  if (/^#{1,3}\s*第\s*\d+\s*章/.test(firstLine) && !/审查报告|审核报告/.test(firstLine)) return null;
  const roleNames = new Set([...text.matchAll(/(?:^|[：:；;\n])\s*(editor|reviewer|memory_keeper)(?:\s*限定检查)?\s*[:：]/gi)].map(match => match[1].toLowerCase()));
  const fields = new Set(text.match(/\b(?:expression|readability|source_queries|source_comparisons|missing_source_ids|writer_note_questions|notes_after|findings|hook_assessment|memory_patch)\b/g) || []);
  if (!roleNames.size || fields.size < 2 || !/\b(?:unknown|pass|patch)\b/.test(text)) return null;
  const steps = process?.steps || [];
  const reviewProcess = steps.some(step => [step.stage, step.type].some(value => /^(?:review\.(?:mode|source_recovery|evidence_resume|notes_resume|correction)|mode\.(?:editor|reviewer|memory_keeper)|engine\.review_mode)(?:\.|$)/.test(value || "")));
  // 没有真实审查过程的技术讨论不靠几个字段猜为报告；仅旧的固定“限定检查”回执兼容无过程记录。
  if (!reviewProcess && (roleNames.size < 2 || !/(?:editor|reviewer|memory_keeper)\s*限定检查[：:]\s*(?:unknown|pass|patch)\b/i.test(text))) return null;
  const finalReview = [...steps].reverse().find(step => step.stage === "review.mode" || step.type === "review.mode.completed");
  const number = typeof chapterNo === "number" && Number.isInteger(chapterNo) && chapterNo > 0 ? chapterNo : finalReview?.metadata?.chapter_no;
  const chapter = typeof number === "number" && Number.isInteger(number) && number > 0 ? `第${number}章` : "";
  const verdict = finalReview?.metadata?.verdict;
  const state = verdict === "pass" ? "这次审查记录的结论为通过，正式接受状态仍以当前章节为准。"
    : verdict === "patch" ? "当时的审查提出修补意见，是否仍需处理应对照当前正文版本。"
    : verdict === "unknown" ? "当时的审查仍需核实依据，尚未形成通过结论。"
    : "这份详细记录包含待核判断；当前是否已解决，应查看最新章节报告。";
  const groups: string[] = [];
  if (fields.has("expression") || fields.has("readability")) groups.push("表达审读");
  if (["source_queries", "source_comparisons", "missing_source_ids"].some(field => fields.has(field))) groups.push("来源与引文");
  if (["notes_after", "writer_note_questions", "hook_assessment"].some(field => fields.has(field))) groups.push("钩子与写作说明");
  if (fields.has("memory_patch")) groups.push("记忆交接");
  return { summary: `${chapter}这条记录包含各角色的详细审查说明。`, groups, state };
}

/** 正常消息保持完整显示，旧技术报告只收起展示层，原消息与逐字内容仍保存在“完整说明”。 */
export const MessageBody = memo(function MessageBody(props: MessageBodyProps) {
  const report = legacyReviewExplanation(props);
  // 用户正文由调用方保持逐字原样；内部待办指令只改变 displayText，text 继续供复制和续接使用。
  if (!report) return props.role === "user" ? <p>{props.displayText}</p> : <ReadableText text={props.displayText} />;
  return <div className="message-review-summary"><p>{report.summary}</p><p>{report.state}</p>
    {report.groups.length > 0 && <p className="message-review-groups">内容分类：{report.groups.join(" · ")}</p>}
    <p>下一步：查看当前章节报告和待办，从仍有效的断点继续。</p>
    <details className="message-full-explanation"><summary>完整说明</summary><div>{props.text}</div></details>
  </div>;
});
