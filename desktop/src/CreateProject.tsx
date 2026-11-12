import { useId, useLayoutEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import "./CreateProject.css";

/** 开书构思沿用现有接口字段；方案说明只供选择，不作为章节或正史提交。 */
type NovelIdea = {
  concept_id: string; title: string; genre: string; premise: string; protagonist: string;
  target_audience: string; core_selling_point: string; target_chapter_words: number;
  estimated_chapters: number; estimated_volumes: number; user_rules: string[];
  opening_hook: string; long_term_engine: string; choice_note: string;
};
type BookBrief = Omit<NovelIdea, "concept_id" | "opening_hook" | "long_term_engine" | "choice_note">;
type CreateProjectProps = {
  onClose: () => void;
  onCreated: (root: string) => Promise<void>;
  formatError: (cause: unknown) => string;
};

/** 分步收集位置、可编辑开书信息和最终确认；创建仅调用本地建项接口。 */
export function CreateProject({ onClose, onCreated, formatError }: CreateProjectProps) {
  const [stage, setStage] = useState(0);
  const [root, setRoot] = useState("");
  const [mode, setMode] = useState<"ai" | "manual">("ai");
  const [preferences, setPreferences] = useState("");
  const [ideas, setIdeas] = useState<NovelIdea[]>([]);
  const [reasoning, setReasoning] = useState<string[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [form, setForm] = useState<BookBrief>({ title: "", genre: "都市悬疑", premise: "", protagonist: "",
    target_audience: "中文网文读者", core_selling_point: "", target_chapter_words: 3000,
    estimated_chapters: 200, estimated_volumes: 6, user_rules: [] });
  const [pending, setPending] = useState<"" | "folder" | "ideate" | "create" | "open">("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [createdRoot, setCreatedRoot] = useState("");
  const [uncertainRoot, setUncertainRoot] = useState("");
  const panel = useRef<HTMLElement>(null);
  const details = useRef<HTMLElement>(null);
  const busy = useRef(false);
  const mounted = useRef(false);
  const sequence = useRef(0);
  const close = useRef(onClose);
  close.current = onClose;
  const titleId = useId(), descriptionId = useId();

  useLayoutEffect(() => {
    mounted.current = true;
    const previous = document.activeElement as HTMLElement | null;
    const ownedPanel = panel.current;
    // 旧布局菜单在浏览器顶层；关闭后由此弹窗独占键盘焦点。
    document.querySelectorAll<HTMLElement>("[popover]:popover-open").forEach(element => element.hidePopover());
    panel.current?.focus({ preventScroll: true });
    const keyboard = (event: KeyboardEvent) => {
      if (ownedPanel?.closest('[data-motion-exiting="true"]')) return;
      if (event.key === "Escape") {
        event.preventDefault(); event.stopPropagation();
        if (!busy.current) close.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const controls = Array.from(panel.current.querySelectorAll<HTMLElement>(
        'button, input, textarea, select, a[href], [tabindex]:not([tabindex="-1"])'))
        .filter(element => !element.matches(":disabled") && element.getClientRects().length > 0);
      const first = controls[0], last = controls.at(-1);
      if (!first) { event.preventDefault(); panel.current.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current || !panel.current.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      mounted.current = false; sequence.current += 1;
      document.removeEventListener("keydown", keyboard);
      if (previous?.isConnected && (ownedPanel?.contains(document.activeElement) || document.activeElement === document.body)) previous.focus({ preventScroll: true });
    };
  }, []);

  useLayoutEffect(() => { panel.current?.focus({ preventScroll: true }); }, [stage]);

  /** 在React重新渲染前锁定按钮；序号和挂载状态共同隔离迟到返回。 */
  const begin = (operation: typeof pending) => {
    if (busy.current) return null;
    busy.current = true; setPending(operation); setError("");
    return ++sequence.current;
  };
  /** 回包只更新仍挂载且仍拥有本次请求的窗口。 */
  const current = (request: number) => mounted.current && request === sequence.current;
  /** 原请求结束后才释放本窗口的操作锁。 */
  const finish = (request: number) => {
    if (current(request)) { busy.current = false; setPending(""); }
  };

  /** 只选择保存位置，不创建目录或调用模型；取消保留原来的位置。 */
  const chooseFolder = async () => {
    const request = begin("folder");
    if (request === null) return;
    try {
      const value = await window.inkflow.chooseFolder("选择小说项目文件夹");
      if (current(request) && value) { setRoot(value); setStatus(""); }
    } catch (cause) { if (current(request)) setError(formatError(cause)); }
    finally { finish(request); }
  };

  /** 采用方案只更新建项草稿；换方案是作者主动替换当前编辑信息。 */
  const chooseIdea = (idea: NovelIdea, index: number) => {
    if (busy.current) return;
    setSelected(index);
    setForm({ title: idea.title, genre: idea.genre, premise: idea.premise, protagonist: idea.protagonist,
      target_audience: idea.target_audience, core_selling_point: idea.core_selling_point,
      target_chapter_words: idea.target_chapter_words, estimated_chapters: idea.estimated_chapters,
      estimated_volumes: idea.estimated_volumes, user_rules: [...idea.user_rules] });
    setStatus(`已采用《${idea.title}》，可以在下方修改建项信息。`);
    window.requestAnimationFrame(() => {
      details.current?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    });
  };

  /** 每次点击仅发起一次现有构思请求；失败保留之前方案，不自动重试或补发。 */
  const ideate = async (fast: boolean) => {
    const request = begin("ideate");
    if (request === null) return;
    setStatus(fast ? "正在构思一个故事方向，请保留此窗口等待本次结果。" : "正在构思三个不同方向，请保留此窗口等待本次结果。");
    try {
      const result = await window.inkflow.request<{ candidates: NovelIdea[]; public_reasoning_summary: string[]; notice?: string }>(
        "project.ideate", { preferences, fast });
      if (!current(request)) return;
      if (!Array.isArray(result.candidates) || !result.candidates.length) throw new Error("这次没有返回可用的故事方案，已有信息已保留。");
      setIdeas(result.candidates); setReasoning(result.public_reasoning_summary || []); setSelected(null);
      setStatus([result.notice, `已返回 ${result.candidates.length} 个方向。完整阅读后选择一个，再编辑细节。`].filter(Boolean).join(" "));
    } catch (cause) {
      if (current(request)) { setError(formatError(cause)); setStatus("本次构思未完成，已有方案和编辑信息已保留；没有自动重发请求。"); }
    } finally { finish(request); }
  };

  /** 创建成功与打开失败分开记录，避免一次打开失败导致重复建项。 */
  const openCreated = async () => {
    const request = begin("open");
    if (request === null) return;
    try {
      await onCreated(createdRoot);
      if (current(request)) close.current();
    } catch (cause) { if (current(request)) setError(`小说已经创建。打开时遇到问题：${formatError(cause)}`); }
    finally { finish(request); }
  };

  /** 只在确认页提交BookBrief；异常结果不猜测成功，也不自动重新发送创建。 */
  const create = async () => {
    if (stage !== 2 || createdRoot || uncertainRoot === root) return;
    const request = begin("create");
    if (request === null) return;
    const destination = root;
    let saved = false;
    setStatus("正在保存小说项目，请等待本次创建结果。不会自动生成章节。");
    try {
      await window.inkflow.request("project.create", { project_root: destination, brief: {
        ...form, title: form.title.trim(), genre: form.genre.trim(), premise: form.premise.trim(), protagonist: form.protagonist.trim(),
        user_rules: form.user_rules.map(rule => rule.trim()).filter(Boolean) } });
      saved = true;
      if (!current(request)) return;
      setCreatedRoot(destination); setStatus("小说已创建，正在打开项目…");
      await onCreated(destination);
      if (current(request)) close.current();
    } catch (cause) {
      if (current(request)) {
        if (!saved) setUncertainRoot(destination);
        setError(saved ? `小说已经创建，打开时遇到问题：${formatError(cause)}` : formatError(cause));
        setStatus(saved ? "已保存的项目保留在所选位置，可以重新打开。" : "本次创建没有得到成功确认。请先检查所选文件夹或从主页打开项目，避免重复创建；此窗口不会自动重发。");
      }
    } finally { finish(request); }
  };

  /** 阶段前进先校验当前屏幕；所有数字边界与BookBrief保持一致。 */
  const next = (event: FormEvent) => {
    event.preventDefault();
    if (busy.current || createdRoot) return;
    if (stage === 0) { if (root) { setStage(1); setError(""); } return; }
    if (stage === 1) {
      if (mode === "ai" && selected === null) { setError("请先采用一个故事方案，也可以切换为自己填写。"); return; }
      if (!form.title.trim() || !form.genre.trim() || !form.protagonist.trim() || form.premise.trim().length < 10) {
        setError("请补全书名、题材、主角，以及至少10个字的故事前提。"); return;
      }
      if (!(event.currentTarget as HTMLFormElement).reportValidity()) return;
      setStage(2); setError("");
    } else void create();
  };

  const locked = Boolean(pending), ready = mode === "manual" || selected !== null;
  const steps = ["选择保存位置", "确定故事方向", "确认创建"];
  return <div className="modal-backdrop create-project-backdrop" onMouseDown={event => {
    if (event.target === event.currentTarget && !busy.current) close.current();
  }}>
    <section ref={panel} className="modal create-project-modal" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}>
      <header className="create-project-header">
        <div><p className="eyebrow">从一个念头，开始一本书</p><h2 id={titleId}>新建小说</h2><p id={descriptionId}>先选保存位置，再慢慢确定故事。最后确认后，才会创建本地项目。</p></div>
        <button type="button" className="create-project-close" aria-label="关闭新建小说" disabled={locked} title={locked ? "本次操作进行中，请等待结果后关闭" : "关闭"} onClick={() => { if (!busy.current) close.current(); }}>×</button>
        <ol className="create-project-steps" aria-label="新建小说步骤">{steps.map((label, index) => <li key={label} className={index === stage ? "current" : index < stage ? "complete" : ""} aria-current={index === stage ? "step" : undefined}><span>{index < stage ? "✓" : index + 1}</span>{label}</li>)}</ol>
      </header>
      <form className="create-project-flow" onSubmit={next}>
        <div className="create-project-scroll" aria-busy={locked}>
          {stage === 0 && <section className="create-project-location">
            <span className="create-project-kicker">第一步 · 留一个位置给故事</span><h3>这本小说，保存在哪里？</h3><p>设定、规划、章节和对话记录会保存在这个项目中。现在只选择位置，不会生成内容。</p>
            <label>小说项目文件夹<div className="create-project-path"><input value={root} readOnly placeholder="还没有选择文件夹" aria-label="小说项目文件夹" /><button type="button" disabled={locked} onClick={() => void chooseFolder()}>{pending === "folder" ? "正在选择…" : root ? "更换位置" : "选择文件夹"}</button></div></label>
            <p className="create-project-note">建议为这本小说选择一个单独的文件夹，便于之后查找与备份。</p>
          </section>}
          {stage === 1 && <>
            <section className="create-project-direction"><span className="create-project-kicker">第二步 · 找到想写的故事</span><h3>已经有想法，还是一起想一个？</h3>
              <div className="create-project-modes" role="group" aria-label="故事方向来源"><button type="button" aria-pressed={mode === "ai"} disabled={locked} onClick={() => setMode("ai")}><strong>让 AI 帮我构思</strong><span>留空也可以，从一个或三个方向中选择。</span></button><button type="button" aria-pressed={mode === "manual"} disabled={locked} onClick={() => setMode("manual")}><strong>我自己填写</strong><span>直接写下故事，已有编辑内容会保留。</span></button></div>
            </section>
            {mode === "ai" && <section className="create-project-ideation">
              <label>有什么偏好吗？<small>可选；题材、氛围、想看的开场，都可以写。</small><textarea disabled={locked} value={preferences} onChange={event => setPreferences(event.target.value)} placeholder="例如：都市悬疑，一次普通的送货意外牵出一座城的秘密。没有想法也可以留空。" /></label>
              <div className="create-project-idea-actions"><button className="primary" type="button" disabled={locked} onClick={() => void ideate(true)}>{pending === "ideate" ? "构思进行中…" : "构思一个方向"}</button><button type="button" disabled={locked} onClick={() => void ideate(false)}>构思三个方向，慢慢比较</button></div>
              <p className="create-project-note">构思会使用当前模型配置；本次调用与预算继续由现有流程记录。等待期间不补发请求，已有方案会保留到新结果返回。</p>
              {ideas.length > 0 && <div className="create-project-ideas"><p className="create-project-note">方案完整展示。采用其他方案，会替换下方正在编辑的建项信息。</p>{ideas.map((idea, index) => <article className={`create-project-idea ${selected === index ? "selected" : ""}`} key={`${index}-${idea.concept_id}`}>
                <header><span>方向 {index + 1} · {idea.genre}</span><span>{selected === index ? "当前采用" : "供你比较"}</span></header><h4>{idea.title}</h4><p>{idea.premise}</p>
                <dl><div><dt>主角</dt><dd>{idea.protagonist}</dd></div><div><dt>面向读者</dt><dd>{idea.target_audience}</dd></div><div><dt>核心看点</dt><dd>{idea.core_selling_point || "可在建项信息中补充"}</dd></div><div><dt>开篇抓手</dt><dd>{idea.opening_hook}</dd></div><div><dt>长线动力</dt><dd>{idea.long_term_engine}</dd></div><div><dt>篇幅建议</dt><dd>每章 {idea.target_chapter_words} 字 · 约 {idea.estimated_chapters} 章 · {idea.estimated_volumes} 卷</dd></div></dl>
                {idea.user_rules.length > 0 && <div className="create-project-rules"><strong>本书要求</strong><ul>{idea.user_rules.map((rule, i) => <li key={i}>{rule}</li>)}</ul></div>}<p className="create-project-choice-note">{idea.choice_note}</p>
                <button type="button" className={selected === index ? "selected-action" : ""} disabled={locked || selected === index} onClick={() => chooseIdea(idea, index)}>{selected === index ? "✓ 已采用，在下方编辑" : "采用这个故事方向"}</button>
              </article>)}</div>}
              {reasoning.length > 0 && <details className="create-project-reasoning"><summary>看看这些方向的构思说明</summary><ul>{reasoning.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
            </section>}
            {ready && <section ref={details} className="create-project-edit"><h3>把它改成你想写的样子</h3><p>下面是实际保存的建项信息。现在仍是草稿，下一步还可以检查。</p>
              <fieldset disabled={locked}><legend className="create-project-sr">可编辑建项信息</legend>
                <div className="create-project-fields"><label>书名<input required maxLength={100} value={form.title} onChange={event => setForm({ ...form, title: event.target.value })} /></label><label>题材<input required maxLength={100} value={form.genre} onChange={event => setForm({ ...form, genre: event.target.value })} /></label></div>
                <label>故事前提<textarea required minLength={10} value={form.premise} onChange={event => setForm({ ...form, premise: event.target.value })} placeholder="谁，因为哪件事，必须做什么；最大的阻力是什么。至少10个字。" /></label>
                <div className="create-project-fields"><label>主角<input required maxLength={100} value={form.protagonist} onChange={event => setForm({ ...form, protagonist: event.target.value })} /></label><label>面向读者<input value={form.target_audience} onChange={event => setForm({ ...form, target_audience: event.target.value })} /></label></div>
                <label>核心看点<textarea value={form.core_selling_point} onChange={event => setForm({ ...form, core_selling_point: event.target.value })} placeholder="可选，写下这个故事最吸引你的地方。" /></label>
                <div className="create-project-fields three"><label>单章目标字数<input required type="number" min={500} max={20000} step={1} value={form.target_chapter_words} onChange={event => setForm({ ...form, target_chapter_words: Number(event.target.value) })} /></label><label>预计章节<input required type="number" min={10} max={5000} step={1} value={form.estimated_chapters} onChange={event => setForm({ ...form, estimated_chapters: Number(event.target.value) })} /></label><label>预计卷数<input required type="number" min={1} max={100} step={1} value={form.estimated_volumes} onChange={event => setForm({ ...form, estimated_volumes: Number(event.target.value) })} /></label></div>
                <label>本书要求<small>可选，一行一条。</small><textarea value={form.user_rules.join("\n")} onChange={event => setForm({ ...form, user_rules: event.target.value.split("\n") })} /></label>
              </fieldset>
            </section>}
          </>}
          {stage === 2 && <section className="create-project-confirm"><span className="create-project-kicker">最后一步 · 再看一眼</span><h3>准备开始《{form.title}》</h3><p>确认后只保存本地项目与开书信息。规划和章节都由你之后决定。</p><dl>
            <div><dt>保存位置</dt><dd>{root}</dd></div><div><dt>题材</dt><dd>{form.genre}</dd></div><div><dt>故事前提</dt><dd>{form.premise}</dd></div><div><dt>主角</dt><dd>{form.protagonist}</dd></div><div><dt>面向读者</dt><dd>{form.target_audience || "未填写"}</dd></div><div><dt>核心看点</dt><dd>{form.core_selling_point || "未填写"}</dd></div><div><dt>篇幅目标</dt><dd>每章 {form.target_chapter_words} 字 · 约 {form.estimated_chapters} 章 · {form.estimated_volumes} 卷</dd></div><div><dt>本书要求</dt><dd>{form.user_rules.filter(rule => rule.trim()).join("\n") || "未填写"}</dd></div>
          </dl><p className="create-project-note">创建项目不追加构思请求，也不会自动生成正文。</p></section>}
        </div>
        <footer className="create-project-footer">
          <div className="create-project-feedback" aria-live="polite" aria-atomic="true">{error && <p className="form-error" role="alert">{error}</p>}<p role="status">{locked ? pending === "folder" ? "请在文件夹窗口中选择位置，完成后返回。" : pending === "ideate" ? status : pending === "open" ? "正在打开已创建小说…" : status : status || (stage === 0 ? "选择位置后继续，不会立即创建文件。" : stage === 1 ? "先确定方向，再检查建项信息。" : "信息确认后，再创建小说。")}</p>{locked && <small>操作进行中，暂时不能关闭；请等待本次结果，无需重复点击。</small>}</div>
          <div className="create-project-footer-actions"><button type="button" disabled={locked} onClick={() => { if (!busy.current) close.current(); }}>{createdRoot ? "关闭" : "取消"}</button>{stage > 0 && !createdRoot && <button type="button" disabled={locked} onClick={() => { if (!busy.current) { setStage(stage - 1); setError(""); } }}>上一步</button>}{createdRoot ? <button className="primary" type="button" disabled={locked} onClick={() => void openCreated()}>打开已创建小说</button> : <button className="primary" type="submit" disabled={locked || stage === 0 && !root || stage === 1 && !ready || stage === 2 && uncertainRoot === root}>{stage === 0 ? "继续，确定故事方向" : stage === 1 ? "下一步，检查信息" : pending === "create" ? "正在创建…" : uncertainRoot === root ? "先核对已有创建结果" : "确认创建小说"}</button>}</div>
        </footer>
      </form>
    </section>
  </div>;
}
