export type ContextMaterialItem = {
  key: string; title: string; estimated_tokens: number; source_count: number; hard: boolean; changed?: boolean;
};

export type ContextMaterialGroup = {
  key: string; title: string; estimated_tokens: number; source_count: number; changed: boolean;
  members: ContextMaterialItem[]; color: string;
};

function materialColor(title: string): string {
  const categories: Array<[RegExp, string]> = [
    [/当前任务|用户.*(?:要求|请求)|这次想说/, "task"],
    [/设定|项目契约|书籍信息/, "settings"],
    [/规划|章节卡|本章卡/, "planning"],
    [/正史|事实|证据/, "canon"],
    [/正文|前章|原文|交稿|交接|章节.*决定/, "prose"],
    [/习惯|偏好|声音|文风|技能/, "voice"],
    [/对话|会话/, "dialogue"],
    [/伏笔|线索/, "threads"],
    [/边界|规则|流程|恢复|约束/, "rules"],
  ];
  return categories.find(([pattern]) => pattern.test(title))?.[1] || "reference";
}

export function groupContextMaterials(allocation: ContextMaterialItem[]): {
  total: number; items: ContextMaterialItem[]; groups: ContextMaterialGroup[];
} {
  const items = allocation.filter(item => Number.isFinite(item.estimated_tokens) && item.estimated_tokens > 0);
  const total = items.reduce((sum, item) => sum + item.estimated_tokens, 0);
  if (!total) return { total, items, groups: [] };
  const small = items.filter(item => item.estimated_tokens * 10 < total);
  const main = items.filter(item => item.estimated_tokens * 10 >= total)
    .sort((a, b) => b.estimated_tokens - a.estimated_tokens || a.key.localeCompare(b.key));
  if (main.length + Number(small.length > 0) > 5) small.push(...main.splice(4));
  const groups: ContextMaterialGroup[] = main.map(item => ({ ...item, changed: Boolean(item.changed),
    members: [item], color: materialColor(item.title) }));
  if (small.length) groups.push({ key: "other:context-materials", title: "其他",
    estimated_tokens: small.reduce((sum, item) => sum + item.estimated_tokens, 0),
    source_count: small.reduce((sum, item) => sum + item.source_count, 0), changed: small.some(item => item.changed),
    members: small, color: "other" });
  return { total, items, groups };
}
