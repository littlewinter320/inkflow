import { useCallback, useEffect, useState } from "react";
import { ReadableText } from "./ReadableText";
import "./skills.css";

export type SkillInfo = {
  id: string; name: string; display_name: string; description: string; scope: string; directory: string;
  hash: string; enabled: boolean; automatic: boolean; changed_since_approval: boolean; read_only: boolean;
  metadata: Record<string, unknown>; resources: Record<string, { bytes: number; hash: string; text: boolean }>;
  warnings: string[]; texts?: Record<string, string>;
  shadowed_by?: string | null; text_updated?: boolean; control_scope: string; inherits_default: boolean;
  library_enabled: boolean; library_automatic: boolean; policy?: unknown; previous_policy?: unknown;
  adapter: { activation: "manual" | "automatic"; when?: string; not_when?: string; roles: string[]; contracts: string[];
    kind?: "method" | "workflow" | "hybrid";
    workflow?: { path: string };
    tasks?: string[]; inputs?: string[]; outputs?: string[]; steps?: Array<{ tool: string; script?: string }>;
    parameters?: Array<{ name: string; label: string; required: boolean; default?: string }> };
};
type Inventory = { skills: SkillInfo[]; errors: Array<{ id: string; error: string }>; directories: Record<string, string>; tools: Record<string, string>; update_policy: string };
type PromptInfo = { id: string; module: string; symbol: string; aliases: string[]; kind: string; contract: string | null; current_core: string; legacy_text: string; source: string; line?: number; note: string };
// 显示名不参与技能身份或权限计算。
const scopeName = (scope: string) => (({ builtin: "内置", personal: "个人", book: "本书" } as Record<string, string>)[scope] || scope);
const roleName = (role: string) => (({ coordinator: "Coordinator · 协调", writer: "Writer · 写作", editor: "Editor · 编辑", reviewer: "Reviewer · 连续性专项", memory_keeper: "Memory Keeper · 记忆专项" } as Record<string, string>)[role] || role);

// 只把包内已经存在的声明排成阅读视图，不执行声明或把静态节点标成实际完成。
function DeclaredSkillWorkflow({ skill }: { skill: SkillInfo }) {
  const path = skill.adapter.workflow?.path;
  const raw = path ? skill.texts?.[path] : undefined;
  if (!raw) return <p className="form-hint">方法随当前责任角色的原请求使用，不另建业务执行链；是否调用模型、保存或接受仍由原任务决定。</p>;
  let flow: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("invalid workflow");
    flow = parsed as Record<string, unknown>;
  } catch {
    return <p className="form-error" role="alert">流程声明暂不能读取，请在原始文件中核对；没有执行任何步骤。</p>;
  }
  const nodes = Array.isArray(flow.steps) ? flow.steps.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
  const toolNames: Record<string, string> = { "chapter.write": "写作交草稿", "chapter.review_mode": "审核当前版本并核记忆候选", "chapter.revise": "定点修订", "chapter.accept": "核条件并正式接受" };
  const conditions: Record<string, string> = { always: "进入该分支就执行", draft_missing: "当前尚无有效草稿才执行", review_needed: "当前版仍需审核才执行", revision_needed: "当前版有依据且获准修订才执行", ready_to_accept: "原任务允许接受且当前全部前提满足才执行" };
  const goals: Record<string, string> = { write_draft: "只写草稿", write_review: "写作并审核", write_review_accept: "写作、审核并接受", review: "只审核", review_accept: "审核并接受", revise_draft: "只修改草稿", revise_review: "修改并审核", revise_review_accept: "修改、审核并接受", plan: "制作规划", outline: "制作指定大纲", redesign_story: "修订故事规划", plan_next_arc: "推进下一篇章规划", plan_brief: "规划判断单" };
  const goalLabels = (value: unknown) => Array.isArray(value) ? value.map(item => goals[String(item)] || String(item)).join("、") : "";
  return <details className="skill-declared-workflow"><summary>可执行流程：必做步骤、条件与先后关系</summary>
    <p>这是当前文件的流程声明，实际任务还要核授权、版本、预算和已有成果；此处没有开始执行。</p>
    {typeof flow.description === "string" && <p>{flow.description}</p>}
    <p>适用目标：{goalLabels(flow.actions)}</p>
    {flow.executor === "registered_workflow" ? <p>交给已经登记的整体引擎入口处理，保留原生成、审核、发布与恢复事务；技能不能改写内部提交步骤。逐环节说明见下方完整流程。</p> : <ol>{nodes.map((node, index) => {
      const parents = Array.isArray(node.depends_on) ? node.depends_on.map(id => nodes.findIndex(item => item.id === id) + 1).filter(number => number > 0) : [];
      return <li key={String(node.id || index)}><strong>{toolNames[String(node.tool)] || String(node.tool || "未命名步骤")}</strong>
        <p>{node.required === false ? "可选步骤" : "必要步骤"}；{conditions[String(node.when || "always")] || "按声明条件核对"}。{parents.length ? `承接第${parents.join("、")}步；不适用分支省略后仍保留有效前置。` : ""}</p>
        {typeof node.instruction === "string" && <p>{node.instruction}</p>}
        {Array.isArray(node.actions) && <p>仅用于：{goalLabels(node.actions)}。</p>}
        {node.review_order === "before" && <p>只有本次要求先审后改才进入。</p>}
        {node.review_order === "after" && <p>只有本次要求先改后审才进入。</p>}
        {typeof node.round === "number" && node.round > 0 && <p>属于原任务第{node.round}轮修订范围，原次数不重置。</p>}
      </li>;
    })}</ol>}
  </details>;
}

// 设置中的技能全文查看、创建和外部导入；文件编辑交给作者熟悉的外部编辑器。
export function SkillsManager({ projectRoot }: { projectRoot: string }) {
  const [tab, setTab] = useState<"skills" | "prompts">("skills");
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<SkillInfo | null>(null);
  const [file, setFile] = useState("SKILL.md");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("all");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState({ name: "", description: "", scope: "personal" });
  const [imported, setImported] = useState<{ source: string; preview: SkillInfo } | null>(null);
  const [importScope, setImportScope] = useState("personal");
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [promptId, setPromptId] = useState("");
  const [controlScope, setControlScope] = useState(projectRoot ? "book" : "library");
  const params = projectRoot ? { project_root: projectRoot } : {};

  // RPC只操作本地目录和记录；切换页签不触发模型、安装或外部网络请求。
  const refresh = useCallback(async () => {
    const result = await window.inkflow.request<Inventory>("skills.list", projectRoot ? { project_root: projectRoot } : {});
    setInventory(result);
    window.dispatchEvent(new Event("inkflow:skills-changed"));
  }, [projectRoot]);
  useEffect(() => { let current = true; setInventory(null); setDetail(null); setSelectedId(""); setControlScope(projectRoot ? "book" : "library");
    void window.inkflow.request<Inventory>("skills.list", projectRoot ? { project_root: projectRoot } : {})
      .then(result => { if (current) setInventory(result); }).catch(cause => { if (current) setError(String(cause)); });
    return () => { current = false; };
  }, [projectRoot]);
  useEffect(() => {
    if (!selectedId) { setDetail(null); return; }
    let current = true; setDetail(null);
    void window.inkflow.request<SkillInfo>("skills.get", { ...(projectRoot ? { project_root: projectRoot } : {}), id: selectedId })
      .then(result => { if (current) { setDetail(result); setFile("SKILL.md"); } })
      .catch(cause => { if (current) setError(String(cause)); });
    return () => { current = false; };
  }, [selectedId, projectRoot, inventory]);
  useEffect(() => {
    let current = true;
    if (tab === "prompts") void window.inkflow.request<{ prompts: PromptInfo[] }>("skills.prompts", projectRoot ? { project_root: projectRoot } : {})
      .then(result => { if (current) { setPrompts(result.prompts); setPromptId(id => id || result.prompts[0]?.id || ""); } })
      .catch(cause => { if (current) setError(String(cause)); });
    return () => { current = false; };
  }, [tab, projectRoot]);

  // 每个修改携带已查看的内容哈希；外部文件变化时重新读取，不悄悄批准另一版。
  const act = async (work: () => Promise<void>) => {
    setWorking(true); setError(""); setNotice("");
    try { await work(); } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setWorking(false); }
  };
  const configure = (enabled: boolean, automatic: boolean) => void act(async () => {
    if (!detail) return;
    await window.inkflow.request("skills.configure", { ...params, id: detail.id, expected_hash: detail.hash, enabled, automatic, control_scope: controlScope });
    await refresh(); setNotice(enabled ? "当前版本已启用，只影响新任务。" : "技能已关闭，新任务不再调用。原任务保留原技能版本。");
  });
  const openDirectory = (id?: string, libraryScope = "personal") => void act(async () => {
    const result = await window.inkflow.request<{ directory: string }>("skills.directory", { ...params, ...(id ? { id } : { scope: libraryScope }) });
    const failure = await window.inkflow.openPath(result.directory);
    if (failure) throw new Error(failure);
  });
  const create = () => void act(async () => {
    const saved = await window.inkflow.request<SkillInfo>("skills.create", { ...params, ...draft });
    setCreating(false); await refresh(); setSelectedId(saved.id);
    setNotice("简单模板已创建，默认未启用。打开目录填写内容，刷新后查看并启用当前版本。");
  });
  const previewImport = (folder: boolean) => void act(async () => {
    const source = folder ? await window.inkflow.chooseFolder("选择含SKILL.md的技能目录") : await window.inkflow.chooseFile("选择单技能ZIP或SKILL.md");
    if (!source) return;
    const preview = await window.inkflow.request<SkillInfo>("skills.preview", { ...params, source });
    setImported({ source, preview });
  });

  const visibleSkills = (inventory?.skills || []).filter(item => (scope === "all" || item.scope === scope)
    && `${item.name} ${item.display_name} ${item.description}`.toLowerCase().includes(query.toLowerCase()));
  const visiblePrompts = prompts.filter(item => `${item.id} ${item.kind} ${item.current_core}`.toLowerCase().includes(query.toLowerCase()));
  const currentPrompt = prompts.find(item => item.id === promptId);
  const controlledEnabled = detail ? controlScope === "library" ? detail.library_enabled : detail.enabled : false;
  const controlledAutomatic = detail ? controlScope === "library" ? detail.library_automatic : detail.automatic : false;

  return <section className="skills-manager">
    <header className="skills-heading"><h3>技能与提示词</h3></header>
    <div className="skills-tabs" role="tablist" aria-label="技能与提示词内容">
      {([["skills", "技能库"], ["prompts", "全部提示词"]] as const).map(([id, label]) => <button type="button" role="tab" key={id} aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => { setTab(id); setQuery(""); }}>{label}</button>)}
    </div>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="form-hint" role="status">{notice}</p>}
    {tab === "skills" && <>
      <details className="skills-library-options"><summary>库管理与更新方式</summary><label>外部文件修改后<select value={inventory?.update_policy || "text_auto"} disabled={working || !inventory} onChange={event => {
        const value = event.target.value;
        void act(async () => { await window.inkflow.request("skills.update_policy", { ...params, update_policy: value }); await refresh(); setNotice("更新方式已保存，仅影响后续新任务。"); });
      }}><option value="text_auto">写法与参考文字自动更新；流程、角色、触发与工具变化先确认</option><option value="confirm_all">全部修改先查看并重新启用</option></select></label>
      <div className="skills-actions"><button type="button" disabled={working} onClick={() => void act(refresh)}>刷新外部改动</button><button type="button" onClick={() => openDirectory()}>打开个人库</button>{projectRoot && <button type="button" onClick={() => openDirectory(undefined, "book")}>打开本书库</button>}</div></details>
      <div className="skills-actions skills-primary-actions"><button type="button" className="primary" disabled={working} onClick={() => setCreating(value => !value)}>新建技能</button><button type="button" disabled={working} onClick={() => previewImport(true)}>导入目录</button><button type="button" disabled={working} onClick={() => previewImport(false)}>导入 ZIP / 文件</button></div>
      {creating && <div className="skill-create-panel"><h4>创建简单模板</h4><label>技能名<input value={draft.name} onChange={event => setDraft(current => ({ ...current, name: event.target.value }))} placeholder="例如 character-dialogue" maxLength={64} /></label><label>能力与触发说明<textarea value={draft.description} maxLength={1024} onChange={event => setDraft(current => ({ ...current, description: event.target.value }))} placeholder="说明解决什么问题、什么时候使用。" /></label><label>存放范围<select value={draft.scope} onChange={event => setDraft(current => ({ ...current, scope: event.target.value }))}><option value="personal">个人通用</option>{projectRoot && <option value="book">本书专用</option>}</select></label><div className="skills-actions"><button type="button" onClick={() => setCreating(false)}>取消</button><button type="button" className="primary" disabled={working || !draft.name || !draft.description.trim()} onClick={create}>创建未启用模板</button></div><p className="form-hint">复杂技能可在对话里说“创建一个……技能”；Coordinator会生成带步骤和必要参考的候选。</p></div>}
      {imported && <div className="skill-import-panel"><h4>导入预览：{imported.preview.name}</h4><p>{imported.preview.description}</p><p>{imported.source}</p>{imported.preview.warnings.map((warning, index) => <p className="form-hint" key={index}>{warning}</p>)}<details><summary>查看全部 {Object.keys(imported.preview.resources).length} 份文件</summary>{Object.keys(imported.preview.resources).map(path => <details key={path}><summary>{path}</summary><pre>{imported.preview.texts?.[path] ?? "二进制素材，仅保存，不当作指令执行。"}</pre></details>)}</details><label>存放范围<select value={importScope} onChange={event => setImportScope(event.target.value)}><option value="personal">个人通用</option>{projectRoot && <option value="book">本书专用</option>}</select></label><div className="skills-actions"><button type="button" onClick={() => setImported(null)}>取消</button><button type="button" className="primary" disabled={working} onClick={() => void act(async () => {
        const saved = await window.inkflow.request<SkillInfo>("skills.import", { ...params, source: imported.source, expected_hash: imported.preview.hash, scope: importScope });
        setImported(null); await refresh(); setSelectedId(saved.id); setNotice("已保存为未启用候选。请检查文件和触发条件后启用当前版本。");
      })}>保存为未启用候选</button></div></div>}
      <div className="skills-filter"><input aria-label="搜索技能" placeholder="搜索名字、用途或中文说明" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="筛选技能库" value={scope} onChange={event => setScope(event.target.value)}><option value="all">全部范围</option><option value="builtin">内置</option><option value="personal">个人</option>{projectRoot && <option value="book">本书</option>}</select></div>
      {inventory?.errors.map(item => <p key={item.id} className="form-error">{item.id}：{item.error}</p>)}
      <div className="skills-browser"><nav aria-label="技能列表"><p className="skills-list-count">{visibleSkills.length} 个技能</p>{visibleSkills.map(item => <button type="button" key={item.id} className={selectedId === item.id ? "active" : ""} aria-current={selectedId === item.id ? "true" : undefined} onClick={() => setSelectedId(item.id)}><strong>{item.display_name}</strong><small>{scopeName(item.scope)} · {item.enabled ? item.automatic ? "已启用 · 可被动" : "已启用 · 主动" : "未启用"}</small><span>{item.description}</span></button>)}{!visibleSkills.length && <p>{inventory ? "没有匹配的技能。" : "正在读取本地技能…"}</p>}</nav>
        <article className="skills-detail">{detail ? <>
          <h4>{detail.display_name}</h4><p><code>@{detail.id}</code> · {({ method: "方法技能", workflow: "流程技能", hybrid: "方法与流程结合" })[detail.adapter.kind || "method"]}</p><p>{detail.description}</p>{detail.changed_since_approval && <p className="form-hint">当前文件需要重新确认。请查看变化后启用新版本。</p>}{detail.text_updated && <p className="form-hint">写法或参考文字已按设置更新，将用于新任务。</p>}{detail.shadowed_by && <p className="form-hint">同名默认使用 @{detail.shadowed_by}；明确输入 @{detail.id} 可以指定当前已启用版本。</p>}
          <section className="skills-control-panel" aria-label="技能启用与触发">
          <label>这次修改的开关<select value={controlScope} onChange={event => setControlScope(event.target.value)}>{projectRoot && <option value="book">只改本书</option>}<option value="library">个人默认（各书可覆盖）</option></select></label>
          <p className="form-hint">本书当前{detail.enabled ? "启用" : "关闭"}；{detail.inherits_default ? "继承个人默认" : detail.control_scope === "book" ? "已有独立开关" : "按个人默认"}。文件所在库与开关作用范围分别管理。</p>
          <div className="skills-actions"><button type="button" disabled={working} onClick={() => configure(!controlledEnabled, controlledAutomatic && detail.adapter.activation === "automatic")}>{controlledEnabled ? "关闭技能" : "启用当前版本"}</button><label className="skills-checkbox"><input type="checkbox" disabled={working || !controlledEnabled || detail.adapter.activation !== "automatic"} checked={controlledAutomatic && controlledEnabled} onChange={event => configure(true, event.target.checked)} />允许被动触发</label>{projectRoot && detail.control_scope === "book" && <button type="button" disabled={working} onClick={() => void act(async () => { await window.inkflow.request("skills.inherit", { ...params, id: detail.id }); await refresh(); setNotice("本书已恢复继承个人默认。"); })}>本书恢复继承默认</button>}<button type="button" disabled={working} onClick={() => openDirectory(detail.id)}>打开目录</button>{detail.scope !== "personal" && <button type="button" disabled={working} onClick={() => void act(async () => { const saved = await window.inkflow.request<SkillInfo>("skills.clone", { ...params, id: detail.id, scope: "personal", expected_hash: detail.hash }); await refresh(); setSelectedId(saved.id); setNotice("已复制为个人库未启用副本。可以打开目录修改。"); })}>复制到个人库</button>}</div>
          </section>
          <dl className="skill-boundaries"><dt>触发</dt><dd>{detail.adapter.when || "作者主动点名"}</dd><dt>不使用</dt><dd>{detail.adapter.not_when || "超出当前授权或角色职责"}</dd><dt>责任角色</dt><dd>{detail.adapter.roles.map(roleName).join("；")}</dd><dt>步骤范围</dt><dd>{detail.adapter.contracts.join("、")}</dd><dt>输入</dt><dd>{detail.adapter.inputs?.join("；") || "见入口正文"}</dd><dt>输出</dt><dd>{detail.adapter.outputs?.join("；") || "沿当前任务Schema"}</dd></dl>
          {!!detail.adapter.tasks?.length && <ol>{detail.adapter.tasks.map(task => <li key={task}>{task}</li>)}</ol>}
          <DeclaredSkillWorkflow skill={detail} />
          {detail.texts?.["references/process.md"] && <details className="skill-full-process"><summary>完整流程：输入、步骤、输出、验收、异常与学习</summary>
            <p className="form-hint">这里供完整阅读，不会因展开而请求模型，也不把整份说明重复装入日常请求。模型仍按当前角色和节点加载必要规则。</p>
            <ReadableText text={detail.texts["references/process.md"]} />
          </details>}
          {detail.previous_policy !== null && detail.previous_policy !== undefined && <details><summary>查看上次批准与当前流程、角色和工具配置</summary><h5>上次批准</h5><pre>{JSON.stringify(detail.previous_policy, null, 2)}</pre><h5>当前版本</h5><pre>{JSON.stringify(detail.policy, null, 2)}</pre></details>}
          {detail.read_only && <p className="form-hint">内置资源随软件交付。复制到个人或本书库后外部编辑，避免更新覆盖。</p>}
          {detail.warnings.map((warning, index) => <p className="form-hint" key={index}>{warning}</p>)}
          <div className="skill-files"><label>查看全部文件<select value={file} onChange={event => setFile(event.target.value)}>{Object.keys(detail.resources).map(path => <option key={path} value={path}>{path}</option>)}</select></label><pre tabIndex={0}>{detail.texts?.[file] ?? "二进制素材；可以在文件夹中查看。"}</pre></div>
          <details><summary>内容版本与工具登记</summary><p>当前版本：<code>{detail.hash}</code></p><p>{detail.directory}</p>{detail.adapter.steps?.map((step, index) => <p key={index}><code>{step.tool}</code> {inventory?.tools[step.tool]} {step.script && `（${step.script}）`}</p>)}</details>
        </> : <p>选择一个技能，查看它的职责、触发边界、任务步骤，以及全部参考、工具声明和素材文件。</p>}</article>
      </div>
    </>}
    {tab === "prompts" && <><div className="skills-filter"><input aria-label="搜索提示词" placeholder="搜索用途、符号或原文" value={query} onChange={event => setQuery(event.target.value)} /></div><div className="skills-browser"><nav aria-label="提示词列表"><p className="skills-list-count">{visiblePrompts.length} 条提示词</p>{visiblePrompts.map(item => <button type="button" key={item.id} className={item.id === promptId ? "active" : ""} aria-current={item.id === promptId ? "true" : undefined} onClick={() => setPromptId(item.id)}><strong>{item.module} · {item.symbol}</strong><small>{item.kind}{item.line ? ` · ${item.line}行` : ""}</small></button>)}{!visiblePrompts.length && <p className="form-hint">没有匹配的提示词。</p>}</nav><article className="skills-detail">{currentPrompt ? <><h4>{currentPrompt.kind}</h4><p>{currentPrompt.note}</p><p>{currentPrompt.source}{currentPrompt.line ? `:${currentPrompt.line}` : ""}</p>{currentPrompt.contract && <p>新任务步骤：<code>{currentPrompt.contract}</code></p>}<pre tabIndex={0}>{currentPrompt.current_core}</pre>{!!currentPrompt.legacy_text && <details><summary>旧任务恢复所需原文</summary><pre tabIndex={0}>{currentPrompt.legacy_text}</pre></details>}{!!currentPrompt.aliases.length && <details><summary>同一内容的引用名</summary><p>{currentPrompt.aliases.join("、")}</p></details>}</> : <p className="form-hint">选择一条提示词查看原文和来源。</p>}</article></div></>}

  </section>;
}
