import { useEffect, useId, useRef } from "react";
import { MotionPresence } from "./MotionPresence";
import { arrangementLayout, dockGroup, dockSignature, dockSplit, panelIds, panelLabels } from "./workspaceLayout";
import type { DockNode, PanelId } from "./workspaceLayout";
import type { WorkspaceController } from "./Workspace";

// 三个起点使用同一套可继续拖动的停靠树。
const referenceTabs: PanelId[] = ["navigation", "workbench", "inspector"];
const layouts = [
  { title: "左右分栏", node: dockSplit(dockGroup(["assistant"]), dockGroup(referenceTabs), "horizontal") },
  { title: "上下分栏", node: dockSplit(dockGroup(["assistant"]), dockGroup(referenceTabs), "vertical") },
  { title: "同区叠放", node: dockGroup(["assistant", ...referenceTabs]) },
];
const panelModes = { navigation: "docked", assistant: "docked", inspector: "docked", workbench: "docked" } as const;
const previewLabels = { navigation: "结构", assistant: "对话", inspector: "批注", workbench: "工程" } as const;

/** 缩略图沿用停靠树的分栏方向与页签顺序。 */
function LayoutPreview({ node }: { node: DockNode }) {
  if (node.type === "split") {
    return <span className={`layout-preview-split ${node.axis}`}>
      <LayoutPreview node={node.first} /><LayoutPreview node={node.second} />
    </span>;
  }
  return <span className="layout-preview-group">
    {node.tabs.length > 1 && <span className="layout-preview-tabs">{node.tabs.map(id => <span key={id}>{previewLabels[id]}</span>)}</span>}
    <span className="layout-preview-panel">{previewLabels[node.active]}</span>
  </span>;
}

/** 键盘与读屏用户可以在选择前了解每组的位置和叠放顺序。 */
function describeLayout(node: DockNode): string {
  if (node.type === "group") return node.tabs.map(id => panelLabels[id]).join("、");
  const [first, second] = node.axis === "horizontal" ? ["左", "右"] : ["上", "下"];
  return `${first}侧（${describeLayout(node.first)}），${second}侧（${describeLayout(node.second)}）`;
}

/** 用三个预设提供停靠起点，面板开关和应用流程沿用工作台原实现。 */
export function LayoutLibrary({ open, onClose, workspace }: { open: boolean; onClose: () => void; workspace: WorkspaceController }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
  }, [open]);

  /** 独立窗口须先经原服务收起；普通面板复用开关以保留位置和重新入栏规则。 */
  const setVisible = (id: PanelId, visible: boolean) => {
    if (workspace.nativePanels.includes(id)) {
      void workspace.applyArrangement({ ...workspace.layout, panels: {
        ...workspace.layout.panels, [id]: { ...workspace.layout.panels[id], visible },
      } }, workspace.nativePanels.filter(panel => panel !== id));
    } else workspace.toggle(id, visible);
  };

  const currentSignature = !workspace.nativePanels.length && panelIds.every(id => workspace.layout.panels[id].visible
    && workspace.layout.panels[id].position === "center") ? dockSignature(workspace.layout.dock) : "";

  /** 统一召回独立窗口并写入现有布局状态，失败时留在窗口看原因。 */
  const choose = async (node: DockNode) => {
    if (await workspace.applyArrangement(arrangementLayout(workspace.layout, node, panelModes, workspace.size))) onClose();
  };

  return <MotionPresence open={open}>{() => open && <dialog className="panel-visibility-dialog" ref={dialog} aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); if (!workspace.arranging) onClose(); }} onClose={onClose}>
    <header><h2 id={titleId}>调整布局</h2><button type="button" disabled={workspace.arranging} onClick={onClose} aria-label="关闭布局选择">×</button></header>
    <div className="panel-visibility-list">{panelIds.map(id => <label key={id}>
      <span>{panelLabels[id]}</span>
      <input type="checkbox" role="switch" checked={workspace.nativePanels.includes(id) || workspace.layout.panels[id].visible}
        disabled={workspace.arranging} onChange={event => setVisible(id, event.target.checked)} />
    </label>)}</div>
    <div className="layout-catalogue-grid">{layouts.map(({ title, node }) => {
      const signature = dockSignature(node);
      const description = describeLayout(node);
      return <button type="button" key={signature} aria-label={`${title}，${description}`}
        title={description} aria-pressed={currentSignature === signature}
        disabled={workspace.arranging} onClick={() => void choose(node)}>
        <span aria-hidden="true" className="layout-preview"><LayoutPreview node={node} /></span>
        <strong>{title}</strong>
      </button>;
    })}</div>
    {workspace.arrangementError && <p role="alert">{workspace.arrangementError}</p>}
  </dialog>}</MotionPresence>;
}
