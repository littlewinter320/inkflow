import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

/** 出场期间保留最后一次有效内容；立即禁用交互，避免淡出时重复提交或访问已清空的对象。 */
export function MotionPresence({ open, children }: { open: boolean; children: () => ReactNode }) {
  const [retained, setRetained] = useState(open);
  const content = useRef<ReactNode>(null);
  const host = useRef<HTMLDivElement>(null);
  if (open) content.current = children();
  useLayoutEffect(() => {
    // 原生modal dialog可脱离祖先的inert，退出时也须直接停用其键盘和提交入口。
    host.current?.querySelectorAll("dialog").forEach(dialog => { dialog.inert = !open; });
  }, [open]);
  useEffect(() => {
    if (open) { setRetained(true); return; }
    if (!retained) return;
    // 与studio-motion.css的退出时长一致；再次打开会清除旧计时器，不移除新内容。
    const delay = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 160;
    const timer = window.setTimeout(() => setRetained(false), delay);
    return () => window.clearTimeout(timer);
  }, [open, retained]);
  if (!open && !retained) { content.current = null; return null; }
  return <div ref={host} className="motion-presence" data-motion-exiting={!open} inert={!open} aria-hidden={!open || undefined}>{content.current}</div>;
}
