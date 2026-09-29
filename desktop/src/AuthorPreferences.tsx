import { useEffect, useState } from "react";

type Preference = {
  preference_id: string; text: string; strength: string; scope: string; status: string;
  source_quote?: string; source_ref?: string; topic?: string; revision?: number;
};
type Event = { event_id: string; reason: string; created_at: string; before?: Preference; after: Preference };
type MemoryAudit = { facts: Array<{ fact_id: string; subject: string; predicate: string; status: string;
  evidence_refs: Array<{ source_chapter: number; source_version: number; quote: string; relation: string; status: string }>;
  dependent_artifacts: string[] }>; events: Array<{ event_id: string; fact_id: string; action: string; reason: string; created_at: string }>;
  notice: string };

export function AuthorPreferences({ projectRoot }: { projectRoot: string }) {
  const [level, setLevel] = useState("author");
  const [items, setItems] = useState<Preference[]>([]);
  const [editing, setEditing] = useState<Preference | null>(null);
  const [text, setText] = useState("");
  const [scope, setScope] = useState("project");
  const [topic, setTopic] = useState("");
  const [quote, setQuote] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [reason, setReason] = useState("");
  const [strength, setStrength] = useState("weak");
  const [events, setEvents] = useState<Event[]>([]);
  const [disabled, setDisabled] = useState<string[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [memory, setMemory] = useState<MemoryAudit | null>(null);
  const scopeKind = scope.split(":", 1)[0];
  const scopeLabels: Record<string, string> = { project: "整个作品", genre: "指定题材", scene: "指定场景", character: "指定人物", chapter: "指定章节", arc: "指定篇章" };
  const api = <T,>(action: string, values: Record<string, unknown> = {}) =>
    window.inkflow.request<T>("preferences.manage", { project_root: projectRoot, level, action, ...values });
  const refresh = async () => {
    const value = await api<{ preferences: Preference[] }>("list");
    setItems(value.preferences);
    if (projectRoot) {
      const result = await api<{ disabled: string[]; selection: Record<string, unknown> }>("usage");
      setDisabled(result.disabled); setUsage(result.selection);
    }
  };
  useEffect(() => {
    let current = true;
    setItems([]); setEvents([]); setEditing(null); setText(""); setUsage({}); setError(""); setMemory(null);
    if (level === "project" && !projectRoot) return;
    void window.inkflow.request<{ preferences: Preference[] }>("preferences.manage", {
      project_root: projectRoot, level, action: "list",
    }).then(value => { if (current) setItems(value.preferences); })
      .catch(cause => { if (current) setError(String(cause)); });
    if (projectRoot) void window.inkflow.request<{ disabled: string[]; selection: Record<string, unknown> }>(
      "preferences.manage", { project_root: projectRoot, level, action: "usage" },
    ).then(value => { if (current) { setDisabled(value.disabled); setUsage(value.selection); } })
      .catch(cause => { if (current) setError(String(cause)); });
    return () => { current = false; };
  }, [level, projectRoot]);
  const run = async (work: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError("");
    try { await work(); } catch (cause) { setError(String(cause)); } finally { setBusy(false); }
  };
  const edit = (item: Preference | null) => {
    setEditing(item); setText(item?.text || ""); setScope(item?.scope || "project");
    setTopic(item?.topic || ""); setQuote(item?.source_quote || ""); setSourceRef(item?.source_ref || "");
    setStrength(item?.strength || "weak"); setReason("");
  };
  const changeStatus = (item: Preference, status: string) => run(async () => {
    await api("save", { ...item, status, expected_revision: item.revision || 0,
      reason: status === "active" ? "用户确认采用" : "用户停止使用，保留历史" }); await refresh();
  });
  return <section className="stack-section">
    <p className="form-hint">作者默认习惯可跨书使用。本书要求和当前指令优先；人物经历与剧情不会进入作者习惯。这里的保存立即生效。</p>
    <label>管理范围<select disabled={busy} value={level} onChange={event => { setLevel(event.target.value); edit(null); }}>
      <option value="author">作者默认习惯 · 所有小说</option>
      <option value="project" disabled={!projectRoot}>本书偏好</option>
    </select></label>
    <div className="settings-fields two">
      <label>偏好内容<textarea value={text} onChange={event => setText(event.target.value)} maxLength={4000} placeholder="例如：用动作和对话表现情绪，少用旁白总结。" /></label>
      <label>适用范围<select value={scopeKind} onChange={event => setScope(event.target.value === "project" ? "project" : event.target.value + ":")}>
        <option value="project">整个作品</option><option value="genre">指定题材</option><option value="scene">指定场景</option>
        {level === "project" && <><option value="character">指定人物</option><option value="chapter">指定章节</option><option value="arc">指定篇章</option></>}
      </select></label>
      {scopeKind !== "project" && <label>{scopeLabels[scopeKind]}名称<input value={scope.slice(scope.indexOf(":") + 1)} onChange={event => setScope(scopeKind + ":" + event.target.value)} placeholder={scopeKind === "chapter" ? "章节数字，例如 12" : "例如：悬疑、争吵、人物姓名"} /></label>}
      <label>同类习惯名称<input value={topic} onChange={event => setTopic(event.target.value)} placeholder="例如：叙事节奏。本书同名项覆盖作者默认。" /></label>
      <label>原话或满意片段<textarea value={quote} onChange={event => setQuote(event.target.value)} maxLength={8000} /></label>
      <label>来源位置<input value={sourceRef} onChange={event => setSourceRef(event.target.value)} placeholder="消息或章节位置，可留空" /></label>
      <label>修改原因<input value={reason} onChange={event => setReason(event.target.value)} maxLength={2000} /></label>
      {level === "project" && <label>要求强度<select value={strength} onChange={event => setStrength(event.target.value)}>
        <option value="weak">普通偏好</option><option value="hard">本书明确硬要求</option>
      </select></label>}
    </div>
    <p className="form-hint">长期表达习惯可跨书使用；人物、章节和篇章要求只保存在本书。默认习惯不会自动变成审查硬门禁。</p>
    <div className="settings-inline-actions">
      <button type="button" disabled={busy || !text.trim()} onClick={() => void run(async () => {
        await api("save", { preference_id: editing?.preference_id, expected_revision: editing?.revision || 0,
          text, scope, topic, source_quote: quote || text, source_ref: sourceRef,
          strength: level === "author" ? "weak" : strength, reason: reason || "用户在设置中保存", status: "active" });
        edit(null); await refresh();
      })}>{editing ? "保存修改" : "添加习惯"}</button>
      {editing && <button type="button" onClick={() => edit(null)}>取消编辑</button>}
    </div>
    {error && <p role="alert" className="form-error">{error}</p>}
    {items.map(item => <article className="memory-card" key={item.preference_id}>
      <strong>{({ active: "正在使用", paused: "已暂停", candidate: "待确认的理解", deleted: "已移出使用" } as Record<string, string>)[item.status] || item.status} · {scopeLabels[item.scope.split(":", 1)[0]] || "指定范围"}{item.scope.includes(":") ? "：" + item.scope.slice(item.scope.indexOf(":") + 1) : ""}</strong>
      <p>{item.text}</p><small>来源：{item.source_quote || "旧记录未保存原话"}{item.source_ref ? ` · ${item.source_ref}` : ""}</small>
      <div className="settings-inline-actions">
        <button type="button" disabled={busy} onClick={() => edit(item)}>编辑</button>
        <button type="button" disabled={busy} onClick={() => void changeStatus(item, item.status === "active" ? "paused" : "active")}>{item.status === "active" ? "暂停" : "采用"}</button>
        {item.status !== "deleted" && <button type="button" disabled={busy} onClick={() => void changeStatus(item, "deleted")}>移出使用并留档</button>}
        <button type="button" disabled={busy} onClick={() => void run(async () => { setEvents((await api<{ events: Event[] }>("history", { preference_id: item.preference_id })).events); })}>变更历史</button>
        {level === "author" && projectRoot && <button type="button" disabled={busy} onClick={() => void run(async () => {
          await api("override", { preference_id: item.preference_id, disabled: !disabled.includes(item.preference_id) }); await refresh();
        })}>{disabled.includes(item.preference_id) ? "本书恢复使用" : "仅本书停用"}</button>}
      </div>
    </article>)}
    {events.length > 0 && <details open><summary>偏好变更历史</summary>{events.map(event => <article key={event.event_id}>
      <small>{event.created_at} · {event.reason}</small><p>修改前：{event.before?.text || "无"}（{event.before?.status || "无"}）</p>
      <p>修改后：{event.after.text}（{event.after.status}）</p>
    </article>)}</details>}
    {projectRoot && <details><summary>最近一次上下文的偏好选择记录</summary><p className="form-hint">记录候选选择与未选择原因；最终模型输入以运行记录的上下文文件为准。</p><pre>{JSON.stringify(usage, null, 2)}</pre></details>}
    {projectRoot && <details><summary>本书事实、证据与变更追踪</summary>
      <button type="button" disabled={busy} onClick={() => void run(async () => {
        setMemory(await window.inkflow.request<MemoryAudit>("memory.audit", { project_root: projectRoot }));
      })}>读取本书记忆记录</button>
      {memory && <><p className="form-hint">{memory.notice} 小说事实由审查与接受流程更新；这里供核对来源。</p>
        {memory.facts.map(fact => <article className="memory-card" key={fact.fact_id}>
          <strong>{fact.subject} · {fact.predicate} · {fact.status === "active" ? "当前记录" : "历史记录"}</strong>
          {fact.evidence_refs.map((ref, index) => <p key={index}>第 {ref.source_chapter} 章 · v{ref.source_version || "旧版未绑定"} · {({ support: "支持", counter: "反证", belief: "人物信念", reference: "参考" } as Record<string, string>)[ref.relation]} · {ref.status === "valid" ? "来源有效" : ref.status === "retired" ? "已停用，保留历史" : "来源变化，待核对"}<br />{ref.quote}</p>)}
          <small>已记录的直接依赖：{fact.dependent_artifacts.length} 份写作上下文</small>
          <details><summary>该事实的增删改历史</summary>{memory.events.filter(event => event.fact_id === fact.fact_id).map(event => <p key={event.event_id}>{event.created_at} · {({ add: "新增", replace: "状态更新", supplement: "补充证据", retract: "撤销当前效力", replace_evidence: "替换附加证据", retire_evidence: "停用附加证据", evidence_add: "登记证据", evidence_replace: "重新绑定证据" } as Record<string, string>)[event.action] || "变更记录"} · {event.reason}</p>)}</details>
        </article>)}
      </>}
    </details>}
  </section>;
}
