import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { KeyboardEvent, RefObject } from "react";
import type { SkillInfo } from "./SkillsManager";
import "./skills.css";

type Props = {
  projectRoot: string;
  value: string;
  onChange: (value: string) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  placeholder: string;
};

// 补全只负责填写；服务端重新核对原话、启用版本、重名和实际角色权限。
export function SkillMentions({ projectRoot, value, onChange, onKeyDown, inputRef, placeholder }: Props) {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [cursor, setCursor] = useState(value.length);
  const [highlight, setHighlight] = useState(0);
  const [dismissed, setDismissed] = useState("");
  const [composing, setComposing] = useState(false);
  const [pending, setPending] = useState<{ skill: SkillInfo; start: number; end: number } | null>(null);
  const [parameters, setParameters] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState("");
  const latestValue = useRef(value);
  const listRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const [popup, setPopup] = useState<{ left: number; width: number; top?: number; bottom?: number; maxHeight: number } | null>(null);
  latestValue.current = value;

  // 按实际换行/自动折行量增长，删除文字也收回；宽度变化时重新测量，不依赖固定rows。
  useLayoutEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    const resize = () => {
      input.style.height = "0px";
      const border = input.offsetHeight - input.clientHeight;
      const height = Math.min(100, Math.max(50, input.scrollHeight + border));
      input.style.height = `${height}px`;
      input.style.overflowY = input.scrollHeight + border > 100 ? "auto" : "hidden";
    };
    resize();
    let previousWidth = input.clientWidth;
    const observer = new ResizeObserver(() => {
      if (input.clientWidth !== previousWidth) { previousWidth = input.clientWidth; resize(); }
    });
    observer.observe(input);
    return () => observer.disconnect();
  }, [value, inputRef]);

  // 库只在入口、换书或作者刷新技能时读取；输入每个字符不发RPC或模型请求。
  useEffect(() => {
    let current = true;
    const load = () => void window.inkflow.request<{ skills: SkillInfo[] }>("skills.list", projectRoot ? { project_root: projectRoot } : {})
      .then(result => { if (current) { setSkills(result.skills.filter(item => item.enabled)); setLoadError(""); } })
      .catch(() => { if (current) setLoadError("技能目录未读到，可在设置中刷新；直接输入仍由服务端核对。"); });
    load();
    window.addEventListener("inkflow:skills-changed", load);
    return () => { current = false; window.removeEventListener("inkflow:skills-changed", load); };
  }, [projectRoot]);

  const prefix = value.slice(0, cursor);
  const match = prefix.match(/(?:^|[\s，。；：、(（])@([\p{L}\p{N}/-]*)$/u);
  const query = match?.[1].toLocaleLowerCase() ?? "";
  const queryKey = match ? `${cursor}:${query}` : "";
  const candidates = match ? skills.filter(item => `${item.id} ${item.display_name} ${item.description}`.toLocaleLowerCase().includes(query)).slice(0, 8) : [];
  const open = Boolean(match && candidates.length && dismissed !== queryKey && !composing && !pending);
  const activeIndex = Math.min(highlight, Math.max(0, candidates.length - 1));
  // 浮层挂到body逃离composer的overflow裁切；只监听打开期间，拖动/缩放/滚动均更新位置。
  useLayoutEffect(() => {
    if (!open) { setPopup(null); return; }
    const place = () => {
      const input = inputRef.current;
      if (!input) return;
      const rect = input.getBoundingClientRect();
      const above = Math.max(0, rect.top - 16);
      const below = Math.max(0, window.innerHeight - rect.bottom - 16);
      const up = above >= 160 || above >= below;
      const width = Math.min(480, Math.max(240, rect.width), window.innerWidth - 24);
      setPopup({ left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)), width,
        ...(up ? { bottom: window.innerHeight - rect.top + 8 } : { top: rect.bottom + 8 }),
        maxHeight: Math.min(300, up ? above : below) });
    };
    let frame = 0;
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(place); };
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !listRef.current?.contains(event.target) && !inputRef.current?.contains(event.target)) setDismissed(queryKey);
    };
    place();
    const observer = new ResizeObserver(schedule);
    if (inputRef.current) observer.observe(inputRef.current);
    window.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, true);
    document.addEventListener("pointerdown", dismiss);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); window.removeEventListener("resize", schedule);
      window.removeEventListener("scroll", schedule, true); document.removeEventListener("pointerdown", dismiss); };
  }, [open, queryKey, inputRef]);
  useEffect(() => { listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); }, [activeIndex]);
  const mentioned = skills.filter(item => new RegExp(`@${item.id}(?![a-z0-9/-])`, "i").test(value)
    || !item.shadowed_by && new RegExp(`@${item.name}(?![a-z0-9/-])`, "i").test(value));

  // 插入库名避免同名歧义；选中右半段也一起替换，保留其余原话。
  const insert = useCallback((skill: SkillInfo, start: number, end: number, values: Record<string, string>) => {
    const current = latestValue.current;
    const invocation = `@${skill.id} `;
    let result = current.slice(0, start) + invocation + current.slice(end);
    if (Object.keys(values).length) {
      const marker = `技能参数（@${skill.id}）：`;
      result = result.split("\n").filter(line => !line.startsWith(marker)).join("\n").trimEnd()
        + `\n${marker}${JSON.stringify(values)}\n`;
    }
    onChange(result);
    setPending(null); setDismissed(""); setHighlight(0);
    const nextCursor = start + invocation.length;
    setCursor(nextCursor);
    requestAnimationFrame(() => { inputRef.current?.focus(); inputRef.current?.setSelectionRange(nextCursor, nextCursor); });
  }, [inputRef, onChange]);

  // 有声明的填空才打开参数面板，简单技能一次插入，不强制额外步骤。
  const choose = (skill: SkillInfo) => {
    if (!match) return;
    const start = cursor - query.length - 1;
    const right = value.slice(cursor).match(/^[\p{L}\p{N}/-]*/u)?.[0].length || 0;
    const end = cursor + right;
    if (skill.adapter.parameters?.length) {
      setParameters(Object.fromEntries(skill.adapter.parameters.map(parameter => [parameter.name, parameter.default || ""])));
      setPending({ skill, start, end });
    } else insert(skill, start, end, {});
  };

  return <div className="skill-composer">
    <textarea className="skill-chat-input" ref={inputRef} value={value} placeholder={placeholder} rows={1}
      aria-label="对话输入，可用@选择技能" aria-controls={open ? listId : undefined}
      aria-expanded={open} aria-autocomplete="list"
      aria-activedescendant={open ? `${listId}-${activeIndex}` : undefined}
      onChange={event => { setCursor(event.target.selectionStart); setHighlight(0); setDismissed(""); onChange(event.target.value); }}
      onSelect={event => setCursor(event.currentTarget.selectionStart)}
      onCompositionStart={() => setComposing(true)} onCompositionEnd={event => { setComposing(false); setCursor(event.currentTarget.selectionStart); }}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || composing) return;
        if (open) {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault(); setHighlight(index => (index + (event.key === "ArrowDown" ? 1 : candidates.length - 1)) % candidates.length); return;
          }
          if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
            event.preventDefault(); choose(candidates[activeIndex]); return;
          }
          if (event.key === "Escape") { event.preventDefault(); setDismissed(queryKey); return; }
        }
        onKeyDown(event);
      }} />
    {open && popup && createPortal(<div ref={listRef} className="skill-mention-options" style={popup} id={listId} role="listbox" aria-label="可用技能">
      <div className="skill-mention-heading">技能 <small>↑↓ 选择 · Enter / Tab 插入 · Esc 收起</small></div>
      {candidates.map((skill, index) => <button type="button" key={skill.id} id={`${listId}-${index}`} role="option" aria-selected={index === activeIndex}
        className={index === activeIndex ? "active" : ""} onMouseDown={event => event.preventDefault()} onClick={() => choose(skill)}>
        <strong>{skill.display_name}<small>@{skill.id}</small></strong><span>{skill.description.split(/[。\n]/)[0]}</span>
      </button>)}
    </div>, document.body)}
    {!!mentioned.length && <div className="skill-mention-chips" aria-label="输入中提到的技能，执行前由后端核对">{mentioned.map(skill => <span key={skill.id}>{skill.display_name}<small>{skill.scope === "book" ? "本书" : skill.scope === "personal" ? "个人" : "内置"}</small></span>)}</div>}
    {match && loadError && <p className="form-hint" role="status">{loadError}</p>}
    {pending && <div className="skill-parameter-panel" role="dialog" aria-label={`${pending.skill.display_name}的填空`}
      onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); } }}>
      <strong>{pending.skill.display_name}</strong><p>这些内容只填入本次任务，目标、权限与原文范围仍按你的要求执行。</p>
      {pending.skill.adapter.parameters?.map(parameter => <label key={parameter.name}>{parameter.label || parameter.name}{parameter.required ? "（必填）" : "（可选）"}
        <input autoFocus value={parameters[parameter.name] || ""} maxLength={4000} onChange={event => setParameters(current => ({ ...current, [parameter.name]: event.target.value }))} />
      </label>)}
      <div className="skills-actions"><button type="button" onClick={() => setPending(null)}>取消</button><button className="primary" type="button"
        disabled={pending.skill.adapter.parameters?.some(parameter => parameter.required && !parameters[parameter.name]?.trim())}
        onClick={() => insert(pending.skill, pending.start, pending.end, parameters)}>填入对话</button></div>
    </div>}
  </div>;
}
