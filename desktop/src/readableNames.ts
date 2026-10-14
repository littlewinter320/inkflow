const documentNames: Record<string, string> = {
  "BOOK.md": "本书设定", "OUTLINE.md": "全书大纲", "STORY_DETAIL.md": "卷细纲",
  "RECENT_PLAN.md": "近期章节规划", "PLAN.md": "旧版近期规划", "STATE.md": "正史与故事状态", "DIALOGUE.md": "对话记录",
};

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

export function readableSourceText(text: string, names: Array<[string, string]> = []): string {
  let result = text;
  for (const [source, name] of [...names].sort((a, b) => b[0].length - a[0].length)) {
    if (source && name) result = result.split(source).join(name);
  }
  return result
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
