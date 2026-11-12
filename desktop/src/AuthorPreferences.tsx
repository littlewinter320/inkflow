import { useCallback, useEffect, useRef, useState } from "react";

type Preference = {
  preference_id: string; text: string; strength: string; scope: string; status: string;
  source_quote?: string; source_ref?: string; topic?: string; revision?: number; level?: string; usage_source?: string;
};
type PreferenceUsage = {
  disabled: string[]; learning_enabled?: boolean;
};
type Event = { event_id: string; reason: string; created_at: string; before?: Preference; after: Preference };
type MaterialTask = { run_id: string; status: string; summary?: string; error_message?: string;
  analysis?: { reason: string; applicability: string; preference: string; question: string; level: string; scope: string;
    coverage?: string[];
    strengths: Array<{ quote: string; location: string; explanation: string }> } };

// 管理跨书习惯、本书偏好和反馈学习开关。
export function AuthorPreferences({ projectRoot }: { projectRoot: string }) {
  const [level, setLevel] = useState("author");
  const [items, setItems] = useState<Preference[]>([]);
  const [editing, setEditing] = useState<Preference | null>(null);
  const [text, setText] = useState("");
  const [scope, setScope] = useState("project");
  const [quote, setQuote] = useState("");
  const [reason, setReason] = useState("");
  const [strength, setStrength] = useState("weak");
  const [events, setEvents] = useState<Event[]>([]);
  const [disabled, setDisabled] = useState<string[]>([]);
  const [learningEnabled, setLearningEnabled] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [materialFiles, setMaterialFiles] = useState<string[]>([]);
  const [materialTasks, setMaterialTasks] = useState<MaterialTask[]>([]);
  const [materialNotice, setMaterialNotice] = useState("");
  const [materialError, setMaterialError] = useState("");
  const [submittingMaterial, setSubmittingMaterial] = useState(false);
  const [materialAnswers, setMaterialAnswers] = useState<Record<string, string>>({});
  const materialRead = useRef(0);
  const scopeKind = scope.split(":", 1)[0];
  const scopeLabels: Record<string, string> = { project: "整个作品", genre: "指定题材", scene: "指定场景", character: "指定人物", chapter: "指定章节", arc: "指定篇章" };
  const scopeText = (value?: string) => value ? (scopeLabels[value.split(":", 1)[0]] || value)
    + (value.includes(":") ? "：" + value.slice(value.indexOf(":") + 1) : "") : "旧记录未保存范围";
  const api = <T,>(action: string, values: Record<string, unknown> = {}) =>
    window.inkflow.request<T>("preferences.manage", { project_root: projectRoot, level, action, ...values });
  const refresh = async () => {
    const value = await api<{ preferences: Preference[] }>("list");
    setItems(value.preferences);
    if (projectRoot) {
      const result = await api<PreferenceUsage>("usage");
      setDisabled(result.disabled);
      setLearningEnabled(typeof result.learning_enabled === "boolean" ? result.learning_enabled : null);
    }
  };
  // 后台结果按小说读取，关闭设置后任务仍由引擎负责。
  const refreshMaterials = useCallback(async () => {
    if (!projectRoot) return;
    const sequence = ++materialRead.current;
    try {
      const result = await window.inkflow.request<{ tasks: MaterialTask[] }>(
        "preferences.materials.list", { project_root: projectRoot });
      if (sequence === materialRead.current) setMaterialTasks(result.tasks);
    } catch (cause) {
      if (sequence === materialRead.current) setMaterialError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [projectRoot]);
  useEffect(() => {
    setMaterialFiles([]); setMaterialTasks([]); setMaterialError(""); setMaterialNotice(""); setMaterialAnswers({});
    void refreshMaterials();
    if (!projectRoot) return;
    const unsubscribe = window.inkflow.onEvent(value => {
      const event = value as { run_id?: string; type?: string; project_root?: string };
      if (event.project_root?.toLowerCase() === projectRoot.toLowerCase() &&
        event.run_id?.startsWith("preference-material-") && event.type?.startsWith("run.")) {
        void refreshMaterials();
      }
    });
    return () => { unsubscribe(); ++materialRead.current; };
  }, [projectRoot, refreshMaterials]);
  useEffect(() => {
    let current = true;
    setItems([]); setEvents([]); setEditing(null); setText(""); setError("");
    setDisabled([]); setLearningEnabled(null);
    if (level === "project" && !projectRoot) return;
    void window.inkflow.request<{ preferences: Preference[] }>("preferences.manage", {
      project_root: projectRoot, level, action: "list",
    }).then(value => { if (current) setItems(value.preferences); })
      .catch(cause => { if (current) setError(String(cause)); });
    if (projectRoot) void window.inkflow.request<PreferenceUsage>(
      "preferences.manage", { project_root: projectRoot, level, action: "usage" },
    ).then(value => { if (current) {
      setDisabled(value.disabled);
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
    setQuote(item?.source_quote || "");
    setStrength(item?.strength || "weak"); setReason("");
  };
  const changeStatus = (item: Preference, status: string) => run(async () => {
    await api("save", { ...item, status, expected_revision: item.revision || 0,
      reason: status === "active" ? "用户确认采用" : "用户停止使用，保留历史" }); await refresh();
  });
  // 使用原生文件选择器，分析时传路径，桌面界面不读取文件正文。
  const chooseMaterials = async () => {
    setMaterialError("");
    try {
      const files = await window.inkflow.choosePreferenceMaterials();
      if (files.length > 4) throw new Error("一次最多选择 4 个文件。");
      if (files.length) setMaterialFiles(files);
    } catch (cause) {
      setMaterialError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  const submitMaterials = async (questionTask?: MaterialTask) => {
    if (submittingMaterial || !projectRoot) return;
    setSubmittingMaterial(true); setMaterialError(""); setMaterialNotice("");
    try {
      await window.inkflow.request("preferences.materials.start", {
        project_root: projectRoot,
        material: { files: materialFiles, excerpt: quote, level: questionTask?.analysis?.level || level,
          scope: questionTask?.analysis?.scope || scope, instruction: text },
        ...(questionTask && { answer_to: questionTask.run_id, answer: materialAnswers[questionTask.run_id] }),
      });
      setMaterialNotice("已交给后台分析。你可以关闭设置，在后台任务中查看结果。");
      await refreshMaterials();
    } catch (cause) {
      setMaterialError(cause instanceof Error ? cause.message : "素材暂时无法读取或提交，请检查文件编码后重试。");
    } finally { setSubmittingMaterial(false); }
  };
  // 采用和结束都绑定原回执，刷新习惯时复用正常保存后的读入流程。
  const decideMaterial = (task: MaterialTask, decision: "adopt" | "dismiss") => run(async () => {
    const result = await window.inkflow.request<{ summary: string }>("preferences.materials.decide", {
      project_root: projectRoot, run_id: task.run_id, decision,
    });
    setMaterialNotice(result.summary);
    await refreshMaterials();
    if (decision === "adopt") await refresh();
  });
  return <section className="stack-section author-preferences">
    <p className="form-hint">作者默认习惯可跨书使用。本书要求和当前指令优先；人物经历与剧情不会进入作者习惯。这里的保存立即生效。</p>
    <label>管理范围<select disabled={busy} value={level} onChange={event => { setLevel(event.target.value); edit(null); }}>
      <option value="author">作者默认习惯 · 所有小说</option>
      <option value="project" disabled={!projectRoot}>本书偏好</option>
    </select></label>
    {projectRoot && <div className="memory-card">
      <strong>本书反馈学习：{learningEnabled === null ? "尚未读取" : learningEnabled ? "已开启" : "已暂停"}</strong>
      <p className="form-hint">墨流在后台参考你明确喜欢或希望改进的写法，不逐次打扰。具体反馈会影响普通参考的取用顺序；长期范围明确才启用为习惯。暂停后不新增隐式学习，现有习惯仍按范围使用。</p>
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
      <label>原话或满意片段<textarea value={quote} onChange={event => setQuote(event.target.value)} maxLength={8000} /></label>
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
          text, scope, source_quote: quote || text,
          strength: level === "author" ? "weak" : strength, reason: reason || "用户在设置中保存", status: "active" });
        edit(null); await refresh();
      })}>{editing ? "保存修改" : "添加习惯"}</button>
      {editing && <button type="button" onClick={() => edit(null)}>取消编辑</button>}
    </div>
    {!editing && <div className="memory-card">
      <strong>分析写作素材</strong>
      <p className="form-hint">可选 TXT、Markdown、Word（DOCX）、PDF、EPUB、HTML，或粘贴片段。最多 4 个文件，每个最多 1 GB。全文按模型容量分批阅读，显示实际进度与范围；模型调用仍遵守本次预算。</p>
      {projectRoot ? <>
        <button type="button" disabled={submittingMaterial} onClick={() => void chooseMaterials()}>选择素材文件</button>
        {materialFiles.length > 0 && <><ul>{materialFiles.map(file => <li key={file}>{file.split(/[\\/]/).at(-1)}</li>)}</ul>
          <button type="button" disabled={submittingMaterial} onClick={() => setMaterialFiles([])}>清除已选文件</button></>}
        <button type="button" disabled={submittingMaterial || (!materialFiles.length && !quote.trim())}
          onClick={() => void submitMaterials()}>{submittingMaterial ? "正在提交…" : "交给后台分析"}</button>
      </> : <p className="form-hint">打开一本小说后即可提交素材分析。</p>}
    </div>}
    {materialNotice && <p role="status" className="form-hint">{materialNotice}</p>}
    {materialError && <p role="alert" className="form-error">{materialError}</p>}
    {materialTasks.map(task => {
      const analysis = task.analysis;
      const needsAnswer = Boolean(analysis && ["waiting_user", "interrupted"].includes(task.status));
      const failed = ["failed", "waiting_condition", "interrupted"].includes(task.status) && !needsAnswer;
      const running = ["running", "queued"].includes(task.status);
      const heading = needsAnswer ? analysis?.preference ? "分析成功 · 等待确认" : "素材尚未识别为可用习惯"
        : running ? "正在后台分析素材" : failed ? "素材分析未完成" : task.status === "cancelled" ? "分析已停止" : "素材分析已处理";
      return <article className="memory-card" key={task.run_id}>
        <strong>{heading}</strong>
        <p role={failed ? "alert" : undefined}>{needsAnswer ? analysis?.reason : task.error_message || task.summary || heading}</p>
        {analysis?.coverage?.length ? <details><summary>实际分析范围</summary><ul>
          {analysis.coverage.map((source, index) => <li key={index}>{source}</li>)}
        </ul></details> : null}
        {needsAnswer && analysis && <>
          {analysis.applicability && <p>适用性：{analysis.applicability}</p>}
          {analysis.strengths.length > 0 && <><strong>写法加分项</strong><ul>{analysis.strengths.map((item, index) => <li key={index}>
            <p>{item.explanation}</p><blockquote>{item.quote}</blockquote><small>{item.location}</small>
          </li>)}</ul></>}
          {analysis.preference && <p>建议习惯（{analysis.level === "author" ? "跨书默认" : "仅本书"} · {scopeText(analysis.scope)}）：{analysis.preference}</p>}
          <p><strong>{analysis.question}</strong></p>
          {!analysis.preference && <label>回答这道问题
            <textarea value={materialAnswers[task.run_id] || ""} maxLength={2000}
              onChange={event => setMaterialAnswers({ ...materialAnswers, [task.run_id]: event.target.value })} />
          </label>}
          <div className="settings-inline-actions">
            {analysis.preference
              ? <button type="button" disabled={busy} onClick={() => void decideMaterial(task, "adopt")}>采用这条习惯</button>
              : <button type="button" disabled={submittingMaterial || Boolean(editing) || !materialAnswers[task.run_id]?.trim()
                  || (!materialFiles.length && !quote.trim())} onClick={() => void submitMaterials(task)}>提交回答和素材</button>}
            <button type="button" disabled={busy} onClick={() => void decideMaterial(task, "dismiss")}>结束这次分析</button>
          </div>
          {!analysis.preference && !materialFiles.length && !quote.trim() &&
            <p className="form-hint">请在上方选择原文件或补充文件，也可以粘贴原文片段。</p>}
        </>}
      </article>;
    })}
    {error && <p role="alert" className="form-error">{error}</p>}
    {items.map(item => <article className="memory-card" key={item.preference_id}>
      <strong>{({ active: "正在使用", paused: "已暂停", candidate: "待确认反馈参考，非硬要求", deleted: "已移出使用" } as Record<string, string>)[item.status] || item.status} · {scopeText(item.scope)}</strong>
      <p>{item.text}</p><details><summary>查看来源原话</summary><blockquote>{item.source_quote || "旧记录未保存原话"}</blockquote>
        {item.source_ref && <small>{item.source_ref}</small>}</details>
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
  </section>;
}
