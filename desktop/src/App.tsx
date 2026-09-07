import Editor, { DiffEditor, loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import type { editor as MonacoEditor } from "monaco-editor";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject, ReactNode } from "react";
import { Mascot, mobaoIdlePoster } from "./Mascot";
import type { MascotMood } from "./Mascot";
import { ProjectCenter } from "./ProjectCenter";

self.MonacoEnvironment = { getWorker: () => new EditorWorker() };
loader.config({ monaco });

type TreeItem = {
  id: string;
  label: string;
  kind: string;
  relative_path: string;
  chapter_no?: number;
  status?: string;
};

type TreeGroup = { id: string; label: string; items: TreeItem[] };
type ProjectTree = { root: string; items: TreeItem[]; groups: TreeGroup[] };
type Statistics = {
  characters: number;
  paragraphs: number;
  dialogue_ratio: number;
  estimated_reading_minutes: number;
};
type Annotation = {
  annotation_id: string;
  start_offset: number;
  end_offset: number;
  quote: string;
  comment: string;
  status: string;
};
type Version = {
  version_id: string;
  source: string;
  applied: number;
  created_at: string;
  characters: number;
};
type DocumentData = {
  relative_path: string;
  content: string;
  content_hash: string;
  statistics: Statistics;
  annotations: Annotation[];
  versions: Version[];
  read_only: boolean;
  reason: string;
};
type Dashboard = {
  root: string;
  brief: Record<string, unknown>;
  status: { chapters?: Record<string, number>; active_facts?: number; open_threads?: number };
  current_plan: Record<string, unknown> | null;
  facts: Array<Record<string, unknown>>;
  threads: Array<Record<string, unknown>>;
  bible_entries: Array<Record<string, unknown>>;
  accepted_characters: number;
};
type EngineEvent = {
  run_id?: string;
  type?: string;
  summary?: string;
  method?: string;
  action?: string;
  timestamp?: string;
};
type Message = { id: string; role: "user" | "assistant" | "system"; text: string; details?: string };
type QuestionOption = { id: string; label: string; description?: string; recommended?: boolean; kind?: "choice" | "other" };
type QuestionCard = { id: string; header: string; question: string; why_it_matters?: string; selection: "single" | "multiple"; options: QuestionOption[] };
type Tab = "project" | "editor" | "chapter" | "review" | "memory" | "references" | "process";
type RecentProject = { root: string; title: string; openedAt: string };
type NovelIdea = {
  concept_id: string;
  title: string;
  genre: string;
  premise: string;
  protagonist: string;
  target_audience: string;
  core_selling_point: string;
  target_chapter_words: number;
  estimated_chapters: number;
  estimated_volumes: number;
  user_rules: string[];
  opening_hook: string;
  long_term_engine: string;
  choice_note: string;
};
type UpdateInfo = { status?: string; currentVersion?: string; availableVersion?: string; progress?: number; message?: string; source?: string };

const emptyStatistics: Statistics = {
  characters: 0,
  paragraphs: 0,
  dialogue_ratio: 0,
  estimated_reading_minutes: 0,
};

function App() {
  const [appInfo, setAppInfo] = useState<Record<string, unknown> | null>(null);
  const [provider, setProvider] = useState<Record<string, unknown> | null>(null);
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo>({ status: "not_configured", message: "正在读取更新设置…" });
  const [projectRoot, setProjectRoot] = useState("");
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [tree, setTree] = useState<ProjectTree | null>(null);
  const [document, setDocument] = useState<DocumentData | null>(null);
  const [text, setText] = useState("");
  const [savedText, setSavedText] = useState("");
  const [activeTab, setActiveTab] = useState<Tab>("editor");
  const [events, setEvents] = useState<EngineEvent[]>([]);
  const [messages, setMessages] = useState<Message[]>([
    {
      id: "welcome",
      role: "assistant",
      text: "告诉我你想写什么，或打开一本已有小说。我会先理解目标，再安排写作、审查和记忆三个角色。",
    },
  ]);
  const [chatInput, setChatInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [mascotMood, setMascotMood] = useState<MascotMood>("idle");
  const [recentProjects, setRecentProjects] = useState<RecentProject[]>(loadRecentProjects);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showUpdate, setShowUpdate] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [pendingQuestions, setPendingQuestions] = useState<QuestionCard[]>([]);
  const [searchResult, setSearchResult] = useState<Record<string, unknown> | null>(null);
  const [compareContent, setCompareContent] = useState<string | null>(null);
  const [chapterWorkspace, setChapterWorkspace] = useState<Record<string, unknown> | null>(null);
  const editorRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);

  const request = useCallback(
    async <T,>(method: string, params: Record<string, unknown> = {}): Promise<T> => {
      const merged = projectRoot && !params.project_root ? { ...params, project_root: projectRoot } : params;
      return window.inkflow.request<T>(method, merged);
    },
    [projectRoot],
  );

  const refresh = useCallback(
    async (root = projectRoot) => {
      if (!root) return;
      const opened = await window.inkflow.request<{ dashboard: Dashboard; tree: ProjectTree }>("project.open", {
        project_root: root,
      });
      setDashboard(opened.dashboard);
      setTree(opened.tree);
    },
    [projectRoot],
  );

  const openProject = useCallback(async (root: string) => {
    setError("");
    setBusy(true);
    setMascotMood("thinking");
    try {
      const opened = await window.inkflow.request<{ dashboard: Dashboard; tree: ProjectTree }>("project.open", {
        project_root: root,
      });
      setProjectRoot(root);
      setDashboard(opened.dashboard);
      setTree(opened.tree);
      setDocument(null);
      setText("");
      setActiveTab("project");
      localStorage.setItem("inkflow.lastProject", root);
      const projectTitle = String(opened.dashboard.brief.title || "未命名小说");
      setRecentProjects((items) => rememberRecentProject(items, root, projectTitle));
      setMessages((items) => [
        ...items,
        {
          id: crypto.randomUUID(),
          role: "system",
          text: `已打开《${projectTitle}》。正史、草稿和审查边界已载入。`,
        },
      ]);
      setMascotMood("success");
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
      localStorage.removeItem("inkflow.lastProject");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void Promise.all([
      window.inkflow.request<Record<string, unknown>>("app.initialize"),
      window.inkflow.request<Record<string, unknown>>("provider.status"),
      window.inkflow.launchContext(),
      window.inkflow.updateStatus(),
    ])
      .then(([info, status, launch, update]) => {
        setAppInfo(info);
        setProvider(status);
        setUpdateInfo(update as UpdateInfo);
        const recent = launch.projectRoot || localStorage.getItem("inkflow.lastProject");
        if (recent) void openProject(recent);
      })
      .catch((cause) => {
        setError(errorMessage(cause));
        setMascotMood("rest");
      });
    const removeEvent = window.inkflow.onEvent((value) => {
      const event = value as EngineEvent;
      setEvents((items) => [...items.slice(-199), { ...event, timestamp: new Date().toISOString() }]);
    });
    const removeStatus = window.inkflow.onStatus((value) => {
      const status = value as { message?: string };
      if (status.message) setNotice(status.message);
    });
    const removeOpenProject = window.inkflow.onOpenProject((root) => void openProject(root));
    const removeUpdate = window.inkflow.onUpdateStatus((value) => setUpdateInfo(value as UpdateInfo));
    return () => {
      removeEvent();
      removeStatus();
      removeOpenProject();
      removeUpdate();
    };
  }, [openProject]);

  useEffect(() => {
    if (!document || document.read_only || text === savedText) return;
    const timer = window.setTimeout(() => void saveDocument(true), 1800);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, savedText, document?.relative_path, document?.read_only]);

  const openDocument = async (item: TreeItem) => {
    setError("");
    try {
      const loaded = await request<DocumentData>("document.read", { relative_path: item.relative_path });
      setDocument(loaded);
      setText(loaded.content);
      setSavedText(loaded.content);
      setCompareContent(null);
      if (item.kind === "review") setActiveTab("review");
      else if (item.kind === "chapter") setActiveTab("editor");
      else setActiveTab("editor");
      if (item.chapter_no) {
        const workspace = await request<Record<string, unknown>>("chapter.workspace", {
          chapter_no: item.chapter_no,
        });
        setChapterWorkspace(workspace);
      } else {
        setChapterWorkspace(null);
      }
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const saveDocument = async (quiet = false) => {
    if (!document || text === savedText) return;
    try {
      const result = await request<Record<string, unknown>>("document.save", {
        relative_path: document.relative_path,
        content: text,
        expected_hash: document.content_hash,
        source: "desktop_editor",
      });
      if (result.saved) {
        const loaded = await request<DocumentData>("document.read", { relative_path: document.relative_path });
        setDocument(loaded);
        setSavedText(loaded.content);
        if (!quiet) setNotice("已保存，并保留上一版本快照。");
        if (!quiet) setMascotMood("success");
      } else {
        setSavedText(text);
        setNotice(String(result.gate || "已保存为未应用的正史修改提案。"));
      }
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
    }
  };

  const annotateSelection = async () => {
    if (!document || !editorRef.current) return;
    const selection = editorRef.current.getSelection();
    const model = editorRef.current.getModel();
    if (!selection || !model || selection.isEmpty()) {
      setNotice("请先在正文中选中要批注的文字。");
      setMascotMood("waiting");
      return;
    }
    const comment = window.prompt("你希望写作角色如何处理这段内容？");
    if (!comment) return;
    const start = model.getOffsetAt(selection.getStartPosition());
    const end = model.getOffsetAt(selection.getEndPosition());
    try {
      await request("document.annotate", {
        relative_path: document.relative_path,
        start_offset: start,
        end_offset: end,
        comment,
      });
      const loaded = await request<DocumentData>("document.read", { relative_path: document.relative_path });
      setDocument(loaded);
      setNotice("批注已记录。它不会直接改正文，可在自然对话中要求写作角色按批注修订。");
      setMascotMood("success");
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
    }
  };

  const sendChat = async (event?: FormEvent, overrideMessage?: string) => {
    event?.preventDefault();
    const message = (overrideMessage ?? chatInput).trim();
    if (!message || !projectRoot || busy) return;
    setChatInput("");
    setError("");
    setMessages((items) => [...items, { id: crypto.randomUUID(), role: "user", text: message }]);
    setBusy(true);
    setMascotMood("thinking");
    const runId = `desktop-${crypto.randomUUID()}`;
    setActiveRunId(runId);
    try {
      const result = await request<unknown>("conversation.send", { message, run_id: runId });
      const visible = visibleResult(result);
      const questionSource = result && typeof result === "object" ? (result as Record<string, unknown>).questions : null;
      if (Array.isArray(questionSource)) setPendingQuestions(questionSource as QuestionCard[]);
      setMessages((items) => [
        ...items,
        { id: crypto.randomUUID(), role: "assistant", text: visible.summary, details: visible.details },
      ]);
      await refresh();
      setMascotMood("success");
    } catch (cause) {
      const messageText = errorMessage(cause);
      const cancelled = messageText.includes("任务已取消");
      setMessages((items) => [
        ...items,
        { id: crypto.randomUUID(), role: "assistant", text: cancelled ? "当前任务已停止，已经落盘的草稿仍然保留。" : `这次没有完成：${messageText}` },
      ]);
      if (!cancelled) setError(messageText);
      setMascotMood(cancelled ? "waiting" : "rest");
    } finally {
      setBusy(false);
      setActiveRunId(null);
    }
  };

  const runWorkflow = async (action: string) => {
    if (!projectRoot || busy) return;
    const chapterNo = currentChapter(document?.relative_path);
    if (["write", "review", "accept"].includes(action) && !chapterNo) {
      setNotice("请先从左侧打开一个章节，或直接在对话里说出章节号。");
      setMascotMood("waiting");
      return;
    }
    if (action === "accept" && !window.confirm("验收会让记忆角色将本章提交为正史。确认继续吗？")) {
      setMascotMood("waiting");
      return;
    }
    setBusy(true);
    setMascotMood("thinking");
    setError("");
    const runId = `desktop-${crypto.randomUUID()}`;
    setActiveRunId(runId);
    try {
      const result = await request<unknown>("workflow.run", {
        action,
        run_id: runId,
        ...(chapterNo ? { chapter_no: chapterNo } : {}),
      });
      const visible = visibleResult(result);
      setMessages((items) => [
        ...items,
        { id: crypto.randomUUID(), role: "assistant", text: visible.summary, details: visible.details },
      ]);
      await refresh();
      setMascotMood("success");
    } catch (cause) {
      const messageText = errorMessage(cause);
      if (messageText.includes("任务已取消")) {
        setNotice("当前任务已停止，未验收内容不会进入正史。");
        setMascotMood("waiting");
      } else {
        setError(messageText);
        setMascotMood("rest");
      }
    } finally {
      setBusy(false);
      setActiveRunId(null);
    }
  };

  const cancelActiveRun = async () => {
    if (!activeRunId) return;
    try {
      await window.inkflow.request("run.cancel", { run_id: activeRunId });
      setNotice("已请求停止当前任务；已经写入的草稿会保留，未验收内容不会越过正史门禁。");
      setMascotMood("waiting");
    } catch (cause) {
      setError(errorMessage(cause));
    }
  };

  const openFolder = async () => {
    const selected = await window.inkflow.chooseFolder("打开墨流小说项目");
    if (selected) await openProject(selected);
  };

  const stats = document?.statistics || emptyStatistics;
  const title = String(dashboard?.brief.title || "墨流");
  const isDirty = Boolean(document && text !== savedText);

  if (!projectRoot) {
    return (
      <div className="welcome-shell">
        <div className="brand-mark mascot-stage">
          <Mascot mood={error ? "rest" : "welcome"} className="welcome-mascot" />
        </div>
        <div className="welcome-copy">
          <p className="eyebrow">INKFLOW · 长篇创作工作台</p>
          <h1>让灵感流动，<br />让正史站得住。</h1>
          <p className="lede">自然对话负责理解你；写作、审查与记忆三个角色各守边界。规划、正文、证据和回退都留在你自己的电脑。</p>
          <div className="welcome-actions">
            <button className="primary" onClick={() => setShowCreate(true)}>新建小说</button>
            <button onClick={openFolder}>打开项目</button>
          </div>
          <div className="welcome-meta">
            <span>版本 {String(appInfo?.version || "0.3.1")}</span>
            <span>{provider?.api_key_configured ? "模型已配置" : "尚未配置模型 Key"}</span>
            <button className="text-button" onClick={() => setShowSettings(true)}>模型设置</button>
            <button className="text-button" onClick={() => setShowUpdate(true)}>检查更新</button>
          </div>
          {recentProjects.length > 0 && (
            <section className="recent-projects">
              <div><strong>最近打开</strong><small>只保存在这台电脑</small></div>
              {recentProjects.map((project) => (
                <button key={project.root} onClick={() => void openProject(project.root)}>
                  <span>{project.title}</span><small>{project.root}</small>
                </button>
              ))}
            </section>
          )}
        </div>
        {error && <Toast kind="error" text={error} onClose={() => { setError(""); setMascotMood("idle"); }} />}
        {showCreate && <CreateProject onClose={() => setShowCreate(false)} onCreated={openProject} />}
        {showSettings && <SettingsDialog provider={provider} onClose={() => setShowSettings(false)} onSaved={setProvider} />}
        {showUpdate && <UpdateDialog info={updateInfo} onClose={() => setShowUpdate(false)} />}
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="wordmark"><img src={mobaoIdlePoster} alt="墨宝" /><div><strong>墨流</strong><small>INKFLOW</small></div></div>
        <div className="project-heading">
          <strong>{title}</strong>
          <span>{dashboard?.accepted_characters.toLocaleString() || 0} 字正史</span>
          {busy && <span className="working-dot">正在工作</span>}
        </div>
        <nav className="top-actions">
          <button onClick={() => setShowCreate(true)}>＋ 新建</button>
          <button onClick={openFolder}>切换项目</button>
          <button onClick={() => setShowSearch(true)}>⌕ 搜索</button>
          <button onClick={() => void refresh()}>↻ 刷新</button>
          <button onClick={() => void window.inkflow.openPath(projectRoot)}>打开文件夹</button>
          <button onClick={() => setShowSettings(true)}>设置</button>
          <button onClick={() => setShowUpdate(true)}>{updateInfo.status === "downloaded" ? "安装更新" : "检查更新"}</button>
        </nav>
      </header>

      <main className="workspace-grid">
        <aside className="left-rail">
          <div className="rail-heading"><span>小说结构</span><button title="新建小说" onClick={() => setShowCreate(true)}>＋</button></div>
          <TreeSection label="核心文档" items={tree?.items || []} onOpen={openDocument} active={document?.relative_path} />
          {(tree?.groups || []).map((group) => (
            <TreeSection key={group.id} label={group.label} items={group.items} onOpen={openDocument} active={document?.relative_path} />
          ))}
          <div className="rail-summary">
            <div><span>草稿</span><strong>{dashboard?.status.chapters?.draft || 0}</strong></div>
            <div><span>正史</span><strong>{dashboard?.status.chapters?.accepted || 0}</strong></div>
            <div><span>伏笔</span><strong>{dashboard?.status.open_threads || 0}</strong></div>
          </div>
        </aside>

        <section className="conversation-panel">
          <div className="panel-title">
            <div><p className="eyebrow">自然对话主控</p><h2>今天写到哪里？</h2></div>
            <div className="panel-mascot">
              <Mascot
                mood={busy ? "thinking" : mascotMood}
                showLabel
                onSettled={() => setMascotMood("idle")}
              />
              <span className="agent-boundary">三个小说角色 · 边界开启</span>
            </div>
          </div>
          <div className="quick-row">
            <button onClick={() => void sendChat(undefined, "先不要执行任务。请根据当前项目状态和最近讨论，用选项卡主动问我一到三个最值得确认、容易回答的问题；说明每个答案会影响什么，最后保留让我自己填写的其他选项。")}>先问我</button>
            <button onClick={() => void runWorkflow("plan")}>规划当前篇章</button>
            <button onClick={() => void runWorkflow("write")}>写当前章</button>
            <button onClick={() => void runWorkflow("review")}>审查当前章</button>
            <button onClick={() => void runWorkflow("accept")}>验收进正史</button>
          </div>
          <div className="messages">
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <span className="avatar">{message.role === "user" ? "你" : message.role === "system" ? "记" : "墨"}</span>
                <div>
                  <p>{message.text}</p>
                  {message.details && <details><summary>查看可复核详情</summary><pre>{message.details}</pre></details>}
                </div>
              </article>
            ))}
            {busy && <article className="message assistant pending" aria-live="polite"><span className="avatar">墨</span><div><p>{[...events].reverse().find(item => item.run_id === activeRunId && item.summary)?.summary || "已收到，先理解你的目标，再把回答或需要确认的问题发在这里。"}</p><small>完成后直接显示答复；也可随时停止。</small></div></article>}
          </div>
          <form className="composer" onSubmit={sendChat}>
            <textarea
              value={chatInput}
              onChange={(event) => setChatInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void sendChat();
                }
              }}
              placeholder="例如：先给我看第 8～12 章的章节卡，再批量写草稿并逐章审查；不要验收。"
            />
            <div className="composer-foot">
              <span>Enter 发送 · Shift+Enter 换行</span>
              <div>
                {busy && <button className="stop-action" type="button" onClick={() => void cancelActiveRun()}>停止任务</button>}
                <button className="primary" type="submit" disabled={busy || !chatInput.trim()}>发送</button>
              </div>
            </div>
          </form>
        </section>

        <section className="workbench">
          <div className="tabbar">
            {(["project", "editor", "chapter", "review", "memory", "references", "process"] as Tab[]).map((tab) => (
              <button key={tab} className={activeTab === tab ? "active" : ""} onClick={() => setActiveTab(tab)}>
                {tabLabel(tab)}
              </button>
            ))}
          </div>
          {activeTab === "project" && (
            <ProjectCenter
              dashboard={dashboard}
              request={request}
              onPrompt={(value) => { setChatInput(value); setMascotMood("waiting"); }}
              onRefresh={() => refresh()}
              onNotice={setNotice}
              onError={setError}
            />
          )}
          {activeTab === "editor" && (
            <EditorPanel
              document={document}
              text={text}
              savedText={savedText}
              compareContent={compareContent}
              isDirty={isDirty}
              statistics={stats}
              editorRef={editorRef}
              onChange={setText}
              onSave={() => void saveDocument(false)}
              onAnnotate={() => void annotateSelection()}
              onStopCompare={() => setCompareContent(null)}
              onCompare={async (versionId) => {
                try {
                  const version = await request<{ content: string }>("document.version.read", { version_id: versionId });
                  setCompareContent(version.content);
                } catch (cause) {
                  setError(errorMessage(cause));
                }
              }}
              onResolve={async (annotationId) => {
                await request("annotation.update", { annotation_id: annotationId, status: "resolved" });
                if (document) {
                  const loaded = await request<DocumentData>("document.read", { relative_path: document.relative_path });
                  setDocument(loaded);
                }
              }}
            />
          )}
          {activeTab === "chapter" && <ChapterPanel workspace={chapterWorkspace} onPrompt={(value) => setChatInput(value)} />}
          {activeTab === "review" && <ReviewPanel document={document} tree={tree} onOpen={openDocument} />}
          {activeTab === "memory" && <MemoryPanel dashboard={dashboard} request={request} onRefresh={() => refresh()} />}
          {activeTab === "references" && <ReferencesPanel request={request} />}
          {activeTab === "process" && <ProcessPanel events={events} />}
        </section>
      </main>

      {(notice || error) && <Toast kind={error ? "error" : "info"} text={error || notice} onClose={() => { setError(""); setNotice(""); setMascotMood("idle"); }} />}
      {showCreate && <CreateProject onClose={() => setShowCreate(false)} onCreated={openProject} />}
      {showSettings && <SettingsDialog provider={provider} onClose={() => setShowSettings(false)} onSaved={setProvider} />}
      {showUpdate && <UpdateDialog info={updateInfo} onClose={() => setShowUpdate(false)} />}
      {showSearch && <SearchDialog request={request} result={searchResult} setResult={setSearchResult} onClose={() => setShowSearch(false)} onOpen={async (relativePath) => {
        const item = [...(tree?.items || []), ...(tree?.groups.flatMap((group) => group.items) || [])].find((entry) => entry.relative_path === relativePath);
        if (item) await openDocument(item);
        setShowSearch(false);
      }} />}
      {pendingQuestions.length > 0 && <QuestionDialog questions={pendingQuestions} onClose={() => setPendingQuestions([])} onSubmit={(answer) => {
        setPendingQuestions([]);
        void sendChat(undefined, answer);
      }} />}
    </div>
  );
}

function TreeSection({ label, items, onOpen, active }: { label: string; items: TreeItem[]; onOpen: (item: TreeItem) => void; active?: string }) {
  const [expanded, setExpanded] = useState(true);
  return (
    <section className="tree-section">
      <button className="tree-label" onClick={() => setExpanded(!expanded)}><span>{expanded ? "⌄" : "›"}</span>{label}<small>{items.length}</small></button>
      {expanded && <div className="tree-items">
        {items.length === 0 && <p className="empty-mini">暂无内容</p>}
        {items.map((item) => (
          <button key={item.id} className={active === item.relative_path ? "active" : ""} onClick={() => void onOpen(item)} title={item.relative_path}>
            <span>{item.kind === "chapter" ? "§" : item.kind === "review" ? "✓" : "◇"}</span>
            <em>{item.label}</em>
          </button>
        ))}
      </div>}
    </section>
  );
}

function EditorPanel(props: {
  document: DocumentData | null;
  text: string;
  savedText: string;
  compareContent: string | null;
  isDirty: boolean;
  statistics: Statistics;
  editorRef: MutableRefObject<MonacoEditor.IStandaloneCodeEditor | null>;
  onChange: (value: string) => void;
  onSave: () => void;
  onAnnotate: () => void;
  onCompare: (versionId: string) => void;
  onStopCompare: () => void;
  onResolve: (annotationId: string) => void;
}) {
  const [side, setSide] = useState<"comments" | "versions">("comments");
  if (!props.document) return <EmptyPanel title="选择一份文档" text="从左侧打开正文、规划或审查报告。" />;
  return (
    <div className="editor-layout">
      <div className="document-toolbar">
        <div><strong>{props.document.relative_path}</strong><span>{props.isDirty ? "尚未保存" : "已同步"}</span></div>
        <div>
          <button onClick={props.onAnnotate}>批注选区</button>
          <button className="primary" disabled={!props.isDirty} onClick={props.onSave}>{props.document.read_only ? "保存修改提案" : "保存"}</button>
        </div>
      </div>
      {props.document.read_only && <div className="canon-banner"><strong>正史保护</strong>{props.document.reason}</div>}
      <div className="editor-body">
        <div className="monaco-wrap">
          {props.compareContent !== null ? (
            <>
              <div className="diff-head"><span>左：历史版本 · 右：当前内容</span><button onClick={props.onStopCompare}>退出对比</button></div>
              <DiffEditor height="100%" language="markdown" original={props.compareContent} modified={props.text} theme="inkflow-dark" options={{ readOnly: true, minimap: { enabled: false }, wordWrap: "on" }} />
            </>
          ) : (
            <Editor
              height="100%"
              language="markdown"
              value={props.text}
              theme="vs-dark"
              onMount={(editor) => { props.editorRef.current = editor; }}
              onChange={(value) => props.onChange(value || "")}
              options={{ minimap: { enabled: false }, wordWrap: "on", fontSize: 16, lineHeight: 27, padding: { top: 22, bottom: 32 }, smoothScrolling: true, renderLineHighlight: "gutter", fontFamily: "'Microsoft YaHei UI', 'Noto Serif SC', Consolas, monospace" }}
            />
          )}
        </div>
        <aside className="inspector">
          <div className="inspector-tabs"><button className={side === "comments" ? "active" : ""} onClick={() => setSide("comments")}>批注</button><button className={side === "versions" ? "active" : ""} onClick={() => setSide("versions")}>版本</button></div>
          {side === "comments" ? (
            <div className="inspector-list">
              {props.document.annotations.length === 0 && <p className="empty-mini">选中正文后可添加行级批注。</p>}
              {props.document.annotations.map((item) => (
                <article key={item.annotation_id} className={`annotation ${item.status}`}>
                  <blockquote>{item.quote}</blockquote><p>{item.comment}</p><small>{statusLabel(item.status)}</small>
                  {item.status === "open" && <button onClick={() => void props.onResolve(item.annotation_id)}>标为已处理</button>}
                </article>
              ))}
            </div>
          ) : (
            <div className="inspector-list">
              {props.document.versions.length === 0 && <p className="empty-mini">保存后会自动保留旧版本。</p>}
              {props.document.versions.map((item) => (
                <article key={item.version_id} className="version-card">
                  <strong>{item.applied ? "历史快照" : "未应用提案"}</strong><small>{formatTime(item.created_at)} · {item.characters} 字</small><span>{item.source}</span>
                  <button onClick={() => void props.onCompare(item.version_id)}>与当前对比</button>
                </article>
              ))}
            </div>
          )}
        </aside>
      </div>
      <footer className="status-strip"><span>{props.statistics.characters.toLocaleString()} 字</span><span>{props.statistics.paragraphs} 段</span><span>对白 {Math.round(props.statistics.dialogue_ratio * 100)}%</span><span>约 {props.statistics.estimated_reading_minutes} 分钟</span></footer>
    </div>
  );
}

function ChapterPanel({ workspace, onPrompt }: { workspace: Record<string, unknown> | null; onPrompt: (value: string) => void }) {
  if (!workspace) return <EmptyPanel title="章节工位" text="打开一章后，这里会集中显示章节卡、场景节拍、人物状态和待回收线索。" />;
  const card = (workspace.card || {}) as Record<string, unknown>;
  const threads = (workspace.open_threads || []) as Array<Record<string, unknown>>;
  return <div className="scroll-panel chapter-panel">
    <div className="section-heading"><p className="eyebrow">第 {String(workspace.chapter_no)} 章</p><h2>{String(card.title_working || "尚无章节卡")}</h2></div>
    <div className="card-grid">
      <InfoCard label="章节功能" value={card.function} />
      <InfoCard label="人物目标" value={card.goal} />
      <InfoCard label="阻力" value={card.obstacle} />
      <InfoCard label="不可逆变化" value={card.irreversible_delta} />
      <InfoCard label="章末钩子" value={card.hook_question} accent />
      <InfoCard label="时空" value={card.time_location} />
    </div>
    <section className="stack-section"><h3>场景节拍</h3>{((card.scenes || []) as unknown[]).map((scene, index) => <div className="beat" key={index}><span>{index + 1}</span><p>{String(scene)}</p></div>)}</section>
    <section className="stack-section"><h3>仍在推进的线索</h3>{threads.slice(0, 8).map((thread) => <article className="thread-card" key={String(thread.thread_id)}><strong>{String(thread.title || thread.thread_id)}</strong><p>{String(thread.description || "")}</p><small>{String(thread.status)}</small></article>)}</section>
    <button className="wide-action" onClick={() => onPrompt(`请检查第 ${String(workspace.chapter_no)} 章的章节卡、场景节拍和人物知识边界，先告诉我风险，不要直接写正文。`)}>把本章约束带入对话</button>
  </div>;
}

function ReviewPanel({ document, tree, onOpen }: { document: DocumentData | null; tree: ProjectTree | null; onOpen: (item: TreeItem) => void }) {
  const reviews = tree?.groups.find((group) => group.id === "reviews")?.items || [];
  return <div className="scroll-panel review-panel">
    <div className="section-heading"><p className="eyebrow">审查角色 · 只审不改</p><h2>证据化审查</h2><p>每个扣分项都应指向正文、章节卡或正史依据；旧审查不能批准新版本。</p></div>
    {document?.relative_path.startsWith("reviews/") && <article className="markdown-preview"><pre>{document.content}</pre></article>}
    <section className="stack-section"><h3>审查记录</h3>{reviews.length === 0 && <p className="empty-mini">还没有审查报告。</p>}{reviews.map((item) => <button className="review-link" key={item.id} onClick={() => void onOpen(item)}><span>✓</span><strong>{item.label}</strong><small>打开报告</small></button>)}</section>
  </div>;
}

function MemoryPanel({ dashboard, request, onRefresh }: { dashboard: Dashboard | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; onRefresh: () => Promise<void> }) {
  const [showAdd, setShowAdd] = useState(false);
  const [kind, setKind] = useState("character");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const save = async () => {
    if (!name.trim()) return;
    await request("bible.upsert", { kind, name, aliases: [], data: { notes } });
    setName(""); setNotes(""); setShowAdd(false); await onRefresh();
  };
  return <div className="scroll-panel memory-panel">
    <div className="section-heading"><p className="eyebrow">记忆角色 · 验收后提交</p><h2>正史与故事圣经</h2><button onClick={() => setShowAdd(!showAdd)}>＋ 手动圣经条目</button></div>
    {showAdd && <div className="inline-form"><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="character">人物</option><option value="location">地点</option><option value="organization">组织</option><option value="item">物品</option><option value="lore">世界观</option><option value="style">文风</option></select><input value={name} onChange={(event) => setName(event.target.value)} placeholder="名称" /><textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="稳定设定、声线、禁忌或说明" /><button className="primary" onClick={() => void save()}>保存</button></div>}
    <section className="stack-section"><h3>人工故事圣经 <small>{dashboard?.bible_entries.length || 0}</small></h3>{dashboard?.bible_entries.map((entry) => <article className="memory-card" key={String(entry.entry_id)}><span>{kindLabel(String(entry.kind))}</span><strong>{String(entry.name)}</strong><p>{String(((entry.data || {}) as Record<string, unknown>).notes || "")}</p></article>)}</section>
    <section className="stack-section"><h3>当前正史事实 <small>{dashboard?.facts.length || 0}</small></h3>{dashboard?.facts.slice(0, 30).map((fact) => <article className="fact-row" key={String(fact.fact_id)}><strong>{String(fact.subject)} · {String(fact.predicate)}</strong><p>{stringifyShort(fact.value)}</p><small>来源第 {String(fact.source_chapter)} 章</small></article>)}</section>
    <section className="stack-section"><h3>开放线索 <small>{dashboard?.threads.length || 0}</small></h3>{dashboard?.threads.map((thread) => <article className="thread-card" key={String(thread.thread_id)}><strong>{String(thread.title)}</strong><p>{String(thread.description)}</p><small>{String(thread.status)} · 预计第 {String(thread.due_chapter || "—")} 章</small></article>)}</section>
  </div>;
}

function ProcessPanel({ events }: { events: EngineEvent[] }) {
  const runs = processRuns(events);
  return <div className="scroll-panel process-panel"><div className="section-heading"><p className="eyebrow">本次安排</p><h2>任务计划</h2><p>先看要做什么，再看当前进度。普通聊天只需理解和回答；写作任务按你提出的范围执行。</p></div><div className="run-list">{runs.length === 0 && <p className="empty-mini">发送需求后，这里显示本次任务的安排。</p>}{runs.slice(0, 8).map(run => {
    const plan = run.method === "conversation.send" ? ["理解需求与已有设定", "回答问题，或执行确认后的写作任务", "在对话中交付结果"] : run.method === "project.ideate" ? ["读取开书偏好", "构思故事方向与开篇", "展示方案，等待你选择"] : run.method === "provider.test" ? ["发送连接检查", "等待模型回答", "显示连接结果"] : ["读取任务范围与相关章节", "执行本次任务", "交付结果供你查看"];
    const started = run.steps.some(step => ["workflow.started", "writer.started", "provider.testing"].includes(step.type || ""));
    const current = run.status === "done" ? 3 : started ? 1 : 0;
    return <article className={`run-card ${run.status}`} key={run.id}><header><strong>{methodLabel(run.method)}</strong><span>{run.status === "done" ? "已完成" : run.status === "failed" ? "未完成" : run.status === "cancelled" ? "已停止" : "进行中"}</span></header><ol>{plan.map((step, index) => <li key={step}><span>{index < current ? "✓" : index === current && run.status === "running" ? "进行中 ·" : "待完成 ·"} {step}</span></li>)}</ol><p>{run.summary}</p><small>{run.method === "project.ideate" ? "结果位置：新建小说窗口" : run.method === "provider.test" ? "结果位置：模型设置窗口" : "结果位置：中间对话区；生成文件可从左侧小说结构打开"}</small></article>;
  })}</div></div>;
}

function ReferencesPanel({ request }: { request: <T>(method: string, params?: Record<string, unknown>) => Promise<T> }) {
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]);
  const [url, setUrl] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<Array<Record<string, unknown>>>([]);
  const [searchNotice, setSearchNotice] = useState("");
  const [working, setWorking] = useState(false);
  const [feature, setFeature] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(async () => setItems(await request<Array<Record<string, unknown>>>("reference.list")), [request]);
  useEffect(() => { void load(); }, [load]);
  const fetchUrl = async () => {
    if (!url.trim()) return;
    setWorking(true); setError("");
    try { await request("reference.fetch", { url: url.trim() }); setUrl(""); await load(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  const searchPublic = async () => {
    if (!searchQuery.trim()) return;
    setWorking(true); setError(""); setSearchNotice("");
    try {
      const result = await request<{ results: Array<Record<string, unknown>>; notice: string }>("reference.search", { query: searchQuery.trim(), limit: 6 });
      setSearchResults(result.results || []);
      setSearchNotice(result.notice || "");
    } catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  const importSearchResult = async (resultUrl: string) => {
    setWorking(true); setError("");
    try { await request("reference.fetch", { url: resultUrl }); await load(); setSearchNotice("已导入所选网页。搜索列表中的其他来源仍未保存。"); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  const importFile = async () => {
    const source = await window.inkflow.chooseFile("导入你有权使用的纯文本或 Markdown 参考资料");
    if (!source) return;
    setWorking(true); setError("");
    try { await request("reference.import", { source_path: source }); await load(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  return <div className="scroll-panel references-panel">
    <div className="section-heading"><p className="eyebrow">参考资料 · 结构学习</p><h2>参考资料与爆款拆解</h2><p>导入公开网页或本地文本后，墨流先提取节奏、段落、对白和章末样本等特征；不会把整本参考正文无差别塞进写作上下文。</p></div>
    <section className="research-search"><div><strong>联网查找写作资料</strong><small>只发送下面的搜索词；小说正文、正史和模型密钥不会上传。搜索结果先预览，点击后才导入。</small></div><form onSubmit={(event) => { event.preventDefault(); void searchPublic(); }}><input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="例如：悬疑小说如何控制信息差；创伤后回避行为的可靠资料" /><button className="primary" disabled={working || !searchQuery.trim()}>搜索公开资料</button></form>{searchNotice && <p>{searchNotice}</p>}{searchResults.length > 0 && <div className="research-results">{searchResults.map((result, index) => <article key={`${String(result.url)}-${index}`}><div><small>{String(result.source || "公开网页")}</small><strong>{String(result.title || "未命名资料")}</strong><p>{String(result.snippet || "该结果没有提供摘要，请查看来源后决定是否导入。")}</p></div><button disabled={working} onClick={() => void importSearchResult(String(result.url))}>导入这页</button></article>)}</div>}</section>
    <div className="reference-divider"><span>或直接导入已知资料</span></div>
    <div className="reference-import"><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="公开网页链接；番茄公开页会自动使用专用适配器" /><button disabled={working || !url.trim()} onClick={() => void fetchUrl()}>抓取公开页</button><button disabled={working} onClick={() => void importFile()}>导入本地文本</button></div>
    {error && <p className="form-error">{error}</p>}
    {feature && <article className="feature-sheet"><div><p className="eyebrow">确定性特征卡</p><h3>{String(feature.reference_id)}</h3></div><dl><dt>总字符</dt><dd>{Number(feature.characters || 0).toLocaleString()}</dd><dt>识别章节</dt><dd>{String(feature.detected_chapters || 0)}</dd><dt>平均段落</dt><dd>{String(feature.average_paragraph_chars || 0)} 字</dd><dt>平均句长</dt><dd>{String(feature.average_sentence_chars || 0)} 字</dd><dt>对白比例</dt><dd>{Math.round(Number(feature.dialogue_ratio || 0) * 100)}%</dd></dl><p>{String(feature.note || "")}</p></article>}
    <section className="stack-section"><h3>资料库 <small>{items.length}</small></h3>{items.length === 0 && <p className="empty-mini">尚未导入资料。你也可以在自然对话里请墨流分析某个公开链接。</p>}{items.map((item) => <article className="reference-card" key={String(item.reference_id)}><div><strong>{referenceTitle(item)}</strong><small>{String(item.source_type)} · {formatTime(String(item.imported_at || ""))}</small><p>{String(item.source)}</p></div><span>{item.analyzed ? "已分析" : "待分析"}</span><button disabled={working} onClick={async () => { setWorking(true); setError(""); try { setFeature(await request<Record<string, unknown>>("reference.analyze", { reference_id: item.reference_id })); await load(); } catch (cause) { setError(errorMessage(cause)); } finally { setWorking(false); } }}>生成特征卡</button></article>)}</section>
  </div>;
}

function CreateProject({ onClose, onCreated }: { onClose: () => void; onCreated: (root: string) => Promise<void> }) {
  const [root, setRoot] = useState("");
  const [form, setForm] = useState({ title: "", genre: "都市悬疑", premise: "", protagonist: "", target_chapter_words: 3000, estimated_chapters: 200, estimated_volumes: 6 });
  const [mode, setMode] = useState<"ai" | "manual">("ai");
  const [preferences, setPreferences] = useState("");
  const [ideas, setIdeas] = useState<NovelIdea[]>([]);
  const [ideaReasoning, setIdeaReasoning] = useState<string[]>([]);
  const [selectedIdeaIndex, setSelectedIdeaIndex] = useState<number | null>(null);
  const [working, setWorking] = useState(false);
  const [ideaStage, setIdeaStage] = useState("");
  const [error, setError] = useState("");
  const detailsRef = useRef<HTMLDivElement | null>(null);
  const ideate = async (fast: boolean) => {
    setWorking(true); setError(""); setIdeas([]); setSelectedIdeaIndex(null); setIdeaReasoning([]);
    setIdeaStage(fast ? "正在快速生成一个可直接使用的方向…" : "正在生成三个不同方向供你比较…");
    try {
      const result = await window.inkflow.request<{ candidates: NovelIdea[]; public_reasoning_summary: string[]; notice?: string }>("project.ideate", { preferences, fast });
      setIdeas(result.candidates);
      setIdeaReasoning(result.public_reasoning_summary || []);
      if (result.candidates.length === 1) chooseIdea(result.candidates[0], 0, false);
      const completion = result.candidates.length === 1 ? "方案已填入建项信息，可以修改后创建。" : `已生成 ${result.candidates.length} 个方案，请采用一个再创建小说。`;
      setIdeaStage(result.notice ? `${result.notice} ${completion}` : completion);
    } catch (cause) { setError(errorMessage(cause)); setIdeaStage(""); } finally { setWorking(false); }
  };
  const chooseIdea = (idea: NovelIdea, index: number, scroll = true) => {
    setSelectedIdeaIndex(index);
    setForm({ title: idea.title, genre: idea.genre, premise: idea.premise, protagonist: idea.protagonist, target_chapter_words: idea.target_chapter_words, estimated_chapters: idea.estimated_chapters, estimated_volumes: idea.estimated_volumes });
    setIdeaStage(`已采用《${idea.title}》。下面可以修改细节并创建项目。`);
    if (scroll) window.requestAnimationFrame(() => detailsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };
  const create = async (event: FormEvent) => {
    event.preventDefault(); setWorking(true); setError("");
    try {
      const idea = selectedIdeaIndex === null ? undefined : ideas[selectedIdeaIndex];
      await window.inkflow.request("project.create", { project_root: root, brief: { ...form, target_audience: idea?.target_audience || "中文网文读者", core_selling_point: idea?.core_selling_point || "", user_rules: idea?.user_rules || [] } });
      onClose(); await onCreated(root);
    } catch (cause) { setError(errorMessage(cause)); } finally { setWorking(false); }
  };
  const canCreate = Boolean(root && form.title.trim() && form.premise.trim() && form.protagonist.trim() && (mode === "manual" || selectedIdeaIndex !== null));
  const createHint = !root ? "先选择项目文件夹" : mode === "ai" && selectedIdeaIndex === null ? "先采用一个方案" : !form.title.trim() || !form.premise.trim() || !form.protagonist.trim() ? "补全书名、故事前提和主角" : "信息完整，可以创建";
  return <Modal title="新建小说" subtitle="没有题材、书名或主角也能开始；可快速生成一个方向，也可比较三个方案。" onClose={onClose}><form className="dialog-form create-project-form" onSubmit={create}>
    <ol className="create-steps"><li className={root ? "done" : "active"}><span>1</span>选择文件夹</li><li className={mode === "manual" || selectedIdeaIndex !== null ? "done" : root ? "active" : ""}><span>2</span>确定故事方向</li><li className={canCreate ? "active" : ""}><span>3</span>确认创建</li></ol>
    <label>项目文件夹<div className="path-picker"><input value={root} readOnly placeholder="选择保存小说的文件夹" /><button type="button" onClick={async () => { const value = await window.inkflow.chooseFolder("选择小说项目文件夹"); if (value) setRoot(value); }}>选择</button></div></label>
    <div className="mode-switch"><button className={mode === "ai" ? "active" : ""} type="button" onClick={() => setMode("ai")}>我没有想法，AI 来构思</button><button className={mode === "manual" ? "active" : ""} type="button" onClick={() => { setMode("manual"); setSelectedIdeaIndex(null); }}>我自己填写</button></div>
    {mode === "ai" && <section className="idea-studio"><label>可选偏好 <small>完全没想法就留空</small><textarea value={preferences} onChange={(event) => setPreferences(event.target.value)} placeholder="例如：女频、爽文、无脑，从×××开始；也可以留空。" /></label><div className="idea-actions"><button className="primary" type="button" disabled={working} onClick={() => void ideate(true)}>快速生成一个</button><button type="button" disabled={working} onClick={() => void ideate(false)}>生成三个供比较</button></div>{ideaStage && <p className={`stage-note ${working ? "working" : ""}`}>{working && <span />} {ideaStage}</p>}{ideas.length > 0 && <div className={`idea-grid ${ideas.length === 1 ? "single" : ""}`}>{ideas.map((idea, index) => <article key={`${index}-${idea.concept_id}`} className={selectedIdeaIndex === index ? "selected" : ""}><span>{idea.genre}</span><strong>{idea.title}</strong><p>{idea.premise}</p><small>开篇抓手：{idea.opening_hook}</small><small>长线动力：{idea.long_term_engine}</small><em>{idea.choice_note}</em><button className={selectedIdeaIndex === index ? "selected-action" : ""} type="button" onClick={() => chooseIdea(idea, index)}>{selectedIdeaIndex === index ? "✓ 已采用这个方案" : "采用这个方案"}</button></article>)}</div>}{ideaReasoning.length > 0 && <details className="public-reasoning" open><summary>写作角色的公开判断说明</summary><ul>{ideaReasoning.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}<p className="form-hint">方案只用于填写建项信息；你仍可修改书名和细节。点击“确认创建小说”后才写入本地文件，不会自动生成正文。</p></section>}
    {(mode === "manual" || selectedIdeaIndex !== null) && <div ref={detailsRef} className="creation-details">
    <div className="form-grid"><label>书名<input required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label><label>题材<input required value={form.genre} onChange={(e) => setForm({ ...form, genre: e.target.value })} /></label></div>
    <label>一句话故事前提<textarea required minLength={10} value={form.premise} onChange={(e) => setForm({ ...form, premise: e.target.value })} placeholder="谁，因为哪件事，必须做什么；最大的阻力是什么。" /></label>
    <label>主角<input required value={form.protagonist} onChange={(e) => setForm({ ...form, protagonist: e.target.value })} /></label>
    <div className="form-grid three"><label>单章字数<input type="number" min={500} max={20000} value={form.target_chapter_words} onChange={(e) => setForm({ ...form, target_chapter_words: Number(e.target.value) })} /></label><label>预计章节<input type="number" min={10} value={form.estimated_chapters} onChange={(e) => setForm({ ...form, estimated_chapters: Number(e.target.value) })} /></label><label>预计卷数<input type="number" min={1} value={form.estimated_volumes} onChange={(e) => setForm({ ...form, estimated_volumes: Number(e.target.value) })} /></label></div>
    </div>}
    {error && <p className="form-error">{error}</p>}<div className="dialog-actions creation-actions"><span className={canCreate ? "ready" : ""}>{createHint}</span><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={working || !canCreate} type="submit">{working ? "正在创建…" : "确认创建小说"}</button></div>
  </form></Modal>;
}

const SETTINGS_PRESETS = [
  { id: "balanced", name: "专业均衡", note: "适合多数长篇项目", reasoning_effort: "high", inquiry_frequency: "medium", context_soft_tokens: 256000, context_hard_tokens: 512000 },
  { id: "stable", name: "稳健连载", note: "更重视确认和前后连续", reasoning_effort: "high", inquiry_frequency: "high", context_soft_tokens: 384000, context_hard_tokens: 512000 },
  { id: "explore", name: "灵感探索", note: "给构思保留更大空间", reasoning_effort: "max", inquiry_frequency: "medium", context_soft_tokens: 384000, context_hard_tokens: 512000 },
  { id: "economy", name: "节省模式", note: "减少上下文与非必要询问", reasoning_effort: "medium", inquiry_frequency: "low", context_soft_tokens: 128000, context_hard_tokens: 256000 },
] as const;

function SettingsDialog({ provider, onClose, onSaved }: { provider: Record<string, unknown> | null; onClose: () => void; onSaved: (value: Record<string, unknown>) => void }) {
  const [form, setForm] = useState({ api_key: "", base_url: String(provider?.base_url || "https://api.deepseek.com"), model: String(provider?.model || "deepseek-v4-flash"), reasoning_effort: String(provider?.reasoning_effort || "high"), inquiry_frequency: String(provider?.inquiry_frequency || "medium"), context_soft_tokens: Number(provider?.context_soft_tokens || 256000), context_hard_tokens: Number(provider?.context_hard_tokens || 512000), max_output_tokens: Number(provider?.max_output_tokens || 16000) });
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [stage, setStage] = useState("");
  const [result, setResult] = useState("");
  const persist = async (testConnection: boolean) => {
    setWorking(true); setError(""); setResult(""); setStage("正在保存模型设置…");
    let testRunId = "";
    try {
      const saved = await withDeadline(window.inkflow.request<Record<string, unknown>>("provider.configure", form), 15000, "本机保存没有及时返回。请重新打开设置核对保存结果后再试。");
      setForm((value) => ({ ...value, api_key: "" }));
      onSaved({ ...provider, ...saved });
      if (!testConnection) { setResult("设置已保存，可以开始对话。"); return; }
      setResult("设置已保存；现在开始检查模型连接。");
      setStage("保存完成，正在直接询问模型；通常约 5～15 秒…");
      testRunId = `provider-test-${crypto.randomUUID()}`;
      const request = window.inkflow.request<{ message: string; reply: string; public_reasoning_summary: string }>("provider.test", { run_id: testRunId });
      const checked = await withDeadline(request, 75000, "连接检查没有及时返回，设置已保存。可以关闭窗口，稍后重试连接。");
      setResult(`${checked.message}\n${checked.reply}\n公开判断：${checked.public_reasoning_summary}`);
    } catch (cause) {
      if (testRunId) void window.inkflow.request("run.cancel", { run_id: testRunId }).catch(() => undefined);
      setError(errorMessage(cause));
    } finally { setWorking(false); setStage(""); }
  };
  const activePreset = SETTINGS_PRESETS.find((preset) => preset.reasoning_effort === form.reasoning_effort && preset.inquiry_frequency === form.inquiry_frequency && preset.context_soft_tokens === form.context_soft_tokens && preset.context_hard_tokens === form.context_hard_tokens)?.id;
  return <Modal title="模型与上下文" subtitle="不懂参数时先选一个创作预设；也可以继续逐项调整。" onClose={onClose}><form className="dialog-form" onSubmit={(event) => { event.preventDefault(); void persist(false); }}>
    <div className={`provider-status ${provider?.api_key_configured ? "ready" : "missing"}`}><strong>{provider?.api_key_configured ? "模型密钥已保存" : "尚未保存模型密钥"}</strong><span>{provider?.api_key_configured ? `凭据来源：${credentialLabel(String(provider?.api_key_storage || ""))}` : "请输入一次，保存后界面不会再显示原文。"}</span></div>
    <section className="settings-presets"><div><strong>创作预设</strong><small>选中后仍可手动微调</small></div><div className="preset-grid">{SETTINGS_PRESETS.map((preset) => <button type="button" key={preset.id} className={activePreset === preset.id ? "active" : ""} onClick={() => setForm((value) => ({ ...value, reasoning_effort: preset.reasoning_effort, inquiry_frequency: preset.inquiry_frequency, context_soft_tokens: preset.context_soft_tokens, context_hard_tokens: preset.context_hard_tokens }))}><strong>{activePreset === preset.id ? "✓ " : ""}{preset.name}</strong><span>{preset.note}</span><small>常用 {preset.context_soft_tokens / 10000} 万 · 最大 {preset.context_hard_tokens / 10000} 万</small></button>)}</div></section>
    <label>更换模型密钥 <small>{provider?.api_key_configured ? "留空表示继续使用已有密钥" : "首次使用必须填写"}</small><input type="password" autoComplete="new-password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder="密钥只发送给本机引擎" /></label>
    <label>接口地址<input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></label>
    <label>模型名称<input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} /></label>
    <div className="form-grid"><label>思考强度<select value={form.reasoning_effort} onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="max">最高</option></select></label><label>单次输出上限<input type="number" readOnly value={form.max_output_tokens} /></label></div>
    <label>主动询问频率<select value={form.inquiry_frequency} onChange={(e) => setForm({ ...form, inquiry_frequency: e.target.value })}><option value="low">低：只问执行必需信息</option><option value="medium">中：理解把握较低时询问</option><option value="high">高：重要创作分岔也询问</option><option value="ultra">超高：有明显未知项就先询问</option></select></label>
    <div className="form-grid"><label>常用上下文<input type="number" min={16000} max={512000} value={form.context_soft_tokens} onChange={(e) => setForm({ ...form, context_soft_tokens: Number(e.target.value) })} /></label><label>最大上下文<input type="number" min={16000} max={1000000} value={form.context_hard_tokens} onChange={(e) => setForm({ ...form, context_hard_tokens: Number(e.target.value) })} /></label></div>
    <p className="form-hint">墨流会把多个来源去重后编译成一个上下文包；常用和最大数值是预算上限，不代表每次都塞满。单次输出上限固定为 1.6 万。</p>{stage && <p className="stage-note working"><span /> {stage}</p>}{result && <p className="form-success">✓ {result}</p>}{error && <p className="form-error">{error}</p>}<div className="dialog-actions"><button type="button" onClick={onClose}>关闭</button><button type="submit" disabled={working}>仅保存</button><button className="primary" type="button" disabled={working || (!provider?.api_key_configured && !form.api_key)} onClick={() => void persist(true)}>{working ? (stage.includes("询问模型") ? "已保存，正在测试…" : "正在保存…") : "保存并测试连接"}</button></div>
  </form></Modal>;
}

function SearchDialog({ request, result, setResult, onClose, onOpen }: { request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; result: Record<string, unknown> | null; setResult: (value: Record<string, unknown> | null) => void; onClose: () => void; onOpen: (path: string) => void }) {
  const [query, setQuery] = useState("");
  const matches = (result?.matches || []) as Array<Record<string, unknown>>;
  return <Modal title="搜索整本小说" subtitle="搜索设定、规划、正文和审查报告，不读取 .inkflow 内部库。" onClose={onClose}><form className="search-form" onSubmit={async (event) => { event.preventDefault(); if (query.trim()) setResult(await request("document.search", { query })); }}><input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder="人物、地点、伏笔或一句原文" /><button className="primary">搜索</button></form><div className="search-results">{matches.map((item, index) => <button key={index} onClick={() => onOpen(String(item.relative_path))}><strong>{String(item.relative_path)} · 第 {String(item.line)} 行</strong><p>{String(item.preview)}</p></button>)}{result && matches.length === 0 && <p className="empty-mini">没有找到匹配内容。</p>}</div></Modal>;
}

function QuestionDialog({ questions, onClose, onSubmit }: { questions: QuestionCard[]; onClose: () => void; onSubmit: (answer: string) => void }) {
  const [answers, setAnswers] = useState<Record<string, string[]>>({});
  const [otherText, setOtherText] = useState<Record<string, string>>({});
  const choose = (question: QuestionCard, option: QuestionOption) => {
    setAnswers((current) => {
      const selected = current[question.id] || [];
      if (question.selection === "single") return { ...current, [question.id]: [option.id] };
      return { ...current, [question.id]: selected.includes(option.id) ? selected.filter((id) => id !== option.id) : [...selected, option.id] };
    });
  };
  const isComplete = questions.every((question) => {
    const selected = answers[question.id] || [];
    if (selected.length === 0) return false;
    const other = question.options.find((option) => option.kind === "other");
    return !other || !selected.includes(other.id) || Boolean(otherText[question.id]?.trim());
  });
  const submit = () => {
    if (!isComplete) return;
    const lines = ["我来回答刚才的问题："];
    questions.forEach((question, index) => {
      const selected = answers[question.id] || [];
      const labels = question.options.filter((option) => selected.includes(option.id)).map((option) => option.kind === "other" ? `其他：${otherText[question.id].trim()}` : option.label);
      lines.push(`${index + 1}. ${question.question}`);
      lines.push(`我的回答：${labels.join("；")}`);
    });
    lines.push("请结合这些答案继续理解原来的目标；如果此前已经明确要求执行且信息足够，就继续原任务，否则先总结你理解到的方案。");
    onSubmit(lines.join("\n"));
  };
  return <div className="modal-backdrop question-backdrop"><section className="modal question-dialog" role="dialog" aria-modal="true" aria-labelledby="question-title"><button className="modal-close" onClick={onClose}>×</button><p className="eyebrow">墨流想先弄清楚</p><h2 id="question-title">回答几个关键选择</h2><p className="modal-subtitle">没有标准答案。选择最接近的方向，或使用每题最后的“其他”自己填写。</p><div className="question-list">{questions.map((question, index) => <fieldset className="question-card" key={question.id}><legend><span>{index + 1}</span><div><small>{question.header}</small><strong>{question.question}</strong></div></legend>{question.why_it_matters && <p className="question-why">为什么问：{question.why_it_matters}</p>}<div className="question-options">{question.options.map((option) => { const checked = (answers[question.id] || []).includes(option.id); return <div className={`question-option ${checked ? "selected" : ""}`} key={option.id}><label><input type={question.selection === "multiple" ? "checkbox" : "radio"} name={question.id} checked={checked} onChange={() => choose(question, option)} /><span><strong>{option.label}{option.recommended ? " · 建议" : ""}</strong>{option.description && <small>{option.description}</small>}</span></label>{option.kind === "other" && checked && <textarea autoFocus value={otherText[question.id] || ""} onChange={(event) => setOtherText((value) => ({ ...value, [question.id]: event.target.value }))} placeholder="用自己的话回答，越具体越容易贴近你的想法。" />}</div>; })}</div>{question.selection === "multiple" && <small className="multiple-hint">这一题可以选择多个答案。</small>}</fieldset>)}</div><div className="dialog-actions"><button onClick={onClose}>暂不回答</button><button className="primary" disabled={!isComplete} onClick={submit}>提交回答并继续</button></div></section></div>;
}

function UpdateDialog({ info, onClose }: { info: UpdateInfo; onClose: () => void }) {
  const [working, setWorking] = useState(false);
  const [local, setLocal] = useState<UpdateInfo>(info);
  useEffect(() => setLocal(info), [info]);
  const action = async (kind: "check" | "download" | "install") => {
    setWorking(true);
    try {
      const next = kind === "check" ? await window.inkflow.checkUpdate() : kind === "download" ? await window.inkflow.downloadUpdate() : await window.inkflow.installUpdate();
      setLocal(next as UpdateInfo);
    } finally { setWorking(false); }
  };
  const sourceLabel = local.source === "embedded" ? "发布包内置更新源" : local.source === "environment" ? "自定义公开更新源" : "尚未配置";
  return <Modal title="软件更新" subtitle="新版会下载后在重启时安装；小说正文、正史数据库和本地项目不会被删除。" onClose={onClose}><section className={`update-card ${local.status || "ready"}`}><div><small>当前版本</small><strong>{local.currentVersion || "0.3.1"}</strong></div><div><small>可用版本</small><strong>{local.availableVersion || "—"}</strong></div><div><small>更新来源</small><strong>{sourceLabel}</strong></div>{typeof local.progress === "number" && <div className="update-progress"><span style={{ width: `${Math.max(0, Math.min(local.progress, 100))}%` }} /></div>}<p>{local.message || "可以检查是否有新版本。"}</p></section>{local.status === "not_configured" && <p className="form-hint">私密仓库的下载需要账号令牌，不适合写进大众软件。仓库或独立发布仓库公开后，只需在构建时配置发布源即可启用在线更新。</p>}<div className="dialog-actions"><button onClick={onClose}>关闭</button>{!new Set(["available", "downloading", "downloaded"]).has(String(local.status)) && <button className="primary" disabled={working || local.status === "not_configured" || local.status === "checking"} onClick={() => void action("check")}>{local.status === "checking" ? "正在检查…" : "检查新版本"}</button>}{local.status === "available" && <button className="primary" disabled={working} onClick={() => void action("download")}>下载更新</button>}{local.status === "downloading" && <button className="primary" disabled>正在下载 {Math.round(Number(local.progress || 0))}%</button>}{local.status === "downloaded" && <button className="primary" disabled={working} onClick={() => void action("install")}>重启并安装</button>}</div></Modal>;
}

function Modal({ title, subtitle, onClose, children }: { title: string; subtitle: string; onClose: () => void; children: ReactNode }) { return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="modal"><button className="modal-close" onClick={onClose}>×</button><p className="eyebrow">墨流 0.3</p><h2>{title}</h2><p className="modal-subtitle">{subtitle}</p>{children}</section></div>; }
function Toast({ kind, text, onClose }: { kind: "error" | "info"; text: string; onClose: () => void }) { return <div className={`toast ${kind}`}><span>{kind === "error" ? "!" : "i"}</span><p>{text}</p><button onClick={onClose}>×</button></div>; }
function EmptyPanel({ title, text }: { title: string; text: string }) { return <div className="empty-panel"><div>◇</div><h2>{title}</h2><p>{text}</p></div>; }
function InfoCard({ label, value, accent = false }: { label: string; value: unknown; accent?: boolean }) { return <article className={`info-card ${accent ? "accent" : ""}`}><small>{label}</small><p>{String(value || "尚未设置")}</p></article>; }

function visibleResult(result: unknown): { summary: string; details?: string } {
  if (typeof result === "string") return { summary: result };
  if (!result || typeof result !== "object") return { summary: "任务完成，可以从右侧工作台查看结果。" };
  const value = result as Record<string, unknown>;
  if (value.reply) {
    const session = value.session && typeof value.session === "object" ? value.session as Record<string, unknown> : null;
    const reason = session?.visible_reason ? `\n\n公开判断：${String(session.visible_reason)}` : "";
    return { summary: `${String(value.reply)}${reason}` };
  }
  if (value.help) return { summary: String(value.help) };
  for (const key of ["message", "summary", "gate", "next_action"]) if (value[key]) return { summary: String(value[key]), details: JSON.stringify(value, null, 2) };
  if (value.result && typeof value.result === "object") {
    const nested = visibleResult(value.result);
    return { summary: nested.summary, details: JSON.stringify(value, null, 2) };
  }
  return { summary: "工作流已返回结果，请查看右侧文件与过程面板。", details: JSON.stringify(value, null, 2) };
}
function errorMessage(cause: unknown): string { return cause instanceof Error ? cause.message : String(cause); }
async function withDeadline<T>(request: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([request, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); })]); }
  finally { if (timer) clearTimeout(timer); }
}
function currentChapter(path?: string): number | null { const match = path?.match(/chapter_(\d+)/); return match ? Number(match[1]) : null; }
function tabLabel(tab: Tab): string { return ({ project: "项目", editor: "正文", chapter: "章工位", review: "审查", memory: "记忆", references: "参考", process: "计划" })[tab]; }
function eventLabel(value?: string): string { return ({ "run.started": "任务开始", "run.completed": "任务完成", "run.failed": "任务失败", "run.cancelled": "任务已停止", "workflow.started": "工作流启动", "workflow.completed": "工作流完成", "controller.routing": "理解与路由", "writer.started": "写作角色构思", "writer.completed": "写作角色完成", "provider.testing": "模型连接" } as Record<string, string>)[value || ""] || value || "过程"; }
function credentialLabel(value: string): string { return ({ windows_credential_manager: "Windows 凭据库", "environment:INKFLOW_API_KEY": "系统环境变量", "environment:DEEPSEEK_API_KEY": "DeepSeek 环境变量" } as Record<string, string>)[value] || "本机安全存储"; }
function methodLabel(value?: string): string { return ({ "conversation.send": "自然对话", "workflow.run": "小说工作流", "project.ideate": "从零构思", "provider.test": "模型连接测试", "reference.search": "搜索公开写作资料", "reference.fetch": "抓取参考资料", "reference.analyze": "分析参考资料" } as Record<string, string>)[value || ""] || "墨流任务"; }
function processRuns(events: EngineEvent[]) {
  const visibleMethods = new Set(["conversation.send", "workflow.run", "project.ideate", "provider.test", "reference.search", "reference.fetch", "reference.analyze"]);
  const groups = new Map<string, EngineEvent[]>();
  for (const event of events) {
    const id = event.run_id || "unknown";
    const current = groups.get(id) || [];
    current.push(event);
    groups.set(id, current);
  }
  return [...groups.entries()].map(([id, steps]) => {
    const method = steps.find((item) => item.method)?.method;
    const failed = steps.some((item) => item.type?.includes("failed"));
    const done = steps.some((item) => item.type === "run.completed");
    const cancelled = steps.some((item) => item.type === "run.cancelled");
    const summary = [...steps].reverse().find((item) => item.summary && !["任务已完成", "任务已进入墨流"].includes(item.summary))?.summary || (done ? "任务已经完成。" : "任务正在执行。");
    const visibleSteps = steps.filter((item) => !["run.started", "run.completed"].includes(item.type || ""));
    const action = steps.find((item) => item.action)?.action;
    return { id, steps: visibleSteps, method, action, status: failed ? "failed" : cancelled ? "cancelled" : done ? "done" : "running", summary, finishedAt: failed || cancelled || done ? [...steps].reverse().find((item) => item.timestamp)?.timestamp : undefined };
  }).filter((run) => !["checkpoint_list", "rollback_preview"].includes(run.action || "") && (visibleMethods.has(run.method || "") || run.steps.some((item) => ["controller.routing", "workflow.started", "writer.started"].includes(item.type || "")))).reverse();
}
function statusLabel(value: string): string { return ({ open: "待处理", resolved: "已处理", dismissed: "已忽略", orphaned: "原文已变化" } as Record<string, string>)[value] || value; }
function kindLabel(value: string): string { return ({ character: "人物", location: "地点", organization: "组织", item: "物品", lore: "世界观", style: "文风" } as Record<string, string>)[value] || value; }
function stringifyShort(value: unknown): string { return typeof value === "string" ? value : JSON.stringify(value, null, 0); }
function formatTime(value: string): string { const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN", { hour12: false }); }
function referenceTitle(value: Record<string, unknown>): string { const source = String(value.source || ""); try { const url = new URL(source); return url.hostname; } catch { return source.split(/[\\/]/).pop() || String(value.reference_id); } }

function loadRecentProjects(): RecentProject[] {
  try {
    const value = JSON.parse(localStorage.getItem("inkflow.recentProjects.v1") || "[]") as unknown;
    if (!Array.isArray(value)) return [];
    return value
      .filter((item): item is RecentProject => Boolean(item && typeof item === "object" && "root" in item && "title" in item))
      .slice(0, 5);
  } catch {
    return [];
  }
}

function rememberRecentProject(items: RecentProject[], root: string, title: string): RecentProject[] {
  const next = [
    { root, title, openedAt: new Date().toISOString() },
    ...items.filter((item) => item.root.toLocaleLowerCase() !== root.toLocaleLowerCase()),
  ].slice(0, 5);
  localStorage.setItem("inkflow.recentProjects.v1", JSON.stringify(next));
  return next;
}

export default App;
