export const panelIds = ["navigation", "assistant", "inspector", "workbench"] as const;
export type PanelId = typeof panelIds[number];
export type DockPosition = "left" | "right" | "top" | "bottom" | "center" | "floating";
export type Rect = { x: number; y: number; width: number; height: number };
export type PanelLayout = Rect & { visible: boolean; position: DockPosition; order: number };
export type DockGroup = { type: "group"; id: string; tabs: PanelId[]; active: PanelId };
export type DockSplit = { type: "split"; id: string; axis: "horizontal" | "vertical"; ratio: number; first: DockNode; second: DockNode };
export type DockNode = DockGroup | DockSplit;
export type PanelMode = "docked" | "floating" | "native" | "hidden";
export type LayoutSnapshot = { panels: Record<PanelId, PanelLayout>; dock: DockNode | null; activePanel: PanelId; conversationOverviewRatio: number; bottomHeight: number };
export type LayoutTemplate = { id: string; name: string; snapshot: LayoutSnapshot; nativePanels: PanelId[] };
export type WorkspaceLayout = LayoutSnapshot & { version: 5; templates: LayoutTemplate[] };
export type WorkspaceSize = { width: number; height: number };
export type DropPreview = { action: "dock" | "close" | "detach" | "swap"; position?: Exclude<DockPosition, "floating">; target?: PanelId; rect: Rect };
export const panelLabels: Record<PanelId, string> = { navigation: "小说结构", assistant: "墨宝对话", inspector: "批注与版本", workbench: "工程工作台" };
export const layoutStorageKey = "inkflow.workspace-layout.v5";
export const defaultConversationOverviewRatio = .4;
export const conversationSplitterHeight = 20;
const minimumWidths: Record<PanelId, number> = { navigation: 200, assistant: 300, inspector: 220, workbench: 340 };
const minimumHeights: Record<PanelId, number> = { navigation: 220, assistant: 340, inspector: 220, workbench: 300 };
const gap = 7, tabHeight = 29;
let sequence = 0;
const uid = () => `dock-${Date.now().toString(36)}-${++sequence}`;
const finite = (value: unknown, fallback: number) => typeof value === "number" && Number.isFinite(value) ? value : fallback;
const clamp = (value: number, minimum: number, maximum: number) => Math.max(Math.min(minimum, maximum), Math.min(maximum, value));
const isPanel = (value: unknown): value is PanelId => panelIds.includes(value as PanelId);
/** 页签组与分割是唯一停靠结构；面板没有被指定为永久主区。 */
export const dockGroup = (tabs: PanelId[], id = uid()): DockGroup => ({ type: "group", id, tabs, active: tabs[0] });
export const dockSplit = (first: DockNode, second: DockNode, axis: DockSplit["axis"], ratio = .5, id = uid()): DockSplit => ({ type: "split", id, axis, ratio, first, second });
export const defaultWorkspaceLayout: WorkspaceLayout = {
  version: 5, templates: [], activePanel: "assistant",
  panels: {
    navigation: { visible: true, position: "center", x: 24, y: 24, width: 240, height: 520, order: 0 },
    assistant: { visible: true, position: "center", x: 260, y: 24, width: 520, height: 620, order: 1 },
    inspector: { visible: false, position: "center", x: 520, y: 48, width: 300, height: 480, order: 2 },
    workbench: { visible: false, position: "center", x: 340, y: 32, width: 620, height: 620, order: 3 },
  },
  dock: dockSplit(dockGroup(["navigation"], "default-navigation"), dockGroup(["assistant"], "default-assistant"), "horizontal", .21, "default-split"),
  bottomHeight: 280, conversationOverviewRatio: defaultConversationOverviewRatio,
};

export function readConversationOverviewRatio(value: unknown): number {
  return clamp(finite(value, defaultConversationOverviewRatio), .15, .75);
}

export function conversationSplitGeometry(value: unknown, height: number) {
  const available = Math.max(0, finite(height, 0) - conversationSplitterHeight);
  // Both regions scroll internally. Small panels share the available space
  // instead of letting a fixed minimum push messages or the composer outside.
  const minimum = available ? Math.max(.15, Math.min(80, available * .4) / available) : .15;
  const maximum = available ? Math.max(minimum, Math.min(.75, 1 - Math.min(120, available * .6) / available)) : .75;
  return { ratio: clamp(readConversationOverviewRatio(value), minimum, maximum), minimum, maximum, available };
}

export function conversationSplitKeyRatio(value: number, key: string, height: number, largeStep = false): number | null {
  const { ratio, minimum, maximum } = conversationSplitGeometry(value, height);
  if (key === "Home") return minimum;
  if (key === "End") return maximum;
  if (key === "Enter") return defaultConversationOverviewRatio;
  const step = largeStep || key.startsWith("Page") ? .1 : .02;
  if (["ArrowUp", "PageUp"].includes(key)) return clamp(ratio - step, minimum, maximum);
  if (["ArrowDown", "PageDown"].includes(key)) return clamp(ratio + step, minimum, maximum);
  return null;
}

/** 枚举树中的面板，页签次序与分割的首/次区域均有意义。 */
export function dockPanels(node: DockNode | null): PanelId[] {
  return !node ? [] : node.type === "group" ? node.tabs : [...dockPanels(node.first), ...dockPanels(node.second)];
}

/** 移除单个面板并压缩空分支；被隐藏的面板不会调用此函数，仍保留原停靠位置。 */
function removeDockPanel(node: DockNode | null, id: PanelId): DockNode | null {
  if (!node) return null;
  if (node.type === "group") {
    const tabs = node.tabs.filter(item => item !== id);
    return tabs.length ? { ...node, tabs, active: tabs.includes(node.active) ? node.active : tabs[0] } : null;
  }
  const first = removeDockPanel(node.first, id), second = removeDockPanel(node.second, id);
  return first && second ? { ...node, first, second } : first || second;
}

/** 局部替换包含目标面板的组，用同一个入口支持区域内分割和合并页签。 */
function replaceDockGroup(node: DockNode, target: PanelId, replace: (group: DockGroup) => DockNode): DockNode {
  if (node.type === "group") return node.tabs.includes(target) ? replace(node) : node;
  return { ...node, first: replaceDockGroup(node.first, target, replace), second: replaceDockGroup(node.second, target, replace) };
}

/** 给指定面板激活页签；全局焦点还用于窄屏自动合组时选择可见内容。 */
export function activatePanel(layout: WorkspaceLayout, id: PanelId): WorkspaceLayout {
  const dock = layout.dock && replaceDockGroup(layout.dock, id, group => group.active === id ? group : { ...group, active: id });
  return { ...layout, dock, activePanel: id };
}

/** 把面板放到目标区域任一侧或页签中；不指定目标时四侧作用于整个主界面。 */
export function dropLayout(layout: WorkspaceLayout, id: PanelId, position: Exclude<DockPosition, "floating">, target?: PanelId): WorkspaceLayout {
  const dock = removeDockPanel(layout.dock, id), incoming = dockGroup([id]);
  const put = (destination: DockNode): DockNode => {
    if (position === "center") {
      const tabs = [...dockPanels(destination), id];
      return { ...dockGroup(tabs), active: id };
    }
    const axis = position === "left" || position === "right" ? "horizontal" : "vertical";
    return position === "left" || position === "top" ? dockSplit(incoming, destination, axis) : dockSplit(destination, incoming, axis);
  };
  const destination = target && dockPanels(dock).includes(target) ? target : position === "center" ? dockPanels(dock)[0] : undefined;
  const nextDock = !dock ? incoming : destination ? replaceDockGroup(dock, destination, put) : put(dock);
  return activatePanel({ ...layout, dock: nextDock, panels: { ...layout.panels, [id]: { ...layout.panels[id], visible: true, position: "center" } } }, id);
}

/** 只改变分割比例，任何隐藏或拆出窗口的面板身份都不变。 */
export function resizeDockSplit(layout: WorkspaceLayout, splitId: string, ratio: number): WorkspaceLayout {
  const visit = (node: DockNode): DockNode => node.type === "group" ? node : node.id === splitId ? { ...node, ratio: clamp(ratio, .02, .98) } : { ...node, first: visit(node.first), second: visit(node.second) };
  return { ...layout, dock: layout.dock ? visit(layout.dock) : null };
}

/** 读取有界布局树，丢弃未知/重复面板及空节点，不信任保存内容中的节点编号或深度。 */
function readDock(value: unknown, used = new Set<PanelId>(), path = "root", depth = 0): DockNode | null {
  if (!value || typeof value !== "object" || depth > 10) return null;
  // 分开判断联合字段，避免损坏的混合节点绕过页签身份校验。
  const item = value as Record<string, unknown>;
  if (item.type === "group" && Array.isArray(item.tabs)) {
    const tabs = item.tabs.filter((id): id is PanelId => { if (!isPanel(id) || used.has(id)) return false; used.add(id); return true; });
    return tabs.length ? { type: "group", id: `${path}-g`, tabs, active: isPanel(item.active) && tabs.includes(item.active) ? item.active : tabs[0] } : null;
  }
  if (item.type !== "split") return null;
  const first = readDock(item.first, used, `${path}-a`, depth + 1), second = readDock(item.second, used, `${path}-b`, depth + 1);
  return first && second ? dockSplit(first, second, item.axis === "vertical" ? "vertical" : "horizontal", clamp(finite(item.ratio, .5), .02, .98), `${path}-s`) : first || second;
}

/** 把v4左右/下方规则一次性转成树；旧记录仍原样保留在旧存储键。 */
function legacyDock(panels: WorkspaceLayout["panels"]): DockNode | null {
  const ids = panelIds.filter(id => panels[id].visible && panels[id].position !== "floating").sort((a, b) => panels[a].order - panels[b].order);
  const primary = ids.find(id => id === "assistant" && panels[id].position !== "bottom") || ids.find(id => id === "workbench" && panels[id].position !== "bottom") || ids[0];
  let result: DockNode | null = primary ? dockGroup([primary]) : null;
  for (const id of ids.filter(id => id !== primary)) {
    const node = dockGroup([id]), position = panels[id].position;
    result = !result ? node : position === "left" ? dockSplit(node, result, "horizontal", .22) : position === "bottom" ? dockSplit(result, node, "vertical", .66) : dockSplit(result, node, "horizontal", .65);
  }
  return result;
}

/** 保存版本只规范布局资料，不读取或迁移聊天、正文和任务。 */
function readSnapshot(saved: unknown, legacy: unknown): LayoutSnapshot {
  const source = saved && typeof saved === "object" ? saved as Partial<WorkspaceLayout> : {};
  const old = legacy && typeof legacy === "object" ? legacy as Record<string, unknown> : {};
  const modern = (source as { version?: number }).version === 4 || source.version === 5;
  const panels = Object.fromEntries(panelIds.map(id => {
    const base = defaultWorkspaceLayout.panels[id], value = source.panels?.[id];
    const candidate = value?.position ?? (id === "assistant" ? old.assistantPosition : base.position);
    return [id, { ...base,
      visible: modern && typeof value?.visible === "boolean" ? value.visible : base.visible,
      position: modern && ["left", "right", "top", "bottom", "center", "floating"].includes(String(candidate)) ? candidate : base.position,
      x: finite(value?.x, finite(old[`${id}X`], base.x)), y: finite(value?.y, finite(old[`${id}Y`], base.y)),
      width: Math.max(1, finite(value?.width, base.width)), height: Math.max(1, finite(value?.height, base.height)), order: finite(value?.order, base.order),
    }];
  })) as WorkspaceLayout["panels"];
  let dock = source.version === 5 ? readDock(source.dock) : modern ? legacyDock(panels) : readDock(defaultWorkspaceLayout.dock);
  for (const id of panelIds) if (panels[id].visible && panels[id].position !== "floating" && !dockPanels(dock).includes(id)) dock = dock ? dockSplit(dock, dockGroup([id]), "horizontal") : dockGroup([id]);
  return { panels, dock, activePanel: isPanel(source.activePanel) ? source.activePanel : "assistant", bottomHeight: 280, conversationOverviewRatio: readConversationOverviewRatio(source.conversationOverviewRatio) };
}

/** 命名方案只含非递归快照，防止方案套方案导致记录膨胀；损坏方案不会覆盖有效工作区。 */
export function readWorkspaceLayout(saved: unknown, legacy: unknown): WorkspaceLayout {
  const snapshot = readSnapshot(saved, legacy), source = saved as Partial<WorkspaceLayout> | null;
  const templates: LayoutTemplate[] = [], ids = new Set<string>();
  if (Array.isArray(source?.templates)) for (const item of source.templates) {
    if (!item || typeof item.id !== "string" || !item.id || item.id.length > 100 || ids.has(item.id) || typeof item.name !== "string" || !item.name.trim() || !item.snapshot || typeof item.snapshot !== "object") continue;
    ids.add(item.id);
    templates.push({ id: item.id, name: item.name.trim().slice(0, 60), snapshot: readSnapshot({ ...item.snapshot, version: 5 }, null), nativePanels: Array.isArray(item.nativePanels) ? [...new Set(item.nativePanels.filter(isPanel))] : [] });
  }
  return { ...snapshot, version: 5, templates };
}

/** 浮动窗口始终约束在当前视口，窄屏下允许低于常用最小值以免内容出界。 */
export function fitFloatingRect(id: PanelId, rect: Rect, size: WorkspaceSize): Rect {
  const width = clamp(rect.width, minimumWidths[id], Math.max(1, size.width)), height = clamp(rect.height, minimumHeights[id], Math.max(1, size.height));
  return { width, height, x: clamp(rect.x, 0, size.width - width), y: clamp(rect.y, 0, size.height - height) };
}

type PanelRect = Rect & { floating: boolean; active: boolean; groupId?: string };
export type DockRegion = Rect & { id: string; tabs: PanelId[]; active: PanelId; adapted: boolean };
export type DockDivider = Rect & { id: string; axis: DockSplit["axis"]; ratio: number; minimum: number; maximum: number; span: number };

/** 只计算几何：空间足够时保持树；不足时先换分割方向，再临时合页签，不改保存的结构。 */
export function workspaceGeometry(layout: WorkspaceLayout, size: WorkspaceSize) {
  const width = Math.max(1, finite(size.width, 1)), height = Math.max(1, finite(size.height, 1));
  const panels: Partial<Record<PanelId, PanelRect>> = {}, groups: DockRegion[] = [], splitters: DockDivider[] = [], adaptations: string[] = [];
  const prune = (node: DockNode | null): DockNode | null => {
    if (!node) return null;
    if (node.type === "group") {
      const tabs = node.tabs.filter(id => layout.panels[id].visible && layout.panels[id].position !== "floating");
      return tabs.length ? { ...node, tabs, active: tabs.includes(node.active) ? node.active : tabs[0] } : null;
    }
    const first = prune(node.first), second = prune(node.second);
    return first && second ? { ...node, first, second } : first || second;
  };
  const minimum = (node: DockNode): WorkspaceSize => {
    if (node.type === "group") return { width: Math.max(...node.tabs.map(id => minimumWidths[id])), height: Math.max(...node.tabs.map(id => minimumHeights[id])) + (node.tabs.length > 1 ? tabHeight : 0) };
    const a = minimum(node.first), b = minimum(node.second);
    return node.axis === "horizontal" ? { width: a.width + b.width + gap, height: Math.max(a.height, b.height) } : { width: Math.max(a.width, b.width), height: a.height + b.height + gap };
  };
  // 子区域允许自行合页签时的最小占用；只在完整结构放不下时使用，避免过早合并整个工作区。
  const compactMinimum = (node: DockNode): WorkspaceSize => {
    const tabs = dockPanels(node);
    return { width: Math.max(...tabs.map(id => minimumWidths[id])), height: Math.max(...tabs.map(id => minimumHeights[id])) + (tabs.length > 1 ? tabHeight : 0) };
  };
  const renderGroup = (node: DockGroup, rect: Rect, adapted: boolean) => {
    const active = node.tabs.includes(layout.activePanel) && adapted ? layout.activePanel : node.active;
    groups.push({ ...rect, id: node.id, tabs: node.tabs, active, adapted });
    const header = node.tabs.length > 1 ? Math.min(tabHeight, Math.max(0, rect.height - 1)) : 0;
    for (const id of node.tabs) panels[id] = { x: rect.x, y: rect.y + header, width: rect.width, height: Math.max(1, rect.height - header), floating: false, active: id === active, groupId: node.id };
  };
  const place = (node: DockNode, rect: Rect) => {
    if (node.type === "group") { renderGroup(node, rect, false); return; }
    let a = minimum(node.first), b = minimum(node.second);
    const fits = (axis: DockSplit["axis"]) => axis === "horizontal" ? rect.width >= a.width + b.width + gap && rect.height >= Math.max(a.height, b.height) : rect.height >= a.height + b.height + gap && rect.width >= Math.max(a.width, b.width);
    let axis = node.axis;
    if (!fits(axis)) {
      const alternative = axis === "horizontal" ? "vertical" : "horizontal";
      if (fits(alternative)) { axis = alternative; adaptations.push(node.id); }
      else {
        // 先保留父分区，让子树局部适配；两边至少一个活动面板仍放不下时才合并父区。
        a = compactMinimum(node.first); b = compactMinimum(node.second);
        if (fits(axis) || fits(alternative)) {
          if (!fits(axis)) axis = alternative;
          adaptations.push(node.id);
        } else {
          adaptations.push(node.id);
          const tabs = dockPanels(node), fallback = tabs.includes(layout.activePanel) ? layout.activePanel : tabs[0];
          renderGroup({ type: "group", id: `adapted-${node.id}`, tabs, active: fallback }, rect, true); return;
        }
      }
    }
    const horizontal = axis === "horizontal", span = (horizontal ? rect.width : rect.height) - gap;
    const lower = (horizontal ? a.width : a.height) / span, upper = 1 - (horizontal ? b.width : b.height) / span;
    const ratio = clamp(node.ratio, lower, upper), length = span * ratio;
    const first = { ...rect, width: horizontal ? length : rect.width, height: horizontal ? rect.height : length };
    const divider = { x: rect.x + (horizontal ? length : 0), y: rect.y + (horizontal ? 0 : length), width: horizontal ? gap : rect.width, height: horizontal ? rect.height : gap };
    const second = { x: rect.x + (horizontal ? length + gap : 0), y: rect.y + (horizontal ? 0 : length + gap), width: horizontal ? span - length : rect.width, height: horizontal ? rect.height : span - length };
    splitters.push({ ...divider, id: node.id, axis, ratio, minimum: lower, maximum: upper, span });
    place(node.first, first); place(node.second, second);
  };
  const root = prune(layout.dock);
  if (root) place(root, { x: 0, y: 0, width, height });
  for (const id of panelIds) if (layout.panels[id].visible && layout.panels[id].position === "floating") panels[id] = { ...fitFloatingRect(id, layout.panels[id], { width, height }), floating: true, active: true };
  return { panels, groups, splitters, adaptations, collapsed: [] as PanelId[], primaryPanel: null, editor: { x: 0, y: 0, width, height } };
}

/** 交换位置保留内容身份，并把树中页签位置一并替换。 */
export function swapPanels(layout: WorkspaceLayout, id: PanelId, target: PanelId): WorkspaceLayout {
  const replace = (value: PanelId) => value === id ? target : value === target ? id : value;
  const visit = (node: DockNode): DockNode => node.type === "group" ? { ...node, tabs: node.tabs.map(replace), active: replace(node.active) } : { ...node, first: visit(node.first), second: visit(node.second) };
  return { ...layout, dock: layout.dock && visit(layout.dock), panels: { ...layout.panels, [id]: { ...layout.panels[target], visible: true }, [target]: { ...layout.panels[id], visible: true } }, activePanel: id };
}

/** 关闭区小于面板，只有主动拖入才收起；缩小浮动尺寸不会被当成删除内容。 */
export function closeDropRect(size: WorkspaceSize): Rect {
  const width = Math.min(280, size.width * .4), height = Math.min(58, size.height * .14);
  return { x: (size.width - width) / 2, y: size.height - height - 10, width, height };
}

/** 外沿停靠整个主区，区域四侧局部分割、中央合页签；Shift才交换，避免中央拖放含义冲突。 */
export function workspaceDropTarget(id: PanelId, rect: Rect, point: { x: number; y: number }, layout: WorkspaceLayout,
  size: WorkspaceSize, previous: DropPreview | null = null, native = false, swap = false): DropPreview | null {
  const inside = (box: Rect) => point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height;
  const close = closeDropRect(size);
  if (inside(close)) return { action: "close", rect: close };
  const outside = Math.max(-point.x, -point.y, point.x - size.width, point.y - size.height);
  if (!native && outside > 30) return { action: "detach", rect: fitFloatingRect(id, rect, size) };
  if (outside > 0) return null;
  const geometry = workspaceGeometry(layout, size);
  const preview = (position: Exclude<DockPosition, "floating">, target?: PanelId): DropPreview | null => {
    const next = workspaceGeometry(dropLayout(layout, id, position, target), size).panels[id];
    return next ? { action: "dock", position, target, rect: next } : null;
  };
  const outer = [ ["left", point.x], ["right", size.width - point.x], ["top", point.y], ["bottom", size.height - point.y] ] as const;
  const edge = [...outer].sort((a, b) => a[1] - b[1])[0];
  if (edge[1] < Math.min(22, size.width * .05, size.height * .05)) return preview(edge[0]);
  for (const group of geometry.groups) {
    if (!inside(group) || group.tabs.length === 1 && group.active === id) continue;
    const target = group.active === id ? group.tabs.find(item => item !== id) : group.active;
    if (!target) continue;
    if (swap) return { action: "swap", target, rect: group };
    const edges = [["left", (point.x - group.x) / group.width], ["right", (group.x + group.width - point.x) / group.width], ["top", (point.y - group.y) / group.height], ["bottom", (group.y + group.height - point.y) / group.height]] as const;
    const nearest = [...edges].sort((a, b) => a[1] - b[1])[0];
    return preview(nearest[1] < .25 ? nearest[0] : "center", target);
  }
  return null;
}

/** 比例不计入结构签名，调整分隔条后仍能识别所选结构。 */
export function dockSignature(node: DockNode | null): string {
  return !node ? "empty" : node.type === "group" ? `[${node.tabs.join(",")}]` : `${node.axis === "horizontal" ? "H" : "V"}(${dockSignature(node.first)}|${dockSignature(node.second)})`;
}

/** 外部窗口状态与停靠结构分开，应用时仍经过现有Electron打开/召回入口。 */
export function arrangementLayout(current: WorkspaceLayout, dock: DockNode | null, modes: Record<PanelId, PanelMode>, size: WorkspaceSize): WorkspaceLayout {
  const panels = Object.fromEntries(panelIds.map((id, index) => [id, { ...current.panels[id], visible: modes[id] !== "hidden", position: modes[id] === "floating" ? "floating" : "center",
    ...(modes[id] === "floating" ? fitFloatingRect(id, { x: size.width * (.06 + index * .16), y: size.height * (.05 + index * .08), width: size.width * .48, height: size.height * .7 }, size) : {}) }])) as WorkspaceLayout["panels"];
  return { ...current, panels, dock, activePanel: dockPanels(dock).includes(current.activePanel) ? current.activePanel : dockPanels(dock)[0] || current.activePanel };
}

export const layoutPresets = [
  { id: "focus", label: "专注聊天", note: "只保留对话" }, { id: "right", label: "结构在左", note: "结构与聊天" },
  { id: "left", label: "结构在右", note: "聊天与结构" }, { id: "writing", label: "聊天与正文", note: "结构、聊天、工程" },
  { id: "review", label: "审稿工作区", note: "结构、工程、批注" }, { id: "desk", label: "自由浮动", note: "所有面板均可自由摆放" },
] as const;
export type LayoutPreset = typeof layoutPresets[number]["id"] | "below";
/** 原工作台默认布局和恢复入口沿用相同的停靠几何。 */
export function presetLayout(current: WorkspaceLayout, preset: LayoutPreset, size: WorkspaceSize): WorkspaceLayout {
  const g = (id: PanelId) => dockGroup([id]);
  // 快捷起点限制结构栏的初始占宽；作者拖过分隔条后仍可自由改变，不施加永久最大宽度。
  const railRatio = clamp(size.width * .16, 200, 280) / Math.max(1, size.width - gap);
  let dock: DockNode | null = g("assistant");
  if (preset === "right") dock = dockSplit(g("navigation"), g("assistant"), "horizontal", railRatio);
  if (preset === "left") dock = dockSplit(g("assistant"), g("navigation"), "horizontal", 1 - railRatio);
  if (preset === "writing") dock = dockSplit(g("navigation"), dockSplit(g("assistant"), g("workbench"), "horizontal", .48), "horizontal", railRatio);
  if (preset === "review") dock = dockSplit(g("navigation"), dockSplit(g("workbench"), g("inspector"), "horizontal", .72), "horizontal", railRatio);
  if (preset === "below") dock = dockSplit(g("workbench"), g("assistant"), "vertical", .64);
  if (preset === "desk") dock = null;
  const visible = dockPanels(dock), modes = Object.fromEntries(panelIds.map(id => [id, preset === "desk" ? "floating" : visible.includes(id) ? "docked" : "hidden"])) as Record<PanelId, PanelMode>;
  return arrangementLayout(current, dock, modes, size);
}
