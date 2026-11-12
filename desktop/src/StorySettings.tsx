import { useEffect, useRef, useState } from "react";
import "./story-settings.css";
import { documentLabel, readableSourceText, readableConversationText } from "./readableNames";
import { readableRecordText, readableValue } from "./readableRecords";
import { KnowledgeCenter as BookMemory } from "./KnowledgeCenter";

type Field = { key: string; label: string; description?: string; default?: string };
type Template = { template_id: string; name: string; category: string; description: string; fields: Field[] };
type Evidence = { source_kind: string; chapter_no?: number; path?: string; quote: string };
const displayValue = (value: unknown): string => value !== null && typeof value === "object" ? readableValue(value) : String(value ?? "");
type SettingCandidate = { target_records?: Array<{ record_id: string; revision: number }>; candidate_hash: string; source_chapter: number; status: string; reason?: string; differences?: Array<{ field: string; label: string; before: unknown; proposed: unknown }>; proposal?: { title?: string; values?: Record<string, unknown>; evidence_refs?: Evidence[] } };
type SettingRecord = { stored_status?: string; pending_candidates?: SettingCandidate[]; duplicate_name?: boolean; classification_issues?: string[]; record_id: string; title: string; values: Record<string, unknown>; status: string; revision: number; epistemic_status?: string; source_role?: string; review?: { summary?: string; issues?: Array<{ reason?: string; explanation?: string; quote?: string; source_quote?: string; differences?: Array<{ field: string; label: string; before: unknown; proposed: unknown }> }>; decision?: string }; evidence_refs?: Evidence[] };
type Collection = { pending_candidates?: SettingCandidate[]; collection_id: string; name: string; category: string; template_id: string; instructions: string; fields: Field[]; revision: number; status: string; builtin?: boolean; auto_record?: boolean; read_roles?: string[]; write_roles?: string[]; records?: SettingRecord[] };
const emptyCollection = (): Collection => ({ collection_id: "", name: "", category: "", template_id: "", instructions: "", fields: [], revision: 0, status: "active", auto_record: true, read_roles: ["coordinator", "writer", "editor", "reviewer", "memory_keeper"], write_roles: ["coordinator", "writer", "editor", "reviewer", "memory_keeper"] });
const roles = [{ id: "coordinator", label: "墨宝 · 调度" }, { id: "writer", label: "创作" }, { id: "editor", label: "编辑" }, { id: "reviewer", label: "专项审查" }, { id: "memory_keeper", label: "记忆整理" }];
const statuses: Record<string, string> = { needs_classification: "内容放错分类，待整理", stale_evidence: "原文已变化，待核对", configuration_changed: "记录要求已变化，待核对", active: "使用中", archived: "已留档", pending_review: "等待核对", approved: "已核对", accepted: "已采用", conflict: "存在分歧", rejected: "未采用", needs_user: "需要你决定", needs_evidence: "需要补充依据", verified_reference: "已核对参考", user_kept_hypothesis: "保留为设想" };

/** 只翻译说明层的字段名；引号内原话、独立引文和原始审核对象不经过此转换。 */
function settingExplanation(value: unknown): string {
  const labels: Record<string, string> = { result: "处理结果", evidence_ref: "引用依据", evidence_refs: "原文依据", recovered_evidence: "补回的依据", semantic_support: "含义支持", claim: "原意见", findings: "问题记录", correct: "更正", user: "作者", when: "时间", where: "地点", hash: "内容校验", source_kind: "来源类型", canonical: "已接受正文", draft: "当前草稿", planning: "当前规划" };
  return String(value || "").split(/(“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"|'[^'\n]*')/g)
    .map((part, index) => index % 2 ? part : readableConversationText(readableRecordText(part))
      .replace(/\b[a-z]+(?:_[a-z]+)*\b/g, word => labels[word] || word)).join("");
}

/** 展开后就地编辑；折叠仅隐藏内容，不卸载表单，因此未保存输入仍留在原卡片。 */
function BookSettingRecordCard({ projectRoot, collection, record, focusToken = "", hidden, onChanged }: { projectRoot: string; collection: Collection; record: SettingRecord; focusToken?: string; hidden?: boolean; onChanged: () => void }) {
  const element = useRef<HTMLDetailsElement | null>(null);
  const [open, setOpen] = useState(Boolean(focusToken));
  const [activated, setActivated] = useState(Boolean(focusToken));
  useEffect(() => { if (focusToken) { setOpen(true); setActivated(true); element.current?.scrollIntoView({ block: "nearest" }); } }, [focusToken]);
  return <details ref={element} hidden={hidden} className="book-setting-record" data-status={record.status} open={open} onToggle={event => {
    if (event.target !== event.currentTarget) return;
    setOpen(event.currentTarget.open); if (event.currentTarget.open) setActivated(true);
  }}><summary>{record.title}<small>{statuses[record.status] || "等待核对"}</small></summary>
    {activated && <StorySettings key={`${projectRoot}:${record.record_id}`} projectRoot={projectRoot} focusRecordId={record.record_id} focusCollectionId={collection.collection_id}
      inlineContext={{ collection, record }} onChanged={onChanged} />}
  </details>;
}

/** 阅读、日常编辑和取舍在当前卡完成；合集字段、自动记录和权限仍只在管理入口配置。 */
export function BookSettingsBrowser({ projectRoot, brief, refreshKey, graphLimit, onGraphLimitChange, onEditSetting, focusRecordId = "", focusCollectionId = "", focusToken = "", onManage, onEdit, onAudit }: { projectRoot: string; brief: Record<string, unknown>; refreshKey: string; graphLimit: number | null; onGraphLimitChange: (limit: number | null) => void; onEditSetting: (recordId: string, collectionId: string) => void; focusRecordId?: string; focusCollectionId?: string; focusToken?: string; onManage: () => void; onEdit: () => void; onAudit: () => void }) {
  const [snapshot, setSnapshot] = useState<{ root: string; collections: Collection[] } | null>(null);
  // 切项目的第一次渲染也不借用旧项目条目，避免把旧记录装入新项目的卡内编辑器。
  const collections = snapshot?.root === projectRoot ? snapshot.collections : [];
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState("");
  const [visited, setVisited] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const focusedCollection = (focusRecordId
    ? collections.find(item => item.records?.some(record => record.record_id === focusRecordId))?.collection_id
    : focusCollectionId) || "";
  useEffect(() => {
    if (!focusedCollection) return;
    setSelected(focusedCollection);
    setVisited(previous => previous.includes(focusedCollection) ? previous : [...previous, focusedCollection]);
    setSearch("");
  }, [focusedCollection, focusToken]);
  useEffect(() => { setSnapshot(null); setError(""); }, [projectRoot]);
  useEffect(() => {
    let current = true;
    void window.inkflow.request<{ collections: Collection[] }>("story_settings.list", { project_root: projectRoot }).then(value => { if (current) { setSnapshot({ root: projectRoot, collections: value.collections }); setError(""); } }).catch(cause => { if (current) setError(readableSourceText(String(cause))); });
    return () => { current = false; };
  }, [projectRoot, refreshKey, revision]);
  const labels: Array<[string, string]> = [["genre", "题材"], ["target_audience", "读者"], ["protagonist", "主角"], ["premise", "故事前提"], ["core_selling_point", "故事重点"], ["target_chapter_words", "单章目标字数"], ["estimated_chapters", "预计章节"], ["estimated_volumes", "预计篇章"]];
  const query = search.trim().toLowerCase();
  const matchesCollection = (item: Collection) => `${item.name} ${item.category}`.toLowerCase().includes(query);
  const visible = collections.filter(item => matchesCollection(item)
    || item.records?.some(record => record.title.toLowerCase().includes(query)));
  const activeCollection = collections.find(item => item.collection_id === selected);
  const memoryView = selected === "memory";
  // 访问过的模块保留表单实例，切换目录后仍可回来继续编辑。
  const selectCollection = (id: string) => {
    setSelected(id);
    if (id) setVisited(previous => previous.includes(id) ? previous : [...previous, id]);
  };
  const refreshRecords = () => setRevision(value => value + 1);
  return <section className="book-settings-browser">
    <header><h2>本书设定</h2><div className="book-settings-actions">
      <button type="button" onClick={onAudit}>检查缺项与冲突</button>
      <button type="button" className="primary" onClick={onManage}>管理设定</button>
    </div></header>
    {error && <p className="form-error" role="alert">{error}</p>}
    <div className="book-settings-layout">
      <nav className="book-settings-index" aria-label="设定目录">
        <input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="查找设定或条目" aria-label="查找设定或条目" />
        <button type="button" aria-current={!activeCollection ? "page" : undefined} onClick={() => selectCollection("")}>书籍要求</button>
        {([true, false] as const).map(base => {
          const group = visible.filter(item => Boolean(item.builtin) === base);
          if (!group.length) return null;
          return <section key={String(base)}><h3>{base ? "基础设定" : "高级设定"}</h3>
            {group.map(item => <button type="button" key={item.collection_id}
              aria-current={selected === item.collection_id ? "page" : undefined}
              onClick={() => selectCollection(item.collection_id)}>
              <span>{item.name}</span><small>{item.records?.length || 0}</small>
            </button>)}
          </section>;
        })}
        {query && !visible.length && <p className="empty-mini">没有匹配的设定或条目。</p>}
        {!snapshot && !error && <p role="status" className="form-hint">正在读取设定目录…</p>}
        <section><button type="button" aria-current={memoryView ? "page" : undefined}
          onClick={() => selectCollection("memory")}>本书记忆</button></section>
      </nav>
      <div className="book-settings-detail">
        <section className="book-setting-view" hidden={Boolean(activeCollection || memoryView)}>
          <header><h3>书籍要求</h3><button type="button" onClick={onEdit}>编辑书籍要求</button></header>
          <dl>{labels.filter(([key]) => brief[key] !== undefined && brief[key] !== "").map(([key, label]) =>
            <div key={key} className={["premise", "core_selling_point"].includes(key) ? "book-setting-wide" : undefined}><dt>{label}</dt><dd>{readableValue(brief[key])}</dd></div>)}</dl>
          {Array.isArray(brief.user_rules) && brief.user_rules.length > 0 && <div className="book-setting-rules">
            <h4>必读规则与写作要求</h4><ol>{brief.user_rules.map((rule, index) => <li key={index}>{readableValue(rule)}</li>)}</ol>
          </div>}
        </section>
        {collections.filter(item => visited.includes(item.collection_id)).map(collection =>
          <section className="book-setting-view" key={collection.collection_id} hidden={selected !== collection.collection_id}>
            <header><div>{collection.category && <small>{collection.category}</small>}<h3>{collection.name}</h3></div>
              <span>{collection.records?.length || 0} 条</span></header>
            {!collection.records?.length && <p className="empty-mini">这个分类还没有条目。</p>}
            {query && collection.records?.length && !visible.includes(collection) ? <p className="empty-mini">当前分类没有匹配的条目。</p> : null}
            {collection.records?.map(record => <BookSettingRecordCard key={record.record_id} projectRoot={projectRoot} collection={collection} record={record}
              hidden={!matchesCollection(collection) && !record.title.toLowerCase().includes(query)}
              focusToken={record.record_id === focusRecordId ? focusToken : ""} onChanged={refreshRecords} />)}
            {!focusRecordId && collection.collection_id === focusCollectionId && <StorySettings key={`${projectRoot}:${collection.collection_id}:candidates`} projectRoot={projectRoot}
              focusCollectionId={collection.collection_id} inlineContext={{ collection }} onChanged={refreshRecords} />}
          </section>)}
        {visited.includes("memory") && <div hidden={!memoryView}><BookMemory projectRoot={projectRoot} refreshKey={refreshKey} graphLimit={graphLimit} onGraphLimitChange={onGraphLimitChange} onEditSetting={onEditSetting} /></div>}
      </div>
    </div>
  </section>;
}

function SettingCollectionTree({ items, selected, busy, onChoose, onRecording, depth = 0 }: { items: Collection[]; selected: string; busy: boolean; onChoose: (item: Collection) => void; onRecording: (item: Collection, enabled: boolean) => void; depth?: number }) {
  const groups = new Map<string, Collection[]>();
  for (const item of items) {
    const path = item.category.split(/[/>]/).map(part => part.trim()).filter(Boolean);
    const key = path[depth] || "其他设定";
    groups.set(key, [...(groups.get(key) || []), item]);
  }
  return <>{[...groups].map(([name, group]) => <details className="setting-category-branch" key={name} open={group.some(item => item.collection_id === selected)}><summary>{name}<small>{group.length} 份</small></summary>{group.some(item => item.category.split(/[/>]/).filter(part => part.trim()).length > depth + 1) && depth < 3
    ? <SettingCollectionTree items={group} selected={selected} busy={busy} onChoose={onChoose} onRecording={onRecording} depth={depth + 1} />
    : group.map(item => <article className={`setting-collection-card ${selected === item.collection_id ? "active" : ""}`} key={item.collection_id}><button type="button" disabled={busy} onClick={() => onChoose(item)}><strong>{item.name}</strong><small>{item.status === "archived" ? "已移出使用" : item.records?.filter(record => record.status !== "archived").length ? `${item.records.filter(record => record.status !== "archived").length} 条记录` : "尚无具体记录"}</small></button>{item.status !== "archived" && <label><input type="checkbox" checked={item.auto_record !== false} disabled={busy || item.builtin} onChange={event => onRecording(item, event.target.checked)} />{item.builtin ? "基础设定持续维护" : "持续记录与维护"}</label>}</article>)}
  </details>)}</>;
}

const actionableCandidateStatuses = ["needs_user", "pending_review", "needs_evidence", "deferred_by_user"];

/** 每个候选单独展示原值、建议和依据；三种决定沿同一个版本校验入口保存。 */
function SettingCandidateCard({ candidate, busy, onDecide }: { candidate: SettingCandidate; busy: boolean; onDecide: (decision: "keep_current" | "defer" | "adopt_candidate") => void }) {
  const label = ({ deferred_by_user: "已留到下次", pending_review: "等待责任角色核对", needs_evidence: "还需补证" } as Record<string, string>)[candidate.status] || "需要你决定";
  const ambiguous = (candidate.target_records?.length || 0) > 1;
  const needsEvidence = candidate.status === "needs_evidence";
  return <article className="setting-candidate-card">
    <header><strong>{candidate.proposal?.title || "这次设定建议"}</strong><small>{label}{candidate.source_chapter ? ` · 来自第${candidate.source_chapter}章` : ""}</small></header>
    <p>{settingExplanation(candidate.reason || "这次建议和现有记录不同，先比较依据再决定。")}</p>
    {candidate.differences?.map(diff => <div className="setting-difference" key={diff.field}>
      <strong>{diff.label || diff.field}</strong><p><span>原来记录：</span>{readableValue(diff.before)}</p>
      <p><span>这次建议：</span>{readableValue(diff.proposed)}</p>
    </div>)}
    {!!candidate.proposal?.evidence_refs?.length && <div className="setting-candidate-sources">{candidate.proposal.evidence_refs.map((ref, index) =>
      <blockquote key={index}><small>{ref.chapter_no ? `第${ref.chapter_no}章` : documentLabel(ref.path || "本书资料")}</small><p>{ref.quote}</p></blockquote>
    )}</div>}
    <div className="settings-inline-actions">
      <button type="button" disabled={busy} onClick={() => onDecide("keep_current")}>保留现有设定</button>
      <button type="button" disabled={busy} onClick={() => onDecide("defer")}>稍后决定</button>
      <button type="button" disabled={busy || ambiguous || needsEvidence} onClick={() => onDecide("adopt_candidate")}>采用建议并重新核对</button>
    </div>
    <p className="form-hint">{ambiguous ? "同名记录不止一条，请先核对对象。" : needsEvidence ? "请先在设定条目里补上有效原文依据。" : "采用建议后仍会核对依据，通过后才供写作参考。"}</p>
  </article>;
}

/** 卡内与管理页共用同一保存、候选决定和版本守卫；卡内只隐藏模块配置，不另建写入路径。 */
export function StorySettings({ projectRoot, focusRecordId = "", focusCollectionId = "", inlineContext, onChanged }: { projectRoot: string; focusRecordId?: string; focusCollectionId?: string; inlineContext?: { collection: Collection; record?: SettingRecord }; onChanged?: () => void }) {
  const recordOnly = Boolean(inlineContext);
  const focusedRecord = useRef("");
  const live = useRef(false), currentRoot = useRef(projectRoot);
  currentRoot.current = projectRoot;
  const [templates, setTemplates] = useState<Template[]>([]);
  const [showTemplates, setShowTemplates] = useState(false);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [editing, setEditing] = useState<Collection>(emptyCollection);
  const [record, setRecord] = useState<SettingRecord | null>(null);
  const [title, setTitle] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [epistemic, setEpistemic] = useState("hypothesis");
  const [sourceKind, setSourceKind] = useState("canonical");
  const [sourceChapter, setSourceChapter] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [sourceQuote, setSourceQuote] = useState("");
  const [extraEvidence, setExtraEvidence] = useState<Evidence[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [recordDecisionReason, setRecordDecisionReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // 页面切换后不再发旧卡片的请求，迟到回包也不能覆盖另一项目的输入；已发出的操作仍以后台记录为准。
  const isCurrent = () => live.current && currentRoot.current === projectRoot;
  const api = async <T,>(method: string, params: Record<string, unknown> = {}): Promise<T> => {
    if (!isCurrent()) throw new Error("当前设定页面已切换，请在原项目查看保存结果。");
    const result = await window.inkflow.request<T>(method, { project_root: projectRoot, ...params });
    if (!isCurrent()) throw new Error("页面已切换，原操作结果保留在对应项目中。");
    return result;
  };
  const refresh = async (collectionId = editing.collection_id) => {
    const result = await api<{ collections: Collection[] }>("story_settings.list", { include_archived: true });
    setCollections(result.collections || []);
    const current = result.collections.find(item => item.collection_id === collectionId);
    if (current) setEditing(current);
    return current;
  };
  useEffect(() => {
    let current = true;
    live.current = true;
    setEditing(emptyCollection()); setRecord(null); setCollections([]); setError(""); setNotice("");
    // 浏览页已读取过当前条目；只在首次进入时装入，后续背景刷新不替换作者尚未保存的输入。
    if (inlineContext) {
      setEditing(inlineContext.collection); setCollections([inlineContext.collection]);
      editRecord(inlineContext.record || null, inlineContext.collection);
      focusedRecord.current = focusRecordId || focusCollectionId;
      return () => { current = false; live.current = false; };
    }
    void window.inkflow.request<{ templates: Template[] }>("story_settings.templates", projectRoot ? { project_root: projectRoot } : {}).then(result => { if (current) setTemplates(result.templates || []); }).catch(cause => { if (current) setError(readableSourceText(String(cause))); });
    if (projectRoot) void window.inkflow.request<{ collections: Collection[] }>("story_settings.list", { project_root: projectRoot, include_archived: true }).then(result => { if (current) setCollections(result.collections || []); }).catch(cause => { if (current) setError(readableSourceText(String(cause))); });
    return () => { current = false; live.current = false; };
  }, [projectRoot]);
  const run = async (work: () => Promise<void>) => {
    if (busy || !isCurrent()) return;
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (cause) { if (isCurrent()) setError(settingExplanation(String(cause))); } finally { if (isCurrent()) setBusy(false); }
  };
  const editRecord = (item: SettingRecord | null, collection = editing) => {
    setRecord(item); setRecordDecisionReason(""); setTitle(item?.title || ""); setEpistemic(item?.epistemic_status || "hypothesis");
    setValues(Object.fromEntries(collection.fields.map(field => [field.key, displayValue(item?.values[field.key] ?? field.default ?? "")])));
    const evidence = item?.evidence_refs?.[0];
    setExtraEvidence(item?.evidence_refs?.slice(1) || []);
    setSourceKind(evidence?.source_kind || "canonical"); setSourceChapter(evidence?.chapter_no ? String(evidence.chapter_no) : ""); setSourcePath(evidence?.path || ""); setSourceQuote(evidence?.quote || "");
  };
  // 与采用候选共用同一未保存判定；保留/延期不清输入，重新读取必须由作者明确同意。
  const hasUnsavedInput = () => {
    const source = record?.evidence_refs?.[0];
    if (!record) {
      // 新条目也可能已有输入；只与真正的初始默认值比较，未改过的预填内容不触发丢稿确认。
      const defaults = Object.fromEntries(editing.fields.map(field => [field.key, displayValue(field.default ?? "")]));
      return title !== "" || epistemic !== "hypothesis" || Object.entries(values).some(([key, value]) => value !== (defaults[key] ?? ""))
        || sourceKind !== "canonical" || sourceChapter !== "" || sourcePath !== "" || sourceQuote !== "" || extraEvidence.length > 0;
    }
    return Boolean(record && (title !== record.title || epistemic !== (record.epistemic_status || "hypothesis")
      || Object.entries(values).some(([key, value]) => value !== displayValue(record.values[key] ?? ""))
      || sourceKind !== (source?.source_kind || "canonical") || sourceChapter !== String(source?.chapter_no || "")
      || sourcePath !== (source?.path || "") || sourceQuote !== (source?.quote || "")
      || JSON.stringify(extraEvidence) !== JSON.stringify(record.evidence_refs?.slice(1) || [])));
  };
  const reloadInlineRecord = () => run(async () => {
    if (hasUnsavedInput() && !await window.inkflow.confirm("这里还有未保存的修改。重新读取会替换本卡片的输入，要继续吗？")) return;
    const updated = await refresh();
    const targetId = record?.record_id || focusRecordId;
    const current = updated?.records?.find(item => item.record_id === targetId);
    if (targetId && !current) { setNotice("这条设定已变化或移出使用，原输入仍保留；请核对当前项目中的记录。"); return; }
    editRecord(current || null, updated || editing); setNotice("已刷新这条设定。");
  });
  const chooseCollection = (item: Collection) => { setEditing(item); editRecord(null, item); setShowTemplates(false); setNotice(""); setError(""); };
  useEffect(() => {
    const identity = focusRecordId || focusCollectionId;
    if (!identity || !collections.length || focusedRecord.current === identity) return;
    const owner = collections.find(item => focusRecordId ? item.records?.some(value => value.record_id === focusRecordId) : item.collection_id === focusCollectionId);
    const target = owner?.records?.find(item => item.record_id === focusRecordId);
    if (owner && (!focusRecordId || target)) { focusedRecord.current = identity; setEditing(owner); editRecord(target || null, owner); setShowTemplates(false); setNotice("已打开相关设定。先看下面的差异和依据，再选择如何处理。"); }
    else setNotice("这条设定可能已被更新或移出当前列表。请查看最新记录，不会按旧提醒自动保存。");
    // 从提醒进入会重挂当前设置页，资料刷新不覆盖正在填写的内容。
  }, [focusRecordId, focusCollectionId, collections.length]);
  const chooseTemplate = (template: Template) => {
    const next = { ...emptyCollection(), name: template.name, category: template.category, template_id: template.template_id, instructions: template.description, fields: template.fields.map(field => ({ ...field })) };
    setEditing(next); editRecord(null, next); setShowTemplates(false); setNotice("");
  };
  const saveCollection = () => run(async () => {
    const result = await api<Collection | { collection: Collection }>("story_settings.save", { ...editing, expected_revision: editing.revision });
    const saved = "collection" in result ? result.collection : result;
    await refresh(saved.collection_id);
    setNotice("设定模块已保存。你可以调整负责记录的内容，再添加具体条目。");
  });
  const toggleRecording = (item: Collection, enabled: boolean) => run(async () => {
    await api("story_settings.save", { collection_id: item.collection_id, name: item.name, expected_revision: item.revision, auto_record: enabled });
    await refresh();
    setNotice(enabled ? `已开启“${item.name}”的持续记录：随章节接受核对变化，没有变化不重复保存。` : `已关闭“${item.name}”的自动记录，已有内容仍可查看与核对。`);
  });
  const archiveCollection = (item: Collection) => run(async () => {
    if (!await window.inkflow.confirm(`将“${item.name}”移出使用并留档？它的条目和引用历史会保留，已有正文不会被删除。`)) return;
    await api("story_settings.archive", { collection_id: item.collection_id, expected_revision: item.revision });
    await refresh(); setEditing(emptyCollection()); editRecord(null, emptyCollection()); setNotice("设定模块已移出使用并留档。");
  });
  const saveRecord = () => run(async () => {
    const prior = record?.evidence_refs?.[0];
    const sameEvidence = prior && prior.source_kind === sourceKind && String(prior.chapter_no || "") === sourceChapter && (prior.path || "") === sourcePath && prior.quote === sourceQuote;
    const evidence: Evidence[] = [...(sourceQuote.trim() ? [sameEvidence && prior ? prior : { source_kind: sourceKind, ...(sourceChapter ? { chapter_no: Number(sourceChapter) } : {}), ...(sourcePath.trim() ? { path: sourcePath.trim() } : {}), quote: sourceQuote.trim() }] : []), ...extraEvidence];
    const savedValues = Object.fromEntries(Object.entries(values).filter(([key, value]) => value.trim() || record && key in record.values).map(([key, value]) => [key, record && displayValue(record.values[key]) === value ? record.values[key] : value]));
    const result = await api<{ record: SettingRecord }>("story_settings.record.save", { collection_id: editing.collection_id, record_id: record?.record_id || "", title, values: savedValues, epistemic_status: epistemic, evidence_refs: evidence, expected_revision: record?.revision || 0, operation: record ? "correct" : "new" });
    const updated = await refresh();
    if (recordOnly) editRecord(updated?.records?.find(item => item.record_id === result.record.record_id) || result.record, updated || editing);
    else editRecord(null);
    onChanged?.(); setNotice("内容已保存，正在等待核对；不会直接改动已收稿的正文。");
  });
  // 只处理点击的同章候选；更新后回读新修订，不能继续把旧表单当成最新内容保存。
  const decideCandidate = (candidate: SettingCandidate, decision: "keep_current" | "defer" | "adopt_candidate", target?: SettingRecord) => run(async () => {
    // 合集卡片只能绑定唯一且已显示的条目；同名多个对象须先打开具体记录。
    if (!target && candidate.target_records?.length === 1) target = editing.records?.find(item => item.record_id === candidate.target_records![0].record_id);
    if (decision === "adopt_candidate" && candidate.target_records?.length && !target) { setNotice("先打开要修改的具体记录，再查看差异并采用，避免改错同名对象。"); return; }
    if (decision === "adopt_candidate" && hasUnsavedInput()
      && !await window.inkflow.confirm("这条设定还有未保存的编辑。采用建议会重新打开核对后的内容，是否继续？取消后可以先保存你的修改。")) return;
    const result = await api<{ record?: SettingRecord }>("story_settings.candidate.decide", { candidate_hash: candidate.candidate_hash, source_chapter: candidate.source_chapter, decision,
      ...(target ? { record_id: target.record_id, expected_revision: target.revision } : {}) });
    const updated = await refresh();
    onChanged?.();
    if (record) {
      const current = updated?.records?.find(item => item.record_id === record.record_id);
      // 保留或延期只更新候选状态，不清掉作者尚未保存的输入；采用才读回已核边界的新版本。
      if (decision === "adopt_candidate") editRecord(current || result.record || null);
      else if (current && current.revision !== record.revision) {
        // 只读刷新不替作者“看过新版”；旧表单继续携带原修订号，避免迟到回包放宽保存门禁。
        setNotice("已记录你的选择。设定在别处更新了，你未保存的输入已保留；请核对新版本后再保存。"); return;
      } else setRecord(current || record);
    } else if (recordOnly && decision === "adopt_candidate" && result.record) {
      // 合集中的新条目候选采用后，继续编辑服务实际创建的待核版本，不回到空白表单重复新建。
      editRecord(updated?.records?.find(item => item.record_id === result.record!.record_id) || result.record, updated || editing);
    }
    setNotice(decision === "keep_current" ? "已保留现有设定，这一条建议不再催办。" : decision === "defer" ? "已留到下次，可以在这份设定里再查看。" : "已保存为待核对的设定建议；审核结果会在这里更新。");
  });
  const keepRecordHypothesis = (item: SettingRecord) => run(async () => {
    if (hasUnsavedInput()) { setNotice("这里还有未保存的修改，请先保存，或重新读取原内容后再作决定。"); return; }
    if (!recordDecisionReason.trim() || !await window.inkflow.confirm("确认保留为创作设想吗？未解决的原审查意见会保留，这个决定不会让它成为已发生的正史。")) return;
    await api("story_settings.record.decide", { record_id: item.record_id, expected_revision: item.revision, decision: "keep_hypothesis", reason: recordDecisionReason });
    const updated = await refresh();
    editRecord(recordOnly ? updated?.records?.find(value => value.record_id === item.record_id) || item : null, updated || editing);
    onChanged?.(); setNotice("已按你的原因保留为设想，原审核意见仍保留，不代表正文正史已改变。");
  });
  const archiveRecord = (item: SettingRecord) => run(async () => {
    if (!await window.inkflow.confirm(`将“${item.title}”移出使用并保留历史？`)) return;
    await api("story_settings.record.archive", { collection_id: editing.collection_id, record_id: item.record_id, expected_revision: item.revision }); await refresh();
    if (record?.record_id === item.record_id) editRecord(null);
  });
  return <section className="story-settings-manager">
    {recordOnly ? <><p className="form-hint">直接修改下面的内容，保存后继续核对；有不同建议时，可以在这里比较和决定。</p><button type="button" disabled={busy} onClick={() => void reloadInlineRecord()}>刷新当前条目</button></>
      : <p className="form-hint">人物与世界是默认基础设定。高级设定按需要添加，并勾选是否持续记录；每次章节接受时整理有变化的内容，没有变化不重复保存。创作设想与已有正文依据分开，保存时可以补充来源。</p>}
    {!projectRoot && <p className="form-hint">先打开一本小说，再创建本书设定。下面可以先查看预设模板。</p>}
    {!recordOnly && <details className="setting-template-picker" open={showTemplates} onToggle={event => setShowTemplates(event.currentTarget.open)}>
      <summary>选择预设模板 · {templates.length} 类</summary>
      <div className="setting-template-grid">{templates.map(template => <button type="button" disabled={!projectRoot || busy} key={template.template_id} onClick={() => chooseTemplate(template)}><strong>{template.name}</strong><small>{template.description}</small></button>)}</div>
    </details>}
    <div className={recordOnly ? undefined : "setting-manager-columns"}>
      {!recordOnly && <aside className="setting-collection-list" aria-label="本书设定模块">
        <div className="settings-inline-actions"><button type="button" disabled={!projectRoot || busy} onClick={() => chooseCollection(emptyCollection())}>＋ 自定义模块</button><button type="button" disabled={!projectRoot || busy} onClick={() => void run(async () => { await refresh(); setNotice("已读取当前设定版本与核对结果。"); })}>刷新</button><label><input type="checkbox" checked={showArchived} onChange={event => setShowArchived(event.target.checked)} />显示留档</label></div>
        {([true, false] as const).map(base => <section className="setting-collection-group" key={String(base)}><h4>{base ? "基础设定 · 必备" : "高级设定 · 按需"}</h4><SettingCollectionTree items={collections.filter(item => Boolean(item.builtin) === base && (showArchived || item.status !== "archived"))} selected={editing.collection_id} busy={busy} onChoose={chooseCollection} onRecording={(item, enabled) => void toggleRecording(item, enabled)} /></section>)}
        {!collections.length && <p className="empty-mini">选择一个模板即可开始，也可以自行定义记录字段。</p>}
      </aside>}
      <fieldset className="setting-module-editor" disabled={busy}>
        {!recordOnly && <><header className="setting-editor-heading"><h3>{editing.name || "新建设定模块"}</h3></header><details key={editing.collection_id} className="setting-module-options" open={!editing.collection_id}><summary>模块配置</summary>
        <div className="settings-fields two"><label>设定名称<input value={editing.name} disabled={busy || editing.status === "archived"} maxLength={100} placeholder="例如：人物变化档案" onChange={event => setEditing({ ...editing, name: event.target.value })} /></label><label>设定分类<input value={editing.category} disabled={busy || editing.status === "archived"} maxLength={100} placeholder="例如：道具 / 证据物件（用 / 分层）" onChange={event => setEditing({ ...editing, category: event.target.value })} /></label></div>
        <label>记录说明与提示词<textarea value={editing.instructions} disabled={busy || editing.status === "archived"} maxLength={6000} placeholder="告诉各角色需要记录哪些内容、何时更新，以及哪些内容不要加入。" onChange={event => setEditing({ ...editing, instructions: event.target.value })} /></label>
        <details><summary>哪些角色可以读取和补充</summary><p className="form-hint">所有角色默认可读。Coordinator 可按当前对话要求整理有原文依据的待核候选；Editor、Memory Keeper 按正文和现行规划持续补充、修正。未来规划按设想保存，核对通过才作为已核参考；正史仍由引擎提交。</p>{(["read_roles", "write_roles"] as const).map(permission => <fieldset key={permission} className="setting-role-permissions"><legend>{permission === "read_roles" ? "读取" : "记录和补充"}</legend>{roles.map(role => <label key={role.id}><input type="checkbox" checked={(editing[permission] || []).includes(role.id)} disabled={busy || editing.status === "archived"} onChange={event => setEditing({ ...editing, [permission]: event.target.checked ? [...(editing[permission] || []), role.id] : (editing[permission] || []).filter(id => id !== role.id) })} />{role.label}</label>)}</fieldset>)}</details>
        <details className="setting-fields-editor"><summary>记录字段 · {editing.fields.length} 项</summary>{editing.fields.map((field, index) => <div key={index} className="setting-field-row"><label>字段标识<input disabled={busy || Boolean(editing.collection_id)} value={field.key} maxLength={48} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, key: event.target.value } : value) })} /></label><label>显示名称<input disabled={busy || editing.status === "archived"} value={field.label} maxLength={100} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, label: event.target.value } : value) })} /></label><label>填写提示<input disabled={busy || editing.status === "archived"} value={field.description || ""} maxLength={1000} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, description: event.target.value } : value) })} /></label><label>新条目默认内容<input disabled={busy || editing.status === "archived"} value={field.default || ""} maxLength={2000} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, default: event.target.value } : value) })} /></label><button type="button" disabled={busy || editing.status === "archived"} aria-label={`移除字段 ${field.label}`} onClick={() => setEditing({ ...editing, fields: editing.fields.filter((_, at) => at !== index) })}>移除</button></div>)}<button type="button" disabled={busy || editing.status === "archived" || editing.fields.length >= 40} onClick={() => setEditing({ ...editing, fields: [...editing.fields, { key: `field_${Date.now()}`, label: "新字段", description: "" }] })}>＋ 添加字段</button></details>
        <div className="settings-inline-actions"><button type="button" className="primary" disabled={!projectRoot || busy || !editing.name.trim() || !editing.fields.length || editing.status === "archived"} onClick={() => void saveCollection()}>保存模块</button>{editing.collection_id && !editing.builtin && editing.status !== "archived" && <button type="button" disabled={busy} onClick={() => void archiveCollection(editing)}>删除高级设定（保留历史）</button>}</div>
        </details></>}
        {editing.collection_id && <section className="setting-records">{!record && editing.pending_candidates?.filter(item => actionableCandidateStatuses.includes(item.status)).map(candidate => <SettingCandidateCard key={`${candidate.source_chapter}:${candidate.candidate_hash}`} candidate={candidate} busy={busy} onDecide={decision => void decideCandidate(candidate, decision)} />)}{!recordOnly && <><h4>具体条目</h4>{!(editing.records || []).some(item => item.status !== "archived") && <p className="empty-mini">这个模块只有填写模板，还没有具体设定。它不会算作已审查；请填写条目，或在对话中让墨宝按已接受正文整理这里的资料。</p>}<div className="setting-record-list">{(editing.records || []).filter(item => showArchived || item.status !== "archived").map(item => <article key={item.record_id}><button type="button" onClick={() => editRecord(item)}><strong>{item.title}</strong><span>{statuses[item.status] || item.status} · 修订 {item.revision}</span></button>{item.status !== "archived" && <button type="button" disabled={busy} onClick={() => void archiveRecord(item)}>留档</button>}</article>)}</div></>}
          {record?.pending_candidates?.filter(item => actionableCandidateStatuses.includes(item.status)).map(candidate => <SettingCandidateCard key={`${candidate.source_chapter}:${candidate.candidate_hash}`} candidate={candidate} busy={busy} onDecide={decision => void decideCandidate(candidate, decision, record)} />)}{record?.duplicate_name && <p className="form-hint">存在同名记录，请核对来源并沿原条目维护，避免再次新建。</p>}{record?.classification_issues?.map((issue, index) => <p className="form-error" key={index}>{issue}</p>)}
          {record && ["needs_user", "needs_evidence"].includes(record.stored_status || record.status) && <section className="setting-record-review"><p className="form-hint">这里有尚未解决的意见。可以修改内容后保存待核，或说明为什么保留为设想；暂时不决定也会保留原记录。</p><label>保留这项设想的原因<textarea value={recordDecisionReason} onChange={event => setRecordDecisionReason(event.target.value)} maxLength={4000} /></label><button type="button" disabled={busy || !recordDecisionReason.trim()} onClick={() => void keepRecordHypothesis(record)}>保留为设想并记录原因</button></section>}
          {editing.status !== "archived" && <><div className="settings-inline-actions"><h4>{record ? `编辑：${record.title}` : "添加新条目"}</h4>{record && !recordOnly && <button type="button" onClick={() => editRecord(null)}>取消编辑</button>}</div><div className="settings-fields two"><label>条目名称<input value={title} onChange={event => setTitle(event.target.value)} maxLength={200} /></label><label>内容性质<select value={epistemic} onChange={event => setEpistemic(event.target.value)}><option value="hypothesis">未来设想 / 未定设定</option><option value="objective">有正文依据的客观记录</option><option value="belief">人物相信的事</option><option value="rumor">传闻 / 未证实信息</option><option value="user_constraint">用户约定 / 需按范围理解</option></select></label></div><div className="settings-fields two">{editing.fields.map(field => <label key={field.key}>{field.label}<textarea value={values[field.key] || ""} placeholder={field.description || field.default || ""} maxLength={8000} onChange={event => setValues({ ...values, [field.key]: event.target.value })} /></label>)}</div><details><summary>原文来源 · 可选，核对时不会只凭设定自证</summary><div className="settings-fields two"><label>资料类型<select value={sourceKind} onChange={event => setSourceKind(event.target.value)}><option value="canonical">已接受正文</option><option value="draft">当前草稿</option><option value="planning">当前规划</option><option value="writer_hook">Writer 钩子说明</option></select></label><label>来源章节<input type="number" min={1} value={sourceChapter} onChange={event => setSourceChapter(event.target.value)} /></label><label>本书文件位置<input value={sourcePath} onChange={event => setSourcePath(event.target.value)} placeholder="可选，项目内相对路径" /></label><label>可定位的原文<textarea value={sourceQuote} onChange={event => setSourceQuote(event.target.value)} maxLength={8000} /></label></div></details>{extraEvidence.length > 0 && <details><summary>保留的其他原文依据 · {extraEvidence.length} 条</summary>{extraEvidence.map((ref, index) => <article key={index} className="thread-card"><p>{ref.chapter_no ? `第 ${ref.chapter_no} 章` : ref.path || ref.source_kind}</p><blockquote>{ref.quote}</blockquote><button type="button" disabled={busy} onClick={() => setExtraEvidence(extraEvidence.filter((_, at) => at !== index))}>从这次候选中移除此条依据</button></article>)}</details>}<button type="button" className="primary" disabled={busy || !title.trim() || !Object.values(values).some(value => value.trim()) || record?.status === "archived"} onClick={() => void saveRecord()}>保存条目并等待核对</button></>}
        </section>}
      </fieldset>
    </div>
    {notice && <p className="form-success" role="status">{notice}</p>}{error && <p className="form-error" role="alert">{error}</p>}
  </section>;
}

export type ManualEditJob = { job_id: string; status: string; kind?: string; display_name?: string; relative_path?: string; path?: string; current_hash?: string; expected_hash?: string; content_hash?: string; summary?: string; reason?: string; error?: string; next_action?: string; planning_gap?: boolean; planning_decision?: string; requires_explicit_review?: boolean; user_decision?: string; user_reason?: string; issues?: Array<{ summary?: string; explanation?: string; message?: string; evidence?: string; reason?: string; quote?: string; source_quote?: string; source_path?: string; hard_conflict?: boolean }>; created_at?: string };
const jobLabels: Record<string, string> = { pending: "等待核对", interrupted: "断点已保存", cancelled: "已停止，记录保留", reviewing: "正在核对", paused: "已按设置暂停", reconciled: "新规划已衔接", acknowledged_hypothesis: "已保留为设想", pending_review: "等待核对", review_pending: "等待核对", needs_confirmation: "需要你决定", awaiting_confirmation: "需要你决定", conflict: "有分歧待确认", blocked: "缺少依据", failed: "核对未完成", approved: "核对通过", unchanged: "内容未变", passed: "核对通过", needs_evidence: "需要补充依据", revision_requested: "待定向修订", discarded: "候选未采用", superseded: "已由新版替代", completed: "已完成", kept_pending: "保留待处理", exception_confirmed: "例外已确认" };

/** 只根据已保存的处理状态生成概览；补证字样、旧意见或作者解释都不能自行变成通过／冲突。 */
function manualEditOverview(job: ManualEditJob): { found: string; missing: string; next: string } {
  const count = job.issues?.length || 0;
  const name = job.display_name || documentLabel(job.relative_path || job.path || "设定参考条目");
  const conflicts = job.issues?.filter(issue => issue.hard_conflict === true).length || 0;
  // 这里只能复述报告的结构化标记；尤其缺证时，旧冲突标记不代表当前正文确实有错。
  const advice = job.planning_gap ? `${name}与当前规划还没完成衔接核对。`
    : conflicts && !["needs_evidence", "blocked"].includes(job.status) ? `${name}的报告有${conflicts}条标为冲突的意见，还需要你确认。`
    : `${name}这条记录还没完成确认${count ? `，有${count}条意见待核` : ""}。`;
  switch (job.status) {
    case "superseded": return { found: "这份旧记录已由后续版本接替，原意见保留供回看。", missing: "旧意见不再作为当前待办处理。", next: "查看对应内容的最新版本即可。" };
    case "discarded": return { found: "这次修改候选已不再采用，处理记录仍保留。", missing: "这项候选没有继续核对的待办。", next: "若还有新的修改，以当前内容重新提出。" };
    case "acknowledged_hypothesis": return { found: "你已明确把这条内容保留为创作设想。", missing: "设想仍不是已发生的剧情，也不代表原核对已通过。", next: "需要落实到剧情时，再核对届时的正文和依据。" };
    case "exception_confirmed": return { found: "你的例外决定已记录。", missing: "记录例外不等于核对通过。", next: "后续修改仍按原有正文和设定流程处理。" };
    case "reconciled": return { found: "这份修改已有规划衔接记录。", missing: "原核对说明保留在下方，不能据此把设想当成正史。", next: "继续使用当前有效的章节规划。" };
    case "unchanged": return { found: "这次扫描没有发现新的内容变化。", missing: "没有因本次扫描新增的核对事项。", next: "相同内容无需重复核对。" };
    case "pending": case "pending_review": case "review_pending": return {
      found: "修改已登记，等待核对。", missing: "还没有这次修改的最终核对结论。",
      next: job.requires_explicit_review ? "需要继续时，在对话里说明要核对这项修改。" : "等待本次核对结果；需要你决定时会明确提示。",
    };
    case "reviewing": return { found: "正在核对这项修改和现有正文、设定的关系。", missing: "本轮还没有结束。", next: "等待当前核对完成，再根据结果决定。" };
    case "paused": return { found: "自动核对已按设置暂停，原记录仍保留。", missing: "这项修改还没有完成核对。", next: "需要继续时，先在设置中恢复自动核对。" };
    case "failed": case "interrupted": case "cancelled": return {
      found: job.status === "failed" ? "这轮核对没能完成，不能据此判断修改是否成立。" : "这轮核对已停止，已有记录保留。",
      missing: "还缺有效的完整结论；具体停止原因可在下方展开查看。", next: "先查看停止原因，再决定留待处理，或填写你的解释和修改要求。",
    };
    case "needs_evidence": case "blocked": return {
      found: advice, missing: job.planning_gap ? "缺少与当前规划的有效对照。" : "报告中的原句或支持依据仍需核实。",
      next: "先对照原句，再补依据、说明创作意图，或留待以后处理。",
    };
    case "revision_requested": return { found: "你的修改要求已记录。", missing: "记录要求本身不会自动修改正文，也不代表新内容已经核对。", next: "按这份要求修改后，再核对实际变化。" };
    case "needs_confirmation": case "awaiting_confirmation": case "conflict": case "kept_pending": return {
      found: job.user_decision === "confirm_exception" ? "你的解释已保存，这项核对仍未收尾。" : advice,
      missing: job.planning_gap ? "修改与当前规划还需要核对衔接。" : job.kind === "accepted" && !count ? "对已接受正文的修改仍需要你的明确决定。" : "报告意见仍需结合完整原文核实。",
      next: "可以稍后处理、保存修改要求，或解释为什么要保留这次改动。",
    };
    case "passed": case "approved": case "completed": return job.planning_gap ? {
      found: "本轮核对已有完成的部分。", missing: "修改与当前规划的衔接仍待核实。", next: "先补齐规划对照，再继续处理这项修改。",
    } : {
      found: job.kind === "setting_record" ? "这条设定已通过本轮核对，可作为参考使用。" : "这次修改已通过本轮核对。",
      missing: "当前记录没有标出的待补依据；通过核对不等于自动收入正文。", next: "需要采用为正文时，继续按原有接收方式处理。",
    };
    default: return { found: "这项修改已有保存记录。", missing: "当前状态尚不能对应到确定的核对结论。", next: "先刷新或展开原始记录确认进度，不要据此改稿。" };
  }
}

/** 主卡只摘取报告已经给出的连续引文；按 Unicode 字符截取，不翻译、补字或据此确认冲突。 */
function manualEditQuotePreview(job: ManualEditJob): { quote: string; label: string; shortened: boolean } | null {
  const source = job.issues?.find(issue => issue.source_quote?.trim());
  const changed = job.issues?.find(issue => (issue.quote || issue.evidence)?.trim());
  const text = source?.source_quote || changed?.quote || changed?.evidence;
  if (!text) return null;
  const characters = Array.from(text);
  return { quote: characters.slice(0, 120).join(""), label: source ? `报告提供的对照原句${source.source_path ? ` · ${documentLabel(source.source_path)}` : ""}` : "报告提供的修改引文", shortened: characters.length > 120 };
}

/** 审核原话与双方引文按需展开；普通页面不显示内部任务对象，后台记录与原始引文继续保留。 */
function ManualEditEvidence({ job }: { job: ManualEditJob }) {
  const stopped = ["failed", "interrupted", "cancelled"].includes(job.status);
  return <details className="setting-record-review"><summary>{stopped ? "查看停止原因、审核说明与依据" : "查看审核说明与原文依据"}{job.issues?.length ? ` · ${job.issues.length}条意见` : ""}</summary>
    {job.error && <p><strong>停止原因：</strong>{settingExplanation(job.error)}</p>}
    {job.reason && <p><strong>核对原因：</strong>{settingExplanation(job.reason)}</p>}
    {job.summary && <p><strong>原审核说明：</strong>{settingExplanation(job.summary)}</p>}
    {job.next_action && <p><strong>原处理建议：</strong>{settingExplanation(job.next_action)}</p>}
    {job.issues?.map((issue, index) => <article className="thread-card" key={index}><strong>核对意见 {index + 1}{issue.hard_conflict ? " · 报告标为冲突，仍须结合当前原文判断" : " · 依据与判断请对照原文"}</strong>
      <p>{settingExplanation(issue.reason || issue.summary || issue.explanation || issue.message || "")}</p>
      {(issue.quote || issue.evidence) && <><p>本次改动中的原句：</p><blockquote>{issue.quote || issue.evidence}</blockquote></>}
      {issue.source_quote && <><p>对照来源{issue.source_path ? `：${documentLabel(issue.source_path)}` : "中的原句："}</p><blockquote>{issue.source_quote}</blockquote></>}
    </article>)}
    {job.user_reason && <><p>你保存的解释：</p><blockquote>{job.user_reason}</blockquote></>}
  </details>;
}

/** 展示当前核对状态，并沿原决定服务保存作者选择；表单输入不因详情折叠而清空。 */
export function ManualEditMonitor({ jobs, error, onRefresh, onDecision }: { jobs: ManualEditJob[]; error: string; onRefresh: () => void; onDecision: (job: ManualEditJob, decision: string, reason: string) => Promise<void> }) {
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [working, setWorking] = useState<string | null>(null);
  const act = async (job: ManualEditJob, decision: string) => {
    if (working) return;
    const reason = (reasons[job.job_id] || "").trim();
    // 缺依据也可能需要作者解释，不能在确认窗里预先断言它已经与正文冲突。
    if (decision === "confirm_exception" && (!reason || !await window.inkflow.confirm("确认保留这次改动并提交你的解释吗？解释会单独记录；后续是否再核仍按原有次数与设置处理，不会直接算作核对通过，也不会自动改写正史。"))) return;
    setWorking(job.job_id);
    try { await onDecision(job, decision, reason); } finally { setWorking(null); }
  };
  return <section className="manual-edit-monitor"><div className="settings-inline-actions"><h3>手动修改核对</h3><button type="button" onClick={onRefresh}>刷新进度</button></div><p className="form-hint">这里显示修改核对的当前结果。具体意见、双方原句和处理记录可以展开查看；等待核对不等于内容已经有错。</p>{error && <p role="alert" className="form-error">{error}</p>}{!jobs.length && <p className="empty-mini">没有需要核对的改动。</p>}{jobs.map(job => {
    const overview = manualEditOverview(job);
    const quote = manualEditQuotePreview(job);
    return <article className="thread-card" key={job.job_id}><header><strong>{job.display_name || documentLabel(job.relative_path || job.path || "设定参考条目")}</strong><span>{jobLabels[job.status] || "状态待确认"}</span></header>
      <div><p><strong>查到了什么：</strong>{overview.found}</p><p><strong>还差什么：</strong>{overview.missing}</p><p><strong>你可以怎么做：</strong>{overview.next}</p></div>
      {quote && <div><p>{quote.label}{quote.shortened ? "（节选，完整引文见详情）" : ""}</p><blockquote>{quote.quote}</blockquote></div>}
      <ManualEditEvidence job={job} />
      {["needs_confirmation", "awaiting_confirmation", "conflict", "blocked", "failed", "interrupted", "cancelled", "needs_evidence", "revision_requested", "kept_pending"].includes(job.status) && <><label>补充你的解释或修改要求<textarea value={reasons[job.job_id] || ""} onChange={event => setReasons({ ...reasons, [job.job_id]: event.target.value })} placeholder="例如：这是人物的误解，请保留；或说明你希望怎样修改。" /></label><p className="form-hint">保存解释或修改要求不会直接改稿；不采用时会按原规则撤回候选，设定条目会留档。</p><div className="settings-inline-actions"><button type="button" disabled={working !== null} onClick={() => void act(job, "keep_pending")}>先留着，稍后处理</button><button type="button" disabled={working !== null} onClick={() => void act(job, "request_revision")}>保存修改要求</button><button type="button" disabled={working !== null || !reasons[job.job_id]?.trim()} onClick={() => void act(job, "confirm_exception")}>保留改动并提交解释</button><button type="button" disabled={working !== null} onClick={() => void act(job, "discard_candidate")}>不采用这次改动</button></div></>}
    </article>;
  })}</section>;
}
