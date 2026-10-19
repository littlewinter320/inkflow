import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode, RefObject } from "react";
import { closeDropRect, layoutPresets, presetLayout, shouldCollapse, swapPanels, defaultWorkspaceLayout, dockedWidth, dropLayout, fitFloatingRect, layoutStorageKey, panelIds, panelLabels, readWorkspaceLayout, workspaceDropTarget, workspaceGeometry } from "./workspaceLayout";
import type { DockPosition, DropPreview, LayoutPreset, PanelId, Rect, WorkspaceLayout, WorkspaceSize } from "./workspaceLayout";
import "./workspace.css";

const rectStyle = (rect: Rect): CSSProperties => ({ left: rect.x, top: rect.y, width: rect.width, height: rect.height });
const detached = new URLSearchParams(window.location.search).get("workspace_panel");
export const detachedPanel = panelIds.includes(detached as PanelId) ? detached as PanelId : null;

function loadLayout() {
  try { return readWorkspaceLayout(JSON.parse(localStorage.getItem(layoutStorageKey) || "null"), JSON.parse(localStorage.getItem("inkflow.workspace-layout.v2") || "null")); }
  catch { return readWorkspaceLayout(null, null); }
}

export function useWorkspaceLayout(area: RefObject<HTMLElement | null>, projectRoot: string,
  onDetach: (id: PanelId, bounds?: Rect) => Promise<void>, onReturn: (id: PanelId, hide?: boolean) => Promise<void>) {
  const [layout, setLayout] = useState(loadLayout);
  const layoutRef = useRef(layout);
  const [previous, setPrevious] = useState<{ layout: WorkspaceLayout; native: PanelId[] } | null>(null);
  const [size, setSize] = useState({ width: Math.max(1, window.innerWidth), height: Math.max(1, window.innerHeight - 100) });
  const [moving, setMoving] = useState<PanelId | null>(null);
  const [preview, setPreview] = useState<DropPreview | null>(null);
  const [active, setActive] = useState<PanelId | null>(null);
  const [nativePanels, setNativePanels] = useState<PanelId[]>([]);
  const nativePanelsRef = useRef(nativePanels); nativePanelsRef.current = nativePanels;
  const gesture = useRef<{ id: PanelId; pointerId: number; x: number; y: number; rect: Rect; original: WorkspaceLayout; edge: string; moved: boolean; drop: DropPreview | null; detachedRect: Rect } | null>(null);
  const nativeDragRef = useRef<{ id: PanelId; drop: DropPreview | null } | null>(null);
  const detachRef = useRef(onDetach); detachRef.current = onDetach;
  const returnRef = useRef(onReturn); returnRef.current = onReturn;
  const pendingFrame = useRef<number | null>(null);
  const geometryFor = (value: WorkspaceLayout, dimensions: WorkspaceSize) => {
    const panels = { ...value.panels };
    for (const id of nativePanelsRef.current) panels[id] = { ...panels[id], visible: false };
    return workspaceGeometry({ ...value, panels }, dimensions);
  };
  const geometry = geometryFor(layout, size);
  const sizeRef = useRef(size); sizeRef.current = size;
  const update = (next: WorkspaceLayout) => { layoutRef.current = next; setLayout(next); };
  const remember = (value = layoutRef.current) => setPrevious({ layout: value, native: [...nativePanelsRef.current] });
  const change = (next: WorkspaceLayout) => { remember(); update(next); };
  const updatePreview = (next: DropPreview | null) => setPreview(old => old?.action === next?.action && old?.position === next?.position && old?.target === next?.target
    && old?.rect.x === next?.rect.x && old?.rect.y === next?.rect.y && old?.rect.width === next?.rect.width && old?.rect.height === next?.rect.height ? old : next);
  const previewLayout = (value: WorkspaceLayout) => ({ ...value, panels: Object.fromEntries(panelIds.map(id => [id,
    { ...value.panels[id], visible: value.panels[id].visible && !nativePanelsRef.current.includes(id) }])) as WorkspaceLayout["panels"] });

  useLayoutEffect(() => {
    const element = area.current;
    if (!element) return;
    const measure = () => setSize({ width: Math.max(1, element.clientWidth), height: Math.max(1, element.clientHeight) });
    measure();
    const observer = new ResizeObserver(measure); observer.observe(element);
    return () => observer.disconnect();
  }, [area, projectRoot]);

  useEffect(() => {
    if (detachedPanel || moving) return;
    const timer = window.setTimeout(() => { try { localStorage.setItem(layoutStorageKey, JSON.stringify(layout)); } catch { /* A full local store does not disable dragging. */ } }, 150);
    return () => window.clearTimeout(timer);
  }, [layout, moving]);

  useEffect(() => {
    const save = () => { if (!detachedPanel) { try { localStorage.setItem(layoutStorageKey, JSON.stringify(layoutRef.current)); } catch { /* Keep the current window usable. */ } } };
    window.addEventListener("beforeunload", save);
    return () => window.removeEventListener("beforeunload", save);
  }, []);

  useEffect(() => {
    const draw = (next: WorkspaceLayout) => {
      const current = geometryFor(next, sizeRef.current);
      for (const id of panelIds) {
        const rect = current.panels[id], element = area.current?.querySelector<HTMLElement>(`[data-panel="${id}"]`);
        if (!rect || !element) continue;
        Object.assign(element.style, { left: `${rect.x}px`, top: `${rect.y}px`, width: `${rect.width}px`, height: `${rect.height}px` });
        element.dataset.floating = String(rect.floating);
      }
      const editor = area.current?.querySelector<HTMLElement>(".workbench");
      if (editor) Object.assign(editor.style, { left: `${current.editor.x}px`, top: `${current.editor.y}px`, width: `${current.editor.width}px`, height: `${current.editor.height}px` });
    };
    const move = (event: globalThis.PointerEvent) => {
      // Only panel rectangles change per frame; the novel workbench does not rerender.
      const current = gesture.current;
      if (!current || current.pointerId !== event.pointerId) return;
      const dx = event.clientX - current.x, dy = event.clientY - current.y;
      if (!current.moved && Math.hypot(dx, dy) < 5) return;
      current.moved = true;
      const bounds = area.current?.getBoundingClientRect();
      const raw = { ...current.rect, height: Math.min(current.rect.height, current.original.panels[current.id].height), x: current.rect.x + dx, y: current.rect.y + dy };
      const proposed = { ...current.original, panels: { ...current.original.panels,
        [current.id]: { ...current.original.panels[current.id], ...fitFloatingRect(current.id, raw, sizeRef.current), position: "floating" as const } } };
      current.drop = !current.edge && bounds ? workspaceDropTarget(current.id, raw,
        { x: event.clientX - bounds.left, y: event.clientY - bounds.top }, previewLayout(proposed), sizeRef.current, current.drop) : null;
      if (bounds) current.detachedRect = { ...raw, x: bounds.left + raw.x, y: bounds.top + raw.y };
      setMoving(current.id); updatePreview(current.drop);
      const next: WorkspaceLayout = { ...current.original, panels: { ...current.original.panels } };
      const panel = current.original.panels[current.id];
      if (!current.edge) next.panels[current.id] = { ...panel, ...fitFloatingRect(current.id, raw, sizeRef.current), position: "floating" };
      else {
        const floating = geometryFor(current.original, sizeRef.current).panels[current.id]?.floating;
        if (floating) {
          const width = current.rect.width + (current.edge.includes("w") ? -dx : current.edge.includes("e") ? dx : 0);
          const height = current.rect.height + (current.edge.includes("n") ? -dy : current.edge.includes("s") ? dy : 0);
          const fitted = fitFloatingRect(current.id, { ...current.rect, width, height }, sizeRef.current);
          next.panels[current.id] = { ...panel, ...fitFloatingRect(current.id, { ...fitted, x: current.rect.x + (current.edge.includes("w") ? current.rect.width - fitted.width : 0), y: current.rect.y + (current.edge.includes("n") ? current.rect.height - fitted.height : 0) }, sizeRef.current), position: "floating" };
        } else if (panel.position === "bottom") {
          if (current.edge === "n") next.bottomHeight = Math.max(160, Math.min(sizeRef.current.height - Math.min(240, sizeRef.current.height), current.rect.height - dy));
          else {
            const originalGeometry = geometryFor(current.original, sizeRef.current);
            const bottom = panelIds.filter(id => originalGeometry.panels[id] && !originalGeometry.panels[id]?.floating && current.original.panels[id].position === "bottom").sort((a, b) => originalGeometry.panels[a]!.x - originalGeometry.panels[b]!.x);
            const neighbor = bottom[bottom.indexOf(current.id) + (current.edge === "e" ? 1 : -1)];
            if (neighbor) {
              for (const id of bottom) next.panels[id] = { ...next.panels[id], width: originalGeometry.panels[id]!.width };
              const other = originalGeometry.panels[neighbor]!;
              const minimum = fitFloatingRect(current.id, { ...current.rect, width: 0 }, sizeRef.current).width;
              const otherMinimum = fitFloatingRect(neighbor, { ...other, width: 0 }, sizeRef.current).width;
              const delta = Math.max(minimum - current.rect.width, Math.min(other.width - otherMinimum, dx * (current.edge === "e" ? 1 : -1)));
              next.panels[current.id].width += delta; next.panels[neighbor].width -= delta;
            }
          }
        }
        else {
          const editor = geometryFor(current.original, sizeRef.current).editor;
          next.panels[current.id] = { ...panel, width: dockedWidth(current.id, current.rect.width + (panel.position === "left" ? dx : -dx), current.rect, editor, sizeRef.current) };
        }
      }
      if (current.edge) {
        const isFloating = geometryFor(current.original, sizeRef.current).panels[current.id]?.floating;
        const width = current.rect.width + (isFloating ? (current.edge.includes("w") ? -dx : current.edge.includes("e") ? dx : 0) : panel.position === "bottom" ? 0 : panel.position === "left" ? dx : -dx);
        const height = current.rect.height + (current.edge.includes("n") ? -dy : current.edge.includes("s") ? dy : 0);
        current.drop = shouldCollapse(current.id, width, height) ? { action: "close", rect: current.rect } : null;
        updatePreview(current.drop);
      }
      layoutRef.current = next;
      if (pendingFrame.current === null) pendingFrame.current = window.requestAnimationFrame(() => { pendingFrame.current = null; draw(layoutRef.current); });
    };
    const finish = (event: Event) => {
      const current = gesture.current;
      if (!current || (event instanceof globalThis.PointerEvent && event.pointerId !== current.pointerId)) return;
      if (pendingFrame.current !== null) { window.cancelAnimationFrame(pendingFrame.current); pendingFrame.current = null; }
      if (current.moved && event.type === "pointerup") {
        const next = layoutRef.current;
        remember(current.original);
        const finished = current.drop?.action === "swap" ? swapPanels(current.original, current.id, current.drop.target!)
          : current.drop?.action === "dock" ? dropLayout(next, current.id, current.drop.position!)
          : current.drop?.action === "close" ? { ...current.original, panels: { ...current.original.panels, [current.id]: { ...current.original.panels[current.id], visible: false } } } : next;
        if (area.current) area.current.dataset.moving = "false";
        draw(finished);
        update(finished);
        if (current.drop?.action === "detach") void detachRef.current(current.id, current.detachedRect);
      } else { draw(current.original); update(current.original); }
      gesture.current = null; setMoving(null); setPreview(null);
      window.document.body.classList.remove("workspace-interacting");
    };
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape" && gesture.current) { event.preventDefault(); finish(event); } };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", finish); window.addEventListener("pointercancel", finish); window.addEventListener("blur", finish); window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", finish); window.removeEventListener("pointercancel", finish); window.removeEventListener("blur", finish); window.removeEventListener("keydown", escape); if (pendingFrame.current !== null) window.cancelAnimationFrame(pendingFrame.current); window.document.body.classList.remove("workspace-interacting"); };
  }, [area]);

  const start = (id: PanelId, event: PointerEvent<HTMLElement>, edge = "") => {
    if (event.button !== 0 || detachedPanel) return;
    const rect = geometry.panels[id]; if (!rect) return;
    event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); setActive(id);
    gesture.current = { id, pointerId: event.pointerId, x: event.clientX, y: event.clientY, rect, original: layoutRef.current, edge, moved: false, drop: null, detachedRect: rect };
    window.document.body.classList.add("workspace-interacting");
  };
  const setPosition = (id: PanelId, position: DockPosition) => {
    const current = layoutRef.current, rect = geometry.panels[id] || current.panels[id];
    const order = position === "left" ? Math.min(...panelIds.map(item => current.panels[item].order)) - 1 : Math.max(...panelIds.map(item => current.panels[item].order)) + 1;
    change({ ...current, panels: { ...current.panels, [id]: { ...current.panels[id], ...fitFloatingRect(id, rect, size), position, order, visible: true } } });
  };
  const toggle = (id: PanelId, visible = !layoutRef.current.panels[id].visible) => {
    const current = layoutRef.current; change({ ...current, panels: { ...current.panels, [id]: { ...current.panels[id], visible } } });
  };
  const markNative = (id: PanelId, open: boolean, restore = true) => {
    const ids = [...nativePanelsRef.current.filter(item => item !== id), ...(open ? [id] : [])];
    nativePanelsRef.current = ids; setNativePanels(ids);
    if (!open && !restore) update({ ...layoutRef.current, panels: { ...layoutRef.current.panels, [id]: { ...layoutRef.current.panels[id], visible: false } } });
    else if (!open && !layoutRef.current.panels[id].visible) update({ ...layoutRef.current, panels: { ...layoutRef.current.panels, [id]: { ...layoutRef.current.panels[id], visible: true } } });
    if (nativeDragRef.current?.id === id) { nativeDragRef.current = null; setMoving(null); setPreview(null); }
  };
  const nativeDrag = (value: Record<string, unknown>) => {
    const id = value.panel as PanelId, bounds = area.current?.getBoundingClientRect();
    if (detachedPanel || !bounds || !panelIds.includes(id) || !nativePanelsRef.current.includes(id)) return;
    const rect = value.rect as Rect, origin = value.origin as { x: number; y: number }, point = value.point as { x: number; y: number };
    if (!rect || !origin || !point || ![rect.x, rect.y, rect.width, rect.height, origin.x, origin.y, point.x, point.y].every(Number.isFinite)) return;
    const cancelled = value.phase === "cancel";
    const scale = typeof value.scale === "number" && Number.isFinite(value.scale) && value.scale > 0 ? value.scale : 1;
    const localRect = { x: (rect.x - origin.x) / scale - bounds.left, y: (rect.y - origin.y) / scale - bounds.top, width: rect.width / scale, height: rect.height / scale };
    const incoming = { ...layoutRef.current, panels: { ...layoutRef.current.panels, [id]: { ...layoutRef.current.panels[id], width: localRect.width, height: localRect.height } } };
    const drop = cancelled ? null : workspaceDropTarget(id,
      localRect, { x: (point.x - origin.x) / scale - bounds.left, y: (point.y - origin.y) / scale - bounds.top }, previewLayout(incoming), sizeRef.current,
      nativeDragRef.current?.id === id ? nativeDragRef.current.drop : null, true);
    if (value.phase === "move") { nativeDragRef.current = { id, drop }; setMoving(id); updatePreview(drop); return; }
    nativeDragRef.current = null; setMoving(null); setPreview(null);
    if (value.phase === "drop" && drop) {
      remember();
      if (drop.action === "swap") update(swapPanels(incoming, id, drop.target!));
      else if (drop.action === "dock") update(dropLayout(incoming, id, drop.position!));
      else if (drop.action === "close") update({ ...layoutRef.current, panels: { ...layoutRef.current.panels, [id]: { ...layoutRef.current.panels[id], visible: false } } });
      void returnRef.current(id, drop.action === "close");
    }
  };
  const undo = previous ? () => {
    const saved = previous;
    update(saved.layout); setPrevious(null);
    for (const id of nativePanelsRef.current) if (!saved.native.includes(id)) void returnRef.current(id, !saved.layout.panels[id].visible);
    for (const id of saved.native) if (!nativePanelsRef.current.includes(id)) void detachRef.current(id);
  } : null;
  const keyMove = (id: PanelId, event: KeyboardEvent<HTMLButtonElement>) => {
    const direction = ({ ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] } as Record<string, number[]>)[event.key];
    if (!direction) return;
    event.preventDefault();
    if (event.altKey && ["ArrowLeft", "ArrowRight", "ArrowDown"].includes(event.key)) { setPosition(id, event.key === "ArrowLeft" ? "left" : event.key === "ArrowRight" ? "right" : "bottom"); return; }
    const current = layoutRef.current, rect = geometry.panels[id] || current.panels[id];
    const fitted = fitFloatingRect(id, event.shiftKey ? { ...rect, width: rect.width + direction[0], height: rect.height + direction[1] } : { ...rect, x: rect.x + direction[0], y: rect.y + direction[1] }, size);
    change({ ...current, panels: { ...current.panels, [id]: { ...current.panels[id], ...fitted, position: "floating" } } });
  };
  return { applyPreset: (preset: LayoutPreset) => change(presetLayout(layoutRef.current, preset, size)), layout, geometry, size, moving, preview, active, nativePanels, markNative, nativeDrag, remember, start, setActive, keyMove, setPosition, toggle, undo, reset: () => change(readWorkspaceLayout(defaultWorkspaceLayout, null)) };
}
type WorkspaceController = ReturnType<typeof useWorkspaceLayout>;

function WorkspacePopover({ label, className, icon, children }: { label: string; className: string; icon: ReactNode; children: ReactNode }) {
  const id = useId();
  const [point, setPoint] = useState({ left: 0, top: 0 });
  return <><button className={className} type="button" popoverTarget={id} title={label} aria-label={label} onClick={event => {
    const rect = event.currentTarget.getBoundingClientRect();
    setPoint({ left: Math.max(8, Math.min(window.innerWidth - 218, rect.right - 210)), top: Math.max(8, Math.min(window.innerHeight - 250, rect.bottom + 6)) });
  }}>{icon}</button><div id={id} popover="auto" className="workspace-menu-content workspace-popover" style={point} onClick={event => { if ((event.target as HTMLElement).closest("button:not(:disabled)")) event.currentTarget.hidePopover(); }}>{children}</div></>;
}

export function WorkspaceMenu({ workspace, onReturn }: { workspace: WorkspaceController; onReturn: (id: PanelId) => void }) {
  return <WorkspacePopover label="显示面板和恢复布局" className="workspace-layout-button" icon={<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><rect x="2" y="3" width="16" height="14" rx="2" /><path d="M7 3v14M13 3v14" /></svg>}><small>布局预选</small><div className="workspace-presets">{layoutPresets.map(preset => <button key={preset.id} type="button" onClick={() => workspace.applyPreset(preset.id)}><span className={`layout-mini layout-${preset.id}`} aria-hidden="true"><i /><i /><i /></span>{preset.label}</button>)}</div><hr /><small>工作区面板</small>{panelIds.map(id => <button key={id} type="button" aria-pressed={workspace.layout.panels[id].visible} onClick={() => workspace.nativePanels.includes(id) ? onReturn(id) : workspace.geometry.collapsed.includes(id) ? workspace.setPosition(id, "floating") : workspace.toggle(id)}><span>{panelLabels[id]}</span><span aria-hidden="true">{workspace.nativePanels.includes(id) ? "返回工作台" : workspace.geometry.collapsed.includes(id) ? "展开" : workspace.layout.panels[id].visible ? "✓" : "打开"}</span></button>)}<hr /><button disabled={!workspace.undo} onClick={() => workspace.undo?.()}>回到上一步布局</button><button onClick={() => { workspace.nativePanels.forEach(onReturn); workspace.reset(); }}>恢复初始布局</button><p>拖标题移动或交换位置；缩得很小时松开即可收起。</p></WorkspacePopover>;
}

export function WorkspacePanel({ id, workspace, className, actions, onDetach, children }: { id: PanelId; workspace: WorkspaceController; className: string; actions?: ReactNode; onDetach: (id: PanelId) => void; children: ReactNode }) {
  const panel = workspace.layout.panels[id], detachedHere = detachedPanel === id;
  const rect = detachedHere ? { x: 0, y: 0, ...workspace.size, floating: false } : workspace.geometry.panels[id];
  const hidden = detachedPanel ? !detachedHere : !panel.visible || workspace.nativePanels.includes(id) || !rect;
  const bottom = panelIds.filter(item => workspace.geometry.panels[item] && !workspace.geometry.panels[item]?.floating && workspace.layout.panels[item].position === "bottom").sort((a, b) => workspace.geometry.panels[a]!.x - workspace.geometry.panels[b]!.x);
  const edges = rect?.floating ? ["n", "e", "s", "w", "ne", "se", "sw", "nw"] : panel.position === "bottom" ? ["n", ...(bottom.indexOf(id) < bottom.length - 1 ? ["e"] : []), ...(bottom.indexOf(id) > 0 ? ["w"] : [])] : [panel.position === "left" ? "e" : "w"];
  return <section className={`workspace-panel ${className}`} hidden={hidden} style={rect ? { ...rectStyle(rect), zIndex: workspace.moving === id || (rect.floating && workspace.active === id) ? 24 : rect.floating ? 22 : 1 } : undefined} data-floating={rect?.floating} data-panel={id} onPointerDown={() => workspace.setActive(id)}>
    <header className="workspace-panel-heading"><button className="workspace-title-handle" title="拖动标题移动；方向键移动，Shift加方向键缩放，Alt加方向键停靠" onPointerDown={event => workspace.start(id, event)} onKeyDown={event => workspace.keyMove(id, event)}><span className="panel-grip" aria-hidden="true">⠿</span>{panelLabels[id]}</button><div className="workspace-panel-actions">{actions}{detachedHere ? <button className="panel-icon" title="回到主工作台" aria-label="回到主工作台" onClick={() => void window.inkflow.returnWorkspacePanel()}>↙</button> : <WorkspacePopover label={`${panelLabels[id]}的移动选项`} className="panel-icon" icon="⋯">{(["left", "right", "bottom", "floating"] as DockPosition[]).map(position => <button key={position} onClick={() => workspace.setPosition(id, position)}>{({ left: "放到左侧", right: "放到右侧", bottom: "放到下方", floating: "自由浮动" })[position]}</button>)}<hr /><button onClick={() => onDetach(id)}>移到独立窗口 ↗</button></WorkspacePopover>}<button className="panel-icon panel-close" title="关闭面板" aria-label={`关闭${panelLabels[id]}`} onClick={() => detachedHere ? void window.inkflow.returnWorkspacePanel() : workspace.toggle(id, false)}>×</button></div></header>
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
      <span>{preview.action === "close" ? `松开后收起${label}` : preview.action === "swap" ? `松开后与${panelLabels[preview.target!]}交换位置` : preview.action === "detach" ? "松开后移到独立窗口" : `松开后放到${preview.position === "left" ? "左侧" : preview.position === "right" ? "右侧" : "下方"}`}</span>
      <small>{preview.action === "close" ? "可从顶端布局重新打开" : "移出预览区域可继续自由摆放"}</small>
    </div>}
  </>;
}
