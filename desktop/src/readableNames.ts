const documentNames: Record<string, string> = {
  "BOOK.md": "本书设定", "OUTLINE.md": "全书大纲", "STORY_DETAIL.md": "卷细纲",
  "RECENT_PLAN.md": "近期章节规划", "PLAN.md": "旧版近期规划", "STATE.md": "正史与故事状态", "DIALOGUE.md": "对话记录",
};

/** 仅把完整的内部待办指令投影成中文；原指令不变，历史展示也不借当前待办猜测章节范围。 */
export function pendingWorkDisplayText(text: string): string {
  // project.py 只生成24位摘要、plan-摘要或manual-加手改任务号；未知格式保留为作者原话。
  const command = text.match(/^\s*(处理待办|查看待办|下次再处理待办)\s+(?:[a-f0-9]{24}|plan-[a-f0-9]{24}|manual-manual-[a-f0-9]{32})[。！!\s]*$/);
  if (!command) return text;
  return ({ 处理待办: "继续上次的工作", 查看待办: "查看上次的工作", 下次再处理待办: "稍后继续上次的工作" } as Record<string, string>)[command[1]];
}

/** 仅翻译回复说明中的已知术语；引文、作品专名和未知词保留，不据此改判审核状态。 */
export function readableConversationText(text: string): string {
  const terms: Record<string, string> = {
    coordinator: "任务协调", writer: "写作", editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理",
    finding: "问题记录", findings: "问题记录", readability: "表达与阅读连贯", continuity: "前后剧情一致性",
    causality: "行动因果", motivation: "人物动机", progression: "剧情推进", requirements: "本书要求",
    pass: "通过", patch: "需要局部修补", replan: "需要调整规划", unknown: "尚未核实", partial: "部分满足",
    decision_summary: "修改说明", public_reasoning_summary: "安排依据", memory_patch: "记忆候选",
    parent_thread_ids: "前置伏笔关系", causal_basis: "剧情因果依据",
  };
  return text.split(/(“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"|'[^'\n]*')/g).map((part, index) => index % 2 ? part : part
    // 旧规划回执曾把后台断点拼进下一步；仅识别这份已知格式，原消息仍可复制核对。
    .replace(/当前断点[：:]\s*promise-alignment\s*\/\s*(?:review|writer|source_check)\s*[；;]\s*来源与保留成果见\s+[^。\n]+[。]?/g, "伏笔一致性核对尚未完成，相关成果和来源已保留，可到待处理工作查看。")
    .replace(/\bparent thread ids\b/gi, "前置伏笔关系")
    .replace(/\bcausal basis\b/gi, "剧情因果依据")
    .replace(/\bpromise-alignment\b/g, "伏笔一致性核对")
    .replace(/\bMemory Keeper\b/gi, "记忆整理")
    .replace(/\b([a-z]+(?:_[a-z]+)*)\b/gi, word => terms[word.toLowerCase()] || word)
    .replace(/\bthread-[a-zA-Z0-9_-]+\b/g, "相关伏笔")
    .replace(/部分满足\s*\/\s*(\d+)/g, "部分满足（评分：$1）")
  ).join("");
}

export function documentLabel(source: string): string {
  const path = source.replace(/\\/g, "/");
  const name = path.split("/").pop() || path;
  if (documentNames[name]) return documentNames[name];
  if (/^(?:story-setting:)?setting-record-/.test(path)) return "设定参考条目";
  const chapter = name.match(/chapter_(\d+)/);
  if (chapter) return `第${Number(chapter[1])}章${path.includes("reviews/") ? "审查报告" : name.includes("draft") ? "草稿" : "正文"}`;
  if (/^outline-/.test(name)) return "旧版全书大纲";
  if (/^volume-detail-/.test(name)) return "旧版卷细纲";
  if (/^chapter-window-/.test(name)) return "旧版章节规划";
  if (/^database-/.test(name)) return "章节衔接记录";
  if (/^batch[-_]/.test(name) || path.includes("batches/")) return "批次记录";
  if (path.includes(".inkflow/")) return "后台核对记录";
  return name.replace(/(?:^|[-_])(?:[a-f0-9]{12,}|\d{8}T\d{6}[^.]*)/gi, "").replace(/\.(?:json|md|log)$/i, "") || "相关资料";
}

// 用本次公开校验、结束和保存记录解释交付缺口；后续已修好的旧问题不再覆盖当前结果。
export function readableOutputFailure(text: string, steps: Array<{ type?: string; stage?: string; status?: string; role?: string; metadata?: Record<string, unknown> }> = []): string {
  const formatFailure = /字段|格式|交付|缺少(?:章节)?标题|必填|结构|校验/.test(text);
  const genericFailure = /任务未完成|本次未完成|本次操作未完成|具体原因尚未返回/.test(text)
    && !/网络|连接|限流|超时|取消|篇幅|字数|低于|少于|版本.*变化|来源.*变化/.test(text);
  if (!formatFailure && !genericFailure) return text;
  const event = [...steps].reverse().find(step => step.type === "model.output.invalid");
  const issues = event?.metadata?.validation_errors;
  if (!event || !Array.isArray(issues)) return text;
  const later = steps.slice(steps.indexOf(event) + 1);
  if (later.some(step => step.status === "completed" && (step.role === event.role && /\.model$/.test(step.stage || "")
      || event.role === "writer" && step.stage === "draft.write" || step.type === `${event.role}.completed`))) return text;
  const problems: string[] = [];
  for (const issue of issues) {
    if (!issue || typeof issue !== "object") continue;
    const path = String(issue.location || ""), missing = issue.type === "missing";
    let problem = "";
    if (event.role === "writer" && path === "title") problem = missing ? "还缺章节标题" : "章节标题没有按要求交回";
    else if (event.role === "writer" && path === "content") problem = missing ? "还缺正文" : "正文的长度或格式需要补正";
    const promise = path.match(/^(?:hook_note\.)?promise_work\.(\d+)\.(rationale|statement)$/);
    if (event.role === "writer" && promise) problem = `第${Number(promise[1]) + 1}条伏笔安排${promise[2] === "rationale" ? "没有说清这样埋的理由" : "的说明放错了位置"}`;
    const assessment = path.match(/^assessments\.(\d+)\.(status|source_id|source_evidence|chapter_evidence|reason|alternative)$/);
    if (assessment) {
      const field = assessment[2];
      problem = `第${Number(assessment[1]) + 1}项检查` + (field === "status" ? "没有给出有效结论"
        : `的${({ source_id: "外部来源", source_evidence: "对照引文", chapter_evidence: "本章引文", reason: "判断理由", alternative: "其他解释" } as Record<string, string>)[field]}${missing ? "还没交齐" : "格式需要补正"}`);
    }
    if (problem && !problems.includes(problem)) problems.push(problem);
  }
  if (!problems.length) return text;
  const sameAttempt = steps.filter(step => step.role === event.role
    && (event.metadata?.attempt === undefined || step.metadata?.attempt === event.metadata.attempt));
  const complete = sameAttempt.some(step => step.type === "model.stream.finished" && ["stop", "end_turn", "stop_sequence"].includes(String(step.metadata?.finish_reason)));
  const preserved = sameAttempt.some(step => step.type === "model.response_preserved");
  const role = ({ writer: "写作模型", editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理" } as Record<string, string>)[event.role || ""] || "负责这一步的模型";
  const chapter = text.match(/第\s*(\d+)\s*章/)?.[1];
  const count = event.metadata?.draft_effective_characters;
  const length = typeof count === "number" && Number.isInteger(count) && count >= 0 ? `，正文有${count.toLocaleString()}个有效字` : "";
  const next = event.role === "writer" ? "可以沿这份返回补齐这些交接信息，再检查篇幅和正文，接着原任务往下走。"
    : "可以保留现有报告，只补正上面列出的检查项，再对照原文复核。";
  const budget = /已用完获准|请求次数已用完|额度已用完/.test(text) ? " 本次允许的请求次数已用完，继续补正前还需要你的追加授权。" : "";
  return `${chapter ? `第${chapter}章：` : ""}${role}${complete ? "已正常交回内容" : "已经返回内容"}${length}，但${problems.join("；")}。${preserved ? "收到的内容已保存，尚未作为通过检查的结果采用。" : "这份结果尚未通过交付检查。"}${next}${budget}`;
}

/** 用真实字符进展解释等待，分开模型停更、连接静默和缓存缺报，不冒充交付完成。 */
export function readableStreamProgress(metadata: Record<string, unknown> = {}): string {
  const phase = String(metadata.phase || "");
  const count = (value: unknown): number | undefined => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
  const bytes = count(metadata.received_bytes);
  const characters = count(metadata.content_chars);
  const thinking = count(metadata.reasoning_chars);
  const message = ({ connecting: "正在连接模型，等待回复。", waiting: Number(metadata.heartbeat_count) > 0 ? "连接还在，模型还没开始回复。" : bytes ? "已经收到回复数据，还没收到可显示的文字。" : "已连上模型，正在等回复。",
    thinking: thinking ? `已收到${thinking.toLocaleString()}个思考阶段字符，最终回复尚未开始。` : "正在等待模型回复；当前没有可核对的思考进展计数。",
    content: characters ? `已收到${characters.toLocaleString()}个回复字符，继续接收中。` : "回复正在传回来，还没收完。",
    finished: "模型已结束输出，正在核对返回与用量。" } as Record<string, string>)[phase];
  if (!message) return "";
  const idle = count(metadata.idle_ms) || 0;
  const textIdle = count(metadata.model_text_idle_ms) || 0;
  const elapsed = count(metadata.elapsed_ms) || 0;
  const waiting = ["connecting", "waiting"].includes(phase) && elapsed >= 30_000 ? ` 本次已等待${Math.floor(elapsed / 1000)}秒。` : "";
  const stalled = ["thinking", "content"].includes(phase) && textIdle >= 30_000 ? ` 模型文本已${Math.floor(textIdle / 1000)}秒未更新，仍在等这条回复。` : "";
  return message + waiting + stalled + (idle >= 30_000 ? ` 连接已有${Math.floor(idle / 1000)}秒没收到新数据；你可以随时停止。` : "");
}

export function readableSourceText(text: string, names: Array<[string, string]> = []): string {
  let result = text;
  for (const [source, name] of [...names].sort((a, b) => b[0].length - a[0].length)) {
    if (source && name) result = result.split(source).join(name);
  }
  return result
    .replace(/正在准备本步请求；实际发送情况会单独记录/g, "正在准备这一步，还没发给模型。")
    .replace(/已按当前任务与模型能力选择思考策略。/g, "正在准备回答。")
    .replace(/本次模型请求已发送(?:；有数据就继续接收，连续两分钟完全无数据才处理连接，可随时停止。|，不设时间限制，可随时停止。)/g, "已发给模型，正在等回复。你可以随时停止。")
    .replace(/\b(editor|reviewer|memory_keeper)\s*限定检查[：:]\s*unknown\b/g, (_, role: string) => `${({ editor: "编辑", reviewer: "专项审查", memory_keeper: "记忆整理" } as Record<string, string>)[role]}检查：待核（依据或同版记忆交接尚未完成）`)
    .replace(/断开前已收到\s*0\s*个返回字符；尚未收到正文片段。/g, "断流前尚未收到回复正文；这份旧记录没有统计传输字节或思考阶段数据。")
    .replace(/(?:story-setting:)?setting-record-[A-Za-z0-9_-]+/g, "设定参考条目")
    .replace(/(?:chapters|reviews|planning|batches|\.inkflow)[/\\][^\s，。；、<>“”‘’"'：:（）()\[\]{}]+/g, value => documentLabel(value))
    .replace(/\b(?:BOOK|OUTLINE|STORY_DETAIL|RECENT_PLAN|PLAN|STATE|DIALOGUE)\.md\b/g, value => documentLabel(value))
    .replace(/\brevision\s*(\d+)\b/gi, "第$1版")
    .replace(/\b(?:epistemic_status|verified_reference|needs_evidence|pending_review|user_constraint|hypothesis|objective|status)\b/g, value => ({
      epistemic_status: "记录性质", verified_reference: "已核对可用", needs_evidence: "仍需核对依据",
      pending_review: "等待核对", user_constraint: "作者明确要求", hypothesis: "未确认设想",
      objective: "客观记录", status: "处理状态",
    } as Record<string, string>)[value])
    .replace(/[（(](?:哈希|编号|ID)\s*[:：]?\s*[a-f0-9]{12,64}[）)]/gi, "")
    .replace(/\b(?:write|review|recover|revise|accept)-0*(\d+)\b/g, value => {
      const [action, number] = value.split("-");
      return `第${Number(number)}章${({ write: "写作", review: "审查", recover: "定点处理", revise: "局部修订", accept: "验收" } as Record<string, string>)[action]}`;
    })
    .replace(/\b(?:task|artifact|manual|batch|setting|branch)-[a-zA-Z0-9+_-]{12,}\b/g, "相关记录")
    .replace(/\b[a-f0-9]{20,64}\b/gi, "相关记录");
}
