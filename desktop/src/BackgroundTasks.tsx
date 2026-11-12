import { MotionPresence } from "./MotionPresence";
import { useCallback, useEffect, useRef, useState } from "react";
import { Mascot } from "./Mascot";
import type { KnowledgeProgress } from "./KnowledgeCenter";
import { readableSourceText } from "./readableNames";

type Request = <T,>(method: string, params?: Record<string, unknown>) => Promise<T>;
type BackgroundItem = { id?: string; job_id?: string; name?: string; source_name?: string; status: string; progress?: number; summary?: string; error?: string; updated_at?: string; completed_segments?: number; total_segments?: number };
type BackgroundSnapshot = { components: BackgroundItem[]; jobs: BackgroundItem[]; data_root: string };
type ChapterTask = { run_id: string; title?: string; method?: string; action?: string; status: string;
  params?: { chapter_no?: number; expected_version?: number; expected_hash?: string; _background?: boolean; work_id?: string; parent_run_id?: string };
  summary?: string; error_message?: string; next_step?: string; historical?: boolean;
  progress?: Array<{ type?: string; summary?: string }> };
type BackgroundQuestion = { id: string; task_id: string; questions: Array<{ id: string; question: string;
  why_it_matters?: string; selection?: "single" | "multiple"; options?: Array<{ id: string; label: string; description?: string; kind?: "choice" | "other" }> }> };
type BackgroundAnswerDraft = { root: string; runId: string; question: BackgroundQuestion;
  selected: Record<string, string[]>; free: Record<string, string> };

// 原题正文也参与草稿身份；替换题面后保留旧输入供查看，不将旧选择套到新题。
function backgroundQuestionKey(root: string, runId: string, question: BackgroundQuestion): string {
  return JSON.stringify([root, runId, question]);
}

// 只传递作者的原题答案，不附加“信息足够就执行”等扩展授权的固定尾句。
function backgroundAnswerText(draft: BackgroundAnswerDraft): string {
  return "我来回答刚才的问题：\n" + draft.question.questions.map((card, index) => {
    const selected = card.options?.filter(option => (draft.selected[card.id] || []).includes(option.id) && option.kind !== "other").map(option => option.label) || [];
    return `${index + 1}. ${card.question}\n我的回答：${[...selected, draft.free[card.id]?.trim()].filter(Boolean).join("；")}`;
  }).join("\n");
}

// 原任务状态决定完成，正文已接受但提交尾项未齐仍显示等待，不能用百分比代替回执。
function chapterTaskLabel(status: string): string {
  return ({ running: "正在核对", completed: "全部完成", waiting_user: "需要你确认", waiting_condition: "条件待补齐",
    failed: "本次未完成", interrupted: "断点已保留", cancelled: "已停止" } as Record<string, string>)[status] || "状态待核";
}

// Use the same human-readable states in the global task entry and the audio library.
export function voiceJobLabel(status: string): string {
  return ({ queued: "排队中", running: "正在转换", installing: "正在安装", installed: "已就绪", completed: "已完成", paused: "已暂停", interrupted: "已保存，等待继续", failed: "遇到问题", cancelled: "已取消", deleted: "音频已删除", not_installed: "未安装", idle: "尚未开始" } as Record<string, string>)[status] || "需要查看记录";
}

// Keep a visible entry for real background work; dismissing this dialog never stops its tasks.
export function BackgroundTasks({ request, projectRoot, revision, writing, writingRunId, onWriting, onTaskRecords, onContinueChapter, onVoice, onSettings, onMaterials, onKnowledge }: {
  request: Request; projectRoot: string; revision: number; writing: boolean;
  writingRunId?: string | null; onWriting: () => void; onTaskRecords: () => void; onVoice: () => void; onSettings: () => void;
  onContinueChapter: (chapterNo: number, version: number, hash: string) => void;
  onMaterials: () => void;
  onKnowledge: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<BackgroundSnapshot>({ components: [], jobs: [], data_root: "" });
  const [problem, setProblem] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [chapterTasks, setChapterTasks] = useState<ChapterTask[]>([]);
  const [knowledge, setKnowledge] = useState<KnowledgeProgress | null>(null);
  const [cancellingRun, setCancellingRun] = useState("");
  const [questionView, setQuestionView] = useState<{ root: string; runId: string; key?: string; problem?: string; loading?: boolean } | null>(null);
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, BackgroundAnswerDraft>>({});
  const [answeringRun, setAnsweringRun] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const inFlight = useRef(false);
  const questionRead = useRef(0);
  const currentRoot = useRef(projectRoot);
  currentRoot.current = projectRoot;
  const refresh = useCallback(async () => {
    if (inFlight.current || document.hidden) return;
    const root = projectRoot;
    inFlight.current = true;
    try {
      // 两类任务独立读取；一个来源暂时忙时，另一个来源的现有进度仍可展示。
      const [voice, chapters, memory] = await Promise.allSettled([
        request<BackgroundSnapshot>("voice.background", root ? { project_root: root } : {}),
        root ? request<{ tasks: ChapterTask[] }>("task.list", { project_root: root, limit: 50, background: true }) : Promise.resolve({ tasks: [] }),
        root ? request<KnowledgeProgress>("knowledge.status", { project_root: root }) : Promise.resolve(null),
      ]);
      if (currentRoot.current !== root) return;
      if (voice.status === "fulfilled") setSnapshot(previous => JSON.stringify(previous) === JSON.stringify(voice.value) ? previous : voice.value);
      if (memory.status === "fulfilled") setKnowledge(memory.value);
      if (chapters.status === "fulfilled") {
        // 附件补全是独立后台工作，完成或失败也保留回执，不混成正文重新验收。
        const tasks = chapters.value.tasks.filter(task => task.method === "memory.attachments"
          || task.method === "preferences.materials.analyze"
          || task.method === "knowledge.maintain"
          || task.method === "workflow.run" && task.action === "review_accept"
          || task.status === "running" && ["conversation.send", "workflow.run"].includes(task.method || ""))
          // 原知识断点已续接时只提醒当前工作，旧失败回执保留在历史，不反复催作者处理。
          .map(task => task.method === "knowledge.maintain" && task.status !== "running"
            && memory.status === "fulfilled" && memory.value?.control.run_id && task.run_id !== memory.value.control.run_id
            ? { ...task, historical: true } : task);
        setChapterTasks(previous => JSON.stringify(previous) === JSON.stringify(tasks) ? previous : tasks);
      }
      setLoaded(true);
      setProblem([voice, chapters, memory].flatMap(value => value.status === "rejected"
        ? [value.reason instanceof Error ? value.reason.message : String(value.reason)] : []).join("；"));
    } catch (cause) { if (currentRoot.current === root) setProblem(cause instanceof Error ? cause.message : String(cause)); }
    finally { inFlight.current = false; }
  }, [projectRoot, request]);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), open ? 3000 : 6000);
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", visible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", visible); };
  }, [refresh, open]);
  useEffect(() => { setSnapshot({ components: [], jobs: [], data_root: "" }); setChapterTasks([]); setKnowledge(null); setLoaded(false); }, [projectRoot]);
  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 600);
    return () => window.clearTimeout(timer);
  }, [revision, refresh]);
  useEffect(() => { if (open && !dialog.current?.open) dialog.current?.showModal(); }, [open]);
  const active = [...snapshot.components, ...snapshot.jobs].filter(item => ["installing", "running", "queued"].includes(item.status));
  const waiting = [...snapshot.components, ...snapshot.jobs].filter(item => ["failed", "paused", "interrupted"].includes(item.status));
  const history = [...snapshot.components.filter(item => item.status === "installed"), ...snapshot.jobs.filter(item => ["completed", "cancelled", "deleted"].includes(item.status))];
  const independent = chapterTasks.filter(task => task.run_id !== writingRunId);
  const chapterActive = independent.filter(task => task.status === "running");
  const chapterWaiting = independent.filter(task => !task.historical && ["failed", "interrupted", "waiting_user", "waiting_condition"].includes(task.status));
  const chapterHistory = independent.filter(task => task.historical || ["completed", "cancelled"].includes(task.status));
  const activeCount = active.length + chapterActive.length + Number(writing);
  const waitingCount = waiting.length + chapterWaiting.length;
  // Navigation is observational; starting, cancelling and deleting remain explicit actions elsewhere.
  const go = (action: () => void) => { setOpen(false); action(); };
  // 同一任务行兼容转换进度与删除回执，避免把历史记录当成尚未生成的音频。
  const row = (item: BackgroundItem) => <article className={`background-task-row status-${item.status}`} key={item.job_id || item.id}>
    <div><strong>{item.source_name || item.name}</strong><span>{voiceJobLabel(item.status)}</span></div>
    {/* 删除回执只保留原段数；后台入口也不能把它画成尚未生成的 0/N。 */}
    {item.total_segments != null && <small>{item.status === "deleted" ? `原 ${item.total_segments} 段 · 删除记录` : `已保存 ${item.completed_segments || 0} / ${item.total_segments} 段`}</small>}
    {["installing", "running", "queued"].includes(item.status) && <progress max="100" value={item.progress || 0} aria-label={`${item.name || item.source_name}进度`} />}
    {(item.error || item.summary) && <p>{item.error || item.summary}</p>}
    <button type="button" onClick={() => go(item.job_id ? onVoice : onSettings)}>{item.job_id ? item.status === "deleted" ? "查看删除记录" : "查看音频与操作" : "查看组件与下一步"}</button>
  </article>;
  // 取消只指向选中的真实run；停止不会撤销此前已经写入的正文或正史。
  const cancelChapter = async (task: ChapterTask) => {
    if (cancellingRun) return;
    setCancellingRun(task.run_id);
    try { await request("run.cancel", { project_root: projectRoot, run_id: task.run_id }); await refresh(); }
    catch (cause) { setProblem(cause instanceof Error ? cause.message : String(cause)); }
    finally { setCancellingRun(""); }
  };
  // 后台问题仍属于原任务；读取和作答不借用主对话的pending state或输入框。
  const openChapterQuestion = async (task: ChapterTask) => {
    const root = projectRoot;
    const readId = ++questionRead.current;
    setQuestionView(previous => ({ root, runId: task.run_id, key: previous?.runId === task.run_id && previous.root === root ? previous.key : undefined, loading: true }));
    try {
      const result = await request<{ pending_question?: BackgroundQuestion | null; question_problem?: string }>("task.status", { project_root: root, task_id: task.run_id });
      if (currentRoot.current !== root || questionRead.current !== readId) return;
      if (!result.pending_question) { setQuestionView(previous => ({ ...previous!, root, runId: task.run_id, loading: false, problem: result.question_problem || "这份问题已处理或来源待核，请刷新任务记录。之前填写的答案仍保留。" })); return; }
      const question = result.pending_question;
      const key = backgroundQuestionKey(root, task.run_id, question);
      setAnswerDrafts(previous => previous[key] ? previous : { ...previous, [key]: { root, runId: task.run_id, question, selected: {}, free: {} } });
      setQuestionView({ root, runId: task.run_id, key });
    } catch (cause) { if (currentRoot.current === root && questionRead.current === readId) setQuestionView(previous => ({ ...previous!, root, runId: task.run_id, loading: false, problem: cause instanceof Error ? cause.message : String(cause) })); }
  };
  // 答案发送携带原题和已保存版本；失败不清空，不自动重发或转到另一个问题。
  const answerChapterQuestion = async (task: ChapterTask, key: string) => {
    const draft = answerDrafts[key], source = task.params;
    if (answeringRun || !draft || !source?.chapter_no || !source.expected_version || !source.expected_hash
      || draft.root !== projectRoot || draft.runId !== task.run_id) return;
    const root = projectRoot;
    setAnsweringRun(task.run_id);
    try {
      const result = await request<{ reused?: boolean }>("workflow.run", { project_root: root, action: "review_accept", _background: true,
        run_id: `desktop-accept-${crypto.randomUUID()}`, chapter_no: source.chapter_no,
        expected_version: source.expected_version, expected_hash: source.expected_hash, source_run_id: task.run_id,
        expected_question_id: draft.question.id, expected_question_task_id: draft.question.task_id,
        question_answer: backgroundAnswerText(draft), authorization_source: "per_chapter_click", include_suggestions: false });
      if (currentRoot.current !== root) return;
      await refresh();
      if (result.reused) { setQuestionView({ root, runId: task.run_id, key,
        problem: "这章已有原任务在核对，答案尚未发送。填写内容已保留；等这项工作结束后重新打开原题再提交。" }); return; }
      await openChapterQuestion(task);
    } catch (cause) { if (currentRoot.current === root) setQuestionView({ root, runId: task.run_id, key, problem: cause instanceof Error ? cause.message : String(cause) }); }
    finally { setAnsweringRun(""); }
  };
  // 单选/多选只更新当前原题草稿；忙时仍可查看和填写，提交由独立任务状态保护。
  const chooseBackgroundAnswer = (key: string, cardId: string, optionId: string, multiple: boolean) => setAnswerDrafts(previous => {
    const draft = previous[key], choices = draft.selected[cardId] || [];
    const selected = multiple ? choices.includes(optionId) ? choices.filter(id => id !== optionId) : [...choices, optionId] : [optionId];
    return { ...previous, [key]: { ...draft, selected: { ...draft.selected, [cardId]: selected } } };
  });
  const questionEditor = (task: ChapterTask) => {
    if (questionView?.root !== projectRoot || questionView.runId !== task.run_id) return null;
    const key = questionView.key, draft = key ? answerDrafts[key] : undefined;
    const ready = draft?.question.questions.every(card => Boolean(draft.free[card.id]?.trim())
      || (draft.selected[card.id] || []).some(id => card.options?.some(option => option.id === id && option.kind !== "other")));
    const previous = Object.entries(answerDrafts).filter(([id, item]) => id !== key && item.root === projectRoot && item.runId === task.run_id);
    return <div className="background-question-answer">
      {questionView.loading && <p role="status">正在读取这项工作的原题…</p>}
      {questionView.problem && <p className="form-error" role="status">{questionView.problem}</p>}
      {draft && <>{draft.question.questions.map(card => <fieldset key={card.id}><legend>{card.question}</legend>
        {card.why_it_matters && <p>{card.why_it_matters}</p>}
        {card.options?.map(option => <label key={option.id}><input type={card.selection === "multiple" ? "checkbox" : "radio"}
          name={`${key}:${card.id}`} checked={(draft.selected[card.id] || []).includes(option.id)}
          onChange={() => chooseBackgroundAnswer(key!, card.id, option.id, card.selection === "multiple")} />{option.label}{option.description && <small>{option.description}</small>}</label>)}
        <textarea aria-label={`填写答案：${card.question}`} placeholder="也可以直接写你的想法" value={draft.free[card.id] || ""}
          onChange={event => { const value = event.target.value; setAnswerDrafts(previous => ({ ...previous, [key!]: { ...previous[key!], free: { ...previous[key!].free, [card.id]: value } } })); }} />
      </fieldset>)}<p>只继续这章原来的核对和接收；修改正文或新增规划请在主对话安排。</p>
        <button type="button" disabled={!ready || Boolean(answeringRun) || questionView.loading || Boolean(questionView.problem)} onClick={() => void answerChapterQuestion(task, key!)}>{answeringRun === task.run_id ? "原任务正在继续…" : "提交答案并继续这项工作"}</button></>}
      {previous.length > 0 && <details><summary>之前填写的原题 · {previous.length}</summary>{previous.map(([id, item]) => <p key={id} style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{backgroundAnswerText(item)}</p>)}</details>}
    </div>;
  };
  const chapterRow = (task: ChapterTask) => <article className={`background-task-row status-${task.status}`} key={task.run_id}>
    <div><strong>{task.method === "memory.attachments" ? (task.params?.chapter_no ? `第${task.params.chapter_no}章 · 补全伏笔记录` : task.title || "补全伏笔记录") : task.params?.chapter_no ? `第${task.params.chapter_no}章 · 检查后转为正史` : task.title || "原小说任务"}</strong><span>{task.historical ? "后续已有进展，保留记录" : chapterTaskLabel(task.status)}</span></div>
    {task.params?.expected_version && <small>本次接收版本 v{task.params.expected_version} · 审查、记忆和正式提交按已完成阶段继续</small>}
    <p>{task.error_message || task.summary || task.progress?.at(-1)?.summary || "正在核对已保存的版本和原任务进度…"}</p>
    {task.progress?.length ? <details><summary>已经记录的处理过程</summary><ol>{task.progress.map((step, index) => <li key={`${step.type}:${index}`}>{step.summary}</li>)}</ol></details> : null}
    {task.method !== "preferences.materials.analyze" && task.next_step && task.status !== "completed" && <p>{task.next_step}</p>}
    {task.method === "knowledge.maintain" ? <button type="button" onClick={() => go(onKnowledge)}>打开知识库与待答事项</button> : task.method === "preferences.materials.analyze"
      ? <button type="button" onClick={() => go(onMaterials)}>查看素材分析与提问</button>
      : <button type="button" onClick={() => go(onTaskRecords)}>查看任务记录与具体原因</button>}
    {!task.historical && task.status === "waiting_user" && task.action === "review_accept"
      && <button type="button" disabled={Boolean(answeringRun)} onClick={() => void openChapterQuestion(task)}>查看并回答原题</button>}
    {questionEditor(task)}
    {!task.historical && task.action === "review_accept" && ["waiting_condition", "interrupted", "failed"].includes(task.status)
      && task.params?.chapter_no && task.params.expected_version && task.params.expected_hash && <button type="button"
        onClick={() => { const source = task.params!; onContinueChapter(source.chapter_no!, source.expected_version!, source.expected_hash!); }}>继续未完成步骤</button>}
    {!task.historical && task.method === "memory.attachments" && task.params?.work_id && ["waiting_condition", "interrupted", "failed", "cancelled"].includes(task.status)
      && <button type="button" onClick={() => void request("memory.attachments", { project_root: projectRoot,
        work_id: task.params!.work_id, chapter_no: task.params!.chapter_no, parent_run_id: task.params!.parent_run_id,
        _background: true, _resume: true }).then(() => refresh()).catch(cause => setProblem(String(cause)))}>继续补全记录</button>}
    {task.status === "running" && <button type="button" disabled={Boolean(cancellingRun)} onClick={() => void cancelChapter(task)}>{cancellingRun === task.run_id ? "正在停止…" : "停止这项工作"}</button>}
  </article>;
  return <>
    <button className="background-tasks-entry" type="button" onClick={() => setOpen(true)} title={projectRoot ? "知识整理、章节和语音任务；关闭窗口仍在后台继续" : "查看转换、安装和已完成记录"}>
      <span aria-hidden="true">{activeCount ? "◌" : "☷"}</span> 后台任务{activeCount > 0 && <b>{activeCount}</b>}{waitingCount > 0 && <small>待处理 {waitingCount}</small>}{!loaded && <small>读取中</small>}
      {knowledge?.control.run_id && <small role="status">{["running", "queued"].includes(knowledge.control.status || "") ? `知识 ${knowledge.completed}/${knowledge.total}` : knowledge.control.status === "cancelled" ? "知识已暂停" : ["failed", "waiting_user", "waiting_condition", "interrupted"].includes(knowledge.control.status || "") ? "知识需处理" : "知识已整理"}</small>}
    </button>
    {<MotionPresence open={Boolean(open)}>{() => (open && <dialog className="background-tasks-dialog" ref={dialog} onCancel={event => { event.preventDefault(); setOpen(false); }} onClose={() => setOpen(false)} onClick={event => { if (event.target === event.currentTarget) setOpen(false); }} aria-labelledby="background-tasks-title">
      <header><div><small>墨流 · 工作进度</small><h2 id="background-tasks-title">后台任务</h2></div><button type="button" autoFocus aria-label="关闭后台任务" onClick={() => setOpen(false)}>×</button></header>
      <div className="background-guide"><Mascot mood={activeCount ? "thinking" : waitingCount ? "frustrated" : "guide"} spriteOnly className="background-guide-mascot" /><div><strong>{problem ? "进度暂时没有读到，现有任务会继续保留。" : !loaded ? "正在读取已保存的任务进度…" : activeCount ? "任务在继续，你可以回去写作。" : waitingCount ? "进度已经保留，下面列出了需要处理的事项。" : "目前没有正在运行的后台任务。"}</strong>
        <p>{projectRoot ? "关闭这里只会收起进度。章节审核、记忆交接和转正史会按已完成阶段继续。" : "打开小说后，在工作台的项目页查看章节任务。"}</p>
        <div>{projectRoot && <><button type="button" onClick={() => go(onTaskRecords)}>打开小说任务记录</button><button type="button" onClick={() => go(onVoice)}>打开听读与录音记录</button></>}<button type="button" onClick={() => go(onSettings)}>打开语音设置</button></div>
      </div></div>
      {problem && <p className="form-error" role="status">进度暂未刷新：{problem}。现有任务状态没有更改。<button type="button" onClick={() => void refresh()}>刷新</button></p>}
      <div className="background-task-sections">
        {knowledge && <section aria-label="知识自动维护"><h3>全书知识 · {knowledge.control.auto ? "自动维护已开启" : "自动维护未开启"}</h3><p>已阅读 {knowledge.completed} / {knowledge.total} 段。关闭知识库或此窗口不会停止整理；正文接收、规划生效、设定确认或导入资料后，自动处理新增内容。</p>{knowledge.control.error && <p className="form-error" role="status">{readableSourceText(knowledge.control.error)}</p>}<progress max={Math.max(1, knowledge.total)} value={knowledge.completed} aria-label="知识整理进度" /><button type="button" onClick={() => go(onKnowledge)}>查看知识与待答事项</button></section>}
        <section><h3>正在进行 <small>{activeCount}</small></h3>{writing && <article className="background-task-row"><strong>当前写作任务</strong><p>逐步进度和引导保留在对话里。</p><button type="button" onClick={() => go(onWriting)}>回到对话查看</button></article>}{chapterActive.map(chapterRow)}{active.map(row)}{!activeCount && <p className="muted">没有正在运行的任务。</p>}</section>
        {waitingCount > 0 && <section><h3>需要处理 <small>{waitingCount}</small></h3>{chapterWaiting.map(chapterRow)}{waiting.map(row)}</section>}
        <details open={showHistory} onToggle={event => setShowHistory(event.currentTarget.open)}><summary>已完成与历史记录 · {history.length + chapterHistory.length}</summary>{showHistory && <>{chapterHistory.map(chapterRow)}{history.map(row)}</>}</details>
      </div>
    </dialog>)}</MotionPresence>}
  </>;
}
