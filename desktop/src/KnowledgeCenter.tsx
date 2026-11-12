import { useEffect, useRef, useState } from "react";
import { KnowledgeGraph, knowledgeColors as colors, type KnowledgeGraphData } from "./KnowledgeGraph";
import { readableSourceText } from "./readableNames";
import "./knowledge.css";

type Section = { id: string; title: string; body: string; source_id: string; chapter: number | null; state: string; quote: string; importance: string; current?: boolean; event_time?: string; conflict?: string; candidate_id?: string; decision?: string; decision_revision?: number };
type Node = { id: string; name: string; kind: string; aliases: string[]; sources: string[]; section_count: number; conflicts: number };
type Page = Node & { sections: Section[]; note: string; revision: number };
type Gap = Pick<Section, "source_id" | "title" | "state" | "candidate_id" | "decision" | "decision_revision"> & {
  question: string; category?: string; answered?: boolean;
  evidence?: Array<Pick<Section, "source_id" | "title" | "body" | "state"> & { answered?: boolean }>;
};
type Source = { id: string; title: string; state: string; path: string; chapter?: number | null; body?: string; excerpt?: string; previous?: string; next?: string; record_id?: string; collection_id?: string };
type Edge = { id: string; source: string; target: string; relation: string; source_id: string; chapter: number | null; state: string; event_time?: string };
export type KnowledgeProgress = { total: number; completed: number; pending: number; failed: number; structured: number; characters: number; read_characters: number; imports: Array<{ import_id: string; title: string; status: string; characters: number; parts: number; error: string; run_id: string }>; control: { status?: string; run_id?: string; auto?: boolean; error?: string; read_complete?: boolean } };
type Progress = KnowledgeProgress;
type Snapshot = { nodes: Node[]; edges: Edge[]; sources: Source[]; problems: string[]; gaps?: Gap[]; kinds: Record<string, string>; states: Record<string, string>; progress: Progress };
type Search = { entity_ids: string[]; sources: Source[] };
const statuses: Record<string, string> = { queued: "等待整理", running: "正在整理", completed: "本轮整理完成", waiting_user: "需要你决定", waiting_condition: "部分完成", failed: "整理遇到问题", cancelled: "已停止", interrupted: "已保留断点" };
/** 全书知识共用目录、完整页面、来源与图谱，异步返回严格绑定当前项目。 */
export function KnowledgeCenter({ projectRoot, refreshKey = "", graphLimit, onGraphLimitChange, onEditSetting }: { projectRoot: string;
  refreshKey?: string; graphLimit: number | null; onGraphLimitChange: (limit: number | null) => void;
  onEditSetting?: (recordId: string, collectionId: string) => void }) {
  const root = useRef(projectRoot); root.current = projectRoot;
  const sourceRequest = useRef(0);
  const reading = useRef<HTMLDivElement>(null);
  const graphSection = useRef<HTMLElement>(null);
  const [snapshot, setSnapshot] = useState<{ root: string; value: Snapshot } | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [query, setQuery] = useState(""); const [kind, setKind] = useState(""); const [selected, setSelected] = useState("");
  const [page, setPage] = useState<Page | null>(null); const [source, setSource] = useState<Source | null>(null);
  const [graph, setGraph] = useState<KnowledgeGraphData | null>(null); const [localGraph, setLocalGraph] = useState(false);
  const [graphVisible, setGraphVisible] = useState(false);
  const [eventLimit, setEventLimit] = useState(12);
  const [gapFilter, setGapFilter] = useState("consistency");
  const [gapLimit, setGapLimit] = useState(18);
  const [search, setSearch] = useState<Search | null>(null); const [revision, setRevision] = useState(0); const [busy, setBusy] = useState(false);
  const [error, setError] = useState(""); const [notice, setNotice] = useState(""); const [shared, setShared] = useState(false);
  const [edit, setEdit] = useState<{ id: string; revision: number; note: string; aliases: string } | null>(null);
  const [claimEdit, setClaimEdit] = useState<{ id: string; revision: number; body: string } | null>(null);
  const current = snapshot?.root === projectRoot ? snapshot.value : null;
  useEffect(() => { setSnapshot(null); setSelected(""); setPage(null); setSource(null); setGraph(null); setEventLimit(12); setEdit(null); setClaimEdit(null); setProgress(null); setError(""); }, [projectRoot]);
  useEffect(() => { let alive = true; void window.inkflow.request<Snapshot>("knowledge.index", { project_root: projectRoot }).then(value => { if (alive) { setSnapshot({ root: projectRoot, value }); setProgress(value.progress); setError(""); setSelected(previous => value.nodes.some(node => node.id === previous) ? previous : value.nodes.find(node => node.kind === "character")?.id || value.nodes[0]?.id || ""); } }).catch(cause => { if (alive) setError(readableSourceText(String(cause))); }); return () => { alive = false; }; }, [projectRoot, refreshKey, revision]);
  useEffect(() => { let alive = true; let previous = ""; const timer = window.setInterval(() => { void window.inkflow.request<Progress>("knowledge.status", { project_root: projectRoot }).then(value => {
    if (!alive) return; const key = `${value.completed}:${value.control.status}`; if (previous && previous !== key) setRevision(number => number + 1); previous = key; setProgress(value);
  }).catch(cause => { if (alive) setError(readableSourceText(String(cause))); }); }, 6000); return () => { alive = false; window.clearInterval(timer); }; }, [projectRoot]);
  useEffect(() => { let alive = true; if (selected) void window.inkflow.request<Page>("knowledge.page", { project_root: projectRoot, entity_id: selected }).then(value => { if (alive) setPage(value); }).catch(cause => { if (alive) setError(readableSourceText(String(cause))); }); return () => { alive = false; }; }, [projectRoot, selected, revision]);
  const graphFocus = localGraph ? selected : "";
  // 同页滚到图谱附近才计算节点位置，避免打开目录时就布局全部节点。
  useEffect(() => {
    if (!graphSection.current) return;
    const observer = new IntersectionObserver(([entry]) => setGraphVisible(entry.isIntersecting), { rootMargin: "240px" });
    observer.observe(graphSection.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!graphVisible) return;
    let alive = true;
    void window.inkflow.request<KnowledgeGraphData>("knowledge.graph", { project_root: projectRoot, focus: graphFocus, limit: graphLimit })
      .then(value => { if (alive) setGraph(value); })
      .catch(cause => { if (alive) setError(readableSourceText(String(cause))); });
    return () => { alive = false; };
  }, [projectRoot, graphVisible, graphFocus, graphLimit, revision]);
  useEffect(() => { let alive = true; setSearch(null); if (!query.trim()) return; const timer = window.setTimeout(() => { void window.inkflow.request<Search>("knowledge.search", { project_root: projectRoot, query }).then(value => { if (alive) setSearch(value); }).catch(cause => { if (alive) setError(readableSourceText(String(cause))); }); }, 300); return () => { alive = false; window.clearTimeout(timer); }; }, [projectRoot, query, revision]);

  /** 请求完成只更新原项目，编辑时沿用开始编辑时的修订号。 */
  async function act(method: string, params: Record<string, unknown> = {}) {
    const identity = projectRoot; setBusy(true); setError(""); setNotice("");
    try { const result = await window.inkflow.request<Record<string, unknown>>(method, { project_root: identity, ...params }); if (root.current !== identity) return;
      setNotice(typeof result.summary === "string" ? result.summary : method === "knowledge.start" ? "已加入后台整理，可以继续写作。" : "已保存。"); setRevision(value => value + 1);
      if (method === "knowledge.annotate") setEdit(null); if (method === "knowledge.export" && typeof result.directory === "string") await window.inkflow.openPath(result.directory);
      if (method === "knowledge.decide") setClaimEdit(null);
    } catch (cause) { if (root.current === identity) setError(readableSourceText(String(cause))); } finally { if (root.current === identity) setBusy(false); }
  }
  /** 图谱和事件共用目录右侧的阅读区，打开资料时带回可见位置。 */
  function revealReading() {
    if (!reading.current) return;
    reading.current.scrollTop = 0;
    reading.current.scrollIntoView({ block: "nearest" });
  }
  /** 原文由服务重新核来源后返回，界面不自行拼接文件路径。 */
  async function openSource(id: string) { const identity = projectRoot, requestId = ++sourceRequest.current; try { const value = await window.inkflow.request<Source>("knowledge.source", { project_root: identity, source_id: id }); if (root.current === identity && requestId === sourceRequest.current) { setSource(value); revealReading(); } } catch (cause) { if (root.current === identity && requestId === sourceRequest.current) setError(readableSourceText(String(cause))); } }
  const active = ["queued", "running"].includes(progress?.control.status || "");
  const resumable = ["failed", "cancelled", "interrupted", "waiting_condition"].includes(progress?.control.status || "");
  const awaitingSources = progress?.control.status === "waiting_condition" && progress.control.read_complete && !progress.pending && !progress.failed;
  const filtered = (current?.nodes || []).filter(node => (!kind || node.kind === kind) && (!query.trim() || [node.name, ...node.aliases].some(name => name.toLowerCase().includes(query.toLowerCase())) || search?.entity_ids.includes(node.id)));
  // 目录只提供原文所在章节。按章节依据排列，实际事件时间在档案中查看。
  const chapterBySource = new Map(current?.sources.map(item => [item.id, item.chapter]));
  const events = (current?.nodes || []).filter(node => node.kind === "event").map(node => ({
    node, chapter: node.sources.reduce((first, id) => Math.min(first, chapterBySource.get(id) ?? Infinity), Infinity),
  })).sort((a, b) => a.chapter - b.chapter);
  // 球体聚焦时保留画布位置，点开档案或事件才跳转阅读区。
  const choose = (id: string) => { sourceRequest.current += 1; setSelected(id); setSource(null); if (reading.current) reading.current.scrollTop = 0; };
  const state = (value: string) => current?.states[value] || value;
  const graphNode = graph?.nodes.find(node => node.id === selected);
  const graphPage = page?.id === selected ? page : null;
  const highlights = graphPage?.sections.filter(section => section.state !== "dismissed") || [];
  const highlight = highlights.find(section => section.importance === "core") || highlights[0];
  const graphRelations = current?.edges.filter(edge => edge.source === selected || edge.target === selected) || [];
  const gapGroups: Record<string, Gap[]> = { consistency: [], citation: [], answered: [], open_question: [], context: [], author: [] };
  for (const gap of current?.gaps || []) {
    const group = gap.state !== "unverified" ? "author" : gap.answered ? "answered" : gap.category || "open_question";
    (gapGroups[group] || gapGroups.open_question).push(gap);
  }
  const gapNames: Record<string, string> = { consistency: "情节与版本待核", citation: "引用待核", answered: "已有原文回答",
    open_question: "未解线索与留白", context: "阅读范围提示", author: "作者已处理" };
  const needsCheck = gapGroups.consistency.length + gapGroups.citation.length;
  const visibleGapGroup = gapGroups[gapFilter]?.length ? gapFilter : Object.keys(gapGroups).find(key => gapGroups[key].length) || gapFilter;
  const visibleGaps = gapGroups[visibleGapGroup] || [];

  /** 归纳与缺口共用取舍操作，提交绑定开始编辑时看到的版本。 */
  function decisionControls(item: Pick<Section, "state" | "candidate_id" | "decision" | "decision_revision">, body: string) {
    const id = item.candidate_id;
    if (!id) return null;
    const decide = (decision: string) => void act("knowledge.decide", { candidate_id: id, decision, expected_revision: item.decision_revision });
    return <details className="knowledge-decisions"><summary>编辑与取舍</summary>
      <div className="knowledge-actions">
        {item.state !== "dismissed" && <>
          <button type="button" disabled={busy} onClick={() => setClaimEdit({ id, revision: item.decision_revision || 0, body })}>编辑为作者补充</button>
          {item.state !== "hypothesis" && <button type="button" disabled={busy} onClick={() => decide("hypothesis")}>保留为设想</button>}
          <button type="button" disabled={busy} onClick={() => decide("dismiss")}>搁置这条</button>
        </>}
        {item.decision && item.decision !== "restore" && <button type="button" disabled={busy} onClick={() => decide("restore")}>恢复原内容</button>}
      </div>
      {claimEdit?.id === id && <div className="knowledge-edit">
        <label>作者补充<textarea rows={5} maxLength={16000} value={claimEdit.body} onChange={event => setClaimEdit({ ...claimEdit, body: event.target.value })} /></label>
        <div className="knowledge-actions"><button type="button" disabled={busy || !claimEdit.body.trim()} onClick={() => void act("knowledge.decide", { candidate_id: id, decision: "edit", body: claimEdit.body, expected_revision: claimEdit.revision })}>保存补充</button><button type="button" onClick={() => setClaimEdit(null)}>取消</button></div>
      </div>}
    </details>;
  }

  // 只改变图谱显示请求的上限。输入不合法时保留作者上次有效选择。
  const limitControls = <div className="knowledge-graph-limit">
    <label>节点上限<input key={graphLimit ?? "all"} type="number" min={1} step={1}
      disabled={graphLimit === null} defaultValue={graphLimit ?? 50}
      onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); } }}
      onBlur={event => {
        const next = event.currentTarget.valueAsNumber;
        if (Number.isSafeInteger(next) && next > 0) onGraphLimitChange(next);
        else event.currentTarget.value = String(graphLimit ?? 50);
      }} /></label>
    <label><input type="checkbox" checked={graphLimit === null}
      onChange={event => onGraphLimitChange(event.target.checked ? null : 50)} />不限数量</label>
  </div>;

  return <section className="knowledge-center"><header className="knowledge-heading"><div><h2>全书知识库</h2></div><div className="knowledge-actions"><button type="button" onClick={() => setRevision(value => value + 1)}>刷新</button><button type="button" disabled={busy} onClick={() => void act("knowledge.export")}>导出 Wiki</button></div></header>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="knowledge-notice" role="status">{notice}</p>}
    <section className="knowledge-progress"><div><strong>{awaitingSources ? "全文已读，仍有资料待核" : statuses[progress?.control.status || ""] || "现有资料已关联，可开始全文精读"}</strong><p>{progress?.total ? `已阅读 ${progress.completed} / ${progress.total} 段 · ${progress.read_characters.toLocaleString()} / ${progress.characters.toLocaleString()} 字符` : "正在登记本书资料…"}</p>{progress && <progress max={Math.max(1, progress.total)} value={progress.completed} aria-label="全文阅读覆盖进度" />}{progress?.control.error && <p className="form-error" role="alert">{readableSourceText(progress.control.error)}</p>}</div>
      <div className="knowledge-actions">{active ? <button type="button" disabled={busy} onClick={() => void act("run.cancel", { run_id: progress?.control.run_id })}>停止整理</button> : !awaitingSources && <button type="button" className="primary" disabled={busy || !current} onClick={() => void act("knowledge.start", { resume: resumable && !progress?.control.read_complete })}>{resumable && !progress?.control.read_complete ? "继续整理" : progress?.completed ? "整理新增资料" : "开始全文整理"}</button>}
        {progress?.control.read_complete && !active && !progress.pending && !progress.failed && (needsCheck > 0 || gapGroups.open_question.length > 0)
          && <button type="button" disabled={busy} onClick={() => void act("knowledge.start", { gaps_only: true })}>后台补证（最多 6 次模型请求）</button>}
        <label><input type="checkbox" checked={Boolean(progress?.control.auto)} disabled={busy} onChange={event => void act("knowledge.auto", { enabled: event.target.checked })} />正式内容变化后自动维护</label></div></section>
    {Boolean(progress?.structured) && <p className="form-hint">{progress?.structured} 份已有结构化资料已完整接入，沿原审核服务维护。</p>}
    {progress?.imports?.filter(item => item.status !== "completed").map(item => <section className="knowledge-progress" key={item.import_id}><div><strong>{item.title}</strong><p>{item.status === "reading" ? `正在导入，已保存 ${item.parts} 段、${item.characters.toLocaleString()} 字符` : readableSourceText(item.error)}</p></div>{item.status === "reading" && item.run_id && <button type="button" onClick={() => void act("run.cancel", { run_id: item.run_id })}>停止导入</button>}</section>)}
    <div className="knowledge-toolbar"><label><input type="checkbox" checked={shared} onChange={event => setShared(event.target.checked)} />导入为跨书参考</label><button type="button" disabled={busy} onClick={() => { void window.inkflow.choosePreferenceMaterials().then(files => { if (files.length) return act("knowledge.import", { files, shared }); }).catch(cause => setError(String(cause))); }}>上传资料</button></div>
    <p className="form-hint">单个文件最多 1 GB。全文分批读取，模型调用使用当前任务预算。</p>
    <section className="knowledge-block" aria-label="知识目录"><h3>知识目录</h3><div className="knowledge-layout"><aside className="knowledge-directory" aria-label="知识目录"><input type="search" aria-label="搜索全部知识与原文" placeholder="名称、别名或全文内容" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="知识类型" value={kind} onChange={event => setKind(event.target.value)}><option value="">全部类型</option>{Object.entries(current?.kinds || {}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><small>{filtered.length} 个对象</small>
      <div className="knowledge-directory-items">{filtered.map(node => <button type="button" key={node.id} aria-current={node.id === selected ? "page" : undefined} onClick={() => choose(node.id)}><span className="knowledge-kind-dot" style={{ background: colors[node.kind] || "#82909c" }} /><span><strong>{node.name}</strong><small>{current?.kinds[node.kind]} · {node.section_count} 项内容{node.conflicts ? ` · ${node.conflicts} 项待定` : ""}</small></span></button>)}</div>
      {search?.sources.length ? <div className="knowledge-search-sources"><strong>原文命中</strong>{search.sources.map(item => <button type="button" key={item.id} onClick={() => void openSource(item.id)}>{item.title}<small>{item.excerpt}</small></button>)}</div> : null}</aside>
      <div className="knowledge-reading" ref={reading}>{source ? <article className="knowledge-source"><header><div><small>{state(source.state)}</small><h3>{source.title}</h3></div><button type="button" onClick={() => setSource(null)}>返回知识页</button></header>{source.record_id && onEditSetting && <button type="button" onClick={() => onEditSetting(source.record_id!, source.collection_id || "")}>编辑原设定</button>}{source.path && <button type="button" onClick={() => void window.inkflow.workspaceCommand("document", source.path, source.path)}>在工作台打开原资料</button>}<div className="knowledge-actions">{source.previous && <button type="button" onClick={() => void openSource(source.previous!)}>上一段</button>}{source.next && <button type="button" onClick={() => void openSource(source.next!)}>下一段</button>}</div><pre>{source.body}</pre></article> : <>
        {page?.id === selected ? <article className="knowledge-page"><header><div><small>{current?.kinds[page.kind]}</small><h3>{page.name}</h3>{page.aliases.length > 0 && <p>也称 {page.aliases.join("、")}</p>}</div><button type="button" onClick={() => setEdit({ id: page.id, revision: page.revision, note: page.note, aliases: page.aliases.join("\n") })}>维护档案</button></header>
          {edit?.id === page.id && <form className="knowledge-edit" onSubmit={event => { event.preventDefault(); void act("knowledge.annotate", { entity_id: edit.id, expected_revision: edit.revision, note: edit.note, aliases: edit.aliases.split(/\n|、/).map(value => value.trim()).filter(Boolean) }); }}><label>别名，每行一个<textarea value={edit.aliases} onChange={event => setEdit({ ...edit, aliases: event.target.value })} /></label><label>作者补充<textarea rows={5} value={edit.note} onChange={event => setEdit({ ...edit, note: event.target.value })} /></label><p className="form-hint">补充保留为作者备注。修改正文或已确认设定，请打开对应原资料，沿原流程保存和核对。</p><div className="knowledge-actions"><button type="submit" disabled={busy}>保存档案</button><button type="button" onClick={() => setEdit(null)}>取消编辑</button></div></form>}
          {page.note && <section className="knowledge-author-note"><h4>作者补充</h4><p>{page.note}</p></section>}{page.sections.map(section => <section key={section.id} className={`knowledge-section state-${section.state}`}><header><h4>{section.title}</h4><span>{state(section.state)}{section.current === false ? " · 历史状态" : ""}{section.importance === "core" ? " · 核心" : ""}</span></header>{section.event_time && <small>{section.event_time}</small>}<p>{section.body}</p>
            {["conflict", "unverified"].includes(section.state) && <p className="form-hint">{section.conflict}</p>}
            {decisionControls(section, section.body)}
            <button type="button" className="knowledge-source-link" onClick={() => void openSource(section.source_id)}>{current?.sources.find(item => item.id === section.source_id)?.title || "查看原文依据"} ↗</button></section>)}
          <section className="knowledge-backlinks"><h4>关联对象</h4>{current?.edges.filter(edge => edge.source === page.id || edge.target === page.id).map(edge => { const target = edge.source === page.id ? edge.target : edge.source; return <button type="button" key={edge.id} onClick={() => choose(target)}>{edge.source === page.id ? "→" : "←"} {edge.relation} · {current.nodes.find(node => node.id === target)?.name}</button>; })}</section></article>
          : <div className="knowledge-welcome"><h3>从一个人物、一件物品或一条伏笔开始</h3><p>在左侧选择对象，阅读完整档案、经历和关联资料。搜索也会查找原文中的内容。</p><div>{Object.entries(current?.kinds || {}).map(([key, label]) => { const count = current?.nodes.filter(node => node.kind === key).length || 0; return count > 0 ? <button type="button" key={key} onClick={() => setKind(key)}><strong>{count}</strong>{label}</button> : null; })}</div></div>}</>}
      </div></div></section>
      <section className="knowledge-block" aria-label="事件与时间">
        <h3>事件与时间 <small>{events.length} 项</small></h3>
        {events.length > 0 ? <>
          <div className="knowledge-events">{events.slice(0, eventLimit).map(({ node, chapter }) => <button type="button" key={node.id}
            onClick={() => { choose(node.id); revealReading(); }}><span>{node.name}</span>
            {Number.isFinite(chapter) && <small>第 {chapter} 章有依据</small>}</button>)}</div>
          {events.length > eventLimit && <button type="button" onClick={() => setEventLimit(limit => limit + 12)}>显示更多事件</button>}
        </> : <p className="form-hint">暂无已整理的事件。</p>}
      </section>
        <section className="knowledge-block" ref={graphSection} aria-label="全书关系图"><h3>全书关系图</h3><div className="knowledge-actions"><label><input type="checkbox" checked={localGraph} onChange={event => setLocalGraph(event.target.checked)} />只看所选对象的一跳关系</label></div>{graph ? <>
          <div className="knowledge-graph-legend" aria-label="图例">{Array.from(new Set(graph.nodes.map(node => node.kind))).map(key =>
            <span key={key}><i style={{ background: colors[key] || "#95a7c1" }} />{current?.kinds[key] || key}</span>)}
            <small>球体大小按内容条数与独立引文数计算</small>
          </div><KnowledgeGraph graph={graph} selected={selected} choose={choose} controls={limitControls}>
          {graphNode || graphPage ? <>
            <small>{current?.kinds[graphPage?.kind || graphNode?.kind || ""]}</small>
            <h3>{graphPage?.name || graphNode?.name}</h3>
            {graphNode && <small className="knowledge-graph-stats">{graphNode.information ?? 0} 条内容 · {graphNode.evidence ?? 0} 条独立引文</small>}
            {graphNode?.group && graphNode.group !== graphNode.name && <small>{graphNode.group}关联群</small>}
            {highlight && <><p>{highlight.body}</p><button type="button" onClick={() => void openSource(highlight.source_id)}>查看这条原文 ↗</button></>}
            {graphRelations.length > 0 && <div className="knowledge-graph-links">
              <strong>关联对象</strong>
              {graphRelations.slice(0, 4).map(edge => {
                const outgoing = edge.source === selected;
                const target = current?.nodes.find(node => node.id === (outgoing ? edge.target : edge.source));
                return target && <button key={edge.id} type="button" onClick={() => choose(target.id)}>
                  <small>{outgoing ? "→" : "←"} {edge.relation}</small><span>{target.name}</span>
                </button>;
              })}
            </div>}
            <button type="button" disabled={!graphPage} onClick={revealReading}>查看完整档案</button>
          </> : <p>点击球体或在目录中选择对象。</p>}
        </KnowledgeGraph></> : <div className="knowledge-graph-tools">{limitControls}{graphVisible && <span role="status">正在组织三维关系图…</span>}</div>}</section>
        {current?.gaps?.length ? <details className="knowledge-gaps"><summary>资料核对 · {needsCheck} 条需回查{gapGroups.answered.length ? ` · ${gapGroups.answered.length} 条已有原文回答` : ""}</summary>
          <div className="knowledge-actions knowledge-gap-filters">
            <label>查看类别<select value={visibleGapGroup} onChange={event => { setGapFilter(event.target.value); setGapLimit(18); }}>
              {Object.entries(gapGroups).filter(([, gaps]) => gaps.length > 0).map(([key, gaps]) =>
                <option key={key} value={key}>{gapNames[key]} · {gaps.length}</option>)}
            </select></label>
          </div>
          {visibleGaps.slice(0, gapLimit).map(gap => <section className="knowledge-section" key={gap.candidate_id}>
            <small>{gap.title} · {state(gap.state)}</small><p>{readableSourceText(gap.question)}</p>
            <button type="button" className="knowledge-source-link" onClick={() => void openSource(gap.source_id)}>查看依据 ↗</button>
            {gap.evidence?.filter(item => item.state !== "dismissed").map((item, index) => <blockquote key={index}>
              <small>{item.answered ? "原文回答" : "相关补证"} · {state(item.state)}</small><p>{item.body}</p>
              <button type="button" className="knowledge-source-link" onClick={() => void openSource(item.source_id)}>{item.title} ↗</button>
            </blockquote>)}
            {decisionControls(gap, gap.question)}
          </section>)}
          {visibleGaps.length > gapLimit && <button type="button" onClick={() => setGapLimit(limit => limit + 18)}>显示更多 · 余 {visibleGaps.length - gapLimit} 条</button>}
        </details> : null}
        {current?.problems.length ? <section className="knowledge-gaps"><h4>资料状态</h4>{current.problems.map((problem, index) => <p key={index}>{readableSourceText(problem)}</p>)}</section> : null}</section>;
}

/** 独立全书窗口，与单条设定编辑分别提供入口。 */
export function KnowledgeDialog({ projectRoot, graphLimit, onGraphLimitChange, onEditSetting, onClose }: {
  projectRoot: string; graphLimit: number | null; onGraphLimitChange: (limit: number | null) => void; onEditSetting: (recordId: string, collectionId: string) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return <dialog className="knowledge-dialog" ref={dialog} onCancel={onClose} onClose={onClose} aria-label="全书知识库"><button type="button" className="knowledge-close" onClick={onClose} aria-label="收起知识库，后台继续">收起到后台</button><KnowledgeCenter projectRoot={projectRoot} graphLimit={graphLimit} onGraphLimitChange={onGraphLimitChange} onEditSetting={onEditSetting} /></dialog>;
}
