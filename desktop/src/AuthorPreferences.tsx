import { useEffect, useState } from "react";

type Preference = {
  preference_id: string; text: string; strength: string; scope: string; status: string;
  source_quote?: string; source_ref?: string; topic?: string; revision?: number; level?: string; usage_source?: string;
};
type PreferenceSelection = {
  chapter_no?: number; task?: string; mode?: string; collaboration_mode?: string;
  selected?: Preference[]; omitted?: Array<{ preference_id: string; text?: string; reason: string }>;
  budget_reason?: string; warnings?: string[];
  actor?: string; context_packet_id?: string; recorded_at?: string;
  voice_budget_tokens?: number; voice_selected_tokens?: number;
};
type PreferenceDecision = {
  run_id: string; created_at: string;
  decisions: Array<{ status: "resolved" | "needs_choice" | "stale"; reason: string;
    selected_id?: string; preference_ids: string[]; source_quotes: string[] }>;
};
type PreferenceUsage = {
  disabled: string[]; selection: PreferenceSelection;
  decisions?: PreferenceDecision[]; learning_enabled?: boolean;
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
  const [usage, setUsage] = useState<PreferenceSelection>({});
  const [decisions, setDecisions] = useState<PreferenceDecision[]>([]);
  const [learningEnabled, setLearningEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [memory, setMemory] = useState<MemoryAudit | null>(null);
  const scopeKind = scope.split(":", 1)[0];
  const scopeLabels: Record<string, string> = { project: "整个作品", genre: "指定题材", scene: "指定场景", character: "指定人物", chapter: "指定章节", arc: "指定篇章" };
  const scopeText = (value?: string) => value ? (scopeLabels[value.split(":", 1)[0]] || value)
    + (value.includes(":") ? "：" + value.slice(value.indexOf(":") + 1) : "") : "旧记录未保存范围";
  const levelText = (value?: string) => value === "author" ? "作者默认" : value === "project" ? "本书偏好" : "旧记录未保存层级";
  const mode = usage.collaboration_mode || usage.mode;
  const modeLabels: Record<string, string> = { everyday: "日常", review_boost: "审查加强", memory_boost: "记忆加强", full_specialist: "全专项", review: "章节审核", draft: "写章", revise: "修订" };
  const actorLabels: Record<string, string> = { writer: "Writer", editor: "Editor", reviewer: "Reviewer", memory_keeper: "Memory Keeper", coordinator: "Coordinator" };
  const api = <T,>(action: string, values: Record<string, unknown> = {}) =>
    window.inkflow.request<T>("preferences.manage", { project_root: projectRoot, level, action, ...values });
  const refresh = async () => {
    const value = await api<{ preferences: Preference[] }>("list");
    setItems(value.preferences);
    if (projectRoot) {
      const result = await api<PreferenceUsage>("usage");
      setDisabled(result.disabled); setUsage(result.selection); setDecisions(result.decisions || []);
      setLearningEnabled(typeof result.learning_enabled === "boolean" ? result.learning_enabled : null);
    }
  };
  useEffect(() => {
    let current = true;
    setItems([]); setEvents([]); setEditing(null); setText(""); setUsage({}); setError(""); setMemory(null);
    setDisabled([]); setDecisions([]); setLearningEnabled(null);
    if (level === "project" && !projectRoot) return;
    void window.inkflow.request<{ preferences: Preference[] }>("preferences.manage", {
      project_root: projectRoot, level, action: "list",
    }).then(value => { if (current) setItems(value.preferences); })
      .catch(cause => { if (current) setError(String(cause)); });
    if (projectRoot) void window.inkflow.request<PreferenceUsage>(
      "preferences.manage", { project_root: projectRoot, level, action: "usage" },
    ).then(value => { if (current) {
      setDisabled(value.disabled); setUsage(value.selection); setDecisions(value.decisions || []);
      setLearningEnabled(typeof value.learning_enabled === "boolean" ? value.learning_enabled : null);
    } })
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
    {projectRoot && <div className="memory-card">
      <strong>本书反馈学习：{learningEnabled === null ? "尚未读取" : learningEnabled ? "已开启" : "已暂停"}</strong>
      <p className="form-hint">从有原话依据的反馈保留待确认参考，明确长期要求才启用为习惯。暂停学习后，现有习惯仍按范围使用，历史记录保留。</p>
      <button type="button" disabled={busy || learningEnabled === null} onClick={() => void run(async () => {
        await api("learning", { enabled: !learningEnabled }); await refresh();
      })}>{learningEnabled === null ? "读取学习状态中" : learningEnabled ? "暂停本书学习" : "恢复本书学习"}</button>
    </div>}
    <div className="settings-fields two">
      <label>偏好内容<textarea value={text} onChange={event => setText(event.target.value)} maxLength={4000} placeholder="例如：用动作和对话表现情绪，少用旁白总结。" /></label>
      <label>适用范围<select value={scopeKind} onChange={event => setScope(event.target.value === "project" ? "project" : event.target.value + ":")}>
        <option value="project">整个作品</option><option value="genre">指定题材</option><option value="scene">指定场景</option>
        {level === "project" && <><option value="character">指定人物</option><option value="chapter">指定章节</option><option value="arc">指定篇章</option></>}
      </select></label>
      {scopeKind !== "project" && <label>{scopeLabels[scopeKind]}名称<input value={scope.slice(scope.indexOf(":") + 1)} onChange={event => setScope(scopeKind + ":" + event.target.value)} placeholder={scopeKind === "chapter" ? "章节数字，例如 12" : "例如：悬疑、争吵、人物姓名"} /></label>}
      <label>同类习惯名称<input value={topic} onChange={event => setTopic(event.target.value)} placeholder="例如：叙事节奏" /></label>
      <label>原话或满意片段<textarea value={quote} onChange={event => setQuote(event.target.value)} maxLength={8000} /></label>
      <label>来源位置<input value={sourceRef} onChange={event => setSourceRef(event.target.value)} placeholder="消息或章节位置，可留空" /></label>
      <label>修改原因<input value={reason} onChange={event => setReason(event.target.value)} maxLength={2000} /></label>
      {level === "project" && <label>要求强度<select value={strength} onChange={event => setStrength(event.target.value)}>
        <option value="weak">普通偏好</option><option value="hard">本书明确硬要求</option>
      </select></label>}
    </div>
    <p className="form-hint">长期表达习惯可跨书使用；人物、章节和篇章要求只保存在本书。默认习惯不会自动变成审查硬门禁。</p>
    <p className="form-hint">本书偏好与作者默认同主题，且两项适用范围都符合当前任务时，优先使用本书偏好。其他冲突按各自来源和当前要求核对，必要时请你选择。</p>
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
      <strong>{({ active: "正在使用", paused: "已暂停", candidate: "待确认反馈参考，非硬要求", deleted: "已移出使用" } as Record<string, string>)[item.status] || item.status} · {scopeText(item.scope)}</strong>
      <p className="form-hint">主题：{item.topic || "未指定"} · 修订：{item.revision === undefined ? "旧记录未保存" : `v${item.revision}`} · ID：{item.preference_id}</p>
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
    {projectRoot && <details><summary>最近一次上下文的偏好选择记录</summary>
      <p>{usage.chapter_no === undefined ? "旧记录未保存章节" : `第 ${usage.chapter_no} 章`} · {mode ? modeLabels[mode] || mode : "旧记录未保存模式"}{usage.actor ? ` · ${actorLabels[usage.actor] || usage.actor}` : ""}</p>
      {usage.recorded_at && <small>{usage.recorded_at}{usage.context_packet_id ? ` · 上下文：${usage.context_packet_id}` : ""}</small>}
      {usage.task && <p>任务：{usage.task}</p>}
      <p className="form-hint">这里显示记录中实际进入上下文的条目；最终模型输入可在关联运行记录中核对。</p>
      <strong>实际选用</strong>
      {usage.selected?.length ? usage.selected.map(item => <article className="memory-card" key={item.preference_id}>
        <p>{item.text}</p><small>{levelText(item.level)} · {scopeText(item.scope)} · ID：{item.preference_id}</small>
        {item.status === "candidate" && <p className="form-hint">待确认反馈参考，非硬要求。</p>}
        {item.usage_source && <p className="form-hint">选用位置：{item.usage_source}</p>}
      </article>) : <p>没有记录实际选用条目。</p>}
      <strong>未选用及原因</strong>
      {usage.omitted?.length ? usage.omitted.map(item => <article className="memory-card" key={item.preference_id}>
        {item.text && <p>{item.text}</p>}<small>ID：{item.preference_id}</small><p>原因：{item.reason || "旧记录未保存原因"}</p>
      </article>) : <p>没有记录未选用条目。</p>}
      {usage.voice_budget_tokens !== undefined && <p className="form-hint">
        声音参考预算：{usage.voice_budget_tokens.toLocaleString()} token；
        {usage.voice_selected_tokens === undefined ? "旧记录未保存实际用量" : `实际声音段约 ${usage.voice_selected_tokens.toLocaleString()} token（含使用边界说明）`}。
        这个用量只统计声音参考段，硬要求和其他上下文另计。
      </p>}
      <p className="form-hint">预算说明：{usage.budget_reason || (usage.voice_budget_tokens === undefined
        ? "旧记录未保存具体预算；不能据此推断某项一定因预算被省略。"
        : "普通习惯与待确认反馈参考按声音预算选取；具体未选用原因见各条记录，整份上下文仍受当前角色预算限制。")}</p>
      {usage.warnings?.map((warning, index) => <p className="form-hint" key={index}>{warning}</p>)}
      <details><summary>原始选择诊断</summary><pre>{JSON.stringify(usage, null, 2)}</pre></details>
    </details>}
    {projectRoot && <details><summary>最近冲突取舍与待答原因</summary>
      {decisions.length ? decisions.map((record, recordIndex) => <article className="memory-card" key={`${record.run_id}-${recordIndex}`}>
        <small>{record.created_at} · 运行：{record.run_id}</small>
        {record.decisions.map((decision, index) => <div key={index}>
          <strong>{({ resolved: "已确定本次取舍", needs_choice: "等待你选择", stale: "来源变化，取舍待重核" })[decision.status]}</strong>
          <p>{decision.reason}</p>
          {decision.selected_id && <p>本次选用 ID：{decision.selected_id}</p>}
          <p>关联项：{decision.preference_ids.join("、") || "当前任务要求"}</p>
          {decision.source_quotes.map((source, sourceIndex) => <blockquote key={sourceIndex}>{source}</blockquote>)}
        </div>)}
      </article>) : <p>暂无冲突取舍记录。</p>}
      <p className="form-hint">取舍绑定原任务与来源版本；等待选择时请回答关联对话中的提问卡。这里显示记录，不改写已保存习惯。</p>
      <details><summary>原始取舍诊断</summary><pre>{JSON.stringify(decisions, null, 2)}</pre></details>
    </details>}
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
