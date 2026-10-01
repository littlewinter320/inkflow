import { useEffect, useState } from "react";
import "./story-settings.css";

type Field = { key: string; label: string; description?: string; default?: string };
type Template = { template_id: string; name: string; category: string; description: string; fields: Field[] };
type Evidence = { source_kind: string; chapter_no?: number; path?: string; quote: string };
const displayValue = (value: unknown): string => value !== null && typeof value === "object" ? JSON.stringify(value, null, 2) : String(value ?? "");
type SettingRecord = { record_id: string; title: string; values: Record<string, unknown>; status: string; revision: number; epistemic_status?: string; source_role?: string; review?: { issues?: Array<{ reason?: string; explanation?: string; quote?: string; source_quote?: string }>; decision?: string }; evidence_refs?: Evidence[] };
type Collection = { collection_id: string; name: string; category: string; template_id: string; instructions: string; fields: Field[]; revision: number; status: string; read_roles?: string[]; write_roles?: string[]; records?: SettingRecord[] };
const emptyCollection = (): Collection => ({ collection_id: "", name: "", category: "", template_id: "", instructions: "", fields: [], revision: 0, status: "active", read_roles: ["coordinator", "writer", "editor", "reviewer", "memory_keeper"], write_roles: ["writer", "editor", "reviewer", "memory_keeper"] });
const roles = [{ id: "coordinator", label: "Coordinator" }, { id: "writer", label: "Writer" }, { id: "editor", label: "Editor" }, { id: "reviewer", label: "Reviewer" }, { id: "memory_keeper", label: "Memory Keeper" }];
const statuses: Record<string, string> = { active: "使用中", archived: "已留档", pending_review: "等待核对", approved: "已核对", accepted: "已采用", conflict: "存在分歧", rejected: "未采用", needs_user: "需要你决定", needs_evidence: "需要补充依据", verified_reference: "已核对参考", user_kept_hypothesis: "保留为设想" };

export function StorySettings({ projectRoot }: { projectRoot: string }) {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [showTemplates, setShowTemplates] = useState(true);
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
  const api = <T,>(method: string, params: Record<string, unknown> = {}) => window.inkflow.request<T>(method, { project_root: projectRoot, ...params });
  const refresh = async (collectionId = editing.collection_id) => {
    const result = await api<{ collections: Collection[] }>("story_settings.list", { include_archived: true });
    setCollections(result.collections || []);
    const current = result.collections.find(item => item.collection_id === collectionId);
    if (current) setEditing(current);
  };
  useEffect(() => {
    let current = true;
    setEditing(emptyCollection()); setRecord(null); setCollections([]); setError(""); setNotice("");
    void window.inkflow.request<{ templates: Template[] }>("story_settings.templates", projectRoot ? { project_root: projectRoot } : {}).then(result => { if (current) setTemplates(result.templates || []); }).catch(cause => { if (current) setError(String(cause)); });
    if (projectRoot) void window.inkflow.request<{ collections: Collection[] }>("story_settings.list", { project_root: projectRoot, include_archived: true }).then(result => { if (current) setCollections(result.collections || []); }).catch(cause => { if (current) setError(String(cause)); });
    return () => { current = false; };
  }, [projectRoot]);
  const run = async (work: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (cause) { setError(String(cause)); } finally { setBusy(false); }
  };
  const editRecord = (item: SettingRecord | null, collection = editing) => {
    setRecord(item); setRecordDecisionReason(""); setTitle(item?.title || ""); setEpistemic(item?.epistemic_status || "hypothesis");
    setValues(Object.fromEntries(collection.fields.map(field => [field.key, displayValue(item?.values[field.key] ?? field.default ?? "")])));
    const evidence = item?.evidence_refs?.[0];
    setExtraEvidence(item?.evidence_refs?.slice(1) || []);
    setSourceKind(evidence?.source_kind || "canonical"); setSourceChapter(evidence?.chapter_no ? String(evidence.chapter_no) : ""); setSourcePath(evidence?.path || ""); setSourceQuote(evidence?.quote || "");
  };
  const chooseCollection = (item: Collection) => { setEditing(item); editRecord(null, item); setShowTemplates(false); setNotice(""); setError(""); };
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
    await api("story_settings.record.save", { collection_id: editing.collection_id, record_id: record?.record_id || "", title, values: savedValues, epistemic_status: epistemic, evidence_refs: evidence, expected_revision: record?.revision || 0, operation: record ? "correct" : "new" });
    await refresh(); editRecord(null); setNotice("条目已保存为待核对记录，点“刷新”查看后台结果。保存不会自动覆盖已接受正史。");
  });
  const keepRecordHypothesis = (item: SettingRecord) => run(async () => {
    if (!recordDecisionReason.trim() || !await window.inkflow.confirm("确认保留为创作设想吗？未解决的原审查意见会保留，这个决定不会让它成为已发生的正史。")) return;
    await api("story_settings.record.decide", { record_id: item.record_id, expected_revision: item.revision, decision: "keep_hypothesis", reason: recordDecisionReason }); await refresh(); editRecord(null);
  });
  const archiveRecord = (item: SettingRecord) => run(async () => {
    if (!await window.inkflow.confirm(`将“${item.title}”移出使用并保留历史？`)) return;
    await api("story_settings.record.archive", { collection_id: editing.collection_id, record_id: item.record_id, expected_revision: item.revision }); await refresh();
    if (record?.record_id === item.record_id) editRecord(null);
  });
  return <section className="story-settings-manager">
    <p className="form-hint">设定是本书持续维护的资料合集。可以从规划、章节和钩子整理内容；未来设想、人物信念与已发生事实分别记录。你决定每个模块写什么，Writer 提出新内容，审查与记忆角色补证核对。</p>
    {!projectRoot && <p className="form-hint">先打开一本小说，再创建本书设定。下面可以先查看预设模板。</p>}
    <details className="setting-template-picker" open={showTemplates} onToggle={event => setShowTemplates(event.currentTarget.open)}>
      <summary>选择预设模板 · {templates.length} 类</summary>
      <div className="setting-template-grid">{templates.map(template => <button type="button" disabled={!projectRoot || busy} key={template.template_id} onClick={() => chooseTemplate(template)}><strong>{template.name}</strong><small>{template.description}</small></button>)}</div>
    </details>
    <div className="setting-manager-columns">
      <aside className="setting-collection-list" aria-label="本书设定模块">
        <div className="settings-inline-actions"><button type="button" disabled={!projectRoot || busy} onClick={() => chooseCollection(emptyCollection())}>＋ 自定义模块</button><button type="button" disabled={!projectRoot || busy} onClick={() => void run(async () => { await refresh(); setNotice("已读取当前设定版本与核对结果。"); })}>刷新</button><label><input type="checkbox" checked={showArchived} onChange={event => setShowArchived(event.target.checked)} />显示留档</label></div>
        {collections.filter(item => showArchived || item.status !== "archived").map(item => <button type="button" disabled={busy} key={item.collection_id} className={editing.collection_id === item.collection_id ? "active" : ""} onClick={() => chooseCollection(item)}><strong>{item.name}</strong><small>{item.category} · {statuses[item.status] || item.status} · {item.records?.length || 0} 条</small></button>)}
        {!collections.length && <p className="empty-mini">选择一个模板即可开始，也可以自行定义记录字段。</p>}
      </aside>
      <fieldset className="setting-module-editor" disabled={busy}>
        <div className="settings-fields two"><label>设定名称<input value={editing.name} disabled={busy || editing.status === "archived"} maxLength={100} placeholder="例如：人物变化档案" onChange={event => setEditing({ ...editing, name: event.target.value })} /></label><label>设定分类<input value={editing.category} disabled={busy || editing.status === "archived"} maxLength={100} placeholder="例如：人物、场景、道具" onChange={event => setEditing({ ...editing, category: event.target.value })} /></label></div>
        <label>这个模块负责记录什么<textarea value={editing.instructions} disabled={busy || editing.status === "archived"} maxLength={6000} placeholder="告诉各角色需要记录哪些内容、何时更新，以及哪些内容不要加入。" onChange={event => setEditing({ ...editing, instructions: event.target.value })} /></label>
        <details><summary>哪些角色可以读取和补充</summary><p className="form-hint">新内容仍由 Writer 提出，审查和记忆角色只能根据依据补充、纠正。Coordinator 只理解和派工。</p>{(["read_roles", "write_roles"] as const).map(permission => <fieldset key={permission} className="setting-role-permissions"><legend>{permission === "read_roles" ? "读取" : "记录和补充"}</legend>{roles.filter(role => permission === "read_roles" || role.id !== "coordinator").map(role => <label key={role.id}><input type="checkbox" checked={(editing[permission] || []).includes(role.id)} disabled={busy || editing.status === "archived"} onChange={event => setEditing({ ...editing, [permission]: event.target.checked ? [...(editing[permission] || []), role.id] : (editing[permission] || []).filter(id => id !== role.id) })} />{role.label}</label>)}</fieldset>)}</details>
        <details className="setting-fields-editor"><summary>记录字段 · {editing.fields.length} 项</summary>{editing.fields.map((field, index) => <div key={index} className="setting-field-row"><label>字段标识<input disabled={busy || Boolean(editing.collection_id)} value={field.key} maxLength={48} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, key: event.target.value } : value) })} /></label><label>显示名称<input disabled={busy || editing.status === "archived"} value={field.label} maxLength={100} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, label: event.target.value } : value) })} /></label><label>填写提示<input disabled={busy || editing.status === "archived"} value={field.description || ""} maxLength={1000} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, description: event.target.value } : value) })} /></label><label>新条目默认内容<input disabled={busy || editing.status === "archived"} value={field.default || ""} maxLength={2000} onChange={event => setEditing({ ...editing, fields: editing.fields.map((value, at) => at === index ? { ...value, default: event.target.value } : value) })} /></label><button type="button" disabled={busy || editing.status === "archived"} aria-label={`移除字段 ${field.label}`} onClick={() => setEditing({ ...editing, fields: editing.fields.filter((_, at) => at !== index) })}>移除</button></div>)}<button type="button" disabled={busy || editing.status === "archived" || editing.fields.length >= 40} onClick={() => setEditing({ ...editing, fields: [...editing.fields, { key: `field_${Date.now()}`, label: "新字段", description: "" }] })}>＋ 添加字段</button></details>
        <div className="settings-inline-actions"><button type="button" className="primary" disabled={!projectRoot || busy || !editing.name.trim() || !editing.fields.length || editing.status === "archived"} onClick={() => void saveCollection()}>保存模块</button>{editing.collection_id && editing.status !== "archived" && <button type="button" disabled={busy} onClick={() => void archiveCollection(editing)}>移出使用并留档</button>}</div>
        {editing.collection_id && <section className="setting-records"><h4>具体条目</h4><div className="setting-record-list">{(editing.records || []).filter(item => showArchived || item.status !== "archived").map(item => <article key={item.record_id}><button type="button" onClick={() => editRecord(item)}><strong>{item.title}</strong><span>{statuses[item.status] || item.status} · 修订 {item.revision}</span></button>{item.status !== "archived" && <button type="button" disabled={busy} onClick={() => void archiveRecord(item)}>留档</button>}</article>)}</div>
          {record && (record.review?.issues?.length || ["needs_user", "needs_evidence"].includes(record.status)) ? <section className="setting-record-review"><h4>这条记录的审查依据</h4>{(record.review?.issues || []).map((issue, index) => <article className="thread-card" key={index}><p>{issue.reason || issue.explanation}</p>{issue.quote && <blockquote>{issue.quote}</blockquote>}{issue.source_quote && <blockquote>对照来源：{issue.source_quote}</blockquote>}</article>)}{["needs_user", "needs_evidence"].includes(record.status) && <><label>保留这项设想的原因<textarea value={recordDecisionReason} onChange={event => setRecordDecisionReason(event.target.value)} maxLength={4000} /></label><button type="button" disabled={busy || !recordDecisionReason.trim()} onClick={() => void keepRecordHypothesis(record)}>保留为设想并记录原因</button></>}</section> : null}
          {editing.status !== "archived" && <><div className="settings-inline-actions"><h4>{record ? `编辑：${record.title}` : "添加新条目"}</h4>{record && <button type="button" onClick={() => editRecord(null)}>取消编辑</button>}</div><div className="settings-fields two"><label>条目名称<input value={title} onChange={event => setTitle(event.target.value)} maxLength={200} /></label><label>内容性质<select value={epistemic} onChange={event => setEpistemic(event.target.value)}><option value="hypothesis">未来设想 / 未定设定</option><option value="objective">已发生的客观事实</option><option value="belief">人物相信的事</option><option value="rumor">传闻 / 未证实信息</option><option value="user_constraint">用户约定 / 需按范围理解</option></select></label></div><div className="settings-fields two">{editing.fields.map(field => <label key={field.key}>{field.label}<textarea value={values[field.key] || ""} placeholder={field.description || field.default || ""} maxLength={8000} onChange={event => setValues({ ...values, [field.key]: event.target.value })} /></label>)}</div><details><summary>原文来源 · 可选，核对时不会只凭设定自证</summary><div className="settings-fields two"><label>资料类型<select value={sourceKind} onChange={event => setSourceKind(event.target.value)}><option value="canonical">已接受正文</option><option value="draft">当前草稿</option><option value="planning">当前规划</option><option value="writer_hook">Writer 钩子说明</option></select></label><label>来源章节<input type="number" min={1} value={sourceChapter} onChange={event => setSourceChapter(event.target.value)} /></label><label>本书文件位置<input value={sourcePath} onChange={event => setSourcePath(event.target.value)} placeholder="可选，项目内相对路径" /></label><label>可定位的原文<textarea value={sourceQuote} onChange={event => setSourceQuote(event.target.value)} maxLength={8000} /></label></div></details>{extraEvidence.length > 0 && <details><summary>保留的其他原文依据 · {extraEvidence.length} 条</summary>{extraEvidence.map((ref, index) => <article key={index} className="thread-card"><p>{ref.chapter_no ? `第 ${ref.chapter_no} 章` : ref.path || ref.source_kind}</p><blockquote>{ref.quote}</blockquote><button type="button" disabled={busy} onClick={() => setExtraEvidence(extraEvidence.filter((_, at) => at !== index))}>从这次候选中移除此条依据</button></article>)}</details>}<button type="button" className="primary" disabled={busy || !title.trim() || record?.status === "archived"} onClick={() => void saveRecord()}>保存条目并等待核对</button></>}
        </section>}
      </fieldset>
    </div>
    {notice && <p className="form-success" role="status">{notice}</p>}{error && <p className="form-error" role="alert">{error}</p>}
  </section>;
}

export type ManualEditJob = { job_id: string; status: string; relative_path?: string; path?: string; current_hash?: string; expected_hash?: string; content_hash?: string; summary?: string; reason?: string; error?: string; next_action?: string; issues?: Array<{ summary?: string; explanation?: string; message?: string; evidence?: string; reason?: string; quote?: string; source_quote?: string; source_path?: string; hard_conflict?: boolean }>; created_at?: string };
const jobLabels: Record<string, string> = { pending: "等待核对", reviewing: "正在核对", paused: "已按设置暂停", reconciled: "新规划已衔接", acknowledged_hypothesis: "已保留为设想", pending_review: "等待核对", review_pending: "等待核对", needs_confirmation: "需要你决定", awaiting_confirmation: "需要你决定", conflict: "发现冲突", blocked: "缺少依据", failed: "核对未完成", approved: "核对通过", unchanged: "内容未变", passed: "核对通过", needs_evidence: "需要补充依据", revision_requested: "待定向修订", discarded: "候选未采用", superseded: "已由新版替代", completed: "已完成", kept_pending: "保留待处理", exception_confirmed: "例外已确认" };
export function ManualEditMonitor({ jobs, error, onRefresh, onDecision }: { jobs: ManualEditJob[]; error: string; onRefresh: () => void; onDecision: (job: ManualEditJob, decision: string, reason: string) => Promise<void> }) {
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [working, setWorking] = useState<string | null>(null);
  const act = async (job: ManualEditJob, decision: string) => {
    if (working) return;
    const reason = (reasons[job.job_id] || "").trim();
    if (decision === "confirm_exception" && (!reason || !await window.inkflow.confirm("这份改动仍有未解决的冲突。确认将它作为明确例外保留吗？你的原因会单独记录，不会被记成审核通过，也不会默默改写正史。"))) return;
    setWorking(job.job_id);
    try { await onDecision(job, decision, reason); } finally { setWorking(null); }
  };
  return <section className="manual-edit-monitor"><div className="settings-inline-actions"><h3>手动修改核对</h3><button type="button" onClick={onRefresh}>刷新</button></div><p className="form-hint">后台只比较真正发生变化的文件。未改动不重新审查；记录、疑问和你的决定在这里查看。</p>{error && <p role="alert" className="form-error">{error}</p>}{!jobs.length && <p className="empty-mini">没有需要核对的改动。</p>}{jobs.map(job => <article className="thread-card" key={job.job_id}><header><strong>{job.relative_path || job.path || "设定记录"}</strong><span>{jobLabels[job.status] || job.status}</span></header><p>{job.error || job.reason || job.summary || "正在根据当前设定和正史核对。"}</p>{job.next_action && <p className="form-hint">下一步：{job.next_action}</p>}{job.issues?.map((issue, index) => <div key={index}><p>{issue.reason || issue.summary || issue.explanation || issue.message}</p>{(issue.quote || issue.evidence) && <blockquote>本次改动：{issue.quote || issue.evidence}</blockquote>}{issue.source_quote && <blockquote>对照原文：{issue.source_quote}{issue.source_path ? `（${issue.source_path}）` : ""}</blockquote>}</div>)}{["needs_confirmation", "awaiting_confirmation", "conflict", "blocked", "failed", "needs_evidence", "revision_requested", "kept_pending"].includes(job.status) && <><label>你的原因或处理要求<textarea value={reasons[job.job_id] || ""} onChange={event => setReasons({ ...reasons, [job.job_id]: event.target.value })} placeholder="例如：人物有意说谎，保留这处叙述；或说明希望如何修改。" /></label><div className="settings-inline-actions"><button type="button" disabled={working !== null} onClick={() => void act(job, "keep_pending")}>保留，稍后处理</button><button type="button" disabled={working !== null} onClick={() => void act(job, "request_revision")}>记录修订要求</button><button type="button" disabled={working !== null || !reasons[job.job_id]?.trim()} onClick={() => void act(job, "confirm_exception")}>确认例外并记录原因</button><button type="button" disabled={working !== null} onClick={() => void act(job, "discard_candidate")}>不采用这次候选</button></div></>}</article>)}</section>;
}
