import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode, RefObject } from "react";
import { activatePanel as activateLayoutPanel, closeDropRect, conversationSplitGeometry, conversationSplitKeyRatio, conversationSplitterHeight, defaultConversationOverviewRatio, presetLayout, swapPanels, defaultWorkspaceLayout, dropLayout, fitFloatingRect, layoutStorageKey, panelIds, panelLabels, readConversationOverviewRatio, readWorkspaceLayout, workspaceDropTarget, workspaceGeometry, resizeDockSplit, dockPanels } from "./workspaceLayout";
import type { DockPosition, DropPreview, LayoutPreset, PanelId, Rect, WorkspaceLayout, WorkspaceSize, LayoutTemplate } from "./workspaceLayout";
import { LayoutLibrary } from "./LayoutLibrary";
import { UiIcon } from "./UiIcon";
import "./workspace.css";

const rectStyle = (rect: Rect): CSSProperties => ({ left: rect.x, top: rect.y, width: rect.width, height: rect.height });
const detached = new URLSearchParams(window.location.search).get("workspace_panel");
export const detachedPanel = panelIds.includes(detached as PanelId) ? detached as PanelId : null;
/** v5另存新键，读取时兼容v4/v3，不覆盖原记录。 */
function loadLayout() {
  let saved: { version?: number } | null = null, legacy: unknown = null;
  try {
    saved = JSON.parse(localStorage.getItem(layoutStorageKey) || localStorage.getItem("inkflow.workspace-layout.v4") || localStorage.getItem("inkflow.workspace-layout.v3") || "null");
    legacy = JSON.parse(localStorage.getItem("inkflow.workspace-layout.v2") || "null");
  } catch { /* 解析失败时用安全布局恢复界面，不读取或重置聊天及书稿。 */ }
  const layout = readWorkspaceLayout(saved, legacy);
  return saved?.version === 4 || saved?.version === 5 ? layout : presetLayout(layout, "right", { width: window.innerWidth, height: window.innerHeight });
}

/** 共用布局状态与一次一帧的几何绘制；窗口操作仍经过App/Electron守卫。 */
export function useWorkspaceLayout(area: RefObject<HTMLElement | null>, projectRoot: string,
  onDetach: (id: PanelId, bounds?: Rect) => Promise<boolean | void>, onReturn: (id: PanelId, hide?: boolean) => Promise<boolean | void>) {
  const [layout, setLayout] = useState(loadLayout), layoutRef = useRef(layout);
  const [history, setHistory] = useState<Array<{ layout: WorkspaceLayout; native: PanelId[] }>>([]);
  const [size, setSize] = useState({ width: Math.max(1, window.innerWidth), height: Math.max(1, window.innerHeight - 100) });
  const sizeRef = useRef(size); sizeRef.current = size;
  const [moving, setMoving] = useState<PanelId | null>(null), [resizing, setResizing] = useState(false);
  const [preview, setPreview] = useState<DropPreview | null>(null);
  const [nativePanels, setNativePanels] = useState<PanelId[]>([]), nativePanelsRef = useRef(nativePanels);
  nativePanelsRef.current = nativePanels;
  const [arranging, setArranging] = useState(false), arrangingRef = useRef(false);
  const [arrangementError, setArrangementError] = useState("");
  const detachRef = useRef(onDetach); detachRef.current = onDetach;
  const returnRef = useRef(onReturn); returnRef.current = onReturn;
  const pendingFrame = useRef<number | null>(null);
  const gesture = useRef<{ kind: "panel" | "split"; id: string; pointerId: number; x: number; y: number; rect: Rect; original: WorkspaceLayout; edge: string; moved: boolean; drop: DropPreview | null; detachedRect?: Rect; ratio?: number; axis?: "horizontal" | "vertical"; span?: number; minimum?: number; maximum?: number } | null>(null);
  const nativeDragRef = useRef<{ id: PanelId; drop: DropPreview | null } | null>(null);
  const geometryFor = (value: WorkspaceLayout, dimensions: WorkspaceSize) => {
    const panels = { ...value.panels };
    for (const id of nativePanelsRef.current) panels[id] = { ...panels[id], visible: false };
    return workspaceGeometry({ ...value, panels }, dimensions);
  };
  const geometry = geometryFor(layout, size);
  const structureKey = (value: typeof geometry) => value.groups.map(group => `${group.id}:${group.tabs.join(",")}:${group.active}`).join("|") + value.splitters.map(divider => `${divider.id}:${divider.axis}`).join("|");
  const renderedKey = useRef(structureKey(geometry)); renderedKey.current = structureKey(geometry);
  const update = (next: WorkspaceLayout) => { layoutRef.current = next; setLayout(next); };
  const remember = (value = layoutRef.current) => setHistory(old => [...old.slice(-29), { layout: value, native: [...nativePanelsRef.current] }]);
  const change = (next: WorkspaceLayout) => { remember(); update(next); };
  const updatePreview = (next: DropPreview | null) => setPreview(old => JSON.stringify(old) === JSON.stringify(next) ? old : next);
  const dockInput = (value: WorkspaceLayout) => ({ ...value, panels: Object.fromEntries(panelIds.map(id => [id, { ...value.panels[id], visible: value.panels[id].visible && !nativePanelsRef.current.includes(id) }])) as WorkspaceLayout["panels"] });

  useEffect(() => {
    // 切书后不能用旧书的手势快照或撤销记录覆盖新工作区。
    gesture.current = null; nativeDragRef.current = null;
    if (pendingFrame.current !== null) { window.cancelAnimationFrame(pendingFrame.current); pendingFrame.current = null; }
    setHistory([]); setMoving(null); setResizing(false); setPreview(null); document.body.classList.remove("workspace-interacting");
  }, [projectRoot]);
  useLayoutEffect(() => {
    const element = area.current; if (!element) return;
    let frame: number | null = null, settle: number | undefined;
    const measure = () => {
      frame = null;
      const width = Math.max(1, element.clientWidth), height = Math.max(1, element.clientHeight);
      setSize(old => old.width === width && old.height === height ? old : { width, height });
    };
    const schedule = () => {
      // 原生窗口缩放不追赶180ms几何过渡；同一帧的多个观察回调只测量一次。
      element.dataset.viewportResizing = "true";
      window.clearTimeout(settle); settle = window.setTimeout(() => { delete element.dataset.viewportResizing; }, 140);
      if (frame === null) frame = window.requestAnimationFrame(measure);
    };
    measure(); const observer = new ResizeObserver(schedule); observer.observe(element);
    return () => { observer.disconnect(); if (frame !== null) window.cancelAnimationFrame(frame); window.clearTimeout(settle); delete element.dataset.viewportResizing; };
  }, [area, projectRoot]);
  useEffect(() => {
    if (detachedPanel || moving || resizing) return;
    const timer = window.setTimeout(() => { try { localStorage.setItem(layoutStorageKey, JSON.stringify(layout)); } catch { setArrangementError("布局暂未写入本机存储，当前面板仍可继续使用。"); } }, 150);
    return () => window.clearTimeout(timer);
  }, [layout, moving, resizing]);
  useEffect(() => {
    const save = () => { if (!detachedPanel) try { localStorage.setItem(layoutStorageKey, JSON.stringify(layoutRef.current)); } catch { /* 原工作区快照仍由App退出保存门禁负责。 */ } };
    window.addEventListener("beforeunload", save); return () => window.removeEventListener("beforeunload", save);
  }, []);

  useEffect(() => {
    // 只在跨过窄屏合组边界时更新React结构，其余拖动帧直接更新矩形。
    const draw = (next: WorkspaceLayout) => {
      const current = geometryFor(next, sizeRef.current);
      for (const id of panelIds) {
        const rect = current.panels[id], element = area.current?.querySelector<HTMLElement>(`[data-panel="${id}"]`);
        if (!element) continue;
        const hidden = !rect || !rect.active || nativePanelsRef.current.includes(id);
        element.hidden = hidden; element.inert = hidden;
        if (rect) { Object.assign(element.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px`, zIndex: gesture.current?.id === id ? "25" : rect.floating ? next.activePanel === id ? "24" : "22" : "1" }); element.dataset.floating = String(rect.floating); }
      }
      area.current?.querySelectorAll<HTMLElement>("[data-dock-group]").forEach(element => {
        const group = current.groups.find(item => item.id === element.dataset.dockGroup); element.hidden = !group || group.tabs.length < 2;
        if (group) Object.assign(element.style, { left: `${group.x}px`, top: `${group.y}px`, width: `${group.width}px` });
      });
      area.current?.querySelectorAll<HTMLElement>("[data-dock-split]").forEach(element => {
        const divider = current.splitters.find(item => item.id === element.dataset.dockSplit); element.hidden = !divider;
        if (divider) Object.assign(element.style, { left: `${divider.x}px`, top: `${divider.y}px`, width: `${divider.width}px`, height: `${divider.height}px` });
      });
      if (renderedKey.current !== structureKey(current)) { renderedKey.current = structureKey(current); setLayout(next); }
    };
    const move = (event: globalThis.PointerEvent) => {
      const current = gesture.current; if (!current || current.pointerId !== event.pointerId) return;
      const dx = event.clientX - current.x, dy = event.clientY - current.y;
      if (!current.moved && Math.hypot(dx, dy) < 4) return;
      current.moved = true;
      let next: WorkspaceLayout;
      if (current.kind === "split") {
        const delta = current.axis === "horizontal" ? dx : dy;
        const ratio = Math.max(current.minimum!, Math.min(current.maximum!, current.ratio! + delta / current.span!));
        next = resizeDockSplit(current.original, current.id, ratio);
      } else {
        const id = current.id as PanelId, panel = current.original.panels[id], bounds = area.current?.getBoundingClientRect();
        let raw = { ...current.rect, x: current.rect.x + dx, y: current.rect.y + dy };
        if (current.edge) {
          const width = current.rect.width + (current.edge.includes("w") ? -dx : current.edge.includes("e") ? dx : 0);
          const height = current.rect.height + (current.edge.includes("n") ? -dy : current.edge.includes("s") ? dy : 0);
          const fitted = fitFloatingRect(id, { ...current.rect, width, height }, sizeRef.current);
          raw = { ...fitted, x: current.rect.x + (current.edge.includes("w") ? current.rect.width - fitted.width : 0), y: current.rect.y + (current.edge.includes("n") ? current.rect.height - fitted.height : 0) };
        }
        next = { ...current.original, activePanel: id, panels: { ...current.original.panels, [id]: { ...panel, ...fitFloatingRect(id, raw, sizeRef.current), position: "floating" } } };
        // 拖动时仍以原分组寻找放入目标，避免自己移出后目标区域在指针下不停移动。
        current.drop = !current.edge && bounds ? workspaceDropTarget(id, raw, { x: event.clientX - bounds.left, y: event.clientY - bounds.top }, dockInput(current.original), sizeRef.current, current.drop, false, event.shiftKey) : null;
        if (bounds) current.detachedRect = { ...raw, x: bounds.left + raw.x, y: bounds.top + raw.y };
        updatePreview(current.drop);
      }
      layoutRef.current = next;
      if (pendingFrame.current === null) pendingFrame.current = window.requestAnimationFrame(() => { pendingFrame.current = null; draw(layoutRef.current); });
    };
    const finish = (event: Event) => {
      const current = gesture.current; if (!current) return;
      if (event instanceof globalThis.PointerEvent && event.pointerId !== current.pointerId) return;
      if (pendingFrame.current !== null) { window.cancelAnimationFrame(pendingFrame.current); pendingFrame.current = null; }
      let next = current.original;
      if (current.moved && event.type === "pointerup") {
        remember(current.original); next = layoutRef.current;
        const id = current.id as PanelId;
        if (current.kind === "panel" && current.drop?.action === "dock") next = dropLayout(current.original, id, current.drop.position!, current.drop.target);
        else if (current.kind === "panel" && current.drop?.action === "swap") next = swapPanels(current.original, id, current.drop.target!);
        else if (current.kind === "panel" && current.drop?.action === "close") next = { ...current.original, panels: { ...current.original.panels, [id]: { ...current.original.panels[id], visible: false } } };
        if (current.kind === "panel" && current.drop?.action === "detach") void detachRef.current(id, current.detachedRect);
      }
      draw(next); update(next); gesture.current = null; setMoving(null); setResizing(false); setPreview(null); document.body.classList.remove("workspace-interacting");
    };
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape" && gesture.current) { event.preventDefault(); finish(event); } };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", finish); window.addEventListener("pointercancel", finish); window.addEventListener("blur", finish); window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", finish); window.removeEventListener("pointercancel", finish); window.removeEventListener("blur", finish); window.removeEventListener("keydown", escape); if (pendingFrame.current !== null) window.cancelAnimationFrame(pendingFrame.current); document.body.classList.remove("workspace-interacting"); };
  }, [area]);

  /** 标题拖动只移动面板；分隔条负责所有停靠比例，二者不再争用宽高。 */
  const start = (id: PanelId, event: PointerEvent<HTMLElement>, edge = "") => {
    if (event.button !== 0 || detachedPanel || arrangingRef.current) return;
    const rect = geometryFor(layoutRef.current, sizeRef.current).panels[id]; if (!rect) return;
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); setMoving(id);
    gesture.current = { kind: "panel", id, pointerId: event.pointerId, x: event.clientX, y: event.clientY, rect, original: layoutRef.current, edge, moved: false, drop: null };
    document.body.classList.add("workspace-interacting");
  };
  /** 分割线独立捕获指针，比例按当前实际方向和最小内容尺寸约束。 */
  const startSplit = (id: string, event: PointerEvent<HTMLElement>) => {
    if (event.button !== 0 || detachedPanel || arrangingRef.current) return;
    const divider = geometryFor(layoutRef.current, sizeRef.current).splitters.find(item => item.id === id); if (!divider) return;
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); setResizing(true);
    gesture.current = { ...divider, kind: "split", pointerId: event.pointerId, x: event.clientX, y: event.clientY, rect: divider, original: layoutRef.current, edge: "", moved: false, drop: null };
    document.body.classList.add("workspace-interacting");
  };
  const activate = (id: PanelId) => { if (!arrangingRef.current) update(activateLayoutPanel(layoutRef.current, id)); };
  const setPosition = (id: PanelId, position: DockPosition, target?: PanelId) => {
    if (arrangingRef.current) return;
    const current = layoutRef.current;
    if (position !== "floating") { change(dropLayout(current, id, position, target)); return; }
    const rect = geometryFor(current, sizeRef.current).panels[id] || current.panels[id];
    const floating = fitFloatingRect(id, { x: size.width * .07, y: size.height * .07, width: Math.min(rect.width, size.width * .86), height: Math.min(rect.height, size.height * .86) }, size);
    change({ ...current, activePanel: id, panels: { ...current.panels, [id]: { ...current.panels[id], ...floating, visible: true, position } } });
  };
  const toggle = (id: PanelId, visible = !layoutRef.current.panels[id].visible) => {
    if (arrangingRef.current) return;
    const current = layoutRef.current;
    let next: WorkspaceLayout = { ...current, panels: { ...current.panels, [id]: { ...current.panels[id], visible } } };
    if (visible && current.panels[id].position !== "floating" && !dockPanels(current.dock).includes(id)) next = dropLayout(next, id, "right");
    if (visible) next = activateLayoutPanel(next, id);
    change(next);
  };
  const markNative = (id: PanelId, open: boolean, restore = true) => {
    // IPC回执与主动召回可能先后抵达，同一返回不应再次改动新布局或切换页签。
    if (!open && !nativePanelsRef.current.includes(id)) return;
    const ids = [...nativePanelsRef.current.filter(item => item !== id), ...(open ? [id] : [])]; nativePanelsRef.current = ids; setNativePanels(ids);
    if (!open) {
      const current = layoutRef.current;
      let next = { ...current, panels: { ...current.panels, [id]: { ...current.panels[id], visible: restore } } };
      if (restore && current.panels[id].position !== "floating" && !dockPanels(current.dock).includes(id)) next = dropLayout(next, id, "right");
      update(restore ? activateLayoutPanel(next, id) : next);
    }
    if (nativeDragRef.current?.id === id) { nativeDragRef.current = null; setMoving(null); setPreview(null); }
  };
  /** 模板应用按顺序召回/拆出窗口，失败显示实际断点，不假装全部应用成功。 */
  const applyArrangement = async (next: WorkspaceLayout, native: PanelId[] = []) => {
    if (arrangingRef.current) return false;
    arrangingRef.current = true; setArranging(true); setArrangementError(""); remember();
    try {
      for (const id of [...nativePanelsRef.current]) if (!native.includes(id) && await returnRef.current(id, !next.panels[id].visible) === false) throw new Error("有面板尚未返回主界面。");
      for (const id of native) if (!nativePanelsRef.current.includes(id) && await detachRef.current(id) === false) throw new Error("有独立窗口尚未打开。");
      update(next); return true;
    } catch (cause) { setArrangementError(`${cause instanceof Error ? cause.message : String(cause)} 已完成的窗口调整保留，可以撤销或再次选择布局。`); return false; }
    finally { arrangingRef.current = false; setArranging(false); }
  };
  const nativeDrag = (value: Record<string, unknown>) => {
    const id = value.panel as PanelId, bounds = area.current?.getBoundingClientRect();
    if (detachedPanel || arrangingRef.current || !bounds || !panelIds.includes(id) || !nativePanelsRef.current.includes(id)) return;
    const rect = value.rect as Rect, origin = value.origin as { x: number; y: number }, point = value.point as { x: number; y: number };
    if (!rect || !origin || !point || ![rect.x, rect.y, rect.width, rect.height, origin.x, origin.y, point.x, point.y].every(Number.isFinite)) return;
    const scale = typeof value.scale === "number" && value.scale > 0 ? value.scale : 1;
    const local = { x: (rect.x - origin.x) / scale - bounds.left, y: (rect.y - origin.y) / scale - bounds.top, width: rect.width / scale, height: rect.height / scale };
    const drop = value.phase === "cancel" ? null : workspaceDropTarget(id, local, { x: (point.x - origin.x) / scale - bounds.left, y: (point.y - origin.y) / scale - bounds.top }, dockInput(layoutRef.current), sizeRef.current, nativeDragRef.current?.drop, true);
    if (value.phase === "move") { nativeDragRef.current = { id, drop }; setMoving(id); updatePreview(drop); return; }
    nativeDragRef.current = null; setMoving(null); setPreview(null);
    if (value.phase === "drop" && drop) {
      remember();
      if (drop.action === "dock") update(dropLayout(layoutRef.current, id, drop.position!, drop.target));
      else if (drop.action === "swap") update(swapPanels(layoutRef.current, id, drop.target!));
      else if (drop.action === "close") update({ ...layoutRef.current, panels: { ...layoutRef.current.panels, [id]: { ...layoutRef.current.panels[id], visible: false } } });
      void returnRef.current(id, drop.action === "close");
    }
  };
  const undo = history.length ? async () => {
    const saved = history.at(-1)!;
    if (await applyArrangement(saved.layout, saved.native)) setHistory(history.slice(0, -1));
  } : null;
  const keyMove = (id: PanelId, event: KeyboardEvent<HTMLButtonElement>) => {
    if (arrangingRef.current || detachedPanel) return;
    const direction = ({ ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] } as Record<string, number[]>)[event.key]; if (!direction) return;
    event.preventDefault();
    if (event.altKey) { setPosition(id, ({ ArrowLeft: "left", ArrowRight: "right", ArrowUp: "top", ArrowDown: "bottom" } as const)[event.key as "ArrowLeft"]); return; }
    const current = layoutRef.current, rect = geometryFor(current, size).panels[id] || current.panels[id];
    const next = fitFloatingRect(id, event.shiftKey ? { ...rect, width: rect.width + direction[0], height: rect.height + direction[1] } : { ...rect, x: rect.x + direction[0], y: rect.y + direction[1] }, size);
    change({ ...current, activePanel: id, panels: { ...current.panels, [id]: { ...current.panels[id], ...next, position: "floating" } } });
  };
  const resizeSplit = (id: string, ratio: number) => { if (!arrangingRef.current) change(resizeDockSplit(layoutRef.current, id, ratio)); };
  const saveTemplate = (name: string) => {
    if (arrangingRef.current) return false;
    const title = name.trim(); if (!title || title.length > 60) { setArrangementError("方案名称需为1至60个字符。"); return false; }
    const { version, templates, ...snapshot } = layoutRef.current;
    const template: LayoutTemplate = { id: `layout-${crypto.randomUUID()}`, name: title, snapshot, nativePanels: [...nativePanelsRef.current] };
    change({ ...layoutRef.current, templates: [...templates, template] }); return true;
  };
  return { layout, geometry, size, moving, resizing, preview, active: layout.activePanel, nativePanels, arranging, arrangementError,
    restore: (value: unknown) => update(readWorkspaceLayout(value, null)), remember, start, startSplit, resizeSplit, setPosition, toggle, markNative, nativeDrag, activatePanel: activate,
    setActive: activate, keyMove, undo, applyArrangement, saveTemplate,
    removeTemplate: (id: string) => { if (!arrangingRef.current) change({ ...layoutRef.current, templates: layoutRef.current.templates.filter(item => item.id !== id) }); },
    applyTemplate: (template: LayoutTemplate) => applyArrangement({ ...template.snapshot, version: 5, templates: layoutRef.current.templates }, template.nativePanels),
    applyPreset: (preset: LayoutPreset) => applyArrangement(presetLayout(layoutRef.current, preset, size)),
    reset: () => applyArrangement(presetLayout({ ...readWorkspaceLayout(defaultWorkspaceLayout, null), templates: layoutRef.current.templates }, "right", size)),
    setConversationOverviewRatio: (value: number, rememberChange = true) => { const next = { ...layoutRef.current, conversationOverviewRatio: readConversationOverviewRatio(value) }; if (rememberChange) change(next); else update(next); return next; },
  };
}
export type WorkspaceController = ReturnType<typeof useWorkspaceLayout>;

export function ConversationSplit({ ratio, onRatioChange, overview, overviewSummary = "章节进度", children }: {
  ratio: number; onRatioChange: (ratio: number) => void; overview: ReactNode; overviewSummary?: string; children: ReactNode;
}) {
  const id = useId();
  const area = useRef<HTMLDivElement | null>(null);
  const separator = useRef<HTMLDivElement | null>(null);
  const [height, setHeight] = useState(0);
  const [preview, setPreview] = useState<number | null>(null);
  const [overviewExpanded, setOverviewExpanded] = useState(false);
  const drag = useRef<{ pointerId: number; y: number; ratio: number; moved: boolean; followMessages: boolean } | null>(null);
  const previewRef = useRef<number | null>(null);
  const bounds = conversationSplitGeometry(preview ?? ratio, height);
  const changePreview = (value: number | null) => { previewRef.current = value; setPreview(value); };
  const cancel = () => {
    const current = drag.current;
    drag.current = null; changePreview(null);
    if (current && separator.current?.hasPointerCapture(current.pointerId)) separator.current.releasePointerCapture(current.pointerId);
  };

  useLayoutEffect(() => {
    const element = area.current;
    if (!element) return;
    const measure = () => setHeight(element.clientHeight);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [Boolean(overview)]);
  useLayoutEffect(() => {
    if (!drag.current?.followMessages) return;
    const messages = area.current?.querySelector<HTMLElement>(".messages");
    if (messages) messages.scrollTop = messages.scrollHeight;
  }, [preview, height]);
  useEffect(() => {
    window.addEventListener("blur", cancel);
    return () => window.removeEventListener("blur", cancel);
  }, []);

  // Keep the message subtree mounted when an asynchronously loaded overview appears.
  return <div className="conversation-split" ref={area} data-resizing={preview !== null}
    style={{ gridTemplateRows: overview ? overviewExpanded ? `minmax(0, ${bounds.ratio}fr) ${conversationSplitterHeight}px minmax(0, ${1 - bounds.ratio}fr)` : "auto minmax(0, 1fr)" : "minmax(0, 1fr)" }}>
    {overview && <div id={`${id}-overview`} className="conversation-overview" data-expanded={overviewExpanded} role="region" aria-label="批次进度概览">
      <button type="button" className="conversation-overview-toggle" aria-expanded={overviewExpanded} aria-controls={`${id}-progress`} onClick={() => { cancel(); setOverviewExpanded(current => !current); }}><span>{overviewSummary}</span><small>{overviewExpanded ? "收起进度 ▴" : "展开进度 ▾"}</small></button>
      <div id={`${id}-progress`} className="conversation-overview-content" hidden={!overviewExpanded}>{overview}</div>
    </div>}
    {overview && overviewExpanded && <div className="conversation-splitter" ref={separator} role="separator" tabIndex={0} aria-orientation="horizontal"
      aria-label="调整批次概览和对话区的高度" aria-controls={`${id}-overview`}
      aria-valuemin={Math.round(bounds.minimum * 100)} aria-valuemax={Math.round(bounds.maximum * 100)}
      aria-valuenow={Math.round(bounds.ratio * 100)} aria-valuetext={`任务区 ${Math.round(bounds.ratio * 100)}%，消息区 ${Math.round((1 - bounds.ratio) * 100)}%`}
      title="上下拖动调整比例；方向键微调，Shift 加方向键快调，Home/End 到边界，Enter 或双击恢复默认，Esc 撤销拖动"
      onPointerDown={event => {
        if (event.button !== 0 || !event.isPrimary) return;
        event.preventDefault(); event.stopPropagation();
        event.currentTarget.focus({ preventScroll: true });
        event.currentTarget.setPointerCapture(event.pointerId);
        const messages = area.current?.querySelector<HTMLElement>(".messages");
        drag.current = { pointerId: event.pointerId, y: event.clientY, ratio: bounds.ratio, moved: false,
          followMessages: Boolean(messages && messages.scrollHeight - messages.scrollTop - messages.clientHeight < 48) };
        changePreview(bounds.ratio);
      }}
      onPointerMove={event => {
        const current = drag.current;
        if (!current || current.pointerId !== event.pointerId || !bounds.available) return;
        current.moved ||= Math.abs(event.clientY - current.y) > 1;
        changePreview(conversationSplitGeometry(current.ratio + (event.clientY - current.y) / bounds.available, height).ratio);
      }}
      onPointerUp={event => {
        const current = drag.current;
        if (!current || current.pointerId !== event.pointerId) return;
        const next = previewRef.current;
        cancel();
        if (current.moved && next !== null) onRatioChange(next);
      }}
      onPointerCancel={cancel} onLostPointerCapture={cancel}
      onDoubleClick={() => onRatioChange(defaultConversationOverviewRatio)}
      onKeyDown={event => {
        if (event.key === "Escape" && drag.current) { event.preventDefault(); event.stopPropagation(); cancel(); return; }
        if (event.altKey || event.ctrlKey || event.metaKey || drag.current) return;
        const next = conversationSplitKeyRatio(ratio, event.key, height, event.shiftKey);
        if (next === null) return;
        event.preventDefault(); event.stopPropagation(); onRatioChange(next);
      }}><span className="conversation-splitter-grip" aria-hidden="true" /></div>}
    <div id={`${id}-messages`} className="conversation-message-region" role="region" aria-label="对话消息">{children}</div>
  </div>;
}

/** 原生顶层菜单共用锚点、关闭与动效，供面板和聊天管理复用。 */
export function WorkspacePopover({ label, className, icon, children, disabled = false }: { label: string; className: string; icon: ReactNode; children: ReactNode; disabled?: boolean }) {
  const id = useId();
  const [point, setPoint] = useState({ left: 0, top: 0 });
  return <><button className={className} type="button" disabled={disabled} popoverTarget={id} title={label} aria-label={label} onClick={event => {
    const rect = event.currentTarget.getBoundingClientRect();
    const width = className === "workspace-layout-button" ? 356 : 230;
    setPoint({ left: Math.max(8, Math.min(window.innerWidth - width - 8, rect.right - width)), top: Math.max(8, Math.min(window.innerHeight - 250, rect.bottom + 6)) });
  }}>{icon}</button><div id={id} popover="auto" className={`workspace-menu-content workspace-popover${className === "workspace-layout-button" ? " workspace-layout-menu" : ""}`} style={{ ...point, maxHeight: Math.max(80, Math.min(520, window.innerHeight - point.top - 8)) }} onClick={event => { if ((event.target as HTMLElement).closest("button:not(:disabled)")) event.currentTarget.hidePopover(); }}>{children}</div></>;
}

/** 渲染页签与分隔条，保留面板内容状态；空工作区仍可通过原服务恢复。 */
export function WorkspaceChrome({ workspace }: { workspace: WorkspaceController }) {
  if (detachedPanel) return null;
  return <>
    {workspace.geometry.groups.filter(group => group.tabs.length > 1).map(group => <nav key={group.id} className="workspace-group-tabs" data-dock-group={group.id} data-adapted={group.adapted} style={{ left: group.x, top: group.y, width: group.width }} role="tablist" aria-label={group.adapted ? "窄窗口临时合并的面板" : "同区域面板页签"}>{group.tabs.map((id, index) => <button key={id} type="button" role="tab" tabIndex={group.active === id ? 0 : -1} aria-selected={group.active === id} aria-controls={`workspace-panel-${id}`} onClick={() => workspace.activatePanel(id)} onKeyDown={event => {
      const target = event.key === "Home" ? 0 : event.key === "End" ? group.tabs.length - 1 : event.key === "ArrowLeft" ? (index + group.tabs.length - 1) % group.tabs.length : event.key === "ArrowRight" ? (index + 1) % group.tabs.length : null;
      if (target === null) return; event.preventDefault(); workspace.activatePanel(group.tabs[target]);
      (event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('button[role="tab"]')[target])?.focus();
    }}>{panelLabels[id]}</button>)}</nav>)}
    {workspace.geometry.splitters.map(divider => <div key={divider.id} className="workspace-dock-splitter" data-dock-split={divider.id} data-axis={divider.axis} style={rectStyle(divider)} role="separator" tabIndex={0} aria-label="调整相邻区域比例" aria-orientation={divider.axis === "horizontal" ? "vertical" : "horizontal"} aria-valuemin={Math.round(divider.minimum * 100)} aria-valuemax={Math.round(divider.maximum * 100)} aria-valuenow={Math.round(divider.ratio * 100)} title="拖动调整比例；方向键微调，Shift快调，双击恢复均分，Esc撤销拖动" onPointerDown={event => workspace.startSplit(divider.id, event)} onDoubleClick={() => workspace.resizeSplit(divider.id, .5)} onKeyDown={event => {
      const backward = divider.axis === "horizontal" ? "ArrowLeft" : "ArrowUp", forward = divider.axis === "horizontal" ? "ArrowRight" : "ArrowDown";
      if (![backward, forward, "Home", "End", "Enter"].includes(event.key)) return;
      event.preventDefault(); const ratio = event.key === "Home" ? divider.minimum : event.key === "End" ? divider.maximum : event.key === "Enter" ? .5 : divider.ratio + (event.key === backward ? -1 : 1) * (event.shiftKey ? .1 : .02);
      workspace.resizeSplit(divider.id, Math.max(divider.minimum, Math.min(divider.maximum, ratio)));
    }} />)}
    {!Object.keys(workspace.geometry.panels).length && <div className="workspace-empty"><strong>主界面暂未放置面板</strong><button type="button" disabled={workspace.arranging} onClick={() => void workspace.reset()}>恢复默认布局</button>{workspace.arrangementError && <p role="alert">{workspace.arrangementError}</p>}</div>}
  </>;
}

/** 工作台只提供建书与布局两项入口，复用原菜单关闭行为和布局服务。 */
export function WorkspaceActions({ workspace, disabled, onCreate }: { workspace: WorkspaceController; disabled: boolean; onCreate: () => void }) {
  const [libraryOpen, setLibraryOpen] = useState(false);
  return <>
    <WorkspacePopover label="工作台" className="workspace-actions-button" disabled={disabled || workspace.arranging} icon={<><UiIcon name="layout" /><span>工作台</span><UiIcon name="chevron" /></>}>
      <button type="button" disabled={disabled || workspace.arranging} onClick={onCreate}>新建小说 <UiIcon name="plus" /></button>
      <button type="button" disabled={disabled || workspace.arranging} onClick={() => setLibraryOpen(true)}>调整布局 <UiIcon name="layout" /></button>
    </WorkspacePopover>
    <LayoutLibrary open={libraryOpen} onClose={() => setLibraryOpen(false)} workspace={workspace} />
  </>;
}

export function WorkspacePanel({ id, workspace, className, actions, onDetach, children, inactive = false }: { id: PanelId; workspace: WorkspaceController; className: string; actions?: ReactNode; onDetach: (id: PanelId) => void; children: ReactNode; inactive?: boolean }) {
  const panel = workspace.layout.panels[id], detachedHere = detachedPanel === id;
  const rect = detachedHere ? { x: 0, y: 0, ...workspace.size, floating: false, active: true, groupId: undefined } : workspace.geometry.panels[id];
  const hidden = detachedPanel ? !detachedHere : !panel.visible || workspace.nativePanels.includes(id) || !rect || !rect.active;
  const tabbed = Boolean(rect?.groupId && (workspace.geometry.groups.find(group => group.id === rect.groupId)?.tabs.length || 0) > 1);
  const element = useRef<HTMLElement>(null);
  // display退出过渡仍需原矩形，隐藏时不让定位瞬间回到auto。
  const lastRect = useRef(rect);
  if (rect) lastRect.current = rect;
  const displayRect = rect || lastRect.current;
  // 查看工程后键盘进入其标题，Esc可立即收起；普通面板不抢走聊天输入焦点。
  useLayoutEffect(() => {
    if (id !== "workbench" || hidden || tabbed) return;
    const previous = document.activeElement as HTMLElement | null;
    element.current?.querySelector<HTMLElement>(".workspace-title-handle")?.focus({ preventScroll: true });
    return () => { if (element.current?.contains(document.activeElement) && previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, [id, hidden, tabbed]);
  const edges = rect?.floating ? ["n", "e", "s", "w", "ne", "se", "sw", "nw"] : [];
  return <section id={`workspace-panel-${id}`} ref={element} className={`workspace-panel ${className}`} role={tabbed ? "tabpanel" : undefined} aria-label={panelLabels[id]} hidden={hidden} inert={inactive || hidden} style={displayRect ? { ...rectStyle(displayRect), zIndex: workspace.moving === id || (displayRect.floating && workspace.active === id) ? 24 : displayRect.floating ? 22 : 1 } : undefined} data-floating={rect?.floating} data-tabbed={tabbed} data-panel={id} onPointerDown={() => workspace.setActive(id)} onKeyDown={event => {
    if (id !== "workbench" || event.key !== "Escape" || event.defaultPrevented || workspace.moving || document.querySelector("[popover]:popover-open")) return;
    event.preventDefault(); event.stopPropagation();
    if (detachedHere) void window.inkflow.returnWorkspacePanel(); else workspace.toggle(id, false);
  }}>
    <header className="workspace-panel-heading"><button className="workspace-title-handle" title={detachedHere ? "拖动标题移动系统窗口；返回后可放入主界面任意区域" : "拖动标题放入任意区域；方向键移动，Shift加方向键缩放，Alt加方向键放到主界面四侧"} onPointerDown={event => workspace.start(id, event)} onKeyDown={event => workspace.keyMove(id, event)}><span className="panel-grip" aria-hidden="true">⠿</span>{panelLabels[id]}</button><div className="workspace-panel-actions">{actions}{detachedHere ? <button className="panel-icon" title="回到主工作台" aria-label="回到主工作台" onClick={() => void window.inkflow.returnWorkspacePanel()}>↙</button> : <WorkspacePopover label={`${panelLabels[id]}的移动选项`} className="panel-icon" icon="⋯">{(["left", "right", "top", "bottom", "center", "floating"] as DockPosition[]).map(position => <button key={position} disabled={workspace.arranging} onClick={() => workspace.setPosition(id, position)}>{({ left: "放到主界面左侧", right: "放到主界面右侧", top: "放到主界面上方", bottom: "放到主界面下方", center: "回到主界面页签", floating: "自由浮动" })[position]}</button>)}
      <hr /><small>放入指定区域</small><div className="workspace-docking-targets">{panelIds.filter(target => target !== id && workspace.layout.panels[target].visible && !workspace.nativePanels.includes(target) && workspace.layout.panels[target].position !== "floating").map(target => <div key={target}><strong>{panelLabels[target]}</strong><div>{(["left", "right", "top", "bottom", "center"] as const).map(position => <button key={position} type="button" title={`${panelLabels[id]}放到${panelLabels[target]}${({ left: "左侧", right: "右侧", top: "上方", bottom: "下方", center: "的页签" })[position]}`} onClick={() => workspace.setPosition(id, position, target)}>{({ left: "左", right: "右", top: "上", bottom: "下", center: "页签" })[position]}</button>)}</div></div>)}</div><hr /><button onClick={() => onDetach(id)}>移到独立窗口 ↗</button></WorkspacePopover>}<button className="panel-icon panel-close" title="关闭面板" aria-label={`关闭${panelLabels[id]}`} onClick={() => detachedHere ? void window.inkflow.returnWorkspacePanel() : workspace.toggle(id, false)}>×</button></div></header>
    <div className="workspace-panel-content">{children}</div>
    {!detachedHere && edges.map(edge => <div key={edge} className={`panel-resize-edge edge-${edge}`} role="separator" aria-orientation={edge === "n" || edge === "s" ? "horizontal" : "vertical"} aria-label={`调整${panelLabels[id]}大小`} onPointerDown={event => workspace.start(id, event, edge)} />)}
  </section>;
}

export function WorkspaceDropPreview({ workspace }: { workspace: WorkspaceController }) {
  if (!workspace.moving) return null;
  const preview = workspace.preview, label = panelLabels[workspace.moving];
  return <>
    <div className={`workspace-close-drop${preview?.action === "close" ? " active" : ""}`} style={rectStyle(closeDropRect(workspace.size))} aria-hidden="true">拖到这里收起面板</div>
    {preview && <div className={`workspace-drop-preview action-${preview.action}`} style={rectStyle(preview.rect)} role="status">
      <span>{preview.action === "close" ? `松开后收起${label}` : preview.action === "swap" ? `松开后与${panelLabels[preview.target!]}交换位置` : preview.action === "detach" ? "松开后移到独立窗口" : preview.position === "center" ? "松开后合并为页签" : `松开后放到${({ left: "左侧", right: "右侧", top: "上方", bottom: "下方", center: "页签组" })[preview.position || "center"]}`}</span>
      <small>{preview.action === "close" ? "可从顶端布局重新打开" : workspace.nativePanels.includes(workspace.moving) ? "区域四侧分割，中央合页签；松开返回主界面" : "区域四侧分割，中央合页签；按Shift交换"}</small>
    </div>}
  </>;
}
