export const panelIds = ["navigation", "assistant", "inspector"] as const;
export type PanelId = typeof panelIds[number];
export type DockPosition = "left" | "right" | "bottom" | "floating";
export type Rect = { x: number; y: number; width: number; height: number };
export type PanelLayout = Rect & { visible: boolean; position: DockPosition; order: number };
export type WorkspaceLayout = { panels: Record<PanelId, PanelLayout>; bottomHeight: number };
export type WorkspaceSize = { width: number; height: number };
export type DropPreview = { action: "dock" | "close" | "detach"; position?: Exclude<DockPosition, "floating">; rect: Rect };
export const panelLabels: Record<PanelId, string> = { navigation: "小说结构", assistant: "墨宝对话", inspector: "批注与版本" };
export const layoutStorageKey = "inkflow.workspace-layout.v3";
const minimumWidths: Record<PanelId, number> = { navigation: 180, assistant: 280, inspector: 200 };
export const defaultWorkspaceLayout: WorkspaceLayout = {
  panels: {
    navigation: { visible: true, position: "left", x: 24, y: 24, width: 210, height: 520, order: 0 },
    assistant: { visible: true, position: "right", x: 260, y: 24, width: 350, height: 600, order: 1 },
    inspector: { visible: false, position: "right", x: 520, y: 48, width: 260, height: 480, order: 2 },
  },
  bottomHeight: 280,
};

const finite = (value: unknown, fallback: number) => typeof value === "number" && Number.isFinite(value) ? value : fallback;
const clamp = (value: number, minimum: number, maximum: number) => Math.max(Math.min(minimum, maximum), Math.min(maximum, value));

export function readWorkspaceLayout(saved: unknown, legacy: unknown): WorkspaceLayout {
  const source = saved && typeof saved === "object" ? saved as Partial<WorkspaceLayout> : {};
  const old = legacy && typeof legacy === "object" ? legacy as Record<string, unknown> : {};
  const panels = Object.fromEntries(panelIds.map(id => {
    const base = defaultWorkspaceLayout.panels[id];
    const value = source.panels?.[id];
    const candidate = value?.position ?? (id === "assistant" ? old.assistantPosition : base.position);
    return [id, {
      visible: typeof value?.visible === "boolean" ? value.visible : typeof old[`${id}Visible`] === "boolean" ? old[`${id}Visible`] : base.visible,
      position: ["left", "right", "bottom", "floating"].includes(String(candidate)) ? candidate : base.position,
      x: finite(value?.x, finite(old[`${id}X`], base.x)), y: finite(value?.y, finite(old[`${id}Y`], base.y)),
      width: Math.max(1, finite(value?.width, finite(old[`${id}Width`], base.width))),
      height: Math.max(1, finite(value?.height, finite(old[`${id}Height`], base.height))),
      order: finite(value?.order, base.order),
    }];
  })) as Record<PanelId, PanelLayout>;
  return { panels, bottomHeight: Math.max(1, finite(source.bottomHeight, 280)) };
}

export function fitFloatingRect(id: PanelId, rect: Rect, size: WorkspaceSize): Rect {
  const width = clamp(rect.width, minimumWidths[id], size.width);
  const height = clamp(rect.height, 180, size.height);
  return { width, height, x: clamp(rect.x, 0, size.width - width), y: clamp(rect.y, 0, size.height - height) };
}

export function workspaceGeometry(layout: WorkspaceLayout, size: WorkspaceSize) {
  const width = Math.max(1, size.width), height = Math.max(1, size.height), gap = 6;
  const editorMinimumHeight = 240, bottomMinimumHeight = 160;
  const ordered = panelIds.filter(id => layout.panels[id].visible).sort((a, b) => layout.panels[a].order - layout.panels[b].order);
  const sides: PanelId[] = [], bottom: PanelId[] = [], floating = new Set<PanelId>();
  let remaining = Math.max(0, width - Math.min(340, width));
  for (const id of ordered) {
    const position = layout.panels[id].position;
    if (position === "floating") floating.add(id);
    else if (position === "bottom") {
      if (height >= editorMinimumHeight + bottomMinimumHeight + gap && bottom.reduce((total, item) => total + minimumWidths[item] + gap, 0) + minimumWidths[id] <= width) bottom.push(id);
      else floating.add(id);
    } else if (remaining >= minimumWidths[id] + gap) { sides.push(id); remaining -= minimumWidths[id] + gap; }
    else floating.add(id);
  }
  const bottomHeight = bottom.length ? clamp(layout.bottomHeight, bottomMinimumHeight, Math.max(1, height - Math.min(editorMinimumHeight, height) - gap)) : 0;
  const bodyHeight = Math.max(1, height - (bottom.length ? bottomHeight + gap : 0));
  const surplus = sides.reduce((total, id) => total + Math.max(0, layout.panels[id].width - minimumWidths[id]), 0);
  const scale = surplus ? Math.min(1, remaining / surplus) : 1;
  const panels: Partial<Record<PanelId, Rect & { floating: boolean }>> = {};
  let left = 0, right = width;
  for (const id of sides) {
    const panelWidth = minimumWidths[id] + Math.max(0, layout.panels[id].width - minimumWidths[id]) * scale;
    const onLeft = layout.panels[id].position === "left";
    const x = onLeft ? left : right - panelWidth;
    panels[id] = { x, y: 0, width: panelWidth, height: bodyHeight, floating: false };
    if (onLeft) left += panelWidth + gap; else right -= panelWidth + gap;
  }
  const bottomSpace = width - gap * Math.max(0, bottom.length - 1);
  const minimumSum = bottom.reduce((total, id) => total + minimumWidths[id], 0);
  const weight = bottom.reduce((total, id) => total + Math.max(1, layout.panels[id].width - minimumWidths[id]), 0);
  let x = 0;
  for (const id of bottom) {
    const panelWidth = minimumWidths[id] + Math.max(0, bottomSpace - minimumSum) * Math.max(1, layout.panels[id].width - minimumWidths[id]) / weight;
    panels[id] = { x, y: height - bottomHeight, width: panelWidth, height: bottomHeight, floating: false };
    x += panelWidth + gap;
  }
  for (const id of floating) panels[id] = { ...fitFloatingRect(id, layout.panels[id], { width, height }), floating: true };
  return { panels, editor: { x: left, y: 0, width: Math.max(1, right - left), height: bodyHeight } };
}

export function closeDropRect(size: WorkspaceSize): Rect {
  const width = Math.min(280, size.width * .4), height = Math.min(64, size.height * .14);
  return { x: (size.width - width) / 2, y: size.height - height - 12, width, height };
}

export function dropLayout(layout: WorkspaceLayout, id: PanelId, position: Exclude<DockPosition, "floating">): WorkspaceLayout {
  const order = position === "left" ? Math.min(...panelIds.map(item => layout.panels[item].order)) - 1
    : Math.max(...panelIds.map(item => layout.panels[item].order)) + 1;
  return { ...layout, panels: { ...layout.panels, [id]: { ...layout.panels[id], visible: true, position, order } } };
}

export function workspaceDropTarget(id: PanelId, rect: Rect, point: { x: number; y: number }, layout: WorkspaceLayout,
  size: WorkspaceSize, previous: DropPreview | null = null, native = false): DropPreview | null {
  const inside = (box: Rect) => point.x >= box.x && point.x <= box.x + box.width && point.y >= box.y && point.y <= box.y + box.height;
  const close = closeDropRect(size);
  if (inside(close)) return { action: "close", rect: close };
  const outside = Math.max(-point.x, -point.y, point.x - size.width, point.y - size.height);
  const margin = Math.max(24, Math.min(size.width, size.height) * .045);
  if (!native && outside > margin) return { action: "detach", rect: fitFloatingRect(id, rect, size) };
  if (outside > margin * 2) return null;
  const overlap = (left: Rect, right: Rect) => Math.max(0, Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x))
    * Math.max(0, Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y));
  const visibleArea = overlap(rect, { x: 0, y: 0, ...size });
  if (!visibleArea) return null;
  const side = size.width * .3, bottom = size.height * .26;
  const zones = [
    { position: "bottom" as const, rect: { x: 0, y: size.height - bottom, width: size.width, height: bottom }, near: point.y > size.height * .68 },
    { position: "left" as const, rect: { x: 0, y: 0, width: side, height: size.height }, near: point.x < size.width * .4 },
    { position: "right" as const, rect: { x: size.width - side, y: 0, width: side, height: size.height }, near: point.x > size.width * .6 },
  ];
  const candidates = zones.map(zone => ({ ...zone, coverage: overlap(rect, zone.rect) / visibleArea,
    threshold: previous?.action === "dock" && previous.position === zone.position ? .3 : .45 }))
    .filter(zone => zone.near && zone.coverage >= zone.threshold);
  candidates.sort((a, b) => b.coverage - a.coverage);
  for (const candidate of candidates) {
    const destination = workspaceGeometry(dropLayout(layout, id, candidate.position), size).panels[id];
    if (destination && !destination.floating) return { action: "dock", position: candidate.position, rect: destination };
  }
  return null;
}

export function dockedWidth(id: PanelId, width: number, rect: Rect, editor: Rect, size: WorkspaceSize): number {
  return clamp(width, minimumWidths[id], Math.max(minimumWidths[id], rect.width + editor.width - Math.min(340, size.width)));
}
