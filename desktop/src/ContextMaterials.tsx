import { useEffect, useId, useRef, useState } from "react";
import { readableSourceText } from "./readableNames";
import { readableValue } from "./readableRecords";
import { groupContextMaterials, type ContextMaterialGroup, type ContextMaterialItem } from "./contextComposition";

export type ContextMaterialsSnapshot = {
  updated_at?: string; packet_hash?: string; chapter_no?: number;
  estimated_tokens?: number; change_since_previous_tokens?: number | null;
  record_kind?: string; configured_hard_limit_tokens?: number; hard_limit_tokens?: number; output_reserve_tokens?: number;
  budget_allocation?: ContextMaterialItem[];
};

function publicMaterial(value: unknown, depth = 0): unknown {
  if (depth > 12) return "更深层的记录已省略";
  if (Array.isArray(value)) return value.map(item => publicMaterial(item, depth + 1));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !/编号|哈希|指纹|(?:^|_)ids?$|hash|fingerprint|token|schema|revision|source_role|authority|located|semantic_support/i.test(key))
    .map(([key, item]) => [key, publicMaterial(item, depth + 1)]));
  return value;
}

function materialText(content: string): string {
  try {
    const value = JSON.parse(content);
    if (Array.isArray(value?.collections)) return value.collections.map((collection: { name: string; instructions?: string; fields?: Array<{ key: string; label: string }>; records?: Array<{ title: string; values: Record<string, unknown>; evidence_refs?: Array<{ chapter_no?: number; quote?: string }> }> }) =>
      [collection.name, collection.instructions || "", ...(collection.records || []).map(record =>
        [record.title, ...Object.entries(record.values).map(([key, item]) => `${collection.fields?.find(field => field.key === key)?.label || (key === "requirement" ? "要求" : "内容")}：${readableValue(item)}`),
          ...(record.evidence_refs || []).filter(ref => ref.quote).map(ref => `${ref.chapter_no ? `第 ${ref.chapter_no} 章` : "书籍要求"}原句：${ref.quote}`)].join("\n"))].join("\n\n")).join("\n\n");
    const clean = publicMaterial(value);
    return Array.isArray(clean) ? clean.map(readableValue).join("\n\n") : readableValue(clean);
  } catch { return readableSourceText(content); }
}

export function ContextMaterials({ projectRoot, role, snapshot, emptyLabel = "旧请求未保存分类资料，下次准备任务时会更新。" }: { projectRoot?: string; role: string; snapshot?: ContextMaterialsSnapshot; emptyLabel?: string }) {
  const [detail, setDetail] = useState<{ key: string; text: string; loading?: boolean } | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const tooltipId = useId();
  const request = useRef(0);
  useEffect(() => { request.current++; setDetail(null); setHovered(null); setExpandedGroup(null); return () => { request.current++; }; }, [projectRoot, role, snapshot?.packet_hash, snapshot?.updated_at]);
  const { total, items, groups } = groupContextMaterials(snapshot?.budget_allocation || []);
  if (!total) return <div className="context-materials"><div className="context-composition context-empty"><button type="button" title={emptyLabel} aria-label={emptyLabel} /></div></div>;
  const selected = groups.find(item => item.key === hovered);
  const expanded = groups.find(item => item.key === expandedGroup);
  const load = async (key: string, force = false) => {
    if (!force && detail?.key === key) { request.current++; setDetail(null); return; }
    if (!projectRoot || !snapshot?.packet_hash) {
      setDetail({ key, text: "这份旧记录只保存了占比，尚无原文详情。下次执行后可在这里查看。" });
      return;
    }
    const id = ++request.current;
    setDetail({ key, text: "正在读取这份依据…", loading: true });
    try {
      const result = await window.inkflow.request<{ content: string }>("context.details", {
        project_root: projectRoot, role, section_key: key, updated_at: snapshot?.updated_at, packet_hash: snapshot?.packet_hash,
      });
      if (request.current === id) setDetail({ key, text: materialText(result.content) });
    } catch { if (request.current === id) setDetail({ key, text: "这份依据暂时无法读取，可能已被新任务更新。请收起后重新打开；当前写作不会因此重做。" }); }
  };
  const openGroup = (group: ContextMaterialGroup) => {
    request.current++; setDetail(null);
    if (expandedGroup === group.key) { setExpandedGroup(null); return; }
    setExpandedGroup(group.key);
    if (group.members.length === 1 && group.color !== "other") void load(group.members[0].key, true);
  };
  const itemButton = (item: ContextMaterialItem) => <button type="button" key={item.key}
    aria-expanded={detail?.key === item.key} onClick={() => void load(item.key)}>
    <span>{item.title}</span><small>{(item.estimated_tokens / total * 100).toFixed(1)}% · 约 {item.estimated_tokens.toLocaleString()} 标记 · {item.source_count} 项来源</small>
  </button>;
  return <div className="context-materials">
    <div className="context-composition" aria-label="资料组成：悬停查看占比，点击查看依据">{groups.map(item => {
      const percent = item.estimated_tokens / total * 100;
      return <button key={item.key} type="button" aria-label={`${item.title}，资料内占比 ${percent.toFixed(1)}%`}
        aria-describedby={hovered === item.key ? tooltipId : undefined} aria-expanded={expandedGroup === item.key}
        onMouseEnter={() => setHovered(item.key)} onMouseLeave={() => setHovered(null)} onFocus={() => setHovered(item.key)} onBlur={() => setHovered(null)}
        onClick={() => openGroup(item)} style={{ width: `${percent}%`, background: `var(--context-color-${item.color})` }} />;
    })}</div>
    {selected && <div className="context-segment-tooltip" id={tooltipId} role="tooltip"><strong>{selected.title}{selected.color === "other" ? ` · ${selected.members.length} 类` : ""}</strong><span>资料内占比 {(selected.estimated_tokens / total * 100).toFixed(1)}% · 约 {selected.estimated_tokens.toLocaleString()} 标记</span><span>{selected.source_count} 项来源{selected.changed ? " · 本次有变化" : ""} · 点击查看依据</span><span>最近准备的资料组成，不代表模型已采用。</span></div>}
    {expanded?.color === "other" && <div className="context-material-list"><strong>其他 · {expanded.members.length} 类资料</strong>{expanded.members.map(itemButton)}</div>}
    {expanded && <details className="context-material-list"><summary>完整分类明细 · {items.length} 类</summary>{items.map(itemButton)}</details>}
    {detail && <div className="context-material-prose" aria-busy={detail.loading}><strong>{items.find(item => item.key === detail.key)?.title || "原文依据"}</strong><button type="button" onClick={() => setDetail(null)}>收起依据</button><p>{detail.text}</p></div>}
  </div>;
}
