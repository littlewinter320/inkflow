import { MotionPresence } from "./MotionPresence";
import { useEffect, useId, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { readableSourceText } from "./readableNames";
import { readableValue } from "./readableRecords";
import { groupContextMaterials, type ContextMaterialGroup, type ContextMaterialItem } from "./contextComposition";

export type ContextMaterialsSnapshot = {
  updated_at?: string; packet_hash?: string; chapter_no?: number | null;
  material_scope?: string; task?: string; task_id?: string; run_id?: string;
  estimated_tokens?: number; change_since_previous_tokens?: number | null;
  record_kind?: string; configured_hard_limit_tokens?: number; hard_limit_tokens?: number; output_reserve_tokens?: number | null;
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

function NarrativeMaterialView({ content }: { content: string }) {
  let value: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(content);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Invalid narrative material");
    value = parsed as Record<string, unknown>;
  } catch { return <p>这份事件对照暂时无法读取，请重新打开当前资料。</p>; }
  const records = (input: unknown): Record<string, unknown>[] => Array.isArray(input)
    ? input.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
  const chapter = (input: unknown) => typeof input === "number" && Number.isInteger(input) && input > 0 ? input : undefined;
  const sources = records(value.sources).filter(item => chapter(item.chapter_no) && typeof item.body === "string");
  const events = records(value.timeline).filter(item => chapter(item.chapter_no) && typeof item.summary === "string");
  const coverage: Record<string, unknown> = records([value.coverage])[0] || {};
  const count = (key: string) => typeof coverage[key] === "number" && Number.isFinite(coverage[key]) ? Math.max(0, coverage[key] as number) : 0;
  const gaps = records(coverage.history_gaps);
  const relations: Record<string, string> = { before: "先于", after: "晚于", overlaps: "时间重叠", same_event: "同一事件的重述" };
  const modes: Record<string, string> = { current: "当前叙述", flashback: "回忆", dream: "梦境", hypothetical: "假设", belief: "人物信念", rumor: "传闻" };
  const statuses: Record<string, string> = { explicit: "原文明确", inferred: "依据剧情推断", unknown: "时间待核" };
  return <div className="narrative-material">
    <p>本次装入 {sources.length} 处原文与 {events.length} 个相关事件。事件按正文出处列出，排列先后不代表故事时间；资料已装入不代表模型已正确采用。</p>
    {(count("history_gap_count") > 0 || count("provisional_gap_count") > 0 || count("omitted_source_count") > 0 || count("timeline_events_omitted") > 0 || count("unavailable_source_count") > 0 || typeof coverage.reason === "string") && <aside role="status">
      {typeof coverage.reason === "string" && <p>{readableSourceText(coverage.reason)}</p>}
      {count("history_gap_count") > 0 && <p>{count("history_gap_count")} 章事件索引缺失或需要重新核对，不能据此声称时间线完整。</p>}
      {count("unavailable_source_count") > 0 && <p>{count("unavailable_source_count")} 章原文暂时无法核对。</p>}
      {count("provisional_gap_count") > 0 && <p>{count("provisional_gap_count")} 项同批次临时事件索引缺失或来源需要核对。</p>}
      {count("omitted_source_count") > 0 && <p>{count("omitted_source_count")} 处候选因资料容量或来源数量限制未装入。</p>}
      {count("timeline_events_omitted") > 0 && <p>{count("timeline_events_omitted")} 个关联事件未能装入。</p>}
      {gaps.length > 0 && <details><summary>查看涉及章节</summary><p>{gaps.map(item => chapter(item.chapter_no)).filter(Boolean).map(number => `第 ${number} 章`).join("、")}{count("history_gap_count") > gaps.length ? "等" : ""}</p></details>}
    </aside>}
    <h4>场景原文对照</h4>
    {!sources.length && <p>本对照未装入原文。需要结合本次其他资料或继续补源，事件摘要不能替代原文。</p>}
    <div className="narrative-source-grid">{sources.map((source, index) => <details key={String(source.source_id || index)}>
      <summary>{source.source_status === "provisional" ? "同批次临时" : "已接受"}第 {chapter(source.chapter_no)} 章 · {source.scope === "whole_chapter_fallback" ? "整章回读" : "场景原文"}</summary>
      <p>{source.body as string}</p>
    </details>)}</div>
    <h4>相关事件与时间依据</h4>
    {count("timeline_sources_not_in_comparison_count") > 0 && <p>{count("timeline_sources_not_in_comparison_count")} 个事件的原文未在此处装入；其他资料可能已有，判断前仍须核对。</p>}
    {!events.length && <p>本次没有可展示的有效事件索引；不表示剧情中没有相关事件。</p>}
    <ol className="narrative-event-list">{events.map((event, index) => <li key={String(event.source_id || index)}>
      <strong>{event.source_status === "provisional" ? "同批次临时" : "已接受"}第 {chapter(event.chapter_no)} 章 · {event.summary as string}</strong>
      <p>{modes[String(event.narrative_mode)] || "叙事层级待核"} · {statuses[String(event.time_status)] || "时间待核"}{typeof event.event_time === "string" ? `：${event.event_time}` : ""}</p>
      {Array.isArray(event.participants) && <p>参与者：{event.participants.filter(item => typeof item === "string").join("、")}{typeof event.location === "string" && event.location ? ` · ${event.location}` : ""}</p>}
      <details><summary>查看原句与事件关系</summary>
        {typeof event.evidence === "string" && <p>事件定位原句：{event.evidence}</p>}
        {records(event.time_evidence).map((ref, position) => typeof ref.quote === "string" && <p key={position}>第 {chapter(ref.source_chapter) || "待核"} 章时间依据：{ref.quote}</p>)}
        {records(event.relations).map((link, position) => {
          const target = events.find(item => item.chapter_no === link.target_chapter && item.event_id === link.target_event_id);
          return <p key={position}>{relations[String(link.relation)] || "关系待核"}：第 {chapter(link.target_chapter) || "待核"} 章{target ? ` · ${String(target.summary)}` : "的关联事件（本轮未装入该索引）"}{typeof link.evidence === "string" ? `。关系依据：${link.evidence}` : ""}</p>;
        })}
        <p>这些索引用于定位和对照；时间推断与事件摘要都不能单独充当正文证据。</p>
      </details>
    </li>)}</ol>
  </div>;
}

function RetrievalFeedback({ content, projectRoot, role, snapshot }: {
  content: string; projectRoot: string; role: string; snapshot: ContextMaterialsSnapshot;
}) {
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  let hits: Array<Record<string, unknown>> = [];
  try {
    const parsed: unknown = JSON.parse(content);
    const value = Array.isArray(parsed) ? parsed : parsed && typeof parsed === "object"
      ? (parsed as Record<string, unknown>)["自适应结果"] : [];
    if (Array.isArray(value)) hits = value.filter((item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && typeof item.source_id === "string" && Boolean(item.source_fingerprint));
  } catch { return null; }
  if (!hits.length) return null;
  const send = async (sourceId: string, outcome: string) => {
    setSaving(current => ({ ...current, [sourceId]: true }));
    try {
      const result = await window.inkflow.request<{ ranking_eligible: boolean }>("retrieval.feedback", {
        project_root: projectRoot, source_id: sourceId, outcome, role,
        chapter_no: snapshot.chapter_no, packet_id: snapshot.packet_hash,
      });
      setMessages(current => ({ ...current, [sourceId]: result.ranking_eligible
        ? "已记录，将有限调整同查询、同角色、同版资料的排序。" : "已记录；这份旧资料缺少完整绑定，暂不用于调整排序。" }));
    } catch { setMessages(current => ({ ...current, [sourceId]: "反馈未保存，请稍后重试。" })); }
    finally { setSaving(current => ({ ...current, [sourceId]: false })); }
  };
  return <details><summary>反馈本次检索资料 · {hits.length} 项</summary>
    <p>反馈只帮助选择资料，不会删除书稿、改变正史或把引用次数当成事实证明。</p>
    {hits.map(hit => <div key={String(hit.source_id)}><strong>{String(hit.title || "本次资料")}</strong>
      <div>{Object.entries({ cited: "有帮助", ignored: "这次不相关", misleading: "这次易误导" }).map(([value, label]) =>
        <button type="button" key={value} disabled={saving[String(hit.source_id)]} onClick={() => void send(String(hit.source_id), value)}>{label}</button>)}</div>
      <p role="status">{messages[String(hit.source_id)] || "未提交反馈"}</p>
    </div>)}
  </details>;
}

// Render material proportions with a matching legend; these segments are not budget usage meters.
export function ContextMaterials({ projectRoot, role, snapshot, emptyLabel = "旧请求未保存分类资料，下次准备任务时会更新。" }: { projectRoot?: string; role: string; snapshot?: ContextMaterialsSnapshot; emptyLabel?: string }) {
  const [detail, setDetail] = useState<{ key: string; text: string; rawContent?: string; loading?: boolean } | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const tooltipId = useId();
  const request = useRef(0);
  useEffect(() => { request.current++; setDetail(null); setHovered(null); setExpandedGroup(null); return () => { request.current++; }; }, [projectRoot, role, snapshot?.packet_hash, snapshot?.updated_at]);
  const { total, items, groups } = groupContextMaterials(snapshot?.budget_allocation || []);
  if (!total) return <p className="context-materials-empty">{emptyLabel}</p>;
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
      if (request.current === id) setDetail({ key, text: key === "F1" ? "" : materialText(result.content), rawContent: result.content });
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
    <div className="context-composition" aria-label="资料内部组成，不是预算占用：点击查看依据">{groups.map(item => {
      const percent = item.estimated_tokens / total * 100;
      return <button key={item.key} type="button" aria-label={`${item.title}，资料内占比 ${percent.toFixed(1)}%`}
        aria-describedby={hovered === item.key ? tooltipId : undefined} aria-expanded={expandedGroup === item.key}
        onMouseEnter={() => setHovered(item.key)} onMouseLeave={() => setHovered(null)} onFocus={() => setHovered(item.key)} onBlur={() => setHovered(null)}
        onClick={() => openGroup(item)} style={{ width: `${percent}%`, "--context-segment-color": `var(--context-color-${item.color})`, background: "var(--context-segment-fill)" } as CSSProperties} />;
    })}</div>
    <div className="context-composition-legend" aria-label="资料分类与占比">{groups.map(item => <button key={item.key} type="button" aria-expanded={expandedGroup === item.key}
      onClick={() => openGroup(item)} style={{ "--context-segment-color": `var(--context-color-${item.color})`, background: "transparent" } as CSSProperties}>
      <i aria-hidden="true" /><span>{item.title}</span><small>{(item.estimated_tokens / total * 100).toFixed(1)}%</small>
    </button>)}</div>
    {<MotionPresence open={Boolean(selected)}>{() => (selected && <div className="context-segment-tooltip" id={tooltipId} role="tooltip"><strong>{selected.title}{selected.color === "other" ? ` · ${selected.members.length} 类` : ""}</strong><span>资料内占比 {(selected.estimated_tokens / total * 100).toFixed(1)}% · 约 {selected.estimated_tokens.toLocaleString()} 标记</span><span>{selected.source_count} 项来源{selected.changed ? " · 本次有变化" : ""} · 点击查看依据</span><span>最近准备的资料组成，不代表模型已采用。</span></div>)}</MotionPresence>}
    {<MotionPresence open={Boolean(expanded && (expanded.color === "other" || expanded.members.length > 1))}>{() => (expanded && (expanded.color === "other" || expanded.members.length > 1) && <div className="context-material-list"><strong>{expanded.title} · {expanded.members.length} 份资料</strong>{expanded.members.map(itemButton)}</div>)}</MotionPresence>}
    {expanded && <details className="context-material-list"><summary>完整分类明细 · {items.length} 类</summary>{items.map(itemButton)}</details>}
    {<MotionPresence open={Boolean(detail)}>{() => (detail && <div className="context-material-prose" aria-busy={detail.loading}><strong>{items.find(item => item.key === detail.key)?.title || "原文依据"}</strong><button type="button" onClick={() => setDetail(null)}>收起依据</button>{detail.key === "F1" && detail.rawContent !== undefined ? <NarrativeMaterialView content={detail.rawContent} /> : <p>{detail.text}</p>}
      {["F", "F0"].includes(detail.key) && detail.rawContent !== undefined && projectRoot && snapshot?.packet_hash
        && <RetrievalFeedback key={`${snapshot.packet_hash}:${detail.key}`} content={detail.rawContent} projectRoot={projectRoot} role={role} snapshot={snapshot} />}
    </div>)}</MotionPresence>}
  </div>;
}
