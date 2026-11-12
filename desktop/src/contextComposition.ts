export type ContextMaterialItem = {
  key: string; title: string; estimated_tokens: number; source_count: number; hard: boolean; changed?: boolean;
};

export type ContextMaterialGroup = {
  key: string; title: string; estimated_tokens: number; source_count: number; changed: boolean;
  members: ContextMaterialItem[]; color: string;
};

function materialColor(title: string): string {
  // Prefer semantic hues; repeated hues are separated after grouping without changing data order.
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
  // Keep exact proportions, group tiny entries, and give each visible group its own swatch.
  const items = allocation.filter(item => Number.isFinite(item.estimated_tokens) && item.estimated_tokens > 0);
  const total = items.reduce((sum, item) => sum + item.estimated_tokens, 0);
  if (!total) return { total, items, groups: [] };
  // 缓存把同类材料拆成公共/变化部分，不能据此画成两种同名分类；原来源仍逐份可读。
  const categories = new Map<string, ContextMaterialGroup>();
  for (const item of items) {
    const identity = JSON.stringify([item.title, item.hard]);
    const group = categories.get(identity);
    if (group) {
      group.estimated_tokens += item.estimated_tokens;
      group.source_count += item.source_count;
      group.changed ||= Boolean(item.changed);
      group.members.push(item);
    } else categories.set(identity, { ...item, key: `category:${identity}`, changed: Boolean(item.changed),
      members: [item], color: materialColor(item.title) });
  }
  const combined = [...categories.values()];
  const small = combined.filter(item => item.estimated_tokens * 10 < total);
  const main = combined.filter(item => item.estimated_tokens * 10 >= total)
    .sort((a, b) => b.estimated_tokens - a.estimated_tokens || a.key.localeCompare(b.key));
  if (main.length + Number(small.length > 0) > 5) small.push(...main.splice(4));
  const groups: ContextMaterialGroup[] = main;
  if (small.length) groups.push({ key: "other:context-materials", title: "其他",
    estimated_tokens: small.reduce((sum, item) => sum + item.estimated_tokens, 0),
    source_count: small.reduce((sum, item) => sum + item.source_count, 0), changed: small.some(item => item.changed),
    members: small.flatMap(item => item.members), color: "other" });
  const usedColors = new Set<string>();
  const repeated: ContextMaterialGroup[] = [];
  // Resolve equal hues by stable identity, so changing proportions alone cannot swap their colors.
  for (const group of [...groups].sort((left, right) => left.key.localeCompare(right.key))) {
    if (usedColors.has(group.color)) repeated.push(group);
    else usedColors.add(group.color);
  }
  for (const group of repeated) {
    group.color = ["task", "canon", "settings", "planning", "prose", "voice", "dialogue", "threads", "rules", "reference"]
      .find(color => !usedColors.has(color)) || group.color;
    usedColors.add(group.color);
  }
  return { total, items, groups };
}
