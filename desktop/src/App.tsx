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
type Tab = "project" | "editor" | "chapter" | "review" | "memory" | "references" | "process";
type RecentProject = { root: string; title: string; openedAt: string };

const emptyStatistics: Statistics = {
  characters: 0,
  paragraphs: 0,
  dialogue_ratio: 0,
  estimated_reading_minutes: 0,
};

function App() {
  const [appInfo, setAppInfo] = useState<Record<string, unknown> | null>(null);
  const [provider, setProvider] = useState<Record<string, unknown> | null>(null);
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
      text: "告诉我你想写什么，或打开一本已有小说。我会先理解目标，再安排 Writer、Reviewer 和 Memory Keeper。",
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
  const [showSearch, setShowSearch] = useState(false);
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
    ])
      .then(([info, status, launch]) => {
        setAppInfo(info);
        setProvider(status);
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
    return () => {
      removeEvent();
      removeStatus();
      removeOpenProject();
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
    const comment = window.prompt("你希望 Writer 如何处理这段内容？");
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
      setNotice("批注已记录。它不会直接改正文，可在自然对话中要求 Writer 按批注修订。");
      setMascotMood("success");
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
    }
  };

  const sendChat = async (event?: FormEvent) => {
    event?.preventDefault();
    const message = chatInput.trim();
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
    if (action === "accept" && !window.confirm("验收会让 Memory Keeper 将本章提交为正史。确认继续吗？")) {
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
          <p className="lede">自然对话负责理解你；Writer、Reviewer 与 Memory Keeper 各守边界。规划、正文、证据和回退都留在你自己的电脑。</p>
          <div className="welcome-actions">
            <button className="primary" onClick={() => setShowCreate(true)}>新建小说</button>
            <button onClick={openFolder}>打开项目</button>
          </div>
          <div className="welcome-meta">
            <span>版本 {String(appInfo?.version || "0.2.0")}</span>
            <span>{provider?.api_key_configured ? "模型已配置" : "尚未配置模型 Key"}</span>
            <button className="text-button" onClick={() => setShowSettings(true)}>模型设置</button>
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
          <button onClick={() => setShowSearch(true)}>⌕ 搜索</button>
          <button onClick={() => void refresh()}>↻ 刷新</button>
          <button onClick={() => void window.inkflow.openPath(projectRoot)}>打开文件夹</button>
          <button onClick={() => setShowSettings(true)}>设置</button>
        </nav>
      </header>

      <main className="workspace-grid">
        <aside className="left-rail">
          <div className="rail-heading"><span>小说结构</span><button title="换一本小说" onClick={openFolder}>＋</button></div>
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
              <span className="agent-boundary">3 Agent · 边界开启</span>
            </div>
          </div>
          <div className="quick-row">
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
            {busy && <article className="message assistant pending"><span className="avatar">墨</span><div><p>正在梳理约束并执行工作流……</p><small>过程面板会显示阶段与工具状态，不展示模型原始思维链。</small></div></article>}
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
              onError={(message) => { setError(message); setMascotMood("rest"); }}
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
      {showSearch && <SearchDialog request={request} result={searchResult} setResult={setSearchResult} onClose={() => setShowSearch(false)} onOpen={async (relativePath) => {
        const item = [...(tree?.items || []), ...(tree?.groups.flatMap((group) => group.items) || [])].find((entry) => entry.relative_path === relativePath);
        if (item) await openDocument(item);
        setShowSearch(false);
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
    <div className="section-heading"><p className="eyebrow">Reviewer · 只审不改</p><h2>证据化审查</h2><p>每个扣分项都应指向正文、章节卡或正史依据；旧审查不能批准新版本。</p></div>
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
    <div className="section-heading"><p className="eyebrow">Memory Keeper · 验收后提交</p><h2>正史与故事圣经</h2><button onClick={() => setShowAdd(!showAdd)}>＋ 手动圣经条目</button></div>
    {showAdd && <div className="inline-form"><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="character">人物</option><option value="location">地点</option><option value="organization">组织</option><option value="item">物品</option><option value="lore">世界观</option><option value="style">文风</option></select><input value={name} onChange={(event) => setName(event.target.value)} placeholder="名称" /><textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="稳定设定、声线、禁忌或说明" /><button className="primary" onClick={() => void save()}>保存</button></div>}
    <section className="stack-section"><h3>人工故事圣经 <small>{dashboard?.bible_entries.length || 0}</small></h3>{dashboard?.bible_entries.map((entry) => <article className="memory-card" key={String(entry.entry_id)}><span>{kindLabel(String(entry.kind))}</span><strong>{String(entry.name)}</strong><p>{String(((entry.data || {}) as Record<string, unknown>).notes || "")}</p></article>)}</section>
    <section className="stack-section"><h3>当前正史事实 <small>{dashboard?.facts.length || 0}</small></h3>{dashboard?.facts.slice(0, 30).map((fact) => <article className="fact-row" key={String(fact.fact_id)}><strong>{String(fact.subject)} · {String(fact.predicate)}</strong><p>{stringifyShort(fact.value)}</p><small>来源第 {String(fact.source_chapter)} 章</small></article>)}</section>
    <section className="stack-section"><h3>开放线索 <small>{dashboard?.threads.length || 0}</small></h3>{dashboard?.threads.map((thread) => <article className="thread-card" key={String(thread.thread_id)}><strong>{String(thread.title)}</strong><p>{String(thread.description)}</p><small>{String(thread.status)} · 预计第 {String(thread.due_chapter || "—")} 章</small></article>)}</section>
  </div>;
}

function ProcessPanel({ events }: { events: EngineEvent[] }) {
  return <div className="scroll-panel process-panel"><div className="section-heading"><p className="eyebrow">可复核过程</p><h2>工作流时间线</h2><p>这里只显示约束、路由、工具状态和结果摘要，不保存供应商原始思维链。</p></div><div className="timeline">{events.length === 0 && <p className="empty-mini">执行一次规划、写作或审查后，这里会出现过程。</p>}{[...events].reverse().map((event, index) => <article key={`${event.run_id}-${index}`}><span className={`timeline-dot ${event.type?.includes("failed") ? "failed" : event.type?.includes("completed") ? "done" : ""}`} /><div><strong>{eventLabel(event.type)}</strong><p>{event.summary || event.method || event.action || "任务状态变化"}</p><small>{event.run_id} · {event.timestamp ? formatTime(event.timestamp) : ""}</small></div></article>)}</div></div>;
}

function ReferencesPanel({ request }: { request: <T>(method: string, params?: Record<string, unknown>) => Promise<T> }) {
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]);
  const [url, setUrl] = useState("");
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
  const importFile = async () => {
    const source = await window.inkflow.chooseFile("导入你有权使用的 TXT 或 Markdown 参考资料");
    if (!source) return;
    setWorking(true); setError("");
    try { await request("reference.import", { source_path: source }); await load(); }
    catch (cause) { setError(errorMessage(cause)); }
    finally { setWorking(false); }
  };
  return <div className="scroll-panel references-panel">
    <div className="section-heading"><p className="eyebrow">Reference Lab · 结构学习</p><h2>参考资料与爆款拆解</h2><p>导入公开网页或本地文本后，墨流先提取节奏、段落、对白和章末样本等特征；不会把整本参考正文无差别塞进写作上下文。</p></div>
    <div className="reference-import"><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="公开网页链接；番茄公开页会自动使用专用适配器" /><button disabled={working || !url.trim()} onClick={() => void fetchUrl()}>抓取公开页</button><button disabled={working} onClick={() => void importFile()}>导入本地 TXT/MD</button></div>
    {error && <p className="form-error">{error}</p>}
    {feature && <article className="feature-sheet"><div><p className="eyebrow">确定性特征卡</p><h3>{String(feature.reference_id)}</h3></div><dl><dt>总字符</dt><dd>{Number(feature.characters || 0).toLocaleString()}</dd><dt>识别章节</dt><dd>{String(feature.detected_chapters || 0)}</dd><dt>平均段落</dt><dd>{String(feature.average_paragraph_chars || 0)} 字</dd><dt>平均句长</dt><dd>{String(feature.average_sentence_chars || 0)} 字</dd><dt>对白比例</dt><dd>{Math.round(Number(feature.dialogue_ratio || 0) * 100)}%</dd></dl><p>{String(feature.note || "")}</p></article>}
    <section className="stack-section"><h3>资料库 <small>{items.length}</small></h3>{items.length === 0 && <p className="empty-mini">尚未导入资料。你也可以在自然对话里请墨流分析某个公开链接。</p>}{items.map((item) => <article className="reference-card" key={String(item.reference_id)}><div><strong>{referenceTitle(item)}</strong><small>{String(item.source_type)} · {formatTime(String(item.imported_at || ""))}</small><p>{String(item.source)}</p></div><span>{item.analyzed ? "已分析" : "待分析"}</span><button disabled={working} onClick={async () => { setWorking(true); setError(""); try { setFeature(await request<Record<string, unknown>>("reference.analyze", { reference_id: item.reference_id })); await load(); } catch (cause) { setError(errorMessage(cause)); } finally { setWorking(false); } }}>生成特征卡</button></article>)}</section>
  </div>;
}

function CreateProject({ onClose, onCreated }: { onClose: () => void; onCreated: (root: string) => Promise<void> }) {
  const [root, setRoot] = useState("");
  const [form, setForm] = useState({ title: "", genre: "都市悬疑", premise: "", protagonist: "", target_chapter_words: 3000, estimated_chapters: 200, estimated_volumes: 6 });
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const create = async (event: FormEvent) => {
    event.preventDefault(); setWorking(true); setError("");
    try {
      await window.inkflow.request("project.create", { project_root: root, brief: { ...form, target_audience: "中文网文读者", core_selling_point: "", user_rules: [] } });
      onClose(); await onCreated(root);
    } catch (cause) { setError(errorMessage(cause)); } finally { setWorking(false); }
  };
  return <Modal title="新建小说" subtitle="先定最少的书籍契约，细节可以在对话中继续商量。" onClose={onClose}><form className="dialog-form" onSubmit={create}>
    <label>项目文件夹<div className="path-picker"><input value={root} readOnly placeholder="选择一个空文件夹" /><button type="button" onClick={async () => { const value = await window.inkflow.chooseFolder("选择小说项目文件夹"); if (value) setRoot(value); }}>选择</button></div></label>
    <div className="form-grid"><label>书名<input required value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label><label>题材<input required value={form.genre} onChange={(e) => setForm({ ...form, genre: e.target.value })} /></label></div>
    <label>一句话故事前提<textarea required minLength={10} value={form.premise} onChange={(e) => setForm({ ...form, premise: e.target.value })} placeholder="谁，因为哪件事，必须做什么；最大的阻力是什么。" /></label>
    <label>主角<input required value={form.protagonist} onChange={(e) => setForm({ ...form, protagonist: e.target.value })} /></label>
    <div className="form-grid three"><label>单章字数<input type="number" min={500} max={20000} value={form.target_chapter_words} onChange={(e) => setForm({ ...form, target_chapter_words: Number(e.target.value) })} /></label><label>预计章节<input type="number" min={10} value={form.estimated_chapters} onChange={(e) => setForm({ ...form, estimated_chapters: Number(e.target.value) })} /></label><label>预计卷数<input type="number" min={1} value={form.estimated_volumes} onChange={(e) => setForm({ ...form, estimated_volumes: Number(e.target.value) })} /></label></div>
    {error && <p className="form-error">{error}</p>}<div className="dialog-actions"><button type="button" onClick={onClose}>取消</button><button className="primary" disabled={working || !root} type="submit">{working ? "正在创建…" : "创建小说"}</button></div>
  </form></Modal>;
}

function SettingsDialog({ provider, onClose, onSaved }: { provider: Record<string, unknown> | null; onClose: () => void; onSaved: (value: Record<string, unknown>) => void }) {
  const [form, setForm] = useState({ api_key: "", base_url: String(provider?.base_url || "https://api.deepseek.com"), model: String(provider?.model || "deepseek-v4-flash"), reasoning_effort: String(provider?.reasoning_effort || "high"), context_soft_tokens: Number(provider?.context_soft_tokens || 256000), context_hard_tokens: Number(provider?.context_hard_tokens || 512000), max_output_tokens: 16000 });
  const [error, setError] = useState("");
  const save = async (event: FormEvent) => { event.preventDefault(); try { const value = await window.inkflow.request<Record<string, unknown>>("provider.configure", form); const status = await window.inkflow.request<Record<string, unknown>>("provider.status"); onSaved({ ...value, ...status }); onClose(); } catch (cause) { setError(errorMessage(cause)); } };
  return <Modal title="模型与上下文" subtitle="Key 只保存到 Windows 凭据库；下面其他设置不会写进小说项目。" onClose={onClose}><form className="dialog-form" onSubmit={save}>
    <label>API Key <small>{provider?.api_key_configured ? "已配置；留空表示不更换" : "尚未配置"}</small><input type="password" autoComplete="off" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} placeholder="不会写入文件或日志" /></label>
    <label>接口地址<input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></label>
    <label>模型名称<input value={form.model} onChange={(e) => setForm({ ...form, model: e.target.value })} /></label>
    <div className="form-grid"><label>思考强度<select value={form.reasoning_effort} onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="max">最高</option></select></label><label>单次输出上限<input type="number" readOnly value={form.max_output_tokens} /></label></div>
    <div className="form-grid"><label>常用上下文<input type="number" min={16000} max={512000} value={form.context_soft_tokens} onChange={(e) => setForm({ ...form, context_soft_tokens: Number(e.target.value) })} /></label><label>最大上下文<input type="number" min={16000} max={1000000} value={form.context_hard_tokens} onChange={(e) => setForm({ ...form, context_hard_tokens: Number(e.target.value) })} /></label></div>
    <p className="form-hint">墨流会从多个来源构建一个去重后的 Context Packet；设置 256K/512K 是上限，不代表每次都塞满。</p>{error && <p className="form-error">{error}</p>}<div className="dialog-actions"><button type="button" onClick={onClose}>取消</button><button className="primary" type="submit">保存设置</button></div>
  </form></Modal>;
}

function SearchDialog({ request, result, setResult, onClose, onOpen }: { request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; result: Record<string, unknown> | null; setResult: (value: Record<string, unknown> | null) => void; onClose: () => void; onOpen: (path: string) => void }) {
  const [query, setQuery] = useState("");
  const matches = (result?.matches || []) as Array<Record<string, unknown>>;
  return <Modal title="搜索整本小说" subtitle="搜索设定、规划、正文和审查报告，不读取 .inkflow 内部库。" onClose={onClose}><form className="search-form" onSubmit={async (event) => { event.preventDefault(); if (query.trim()) setResult(await request("document.search", { query })); }}><input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder="人物、地点、伏笔或一句原文" /><button className="primary">搜索</button></form><div className="search-results">{matches.map((item, index) => <button key={index} onClick={() => onOpen(String(item.relative_path))}><strong>{String(item.relative_path)} · 第 {String(item.line)} 行</strong><p>{String(item.preview)}</p></button>)}{result && matches.length === 0 && <p className="empty-mini">没有找到匹配内容。</p>}</div></Modal>;
}

function Modal({ title, subtitle, onClose, children }: { title: string; subtitle: string; onClose: () => void; children: ReactNode }) { return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="modal"><button className="modal-close" onClick={onClose}>×</button><p className="eyebrow">墨流 0.2</p><h2>{title}</h2><p className="modal-subtitle">{subtitle}</p>{children}</section></div>; }
function Toast({ kind, text, onClose }: { kind: "error" | "info"; text: string; onClose: () => void }) { return <div className={`toast ${kind}`}><span>{kind === "error" ? "!" : "i"}</span><p>{text}</p><button onClick={onClose}>×</button></div>; }
function EmptyPanel({ title, text }: { title: string; text: string }) { return <div className="empty-panel"><div>◇</div><h2>{title}</h2><p>{text}</p></div>; }
function InfoCard({ label, value, accent = false }: { label: string; value: unknown; accent?: boolean }) { return <article className={`info-card ${accent ? "accent" : ""}`}><small>{label}</small><p>{String(value || "尚未设置")}</p></article>; }

function visibleResult(result: unknown): { summary: string; details?: string } {
  if (typeof result === "string") return { summary: result };
  if (!result || typeof result !== "object") return { summary: "任务完成，可以从右侧工作台查看结果。" };
  const value = result as Record<string, unknown>;
  for (const key of ["message", "summary", "gate", "next_action"]) if (value[key]) return { summary: String(value[key]), details: JSON.stringify(value, null, 2) };
  if (value.result && typeof value.result === "object") {
    const nested = visibleResult(value.result);
    return { summary: nested.summary, details: JSON.stringify(value, null, 2) };
  }
  return { summary: "工作流已返回结果，请查看右侧文件与过程面板。", details: JSON.stringify(value, null, 2) };
}
function errorMessage(cause: unknown): string { return cause instanceof Error ? cause.message : String(cause); }
function currentChapter(path?: string): number | null { const match = path?.match(/chapter_(\d+)/); return match ? Number(match[1]) : null; }
function tabLabel(tab: Tab): string { return ({ project: "项目", editor: "正文", chapter: "章工位", review: "审查", memory: "记忆", references: "参考", process: "过程" })[tab]; }
function eventLabel(value?: string): string { return ({ "run.started": "任务开始", "run.completed": "任务完成", "run.failed": "任务失败", "workflow.started": "工作流启动", "workflow.completed": "工作流完成", "controller.routing": "理解与路由" } as Record<string, string>)[value || ""] || value || "过程"; }
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
