import { useEffect, useRef, useState, type PointerEvent, type ReactNode } from "react";

type Point = { x: number; y: number; z: number };
type GraphNode = Point & { id: string; name: string; kind: string; group?: string | null;
  size?: number; information?: number; evidence?: number };
export type KnowledgeGraphData = { total: number; shown: number; nodes: GraphNode[];
  center?: string; tracks?: Array<{ radius: number; minor: number; tilt: number }>;
  edges: Array<{ id: string; source: string; target: string; state: string }> };
type Camera = { center: Point; yaw: number; pitch: number; zoom: number; x: number; y: number };
type ScreenNode = { node: GraphNode; x: number; y: number; z: number; radius: number };

export const knowledgeColors: Record<string, string> = { character: "#5ba7ff", faction: "#bc90ff", location: "#35d7b7",
  item: "#ffcb59", event: "#ff8071", thread: "#f87ec5", plan: "#90d8ff", world: "#a7df69",
  method: "#f5ad79", reference: "#c3cfe0", entity: "#95a7c1" };

/** 重置相机，旋转及缩放都以场景原点开始。 */
function overview(): Camera {
  return { center: { x: 0, y: 0, z: 0 }, yaw: -.18, pitch: .69, zoom: 1, x: 0, y: 0 };
}

/** 轨道与节点共用同一相机，旋转后仍保持空间关系。 */
function projection(camera: Camera, width: number, height: number) {
  const cy = Math.cos(camera.yaw), sy = Math.sin(camera.yaw);
  const cp = Math.cos(camera.pitch), sp = Math.sin(camera.pitch);
  const scale = Math.min(width * .43, height * .56) * camera.zoom;
  return (point: Point) => {
    const x = point.x - camera.center.x, y = point.y - camera.center.y, z = (point.z ?? 0) - camera.center.z;
    const rx = x * cy + z * sy, rz = z * cy - x * sy;
    const depth = y * sp + rz * cp;
    const perspective = 4 / Math.max(1, 4 - depth);
    return { x: width / 2 + camera.x + rx * scale * perspective,
      y: height / 2 + camera.y + (y * cp - rz * sp) * scale * perspective, z: depth,
      perspective };
  };
}

/** 真实三维坐标使用 Canvas 投影，目录与检查栏仍是可操作的 HTML。 */
export function KnowledgeGraph({ graph, selected, choose, controls, children }: { graph: KnowledgeGraphData; selected: string;
  choose: (id: string) => void; controls: ReactNode; children: ReactNode }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const camera = useRef(overview());
  const points = useRef<ScreenNode[]>([]);
  const redraw = useRef(() => {});
  const hovered = useRef("");
  const anchor = useRef("");
  const previous = useRef(selected);
  const [unavailable, setUnavailable] = useState(false);
  const motion = useRef<{ start: number; from: Camera; to: Point } | null>(null);
  const pointer = useRef<{ id: number; x: number; y: number; distance: number; pan: boolean } | null>(null);

  /** 聚焦时平滑回到画布中心，系统减少动态效果时直接定位。 */
  function focus(node: GraphNode) {
    anchor.current = node.id;
    const to = { x: node.x, y: node.y, z: node.z ?? 0 };
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      Object.assign(camera.current, { center: to, x: 0, y: 0 });
      motion.current = null;
    } else {
      motion.current = { start: performance.now(), from: { ...camera.current }, to };
    }
    redraw.current();
  }

  /** 缩放以光标下的位置为锚点。 */
  function zoom(factor: number, x = 0, y = 0) {
    const view = camera.current;
    const next = Math.max(.38, Math.min(9, view.zoom * factor));
    const ratio = next / view.zoom;
    view.x = x - (x - view.x) * ratio;
    view.y = y - (y - view.y) * ratio;
    view.zoom = next;
    redraw.current();
  }

  /** 回到全书视角，清除上一个聚焦和鼠标悬停。 */
  function reset() {
    anchor.current = "";
    hovered.current = "";
    motion.current = null;
    camera.current = overview();
    redraw.current();
  }

  /** 总览时保留完整球体；放大或聚焦后可平移观察局部。 */
  function pan(dx: number, dy: number, surface: HTMLCanvasElement) {
    const view = camera.current;
    const { clientWidth: width, clientHeight: height } = surface;
    const radius = Math.min(width, height) * .43 * view.zoom * 4 / Math.sqrt(15);
    const boundX = view.zoom > 1 || anchor.current ? width : Math.max(0, width / 2 - radius - 13);
    const boundY = view.zoom > 1 || anchor.current ? height : Math.max(0, height / 2 - radius - 13);
    view.x = Math.max(-boundX, Math.min(boundX, view.x + dx));
    view.y = Math.max(-boundY, Math.min(boundY, view.y + dy));
  }

  useEffect(() => {
    const surface = canvas.current;
    const context = surface?.getContext("2d");
    if (!surface || !context) { setUnavailable(true); return; }
    let frame = 0;
    const sprites = new Map<string, HTMLCanvasElement>();

    /** 每种颜色只生成一次有高光的球体贴图，旋转时复用。 */
    function sphere(color: string) {
      const cached = sprites.get(color);
      if (cached) return cached;
      const icon = document.createElement("canvas");
      icon.width = icon.height = 48;
      const brush = icon.getContext("2d")!;
      brush.beginPath(); brush.arc(24, 24, 23, 0, Math.PI * 2);
      brush.fillStyle = color; brush.fill();
      const light = brush.createRadialGradient(15, 12, 1, 24, 25, 25);
      light.addColorStop(0, "#ffffffbd"); light.addColorStop(.52, "#ffffff00"); light.addColorStop(1, "#09112186");
      brush.fillStyle = light; brush.fill();
      brush.lineWidth = 1.5; brush.strokeStyle = "#ffffff55"; brush.stroke();
      sprites.set(color, icon);
      return icon;
    }

    /** 按深度从远到近画球体，弱化非焦点关系，保留少量可读标签。 */
    function draw(now: number) {
      frame = 0;
      const width = surface!.clientWidth, height = surface!.clientHeight;
      if (!width || !height) return;
      const view = camera.current;
      const move = motion.current;
      if (move) {
        const t = Math.min(1, (now - move.start) / 270);
        const remaining = (1 - Math.max(0, t)) ** 3;
        view.center = { x: move.to.x + (move.from.center.x - move.to.x) * remaining,
          y: move.to.y + (move.from.center.y - move.to.y) * remaining,
          z: move.to.z + (move.from.center.z - move.to.z) * remaining };
        view.x = move.from.x * remaining;
        view.y = move.from.y * remaining;
        if (t >= 1) motion.current = null;
      }
      const ratio = Math.min(2, window.devicePixelRatio || 1);
      const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
      if (surface!.width !== pixelWidth || surface!.height !== pixelHeight) {
        surface!.width = pixelWidth; surface!.height = pixelHeight;
      }
      const brush = context!;
      brush.setTransform(ratio, 0, 0, ratio, 0, 0);
      brush.clearRect(0, 0, width, height);
      const project = projection(view, width, height);
      brush.strokeStyle = "#9eb9df37"; brush.lineWidth = .8;
      for (const track of graph.tracks || []) {
        brush.beginPath();
        for (let step = 0; step <= 64; step++) {
          const angle = step / 64 * Math.PI * 2;
          const depth = track.minor * Math.sin(angle);
          const point = project({ x: track.radius * Math.cos(angle), y: depth * Math.sin(track.tilt), z: depth * Math.cos(track.tilt) });
          if (step) brush.lineTo(point.x, point.y);
          else brush.moveTo(point.x, point.y);
        }
        brush.stroke();
      }
      points.current = graph.nodes.map(node => {
        const point = project(node);
        const weight = node.size ?? 0;
        const size = node.id === graph.center ? 16 + 6 * weight : graph.shown > 160 ? 2.8 + 7 * weight : 5 + 12 * weight;
        const radius = Math.min(28, Math.max(node.id === selected ? 9 : 2.8, size * point.perspective * Math.sqrt(view.zoom)));
        return { node, ...point, radius };
      }).sort((a, b) => a.z - b.z);
      const projected = new Map(points.current.map(point => [point.node.id, point]));
      const star = projected.get(graph.center || "");
      if (star) {
        const halo = brush.createRadialGradient(star.x, star.y, star.radius * .5, star.x, star.y, star.radius * 3.1);
        halo.addColorStop(0, (knowledgeColors[star.node.kind] || "#9eb9df") + "66");
        halo.addColorStop(1, "#172b4b00");
        brush.fillStyle = halo;
        brush.beginPath(); brush.arc(star.x, star.y, star.radius * 3.1, 0, Math.PI * 2); brush.fill();
      }
      const primary = hovered.current || anchor.current;
      const near = new Set<string>();
      const origin = projected.get(primary);
      const highlightedTargets = new Set(graph.edges.flatMap(edge => edge.source === primary ? [edge.target]
        : edge.target === primary ? [edge.source] : []));
      // 核心对象可能连到数百个节点；精选眼前连线，其余关系仍以细线和完整档案呈现。
      const featured = new Set(origin ? [...highlightedTargets].filter(id => projected.has(id))
        .sort((a, b) => {
          const x = projected.get(a)!, y = projected.get(b)!;
          return Math.hypot(x.x - origin.x, x.y - origin.y) - Math.hypot(y.x - origin.x, y.y - origin.y);
        }).slice(0, 14) : []);
      const lines = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];
      const drawnPairs = new Set<string>();
      for (const edge of graph.edges) {
        const a = projected.get(edge.source), b = projected.get(edge.target);
        if (!a || !b) continue;
        const highlighted = (a.node.id === primary && featured.has(b.node.id)) || (b.node.id === primary && featured.has(a.node.id));
        if (highlighted) { near.add(a.node.id); near.add(b.node.id); }
        const dashed = ["plan", "hypothesis", "unverified", "conflict"].includes(edge.state);
        const style = (highlighted ? 2 : 0) + Number(dashed);
        // 同两点同线型只画一条，详情仍列出每个独立关系。
        const pair = [edge.source, edge.target].sort().join(":") + ":" + style;
        if (drawnPairs.has(pair)) continue;
        drawnPairs.add(pair);
        const path = lines[style];
        path.moveTo(a.x, a.y); path.lineTo(b.x, b.y);
      }
      // 按样式合并画线，大图旋转时避免为每条关系单独提交绘制。
      lines.forEach((path, index) => {
        const dense = graph.shown > 160;
        brush.strokeStyle = index >= 2 ? "#c7deffa0" : "#92afd4" + (dense ? "10" : primary ? "24" : "36");
        brush.lineWidth = index >= 2 ? 1.25 : .65;
        brush.setLineDash(index % 2 ? [4, 4] : []);
        brush.stroke(path);
      });
      brush.setLineDash([]);
      for (const { node, x, y, z, radius } of points.current) {
        const active = node.id === primary;
        const front = Math.min(1, Math.max(.34, .68 + .36 * z));
        brush.globalAlpha = active || node.id === graph.center ? 1 : primary && !near.has(node.id) ? front * .72 : front;
        const icon = sphere(knowledgeColors[node.kind] || "#8497ab");
        brush.drawImage(icon, x - radius, y - radius, radius * 2, radius * 2);
        if (active) {
          brush.strokeStyle = "#d5e6ff"; brush.lineWidth = 1.25;
          brush.beginPath(); brush.arc(x, y, radius + 4, 0, Math.PI * 2); brush.stroke();
        }
      }
      brush.globalAlpha = 1;
      brush.textAlign = "center";
      const priority = [...new Set([primary, graph.center || "", selected, ...graph.nodes.slice(0, graph.shown > 160 ? 5 : 14).map(node => node.id)])];
      const named = priority.map(id => projected.get(id));
      const labels: Array<{ x: number; y: number; width: number }> = [];
      for (const item of named) {
        if (!item) continue;
        const { node, x, y, radius } = item;
        const active = node.id === primary || node.id === selected;
        const label = node.name.length > 15 ? node.name.slice(0, 15) + "…" : node.name;
        brush.font = active ? "600 15px system-ui" : "500 13px system-ui";
        const labelWidth = brush.measureText(label).width + 12;
        const labelX = Math.max(labelWidth / 2 + 4, Math.min(x, width - labelWidth / 2 - 4));
        // 尝试球体下方、上方与左右四个位置，避免长中文标签相互遮挡。
        const choices = [[labelX, y + radius + 18], [labelX, y - radius - 10],
          [x + radius + labelWidth / 2 + 6, y + 5], [x - radius - labelWidth / 2 - 6, y + 5]];
        const at = choices.find(([cx, cy]) => cy > 23 && cy < height - 19 && cx >= labelWidth / 2 && cx <= width - labelWidth / 2
          && !labels.some(other => Math.abs(other.x - cx) < (other.width + labelWidth) / 2 + 4 && Math.abs(other.y - cy) < 22)
          && !points.current.some(point => point.node.id !== node.id
            && Math.abs(point.x - cx) < labelWidth / 2 + point.radius && Math.abs(point.y - (cy - 5)) < point.radius + 9));
        if (!at && !active) continue;
        const [cx, cy] = at || choices[0];
        brush.fillStyle = active ? "#0b1425ee" : "#0b1425d9";
        brush.beginPath(); brush.roundRect(cx - labelWidth / 2, cy - 15, labelWidth, 20, 5); brush.fill();
        brush.fillStyle = "#f4f8ff"; brush.fillText(label, cx, cy);
        labels.push({ x: cx, y: cy, width: labelWidth });
      }
      if (motion.current) invalidate();
    }

    /** 每帧最多绘制一次，高频触控板输入合并到下一帧。 */
    function invalidate() { if (!frame) frame = requestAnimationFrame(draw); }
    redraw.current = invalidate;
    const resize = new ResizeObserver(invalidate);
    resize.observe(surface);
    // Chromium 将触控板捏合派发成带 Ctrl 的 wheel。双指普通滑动保留两个轴。
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      motion.current = null;
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? surface.clientHeight : 1;
      if (event.ctrlKey) {
        const rect = surface.getBoundingClientRect();
        zoom(Math.exp(-event.deltaY * unit * .009), event.clientX - rect.left - rect.width / 2,
          event.clientY - rect.top - rect.height / 2);
      } else {
        pan(-event.deltaX * unit, -event.deltaY * unit, surface);
        invalidate();
      }
    };
    surface.addEventListener("wheel", wheel, { passive: false });
    invalidate();
    return () => { surface.removeEventListener("wheel", wheel); resize.disconnect(); cancelAnimationFrame(frame); redraw.current = () => {}; };
  }, [graph, selected]);

  useEffect(() => {
    const changed = previous.current !== selected;
    previous.current = selected;
    const node = graph.nodes.find(item => item.id === (changed ? selected : anchor.current));
    if (node && changed) focus(node);
    else if (node && !motion.current) { camera.current.center = { ...node, z: node.z ?? 0 }; redraw.current(); }
  }, [graph, selected]);

  /** 深度最高的命中球接收点击，避免选中被挡住的远处对象。 */
  function hit(event: PointerEvent<HTMLCanvasElement>) {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    for (let index = points.current.length - 1; index >= 0; index--) {
      const point = points.current[index];
      if (Math.hypot(point.x - x, point.y - y) <= point.radius + 5) return point.node;
    }
  }

  return <section className="knowledge-graph">
    <div className="knowledge-graph-tools"><span>显示 {graph.shown} / {graph.total} 个对象{graph.shown < graph.total ? " · 超出显示上限" : ""}</span>
      {controls}
      <button type="button" aria-label="缩小" onClick={() => zoom(1 / 1.2)}>−</button>
      <button type="button" aria-label="放大" onClick={() => zoom(1.2)}>＋</button>
      <button type="button" onClick={reset}>全书视角</button>
    </div>
    <div className="knowledge-graph-layout"><div className="knowledge-graph-viewport">
      <canvas ref={canvas} tabIndex={0} aria-label="三维关系图。方向键旋转，加减键缩放，Home 回到全书视角。"
        onContextMenu={event => event.preventDefault()}
        onPointerDown={event => {
          if (pointer.current || ![0, 2].includes(event.button)) return;
          event.currentTarget.setPointerCapture(event.pointerId);
          pointer.current = { id: event.pointerId, x: event.clientX, y: event.clientY, distance: 0, pan: event.shiftKey || event.button === 2 };
        }}
        onPointerMove={event => {
          const active = pointer.current;
          if (!active) {
            const node = hit(event);
            hovered.current = node?.id || ""; event.currentTarget.title = node?.name || "";
          } else if (active.id === event.pointerId) {
            const dx = event.clientX - active.x, dy = event.clientY - active.y;
            active.distance += Math.hypot(dx, dy);
            active.x = event.clientX; active.y = event.clientY;
            if (active.distance >= 4) {
              motion.current = null; hovered.current = "";
              const view = camera.current;
              if (active.pan) pan(dx, dy, event.currentTarget);
              else { view.yaw += dx * .008; view.pitch = Math.max(-1.5, Math.min(1.5, view.pitch + dy * .008)); }
            }
          }
          redraw.current();
        }}
        onPointerUp={event => {
          const active = pointer.current;
          if (active?.id !== event.pointerId) return;
          pointer.current = null;
          if (!active.pan && active.distance < 4) {
            const node = hit(event);
            if (node) { focus(node); choose(node.id); }
          }
        }}
        onLostPointerCapture={() => { pointer.current = null; }}
        onPointerLeave={() => { hovered.current = ""; redraw.current(); }}
        onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "+", "=", "-"].includes(event.key)) return;
          event.preventDefault();
          const view = camera.current;
          if (event.key === "Home") reset();
          else if (event.key === "+" || event.key === "=" || event.key === "-") zoom(event.key === "-" ? 1 / 1.2 : 1.2);
          else {
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") view.yaw += event.key === "ArrowLeft" ? -.12 : .12;
            else view.pitch = Math.max(-1.5, Math.min(1.5, view.pitch + (event.key === "ArrowUp" ? -.12 : .12)));
            redraw.current();
          }
        }} />
      {unavailable && <p role="status">当前画布不可用，可从知识目录浏览对象和关系。</p>}
      <div className="knowledge-graph-hint">拖动旋转 · 双指滑动平移 · 捏合缩放</div>
    </div><aside className="knowledge-graph-inspector" aria-label="所选对象">{children}</aside></div>
  </section>;
}
