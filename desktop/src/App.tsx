import type { editor as MonacoEditor } from "monaco-editor";
import { FormEvent, lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, MutableRefObject, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { Mascot, mobaoIdlePoster } from "./Mascot";
import type { MascotMood } from "./Mascot";
import { ProjectCenter } from "./ProjectCenter";
import { VoiceCenter } from "./VoiceCenter";
import type { VoiceSettings, VoiceSource, VoiceStatus } from "./VoiceCenter";
import { LocalWavRecorder } from "./voiceRecording";

const Editor = lazy(() => import("./MonacoEditors").then((module) => ({ default: module.Editor })));
const DiffEditor = lazy(() => import("./MonacoEditors").then((module) => ({ default: module.DiffEditor })));

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
type Message = { id: string; role: "user" | "assistant" | "system"; text: string; details?: string; reasoning?: string[]; animate?: boolean; createdAt?: string };
type ConversationHistoryEntry = { id: string; user: string; assistant: string; action_note: string; recorded_at: string };
type PromptOptimizationResult = {
  original_prompt: string;
  optimized_prompt: string;
  change_summary: string[];
  preserved_constraints: string[];
  model: string;
};
type SelectionDraft = {
  relativePath: string;
  startOffset: number;
  endOffset: number;
  quote: string;
  expectedHash: string;
  isDraft: boolean;
  tooLong: boolean;
};
type QuestionOption = { id: string; label: string; description?: string; recommended?: boolean; kind?: "choice" | "other" };
type QuestionCard = { id: string; header: string; question: string; why_it_matters?: string; selection: "single" | "multiple"; options: QuestionOption[] };
type Tab = "project" | "editor" | "chapter" | "review" | "memory" | "references" | "listen" | "process";
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
type AgentRole = "coordinator" | "writer" | "reviewer" | "memory_keeper";
type AgentGeneration = { temperature: number; top_p: number; top_k: number | null };
type AgentGenerationProfiles = Record<AgentRole, AgentGeneration>;
type ContextStatus = {
  status: "idle" | "safe" | "watch" | "near_limit";
  estimated_tokens: number;
  before_compression_tokens: number;
  soft_limit_tokens: number;
  hard_limit_tokens: number;
  hard_usage_percent: number;
  compression_applied: boolean;
  hard_sections: Array<{ key: string; title: string; reason?: string; source_ids?: string[] }>;
  compressible_sections: Array<{ key: string; title: string; reason?: string; source_ids?: string[] }>;
  warnings: string[];
  budget_allocation?: Array<{ key: string; title: string; estimated_tokens: number; hard: boolean; source_count: number }>;
  retrieval_diagnostics?: { candidate_count?: number; initial_top_k?: number; selected?: unknown[]; discarded?: unknown[]; adaptive_factors?: Record<string, number> };
};
type CanonMigration = { required: boolean; confirmation_token: string; accepted_chapter_count: number; impact: string; backup_path?: string; unresolved_chapters?: number[]; message?: string };
type CollaborationMessage = { message_id: string; sender_role: string; recipient_role: string; message_type: string; claim: string; status: string; chapter_no?: number; chapter_version?: number; created_at: string };
type BatchSummary = { batch_id: string; status: string; start_chapter_no?: number; end_chapter_no?: number; chapters: Array<{ chapter_no?: number; version?: number; review_verdict?: string; memory_status?: string }> };
type LearningEvent = { event_id: string; event_type: string; chapter_no?: number; created_at: string; payload: Record<string, unknown> };
type CollaborationOverview = { messages: CollaborationMessage[]; threads?: Array<Record<string, unknown>>; tasks: Array<Record<string, unknown>>; batches: BatchSummary[]; learning_events: LearningEvent[]; artifacts?: Array<Record<string, unknown>>; usage?: { calls: number; prompt_tokens: number; completion_tokens: number; total_tokens: number; estimated_cost: number; currency: string; pricing_configured: boolean } };
type PrefillResult = { insertion: string; document_hash: string; cursor_offset: number; confidence: string };
type WorkspacePreset = "balanced" | "writing" | "planning" | "review";
type WorkspaceResizeTarget = "navigation" | "assistant" | "inspector";
type WorkspaceLayout = {
  navigationVisible: boolean;
  assistantVisible: boolean;
  inspectorVisible: boolean;
  assistantPosition: "left" | "right";
  navigationWidth: number;
  assistantWidth: number;
  inspectorWidth: number;
};

type ThemeMode = "dark" | "light" | "system";
type AccentPalette = "lime" | "jade" | "blue" | "violet" | "amber" | "rose";
type UiDensity = "comfortable" | "compact";
type PrefillLength = "short" | "medium" | "long";
type UiPreferences = {
  theme: ThemeMode;
  accent: AccentPalette;
  density: UiDensity;
  prefillEnabled: boolean;
  prefillDelayMs: number;
  prefillLength: PrefillLength;
};

const workspaceLayoutStorageKey = "inkflow.workspace-layout.v1";
const uiPreferencesStorageKey = "inkflow.ui-preferences.v1";
const defaultWorkspaceLayout: WorkspaceLayout = {
  navigationVisible: true,
  assistantVisible: true,
  inspectorVisible: true,
  assistantPosition: "left",
  navigationWidth: 246,
  assistantWidth: 430,
  inspectorWidth: 236,
};

const workspacePresets: Record<WorkspacePreset, WorkspaceLayout> = {
  balanced: defaultWorkspaceLayout,
  writing: { ...defaultWorkspaceLayout, navigationWidth: 218, assistantVisible: false, inspectorVisible: false },
  planning: { ...defaultWorkspaceLayout, navigationWidth: 260, assistantWidth: 500, inspectorVisible: false },
  review: { ...defaultWorkspaceLayout, navigationWidth: 226, assistantWidth: 390, assistantPosition: "right", inspectorVisible: true },
};

const defaultUiPreferences: UiPreferences = {
  theme: "dark",
  accent: "lime",
  density: "comfortable",
  prefillEnabled: false,
  prefillDelayMs: 900,
  prefillLength: "medium",
};

function clampWorkspaceWidth(target: WorkspaceResizeTarget, value: number) {
  const bounds: Record<WorkspaceResizeTarget, [number, number]> = {
    navigation: [180, 420],
    assistant: [320, 760],
    inspector: [170, 420],
  };
  const [minimum, maximum] = bounds[target];
  return Math.round(Math.max(minimum, Math.min(maximum, value)));
}

function loadWorkspaceLayout(): WorkspaceLayout {
  try {
    const stored = JSON.parse(localStorage.getItem(workspaceLayoutStorageKey) || "{}") as Partial<WorkspaceLayout>;
    return {
      navigationVisible: stored.navigationVisible !== false,
      assistantVisible: stored.assistantVisible !== false,
      inspectorVisible: stored.inspectorVisible !== false,
      assistantPosition: stored.assistantPosition === "right" ? "right" : "left",
      navigationWidth: clampWorkspaceWidth("navigation", Number(stored.navigationWidth) || defaultWorkspaceLayout.navigationWidth),
      assistantWidth: clampWorkspaceWidth("assistant", Number(stored.assistantWidth) || defaultWorkspaceLayout.assistantWidth),
      inspectorWidth: clampWorkspaceWidth("inspector", Number(stored.inspectorWidth) || defaultWorkspaceLayout.inspectorWidth),
    };
  } catch {
    return defaultWorkspaceLayout;
  }
}

function loadUiPreferences(): UiPreferences {
  try {
    const stored = JSON.parse(localStorage.getItem(uiPreferencesStorageKey) || "{}") as Partial<UiPreferences>;
    return {
      theme: ["dark", "light", "system"].includes(String(stored.theme)) ? stored.theme as ThemeMode : defaultUiPreferences.theme,
      accent: ["lime", "jade", "blue", "violet", "amber", "rose"].includes(String(stored.accent)) ? stored.accent as AccentPalette : defaultUiPreferences.accent,
      density: stored.density === "compact" ? "compact" : "comfortable",
      prefillEnabled: stored.prefillEnabled === true,
      prefillDelayMs: Math.max(300, Math.min(3000, Number(stored.prefillDelayMs) || defaultUiPreferences.prefillDelayMs)),
      prefillLength: ["short", "medium", "long"].includes(String(stored.prefillLength)) ? stored.prefillLength as PrefillLength : defaultUiPreferences.prefillLength,
    };
  } catch {
    return defaultUiPreferences;
  }
}

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
      text: "告诉 Coordinator 你想写什么，或打开一本已有小说。它会先理解目标，再安排 Writer、Reviewer 和 Memory Keeper。",
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
  const [showHistory, setShowHistory] = useState(false);
  const [pendingQuestions, setPendingQuestions] = useState<QuestionCard[]>([]);
  const [selectionDraft, setSelectionDraft] = useState<SelectionDraft | null>(null);
  const [mascotSpeech, setMascotSpeech] = useState("我在。先说今天想推进哪一步。");
  const [searchResult, setSearchResult] = useState<Record<string, unknown> | null>(null);
  const [compareContent, setCompareContent] = useState<string | null>(null);
  const [chapterWorkspace, setChapterWorkspace] = useState<Record<string, unknown> | null>(null);
  const [conversationHistory, setConversationHistory] = useState<ConversationHistoryEntry[]>([]);
  const [promptOptimizing, setPromptOptimizing] = useState(false);
  const [promptOptimization, setPromptOptimization] = useState<PromptOptimizationResult | null>(null);
  const [promptUndo, setPromptUndo] = useState<string | null>(null);
  const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
  const [collaboration, setCollaboration] = useState<CollaborationOverview | null>(null);
  const [canonMigration, setCanonMigration] = useState<CanonMigration | null>(null);
  const [showMigration, setShowMigration] = useState(false);
  const [workspaceLayout, setWorkspaceLayout] = useState<WorkspaceLayout>(loadWorkspaceLayout);
  const [uiPreferences, setUiPreferences] = useState<UiPreferences>(loadUiPreferences);
  const [voiceSettings, setVoiceSettings] = useState<VoiceSettings | null>(null);
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus | null>(null);
  const [voiceSource, setVoiceSource] = useState<VoiceSource | null>(null);
  const [voiceRevision, setVoiceRevision] = useState(0);
  const [voiceRecording, setVoiceRecording] = useState(false);
  const [voiceTranscribing, setVoiceTranscribing] = useState(false);
  const [speakingMessageId, setSpeakingMessageId] = useState<string | null>(null);
  const editorRef = useRef<MonacoEditor.IStandaloneCodeEditor | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const resizeRef = useRef<{ target: WorkspaceResizeTarget; startX: number; startWidth: number; direction: 1 | -1 } | null>(null);
  const voiceRecorderRef = useRef<LocalWavRecorder | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => () => {
    audioRef.current?.pause();
    if (voiceRecorderRef.current) void voiceRecorderRef.current.stop();
  }, []);

  const updateWorkspaceLayout = useCallback((change: Partial<WorkspaceLayout>) => {
    setWorkspaceLayout((current) => ({ ...current, ...change }));
  }, []);

  const applyWorkspacePreset = useCallback((preset: WorkspacePreset) => {
    setWorkspaceLayout({ ...workspacePresets[preset] });
  }, []);

  const startWorkspaceResize = useCallback((target: WorkspaceResizeTarget, direction: 1 | -1, event: ReactPointerEvent<HTMLDivElement>) => {
    const widthKey = target === "navigation" ? "navigationWidth" : target === "assistant" ? "assistantWidth" : "inspectorWidth";
    resizeRef.current = { target, startX: event.clientX, startWidth: workspaceLayout[widthKey], direction };
    event.preventDefault();
    window.document.body.classList.add("layout-resizing");
  }, [workspaceLayout]);

  useEffect(() => {
    localStorage.setItem(workspaceLayoutStorageKey, JSON.stringify(workspaceLayout));
  }, [workspaceLayout]);

  useEffect(() => {
    localStorage.setItem(uiPreferencesStorageKey, JSON.stringify(uiPreferences));
    const root = window.document.documentElement;
    const applyTheme = () => {
      const resolved = uiPreferences.theme === "system"
        ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
        : uiPreferences.theme;
      root.dataset.theme = resolved;
      root.dataset.accent = uiPreferences.accent;
      root.dataset.density = uiPreferences.density;
    };
    applyTheme();
    const media = window.matchMedia("(prefers-color-scheme: light)");
    media.addEventListener("change", applyTheme);
    return () => media.removeEventListener("change", applyTheme);
  }, [uiPreferences]);

  useEffect(() => {
    const resize = (event: PointerEvent) => {
      const activeResize = resizeRef.current;
      if (!activeResize) return;
      const width = clampWorkspaceWidth(activeResize.target, activeResize.startWidth + (event.clientX - activeResize.startX) * activeResize.direction);
      const widthKey = activeResize.target === "navigation" ? "navigationWidth" : activeResize.target === "assistant" ? "assistantWidth" : "inspectorWidth";
      setWorkspaceLayout((current) => ({ ...current, [widthKey]: width }));
    };
    const stop = () => {
      resizeRef.current = null;
      window.document.body.classList.remove("layout-resizing");
    };
    window.addEventListener("pointermove", resize);
    window.addEventListener("pointerup", stop);
    return () => {
      window.removeEventListener("pointermove", resize);
      window.removeEventListener("pointerup", stop);
      window.document.body.classList.remove("layout-resizing");
    };
  }, []);

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
      const [opened, overview] = await Promise.all([
        window.inkflow.request<{ dashboard: Dashboard; tree: ProjectTree; canon_migration?: CanonMigration }>("project.open", {
        project_root: root,
        }),
        window.inkflow.request<CollaborationOverview>("collaboration.overview", { project_root: root }),
      ]);
      setDashboard(opened.dashboard);
      setTree(opened.tree);
      setCollaboration(overview);
      setCanonMigration(opened.canon_migration || null);
    },
    [projectRoot],
  );

  const openProject = useCallback(async (root: string) => {
    setError("");
    setBusy(true);
    setMascotMood("thinking");
    try {
      const [opened, history, overview] = await Promise.all([
        window.inkflow.request<{ dashboard: Dashboard; tree: ProjectTree; canon_migration?: CanonMigration }>("project.open", { project_root: root }),
        window.inkflow.request<{ entries: ConversationHistoryEntry[] }>("conversation.history", { project_root: root, limit: 100 }),
        window.inkflow.request<CollaborationOverview>("collaboration.overview", { project_root: root }),
      ]);
      setProjectRoot(root);
      setDashboard(opened.dashboard);
      setTree(opened.tree);
      setCollaboration(overview);
      setCanonMigration(opened.canon_migration || null);
      setShowMigration(Boolean(opened.canon_migration?.required));
      setDocument(null);
      setText("");
      setActiveTab("project");
      localStorage.setItem("inkflow.lastProject", root);
      const projectTitle = String(opened.dashboard.brief.title || "未命名小说");
      setRecentProjects((items) => rememberRecentProject(items, root, projectTitle));
      setConversationHistory(history.entries);
      const restoredMessages = history.entries.flatMap<Message>((entry) => [
        { id: `${entry.id}-user`, role: "user", text: entry.user, createdAt: entry.recorded_at },
        { id: `${entry.id}-assistant`, role: "assistant", text: entry.assistant, createdAt: entry.recorded_at },
      ]);
      setMessages([
        ...(restoredMessages.length ? restoredMessages : [{ id: "welcome", role: "assistant" as const, text: "告诉我你想推进什么。我会保留对话，并把写作、审查和记忆边界说清楚。" }]),
        { id: crypto.randomUUID(), role: "system", text: `已打开《${projectTitle}》。已恢复 ${history.entries.length} 轮对话；正史、草稿和审查边界已载入。` },
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
    messagesRef.current?.scrollTo({ top: messagesRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  useEffect(() => {
    void Promise.all([
      window.inkflow.request<Record<string, unknown>>("app.initialize"),
      window.inkflow.request<Record<string, unknown>>("provider.status"),
      window.inkflow.request<VoiceSettings>("voice.settings.get"),
      window.inkflow.request<VoiceStatus>("voice.status"),
      window.inkflow.launchContext(),
      window.inkflow.updateStatus(),
    ])
      .then(([info, status, loadedVoiceSettings, loadedVoiceStatus, launch, update]) => {
        setAppInfo(info);
        setProvider(status);
        setVoiceSettings(loadedVoiceSettings);
        setVoiceStatus(loadedVoiceStatus);
        setUpdateInfo(update as UpdateInfo);
        const recent = launch.projectRoot || localStorage.getItem("inkflow.lastProject");
        if (recent) void openProject(recent);
      })
      .catch((cause) => {
        const message = errorMessage(cause);
        setError(message);
        if (message.includes("内置引擎") && message.includes("不兼容")) setShowUpdate(true);
        setMascotMood("rest");
      });
    const removeEvent = window.inkflow.onEvent((value) => {
      const event = value as EngineEvent;
      setEvents((items) => [...items.slice(-199), { ...event, timestamp: new Date().toISOString() }]);
      if (event.type?.startsWith("voice.job.")) setVoiceRevision((current) => current + 1);
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

  useEffect(() => {
    if (!projectRoot) { setContextStatus(null); setCollaboration(null); return; }
    let active = true;
    const poll = async () => {
      try {
        const [value, overview] = await Promise.all([
          window.inkflow.request<ContextStatus>("context.status", { project_root: projectRoot }),
          window.inkflow.request<CollaborationOverview>("collaboration.overview", { project_root: projectRoot }),
        ]);
        if (active) { setContextStatus(value); setCollaboration(overview); }
      } catch { /* 状态面板不能打断正文工作流；下一轮继续读取。 */ }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 4000);
    return () => { active = false; window.clearInterval(timer); };
  }, [projectRoot]);

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

  const openSelectionActions = () => {
    if (!document || !editorRef.current) return;
    const selection = editorRef.current.getSelection();
    const model = editorRef.current.getModel();
    if (!selection || !model || selection.isEmpty()) {
      setNotice("请先在正文中选中要批注的文字。");
      setMascotMood("waiting");
      return;
    }
    if (text !== savedText) {
      setNotice("正文还有未保存修改。请先保存，再对稳定版本批注或修订。");
      setMascotMood("waiting");
      return;
    }
    const value = model.getValue();
    const startUtf16 = model.getOffsetAt(selection.getStartPosition());
    const endUtf16 = model.getOffsetAt(selection.getEndPosition());
    const quote = value.slice(startUtf16, endUtf16);
    const startOffset = Array.from(value.slice(0, startUtf16)).length;
    const selectedCharacters = Array.from(quote).length;
    setSelectionDraft({
      relativePath: document.relative_path,
      startOffset,
      endOffset: startOffset + selectedCharacters,
      quote,
      expectedHash: document.content_hash,
      isDraft: document.relative_path.toLowerCase().endsWith(".draft.md"),
      tooLong: selectedCharacters > 8_000,
    });
    setMascotSpeech("我已经读到这段了。告诉我只改什么、必须保留什么。");
    setMascotMood("reading");
  };

  const applySelectionAction = async (mode: "comment" | "revise", comment: string) => {
    if (!selectionDraft || !document || busy) return;
    setBusy(true);
    setError("");
    setMascotMood(mode === "revise" ? "thinking" : "reading");
    try {
      const result = await request<unknown>(mode === "revise" ? "document.revise_selection" : "document.annotate", {
        relative_path: selectionDraft.relativePath,
        start_offset: selectionDraft.startOffset,
        end_offset: selectionDraft.endOffset,
        comment,
        ...(mode === "revise" ? { expected_hash: selectionDraft.expectedHash } : {}),
      });
      const loaded = await request<DocumentData>("document.read", { relative_path: selectionDraft.relativePath });
      setDocument(loaded);
      setText(loaded.content);
      setSavedText(loaded.content);
      if (mode === "revise") {
        const visible = visibleResult(result);
        setMessages((items) => [...items, { id: crypto.randomUUID(), role: "assistant", text: visible.summary, details: visible.details, reasoning: visible.reasoning, animate: true }]);
        setNotice("局部修订已保存为新草稿版本；请重新审查后再决定是否验收。");
      } else {
        setNotice("批注已记录，正文没有被修改。");
      }
      setSelectionDraft(null);
      setMascotMood("success");
      setMascotSpeech(mode === "revise" ? "只改了选中的部分，旧版本也保留着。" : "批注收好了，正文没有动。");
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
    } finally {
      setBusy(false);
    }
  };

  const playAudioPath = async (audioPath: string, messageId?: string) => {
    audioRef.current?.pause();
    if (messageId && speakingMessageId === messageId) {
      setSpeakingMessageId(null);
      return;
    }
    try {
      const url = await window.inkflow.audioUrl(audioPath);
      const audio = new Audio(url);
      const selectableAudio = audio as HTMLAudioElement & { setSinkId?: (deviceId: string) => Promise<void> };
      if (voiceSettings?.voice_output_device && selectableAudio.setSinkId) {
        await selectableAudio.setSinkId(voiceSettings.voice_output_device);
      }
      audioRef.current = audio;
      setSpeakingMessageId(messageId || "voice-center");
      audio.onended = () => setSpeakingMessageId(null);
      audio.onerror = () => { setSpeakingMessageId(null); setError("音频已经生成，但播放器无法打开这个本地文件。"); };
      await audio.play();
    } catch (cause) {
      setSpeakingMessageId(null);
      setError(errorMessage(cause));
    }
  };

  const speakText = async (messageId: string, content: string) => {
    if (speakingMessageId === messageId) {
      audioRef.current?.pause();
      setSpeakingMessageId(null);
      return;
    }
    try {
      setSpeakingMessageId(messageId);
      const result = await request<{ audio_path: string }>("voice.speak", { text: content });
      await playAudioPath(result.audio_path, messageId);
    } catch (cause) {
      setSpeakingMessageId(null);
      setError(errorMessage(cause));
    }
  };

  const openListeningCenter = () => {
    if (!document) {
      setNotice("请先打开要转换的正文或草稿。");
      return;
    }
    const selection = editorRef.current?.getSelection();
    const model = editorRef.current?.getModel();
    const selected = selection && model && !selection.isEmpty() ? model.getValueInRange(selection) : "";
    setVoiceSource({
      name: selected ? `${document.relative_path} · 当前选区` : document.relative_path,
      type: selected ? "selection" : document.relative_path.endsWith(".draft.md") ? "draft" : "document",
      text: selected || text,
    });
    setActiveTab("listen");
  };

  const sendChat = async (event?: FormEvent, overrideMessage?: string) => {
    event?.preventDefault();
    const message = (overrideMessage ?? chatInput).trim();
    if (!message || !projectRoot || busy) return;
    setChatInput("");
    setPromptOptimization(null);
    setPromptUndo(null);
    setError("");
    setMessages((items) => [...items, { id: crypto.randomUUID(), role: "user", text: message, createdAt: new Date().toISOString() }]);
    setBusy(true);
    setMascotMood("thinking");
    const runId = `desktop-${crypto.randomUUID()}`;
    setActiveRunId(runId);
    try {
      const result = await request<unknown>("conversation.send", { message, run_id: runId });
      const visible = visibleResult(result);
      const questionSource = result && typeof result === "object" ? (result as Record<string, unknown>).questions : null;
      if (Array.isArray(questionSource)) setPendingQuestions(questionSource as QuestionCard[]);
      const assistantMessageId = crypto.randomUUID();
      setMessages((items) => [
        ...items,
        { id: assistantMessageId, role: "assistant", text: visible.summary, details: visible.details, reasoning: visible.reasoning, animate: true, createdAt: new Date().toISOString() },
      ]);
      if (voiceSettings?.voice_enabled && voiceSettings.voice_output_enabled && voiceSettings.voice_auto_read) {
        void speakText(assistantMessageId, visible.summary);
      }
      const history = await request<{ entries: ConversationHistoryEntry[] }>("conversation.history", { limit: 100 });
      setConversationHistory(history.entries);
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

  const toggleVoiceInput = async () => {
    if (voiceRecording) {
      setVoiceRecording(false);
      setVoiceTranscribing(true);
      try {
        const bytes = await voiceRecorderRef.current?.stop();
        voiceRecorderRef.current = null;
        if (!bytes?.length) throw new Error("录音内容为空，请重新录制。");
        const path = await window.inkflow.saveVoiceRecording(bytes, "wav");
        const result = await request<{ text: string }>("voice.transcribe", { audio_path: path });
        setChatInput(result.text);
        if (voiceSettings?.voice_auto_send) void sendChat(undefined, result.text);
      } catch (cause) {
        setError(errorMessage(cause));
      } finally {
        setVoiceTranscribing(false);
      }
      return;
    }
    if (!voiceSettings?.voice_enabled || !voiceSettings.voice_input_enabled) {
      setNotice("请先在设置的“语音”分类中开启本地语音输入。");
      setShowSettings(true);
      return;
    }
    try {
      voiceRecorderRef.current = await LocalWavRecorder.start(voiceSettings.voice_input_device);
      setVoiceRecording(true);
      setNotice("正在听普通话，再点一次麦克风即可停止并填入文字。");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法使用麦克风，请检查 Windows 权限。");
    }
  };

  const optimizeChatPrompt = async () => {
    const source = chatInput.trim();
    if (!source || promptOptimizing || busy) return;
    setPromptOptimizing(true);
    setError("");
    setMascotMood("thinking");
    setMascotSpeech("我先检查目标、上下文、约束和交付格式，不会替你扩大授权。");
    try {
      const result = await request<PromptOptimizationResult>("prompt.optimize", { prompt: source });
      setPromptUndo((previous) => previous ?? source);
      setPromptOptimization(result);
      setChatInput(result.optimized_prompt);
      setMascotMood("success");
      setMascotSpeech("优化版准备好了。原文还在，觉得不对就点撤回。");
    } catch (cause) {
      setError(errorMessage(cause));
      setMascotMood("rest");
    } finally {
      setPromptOptimizing(false);
    }
  };

  const undoPromptOptimization = () => {
    if (promptUndo === null) return;
    setChatInput(promptUndo);
    setPromptUndo(null);
    setPromptOptimization(null);
    setMascotMood("welcome");
    setMascotSpeech("已经恢复优化前的原文，没有发送任何内容。");
  };

  const runWorkflow = async (action: string, extra: Record<string, unknown> = {}) => {
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
    if (action === "batch_accept" && !window.confirm("接收批次会按连续章节顺序把已审核内容及其临时记忆提交正史。确认继续吗？")) {
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
        ...extra,
      });
      const visible = visibleResult(result);
      setMessages((items) => [
        ...items,
        { id: crypto.randomUUID(), role: "assistant", text: visible.summary, details: visible.details, reasoning: visible.reasoning, animate: true },
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

  const applyCanonMigration = async () => {
    if (!canonMigration?.required || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await request<CanonMigration>("project.canon_migration.apply", {
        confirmation_token: canonMigration.confirmation_token,
      });
      setCanonMigration(result);
      setShowMigration(false);
      setNotice(result.message || "正史正文数据库升级已完成；备份已保存在项目内部目录。");
      await refresh();
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const setContextPin = async (sourceId: string, pinned: boolean) => {
    if (!sourceId || busy) return;
    const note = pinned ? window.prompt("可选：说明为什么本次任务必须保留这条资料。", "") : "";
    if (pinned && note === null) return;
    try {
      await request("context.pins.set", {
        source_id: sourceId,
        chapter_no: currentChapter(document?.relative_path) || undefined,
        note: note || "",
        pinned,
      });
      setNotice(pinned ? "已锁定这条工作资料；后续 Context Packet 会把它作为不可压缩材料。" : "已解除工作资料锁定。");
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
          <p className="lede">从故事想法到章节正文，规划、证据、版本和回退都留在你自己的电脑。</p>
          <div className="welcome-actions">
            <button className="primary" onClick={() => setShowCreate(true)}>新建小说</button>
            <button onClick={openFolder}>打开项目</button>
          </div>
          <div className="welcome-meta">
            <span>版本 {String(appInfo?.version || "0.5.0")}</span>
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
        {showSettings && <SettingsDialog projectRoot={projectRoot} provider={provider} voiceSettings={voiceSettings} voiceStatus={voiceStatus} layout={workspaceLayout} preferences={uiPreferences} onLayoutChange={updateWorkspaceLayout} onLayoutPreset={applyWorkspacePreset} onPreferencesChange={setUiPreferences} onClose={() => setShowSettings(false)} onSaved={setProvider} onVoiceSaved={(settings, status) => { setVoiceSettings(settings); setVoiceStatus(status); }} />}
        {showUpdate && <UpdateDialog info={updateInfo} onClose={() => setShowUpdate(false)} />}
      </div>
    );
  }

  const workspaceGridClass = [
    "workspace-grid",
    workspaceLayout.navigationVisible ? "navigation-visible" : "navigation-hidden",
    workspaceLayout.assistantVisible ? "assistant-visible" : "assistant-hidden",
    `assistant-${workspaceLayout.assistantPosition}`,
  ].join(" ");
  const workspaceGridStyle = {
    "--navigation-width": `${workspaceLayout.navigationWidth}px`,
    "--assistant-width": `${workspaceLayout.assistantWidth}px`,
  } as CSSProperties;

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
          <button onClick={() => void window.inkflow.openPath(projectRoot)}>打开文件夹</button>
          <button onClick={() => setShowSettings(true)}>设置</button>
          <button onClick={() => setShowUpdate(true)}>{updateInfo.status === "downloaded" ? "安装更新" : "检查更新"}</button>
        </nav>
      </header>

      <main className={workspaceGridClass} style={workspaceGridStyle}>
        {workspaceLayout.navigationVisible && <>
        <aside className="left-rail">
          <div className="rail-heading"><span>小说结构</span><button title="新建小说" onClick={() => setShowCreate(true)}>＋</button></div>
          <TreeSection label="核心文档" items={tree?.items || []} onOpen={openDocument} active={document?.relative_path} />
          {(tree?.groups || []).map((group) => (
            <TreeSection key={group.id} label={group.label} items={group.items} onOpen={openDocument} active={document?.relative_path} />
          ))}
          <ContextBudgetPanel value={contextStatus} onPin={setContextPin} />
          <CollaborationBoard value={collaboration} onAcceptBatch={(batchId) => void runWorkflow("batch_accept", { batch_id: batchId })} />
          <div className="rail-summary">
            <div><span>草稿</span><strong>{dashboard?.status.chapters?.draft || 0}</strong></div>
            <div><span>正史</span><strong>{dashboard?.status.chapters?.accepted || 0}</strong></div>
            <div><span>伏笔</span><strong>{dashboard?.status.open_threads || 0}</strong></div>
          </div>
        </aside>
        <ResizeHandle className="navigation-resize" label="调整小说结构栏宽度" onResizeStart={(event) => startWorkspaceResize("navigation", 1, event)} />
        </>}

        {workspaceLayout.assistantVisible && <>
        <section className="conversation-panel">
          <div className="panel-title">
            <div><h2>今天写到哪里？</h2><p className="panel-status" role="status">{mascotSpeech}</p></div>
            <div className="panel-mascot">
              <button className="history-trigger" onClick={() => setShowHistory(true)}>对话历史 <span>{conversationHistory.length}</span></button>
              <div className="mascot-conversation">
                <Mascot
                  mood={busy ? "thinking" : mascotMood}
                  onSettled={() => setMascotMood("idle")}
                />
              </div>
            </div>
          </div>
          <div className="pet-row" aria-label="与墨宝互动">
            <button onClick={() => { setMascotMood("welcome"); setMascotSpeech("你好。今天从灵感、正文还是审查开始？"); }}>打招呼</button>
            <button onClick={openSelectionActions}>读选区</button>
            <button onClick={() => { setMascotMood("thinking"); setMascotSpeech("我会先核对目标、正史与人物知识边界。"); }}>想一想</button>
            <button onClick={() => { setMascotMood("waiting"); setMascotSpeech("卡住时先缩小问题：人物此刻最怕失去什么？"); }}>找灵感</button>
          </div>
          <div className="quick-row">
            <button onClick={() => void sendChat(undefined, "先不要执行任务。请根据当前项目状态和最近讨论，用选项卡主动问我一到三个最值得确认、容易回答的问题；说明每个答案会影响什么，最后保留让我自己填写的其他选项。")}>先问我</button>
            <button onClick={() => void runWorkflow("plan")}>规划当前篇章</button>
            <button onClick={() => void runWorkflow("write")}>写当前章</button>
            <button onClick={() => void runWorkflow("review")}>审查当前章</button>
            <button onClick={() => void runWorkflow("accept")}>验收进正史</button>
          </div>
          <div className="messages" ref={messagesRef}>
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <span className="avatar">{message.role === "user" ? "你" : message.role === "system" ? "记" : "墨"}</span>
                <div>
                  <RevealText text={message.text} animate={Boolean(message.animate)} />
                  {message.createdAt && <time className="message-time" dateTime={message.createdAt}>{formatTime(message.createdAt)}</time>}
                  {(message.reasoning?.length || message.details) && <details><summary>查看判断摘要与可复核过程</summary>{Boolean(message.reasoning?.length) && <ol className="reasoning-list">{message.reasoning?.map((item, index) => <li key={index}>{item}</li>)}</ol>}{message.details && <pre>{message.details}</pre>}</details>}
                  <button className={`message-speak ${speakingMessageId === message.id ? "playing" : ""}`} title="朗读这条对话" aria-label="朗读这条对话" onClick={() => void speakText(message.id, message.text)}>{speakingMessageId === message.id ? "■" : "◖))"}</button>
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
            {promptOptimization && <div className="prompt-optimization-preview">
              <div><strong>提示词已优化</strong><span>{promptOptimization.change_summary.join(" · ")}</span></div>
              <details><summary>对比原文与保留约束</summary><p><b>原文：</b>{promptOptimization.original_prompt}</p>{promptOptimization.preserved_constraints.length > 0 && <p><b>保留：</b>{promptOptimization.preserved_constraints.join("；")}</p>}</details>
            </div>}
            <div className="composer-foot">
              <span>Enter 发送 · Shift+Enter 换行 · 回答 20 字/秒</span>
              <div>
                <button className={`voice-input-action ${voiceRecording ? "recording" : ""}`} type="button" disabled={voiceTranscribing} onClick={() => void toggleVoiceInput()} title="普通话语音输入">{voiceTranscribing ? "识别中…" : voiceRecording ? "■ 停止" : "● 语音"}</button>
                {busy && <button className="stop-action" type="button" onClick={() => void cancelActiveRun()}>停止任务</button>}
                {promptUndo !== null && <button type="button" onClick={undoPromptOptimization}>撤回优化</button>}
                <button className="optimize-action" type="button" disabled={busy || promptOptimizing || !chatInput.trim()} onClick={() => void optimizeChatPrompt()}>{promptOptimizing ? "优化中…" : "优化提示词"}</button>
                <button className="primary" type="submit" disabled={busy || !chatInput.trim()}>发送</button>
              </div>
            </div>
          </form>
        </section>
        <ResizeHandle className="assistant-resize" label="调整 AI 对话栏宽度" onResizeStart={(event) => startWorkspaceResize("assistant", workspaceLayout.assistantPosition === "left" ? 1 : -1, event)} />
        </>}

        <section className="workbench">
          <div className="tabbar">
            {(["project", "editor", "chapter", "review", "memory", "references", "listen", "process"] as Tab[]).map((tab) => (
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
              onAnnotate={openSelectionActions}
              onListen={openListeningCenter}
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
              inspectorVisible={workspaceLayout.inspectorVisible}
              inspectorWidth={workspaceLayout.inspectorWidth}
              onResizeInspector={(event) => startWorkspaceResize("inspector", -1, event)}
              theme={uiPreferences.theme === "system" ? (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark") : uiPreferences.theme}
              prefillEnabled={uiPreferences.prefillEnabled}
              prefillDelayMs={uiPreferences.prefillDelayMs}
              prefillLength={uiPreferences.prefillLength}
              onPrefill={(content, cursorOffset, length) => request<PrefillResult>("document.prefill", { relative_path: document?.relative_path, content, cursor_offset: cursorOffset, length })}
            />
          )}
          {activeTab === "chapter" && <ChapterPanel workspace={chapterWorkspace} request={request} onPrompt={(value) => setChatInput(value)} />}
          {activeTab === "review" && <ReviewPanel document={document} tree={tree} request={request} onOpen={openDocument} />}
          {activeTab === "memory" && <MemoryPanel dashboard={dashboard} request={request} onRefresh={() => refresh()} />}
          {activeTab === "references" && <ReferencesPanel request={request} />}
          {activeTab === "listen" && <VoiceCenter refreshKey={voiceRevision} projectRoot={projectRoot} source={voiceSource} characterNames={(dashboard?.bible_entries || []).filter((item) => String(item.kind || "") === "character").map((item) => String(item.name || "")).filter(Boolean)} request={request} onNotice={setNotice} onError={setError} onPlay={(path) => playAudioPath(path)} />}
          {activeTab === "process" && <ProcessPanel events={events} collaboration={collaboration} context={contextStatus} provider={provider} request={request} />}
        </section>
      </main>

      {(notice || error) && <Toast kind={error ? "error" : "info"} text={error || notice} onClose={() => { setError(""); setNotice(""); setMascotMood("idle"); }} />}
      {showCreate && <CreateProject onClose={() => setShowCreate(false)} onCreated={openProject} />}
      {showSettings && <SettingsDialog projectRoot={projectRoot} provider={provider} voiceSettings={voiceSettings} voiceStatus={voiceStatus} layout={workspaceLayout} preferences={uiPreferences} onLayoutChange={updateWorkspaceLayout} onLayoutPreset={applyWorkspacePreset} onPreferencesChange={setUiPreferences} onClose={() => setShowSettings(false)} onSaved={setProvider} onVoiceSaved={(settings, status) => { setVoiceSettings(settings); setVoiceStatus(status); }} />}
      {showUpdate && <UpdateDialog info={updateInfo} onClose={() => setShowUpdate(false)} />}
      {showSearch && <SearchDialog request={request} result={searchResult} setResult={setSearchResult} onClose={() => setShowSearch(false)} onOpen={async (relativePath) => {
        const item = [...(tree?.items || []), ...(tree?.groups.flatMap((group) => group.items) || [])].find((entry) => entry.relative_path === relativePath);
        if (item) await openDocument(item);
        setShowSearch(false);
      }} />}
      {showHistory && <ConversationHistoryDialog entries={conversationHistory} onClose={() => setShowHistory(false)} onReuse={(value) => {
        setChatInput(value);
        setPromptOptimization(null);
        setPromptUndo(null);
        setShowHistory(false);
        setMascotMood("waiting");
        setMascotSpeech("这条旧问题已经放回输入框，你可以修改后再发送。");
      }} />}
      {pendingQuestions.length > 0 && <QuestionDialog questions={pendingQuestions} onClose={() => setPendingQuestions([])} onSubmit={(answer) => {
        setPendingQuestions([]);
        void sendChat(undefined, answer);
      }} />}
      {selectionDraft && <SelectionDialog selection={selectionDraft} busy={busy} onClose={() => { setSelectionDraft(null); setMascotMood("idle"); }} onSubmit={(mode, comment) => void applySelectionAction(mode, comment)} />}
      {showMigration && canonMigration?.required && <CanonMigrationDialog migration={canonMigration} busy={busy} onClose={() => setShowMigration(false)} onApply={() => void applyCanonMigration()} />}
    </div>
  );
}

function ContextBudgetPanel({ value, onPin }: { value: ContextStatus | null; onPin: (sourceId: string, pinned: boolean) => void }) {
  const used = Math.max(0, Math.min(100, Number(value?.hard_usage_percent || 0)));
  const label = value?.status === "near_limit" ? "接近上限" : value?.status === "watch" ? "注意容量" : value?.status === "safe" ? "容量安全" : "等待上下文";
  const compressed = Boolean(value?.compression_applied);
  return <details className={`context-budget ${value?.status || "idle"}`}>
    <summary><span>上下文容量</span><strong>{used.toFixed(1)}%</strong></summary>
    <div className="context-meter"><i style={{ width: `${used}%` }} /></div>
    <p>{label} · {Number(value?.estimated_tokens || 0).toLocaleString()} / {Number(value?.hard_limit_tokens || 0).toLocaleString()} tokens</p>
    <small>{compressed ? `已从 ${Number(value?.before_compression_tokens || 0).toLocaleString()} tokens 定向压缩；硬约束未动。` : "每 4 秒刷新。接近软预算时只压缩低权威、低相关资料。"}</small>
    {value && <ul><li>不可压缩：{value.hard_sections.map((item) => item.title).join("、") || "尚未生成"}</li><li>可压缩：{value.compressible_sections.map((item) => item.title).join("、") || "尚未生成"}</li></ul>}
    {value && <details className="context-explain"><summary>查看保留与压缩理由</summary>{[...value.hard_sections, ...value.compressible_sections].map((item) => <div key={`${item.key}-${item.title}`}><strong>{item.title}</strong><small>{item.reason || "由当前资料优先级决定。"}</small>{["D1", "D2"].includes(item.key) && (item.source_ids || []).slice(0, 6).map((sourceId) => <button key={sourceId} type="button" onClick={() => onPin(sourceId, item.key !== "D2")}>{item.key === "D2" ? `解除 ${sourceId}` : `锁定 ${sourceId}`}</button>)}</div>)}</details>}
  </details>;
}

function roleLabel(role: string) {
  return ({ coordinator: "Coordinator", writer: "Writer", reviewer: "Reviewer", memory_keeper: "Memory Keeper", user: "用户" } as Record<string, string>)[role] || role;
}

function messageTypeLabel(type: string) {
  return ({ task_assignment: "任务分配", fact_query: "事实查询", handoff: "交接", review_issue: "审核问题", revision_request: "修订要求", objection: "异议", risk: "风险", memory_sync: "记忆同步", answer: "回应" } as Record<string, string>)[type] || type;
}

function learningLabel(type: string) {
  return ({ accepted: "用户接受", rejected: "用户拒绝", revised: "版本修订", rolled_back: "正史回退", preference_changed: "偏好变化" } as Record<string, string>)[type] || type;
}

function CollaborationBoard({ value, onAcceptBatch }: { value: CollaborationOverview | null; onAcceptBatch: (batchId: string) => void }) {
  const activeMessages = (value?.messages || []).filter((item) => ["pending", "responded", "escalated"].includes(item.status));
  const readyBatches = (value?.batches || []).filter((item) => item.status === "ready_for_acceptance");
  return <details className="collaboration-board" open>
    <summary><span>协作看板</span><strong>{activeMessages.length} 待处理</strong></summary>
    <p>Coordinator 只分配和汇总；正文、审查、记忆各自留痕。</p>
    {readyBatches.length > 0 && <section><h4>可验收批次</h4>{readyBatches.map((batch) => <div className="batch-card" key={batch.batch_id}><strong>第 {batch.start_chapter_no}–{batch.end_chapter_no} 章</strong><small>Reviewer 已通过，Memory Keeper 已保存临时记忆。</small><button type="button" onClick={() => onAcceptBatch(batch.batch_id)}>接收为正史</button></div>)}</section>}
    <section><h4>协作消息</h4>{activeMessages.length === 0 ? <small>当前没有待回应消息。</small> : activeMessages.slice(0, 5).map((item) => <div className="collaboration-message" key={item.message_id}><span>{roleLabel(item.sender_role)} → {roleLabel(item.recipient_role)}</span><strong>{messageTypeLabel(item.message_type)}</strong><p>{item.claim}</p><small>{item.chapter_no ? `第 ${item.chapter_no} 章 · ` : ""}{item.status}</small></div>)}</section>
    <section><h4>内部学习信号</h4><small>仅记录接受、拒绝、修订与偏好变化，用于本地优化；不会上传小说正文。</small>{(value?.learning_events || []).slice(0, 3).map((item) => <div className="learning-event" key={item.event_id}>{learningLabel(item.event_type)}{item.chapter_no ? ` · 第 ${item.chapter_no} 章` : ""}</div>)}</section>
  </details>;
}

function CanonMigrationDialog({ migration, busy, onClose, onApply }: { migration: CanonMigration; busy: boolean; onClose: () => void; onApply: () => void }) {
  return <Modal title="正史正文数据库升级" subtitle="这是一次可恢复的本地数据库结构升级，需要你的确认。" onClose={onClose}>
    <p>{migration.impact}</p>
    <p>受影响的已接受章节：{migration.accepted_chapter_count}。升级前会先创建 SQLite 备份；无法通过哈希验证的旧正文不会被猜测或覆盖。</p>
    <div className="dialog-actions"><button onClick={onClose} disabled={busy}>暂不升级</button><button className="primary" onClick={onApply} disabled={busy}>{busy ? "正在备份并升级…" : "创建备份并确认升级"}</button></div>
  </Modal>;
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
  onListen: () => void;
  onCompare: (versionId: string) => void;
  onStopCompare: () => void;
  onResolve: (annotationId: string) => void;
  inspectorVisible: boolean;
  inspectorWidth: number;
  onResizeInspector: (event: ReactPointerEvent<HTMLDivElement>) => void;
  theme: "dark" | "light";
  prefillEnabled: boolean;
  prefillDelayMs: number;
  prefillLength: PrefillLength;
  onPrefill: (content: string, cursorOffset: number, length: PrefillLength) => Promise<PrefillResult>;
}) {
  const [side, setSide] = useState<"comments" | "versions">("comments");
  const [showPrefill, setShowPrefill] = useState(false);
  const prefillState = useRef({ enabled: props.prefillEnabled, delay: props.prefillDelayMs, length: props.prefillLength, request: props.onPrefill });
  const prefillDisposable = useRef<{ dispose: () => void } | null>(null);
  const prefillSequence = useRef(0);
  useEffect(() => {
    prefillState.current = { enabled: props.prefillEnabled && showPrefill, delay: props.prefillDelayMs, length: props.prefillLength, request: props.onPrefill };
  }, [props.prefillEnabled, props.prefillDelayMs, props.prefillLength, props.onPrefill, showPrefill]);
  useEffect(() => () => prefillDisposable.current?.dispose(), []);
  if (!props.document) return <EmptyPanel title="选择一份文档" text="从左侧打开正文、规划或审查报告。" />;
  return (
    <div className={`editor-layout ${props.document.read_only ? "has-canon-banner" : ""} ${showPrefill ? "has-prefill" : ""}`}>
      <div className="document-toolbar">
        <div><strong>{props.document.relative_path}</strong><span>{props.isDirty ? "尚未保存" : "已同步"}</span></div>
        <div>
          {props.prefillEnabled && props.document.relative_path.endsWith(".draft.md") && <button className={showPrefill ? "active" : ""} onClick={() => setShowPrefill((value) => !value)}>预填续写</button>}
          <button onClick={props.onListen}>听读 / 转语音</button>
          <button onClick={props.onAnnotate}>批注选区</button>
          <button className="primary" disabled={!props.isDirty} onClick={props.onSave}>{props.document.read_only ? "保存修改提案" : "保存"}</button>
        </div>
      </div>
      {props.document.read_only && <div className="canon-banner"><strong>正史保护</strong>{props.document.reason}</div>}
      {showPrefill && <div className="prefill-bar"><div><strong>预填续写已开启</strong><span>停顿后显示灰字候选；Tab 接受、Esc 忽略。候选不会自动保存，正文一旦变化就会作废。</span></div><button onClick={() => setShowPrefill(false)}>关闭</button></div>}
      <div className={`editor-body ${props.inspectorVisible ? "inspector-visible" : "inspector-hidden"}`} style={{ "--inspector-width": `${props.inspectorWidth}px` } as CSSProperties}>
        <div className="monaco-wrap">
          {props.compareContent !== null ? (
            <>
              <div className="diff-head"><span>左：历史版本 · 右：当前内容</span><button onClick={props.onStopCompare}>退出对比</button></div>
              <Suspense fallback={<div className="editor-loading">正在载入对照编辑器…</div>}><DiffEditor height="100%" language="markdown" original={props.compareContent} modified={props.text} theme={props.theme === "light" ? "vs" : "vs-dark"} options={{ readOnly: true, automaticLayout: true, minimap: { enabled: false }, wordWrap: "on" }} /></Suspense>
            </>
          ) : (
            <Suspense fallback={<div className="editor-loading">正在载入正文编辑器…</div>}><Editor
              height="100%"
              language="markdown"
              value={props.text}
              theme={props.theme === "light" ? "vs" : "vs-dark"}
              onMount={(editor, monaco) => {
                props.editorRef.current = editor;
                prefillDisposable.current?.dispose();
                prefillDisposable.current = monaco.languages.registerInlineCompletionsProvider("markdown", {
                  provideInlineCompletions: async (model, position) => {
                    const state = prefillState.current;
                    if (!state.enabled || props.document?.read_only) return { items: [] };
                    const sequence = ++prefillSequence.current;
                    await new Promise((resolve) => window.setTimeout(resolve, state.delay));
                    if (sequence !== prefillSequence.current || !prefillState.current.enabled) return { items: [] };
                    const content = model.getValue();
                    const cursorOffset = model.getOffsetAt(position);
                    const result = await state.request(content, cursorOffset, state.length);
                    if (sequence !== prefillSequence.current || model.getValue() !== content || result.cursor_offset !== cursorOffset) return { items: [] };
                    return { items: [{ insertText: result.insertion, range: new monaco.Range(position.lineNumber, position.column, position.lineNumber, position.column) }] };
                  },
                  freeInlineCompletions: () => undefined,
                });
              }}
              onChange={(value) => props.onChange(value || "")}
              options={{ automaticLayout: true, minimap: { enabled: false }, wordWrap: "on", fontSize: 16, lineHeight: 27, padding: { top: 22, bottom: 32 }, smoothScrolling: true, renderLineHighlight: "gutter", fontFamily: "'Microsoft YaHei UI', 'Noto Serif SC', Consolas, monospace" }}
            /></Suspense>
          )}
        </div>
        {props.inspectorVisible && <>
        <ResizeHandle className="inspector-resize" label="调整批注与版本栏宽度" onResizeStart={props.onResizeInspector} />
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
        </>}
      </div>
      <footer className="status-strip"><span>{props.statistics.characters.toLocaleString()} 字</span><span>{props.statistics.paragraphs} 段</span><span>对白 {Math.round(props.statistics.dialogue_ratio * 100)}%</span><span>约 {props.statistics.estimated_reading_minutes} 分钟</span></footer>
    </div>
  );
}

function ChapterPanel({ workspace, request, onPrompt }: { workspace: Record<string, unknown> | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; onPrompt: (value: string) => void }) {
  const [candidates, setCandidates] = useState<Array<Record<string, unknown>>>([]);
  const [candidateWorking, setCandidateWorking] = useState(false);
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
    <section className="stack-section"><h3>Writer 候选方向</h3><p className="form-hint">多个 Writer 运行只提交方案；选中后仍由固定主笔统一写正文。</p><button disabled={candidateWorking} onClick={async () => { setCandidateWorking(true); try { const value = await request<{ candidates: Array<Record<string, unknown>> }>("chapter.writer_candidates", { chapter_no: Number(workspace.chapter_no), count: 3 }); setCandidates(value.candidates); } finally { setCandidateWorking(false); } }}>生成三个方向</button>{candidates.map((candidate) => { const data = candidate.data as Record<string, unknown>; return <article className="thread-card" key={String(candidate.artifact_id)}><strong>{String(data.title)}</strong><p>{String(data.scene_goal)}</p><small>{String(data.turning_point)}</small><button onClick={async () => { await request("chapter.writer_candidate.select", { artifact_id: candidate.artifact_id }); setCandidates((items) => items.map((item) => ({ ...item, status: item.artifact_id === candidate.artifact_id ? "selected" : "not_selected" }))); }}>{candidate.status === "selected" ? "已选" : "选为主笔方向"}</button></article>; })}</section>
  </div>;
}

function ReviewPanel({ document, tree, request, onOpen }: { document: DocumentData | null; tree: ProjectTree | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; onOpen: (item: TreeItem) => void }) {
  const reviews = tree?.groups.find((group) => group.id === "reviews")?.items || [];
  const [panelResult, setPanelResult] = useState<Record<string, unknown> | null>(null);
  const chapterMatch = document?.relative_path.match(/chapter_(\d+)\.draft\.md$/);
  return <div className="scroll-panel review-panel">
    <div className="section-heading"><p className="eyebrow">审查</p><h2>证据化审查</h2><p>每个扣分项都应指向正文、章节卡或正史依据；旧审查不能批准新版本。</p></div>
    {document?.relative_path.startsWith("reviews/") && <article className="markdown-preview"><pre>{document.content}</pre></article>}
    {chapterMatch && <section className="stack-section"><h3>多维预审</h3><p className="form-hint">连续性、人物、叙事和表达分别审查，再按证据去重；最终仍由正式 Reviewer 门禁放行。</p><button onClick={async () => setPanelResult(await request<Record<string, unknown>>("chapter.review_panel", { chapter_no: Number(chapterMatch[1]) }))}>运行多维预审</button>{panelResult && <pre className="compact-json">{JSON.stringify(panelResult.merged_findings, null, 2)}</pre>}</section>}
    <section className="stack-section"><h3>审查记录</h3>{reviews.length === 0 && <p className="empty-mini">还没有审查报告。</p>}{reviews.map((item) => <button className="review-link" key={item.id} onClick={() => void onOpen(item)}><span>✓</span><strong>{item.label}</strong><small>打开报告</small></button>)}</section>
  </div>;
}

function MemoryPanel({ dashboard, request, onRefresh }: { dashboard: Dashboard | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T>; onRefresh: () => Promise<void> }) {
  const [showAdd, setShowAdd] = useState(false);
  const [kind, setKind] = useState("character");
  const [name, setName] = useState("");
  const [notes, setNotes] = useState("");
  const [previewChapter, setPreviewChapter] = useState(1);
  const [memoryPreview, setMemoryPreview] = useState<Record<string, unknown> | null>(null);
  const [memoryWorking, setMemoryWorking] = useState(false);
  const save = async () => {
    if (!name.trim()) return;
    await request("bible.upsert", { kind, name, aliases: [], data: { notes } });
    setName(""); setNotes(""); setShowAdd(false); await onRefresh();
  };
  return <div className="scroll-panel memory-panel">
    <div className="section-heading"><p className="eyebrow">正史</p><h2>正史与故事圣经</h2><button onClick={() => setShowAdd(!showAdd)}>＋ 手动圣经条目</button></div>
    {showAdd && <div className="inline-form"><select value={kind} onChange={(event) => setKind(event.target.value)}><option value="character">人物</option><option value="location">地点</option><option value="organization">组织</option><option value="item">物品</option><option value="lore">世界观</option><option value="style">文风</option></select><input value={name} onChange={(event) => setName(event.target.value)} placeholder="名称" /><textarea value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="稳定设定、声线、禁忌或说明" /><button className="primary" onClick={() => void save()}>保存</button></div>}
    <section className="stack-section canon-preview"><h3>正史变更预览</h3><p className="form-hint">只针对你已经接受且 Reviewer 已放行的当前草稿。先看事实与伏笔补丁，再决定是否提交 SQLite。</p><div className="settings-inline-actions"><input type="number" min={1} value={previewChapter} onChange={(event) => setPreviewChapter(Number(event.target.value))} /><button disabled={memoryWorking} onClick={async () => { setMemoryWorking(true); try { const value = await request<Record<string, unknown>>("memory.preview", { chapter_no: previewChapter, user_accepted: true }); setMemoryPreview(value); } finally { setMemoryWorking(false); } }}>生成预览</button></div>{memoryPreview && <article className="memory-card"><strong>第 {previewChapter} 章 · 尚未提交</strong><pre>{JSON.stringify((memoryPreview.preview as Record<string, unknown>)?.data || memoryPreview, null, 2)}</pre><button className="primary" disabled={memoryWorking} onClick={async () => { const preview = memoryPreview.preview as Record<string, unknown>; setMemoryWorking(true); try { await request("memory.commit_preview", { chapter_no: previewChapter, artifact_id: preview.artifact_id }); setMemoryPreview(null); await onRefresh(); } finally { setMemoryWorking(false); } }}>确认提交正史</button></article>}</section>
    <section className="stack-section"><h3>人工故事圣经 <small>{dashboard?.bible_entries.length || 0}</small></h3>{dashboard?.bible_entries.map((entry) => <article className="memory-card" key={String(entry.entry_id)}><span>{kindLabel(String(entry.kind))}</span><strong>{String(entry.name)}</strong><p>{String(((entry.data || {}) as Record<string, unknown>).notes || "")}</p></article>)}</section>
    <section className="stack-section"><h3>当前正史事实 <small>{dashboard?.facts.length || 0}</small></h3>{dashboard?.facts.slice(0, 30).map((fact) => <article className="fact-row" key={String(fact.fact_id)}><strong>{String(fact.subject)} · {String(fact.predicate)}</strong><p>{stringifyShort(fact.value)}</p><small>来源第 {String(fact.source_chapter)} 章</small></article>)}</section>
    <section className="stack-section"><h3>开放线索 <small>{dashboard?.threads.length || 0}</small></h3>{dashboard?.threads.map((thread) => <article className="thread-card" key={String(thread.thread_id)}><strong>{String(thread.title)}</strong><p>{String(thread.description)}</p><small>{String(thread.status)} · 预计第 {String(thread.due_chapter || "—")} 章</small></article>)}</section>
  </div>;
}

function ProcessPanel({ events, collaboration, context, provider, request }: { events: EngineEvent[]; collaboration: CollaborationOverview | null; context: ContextStatus | null; provider: Record<string, unknown> | null; request: <T>(method: string, params?: Record<string, unknown>) => Promise<T> }) {
  const [threadUpdates, setThreadUpdates] = useState<Record<string, Record<string, unknown>>>({});
  const runs = processRuns(events);
  const tasks = collaboration?.tasks || [];
  const activeTasks = tasks.filter((task) => ["running", "queued", "waiting"].includes(String(task.status))).length;
  const failedTasks = tasks.filter((task) => ["failed", "interrupted"].includes(String(task.status))).length;
  const messages = collaboration?.messages || [];
  const learning = collaboration?.learning_events || [];
  const usage = collaboration?.usage;
  const retrieval = context?.retrieval_diagnostics;
  const openThreads = (collaboration?.threads || []).filter((thread) => ["open", "waiting", "escalated"].includes(String(thread.status))).length;
  return <div className="scroll-panel process-panel"><div className="section-heading"><p className="eyebrow">协作台</p><h2>工作流与运行状态</h2><p>任务、Agent 讨论、上下文、检索、学习和费用信息集中查看；正文和版本仍留在各自工作区。</p></div>
    <div className="operations-grid">
      <article><header><strong>工作流</strong><span>{runs.length} 项</span></header><p>{runs.length ? "当前安排与每一步进度都记录在下方。" : "发送需求后显示任务安排。"}</p></article>
      <article><header><strong>并发任务</strong><span>{activeTasks} 进行中</span></header><p>{failedTasks ? `${failedTasks} 项需要处理；可在项目页重试或停止。` : "依赖关系由 Coordinator 与 Novel Engine 控制。"}</p></article>
      <article><header><strong>Agent 讨论</strong><span>{openThreads} 个待处理议题</span></header><p>{messages[0] ? `${messages[0].sender_role} → ${messages[0].recipient_role}：${messages[0].claim}` : "有疑问、驳回或交接时显示结构化消息。"}</p></article>
      <article><header><strong>Context 检查器</strong><span>{Number(context?.estimated_tokens || 0).toLocaleString()} tokens</span></header><p>{context?.compression_applied ? "已压缩低相关资料，硬事实保持不动。" : "显示本次保留、压缩和舍弃的资料。"}</p></article>
      <article><header><strong>检索诊断</strong><span>{retrieval?.selected?.length || 0} / {retrieval?.candidate_count || 0} 已召回</span></header><p>{retrieval?.initial_top_k ? `本次动态 Top K 为 ${retrieval.initial_top_k}，另有 ${retrieval.discarded?.length || 0} 项记录舍弃原因。` : context?.warnings?.[0] || "召回数量按任务范围、人物、伏笔和剩余预算动态调整。"}</p></article>
      <article><header><strong>正史变更</strong><span>验收后</span></header><p>Memory Keeper 的新增、更新和结束事实会先预览，再进入事务提交。</p></article>
      <article><header><strong>版本对比</strong><span>正文右侧</span></header><p>草稿、修改提案与历史快照均可打开 Diff，不静默覆盖旧版本。</p></article>
      <article><header><strong>学习中心</strong><span>{learning.length} 条信号</span></header><p>{learning[0] ? `${learningLabel(learning[0].event_type)}${learning[0].chapter_no ? ` · 第 ${learning[0].chapter_no} 章` : ""}` : "记录接受、拒绝、撤回和重写，只用于本机优化。"}</p></article>
      <article><header><strong>费用</strong><span>{usage?.calls ? `${usage.calls} 次 · ${usage.total_tokens.toLocaleString()} tokens` : "尚无调用"}</span></header><p>{usage?.pricing_configured ? `按已填单价估算 ${usage.currency} ${usage.estimated_cost.toFixed(4)}` : "已统计 Token；填写服务商单价后显示金额估算。"} 当前模型：{String(provider?.model || "未配置")}。</p></article>
    </div>
    {context && <div className="run-list"><h3>Context Packet 质量报告</h3><article className="run-card"><header><strong>预算分配</strong><span>输出预留 {Number((context as unknown as Record<string, unknown>).output_reserve_tokens || 0).toLocaleString()} tokens</span></header><div className="context-budget-list">{(context.budget_allocation || []).map((item) => <p key={item.key}><strong>{item.key} · {item.title}</strong><span>{item.estimated_tokens.toLocaleString()} tokens · {item.hard ? "固定保留" : "可压缩"} · {item.source_count} 来源</span></p>)}</div></article>{Boolean(retrieval?.discarded?.length) && <details className="run-card"><summary>查看检索舍弃记录（{retrieval?.discarded?.length}）</summary><pre className="compact-json">{JSON.stringify(retrieval?.discarded, null, 2)}</pre></details>}</div>}
    <div className="run-list"><h3>定向讨论</h3>{(collaboration?.threads || []).slice(0, 8).map((thread) => { const current = threadUpdates[String(thread.thread_id)] || thread; const active = ["open", "waiting"].includes(String(current.status)); return <article className="run-card" key={String(thread.thread_id)}><header><strong>{String(thread.topic)}</strong><span>{String(current.status)} · 第 {String(current.current_round || 1)}/{String(current.max_rounds || 2)} 轮</span></header><p>{String(current.resolution || "等待目标 Agent 回答")}</p>{active && <button onClick={async () => { const value = await request<{ thread: Record<string, unknown> }>("collaboration.reply", { thread_id: thread.thread_id }); setThreadUpdates((items) => ({ ...items, [String(thread.thread_id)]: value.thread })); }}>让目标 Agent 回答</button>}{String(current.status) === "escalated" && <small>两轮后仍有分歧，已暂停分支并交给 Coordinator 向你提出最小问题。</small>}</article>; })}</div>
    <div className="run-list"><h3>任务计划</h3>{runs.length === 0 && <p className="empty-mini">发送需求后，这里显示本次任务的安排。</p>}{runs.slice(0, 8).map(run => {
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

const DEFAULT_AGENT_GENERATION: AgentGenerationProfiles = {
  coordinator: { temperature: 0.25, top_p: 0.8, top_k: null },
  writer: { temperature: 0.85, top_p: 0.95, top_k: null },
  reviewer: { temperature: 0.2, top_p: 0.8, top_k: null },
  memory_keeper: { temperature: 0.1, top_p: 0.7, top_k: null },
};

type CreationPreset = {
  id: string;
  name: string;
  note: string;
  reasoning_effort: string;
  inquiry_frequency: string;
  context_soft_tokens: number;
  context_hard_tokens: number;
  review_verification_mode: string;
  agent_generation: AgentGenerationProfiles;
};

const SETTINGS_PRESETS: CreationPreset[] = [
  { id: "balanced", name: "专业均衡", note: "适合多数长篇项目", reasoning_effort: "high", inquiry_frequency: "medium", context_soft_tokens: 256000, context_hard_tokens: 512000, review_verification_mode: "evidence", agent_generation: DEFAULT_AGENT_GENERATION },
  { id: "stable", name: "稳健连载", note: "降低发散，优先连续与证据", reasoning_effort: "high", inquiry_frequency: "high", context_soft_tokens: 384000, context_hard_tokens: 512000, review_verification_mode: "assisted", agent_generation: { coordinator: { temperature: 0.15, top_p: 0.72, top_k: null }, writer: { temperature: 0.62, top_p: 0.88, top_k: null }, reviewer: { temperature: 0.1, top_p: 0.65, top_k: null }, memory_keeper: { temperature: 0.05, top_p: 0.55, top_k: null } } },
  { id: "explore", name: "灵感探索", note: "构思与正文保留更大空间", reasoning_effort: "max", inquiry_frequency: "medium", context_soft_tokens: 384000, context_hard_tokens: 512000, review_verification_mode: "evidence", agent_generation: { coordinator: { temperature: 0.35, top_p: 0.9, top_k: null }, writer: { temperature: 1.05, top_p: 0.98, top_k: null }, reviewer: { temperature: 0.22, top_p: 0.8, top_k: null }, memory_keeper: { temperature: 0.08, top_p: 0.6, top_k: null } } },
  { id: "economy", name: "节省模式", note: "缩小上下文和额外核验", reasoning_effort: "medium", inquiry_frequency: "low", context_soft_tokens: 128000, context_hard_tokens: 256000, review_verification_mode: "evidence", agent_generation: { coordinator: { temperature: 0.2, top_p: 0.75, top_k: null }, writer: { temperature: 0.72, top_p: 0.9, top_k: null }, reviewer: { temperature: 0.12, top_p: 0.7, top_k: null }, memory_keeper: { temperature: 0.05, top_p: 0.55, top_k: null } } },
];

const PROVIDER_OPTIONS = [
  { id: "deepseek", name: "DeepSeek", note: "官方兼容接口", baseUrl: "https://api.deepseek.com", models: ["deepseek-chat", "deepseek-reasoner"] },
  { id: "openai", name: "OpenAI", note: "官方 Chat Completions", baseUrl: "https://api.openai.com/v1", models: ["gpt-5", "gpt-4.1"] },
  { id: "anthropic", name: "Anthropic", note: "原生 Messages API", baseUrl: "https://api.anthropic.com", models: ["claude-sonnet-4-5"] },
  { id: "gemini", name: "Gemini", note: "OpenAI 兼容模式", baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai", models: ["gemini-2.5-pro", "gemini-2.5-flash"] },
  { id: "openrouter", name: "OpenRouter", note: "多模型聚合接口", baseUrl: "https://openrouter.ai/api/v1", models: ["openrouter/auto"] },
  { id: "ollama", name: "Ollama", note: "本机模型，无需云端密钥", baseUrl: "http://127.0.0.1:11434/v1", models: ["qwen3", "llama3.3"] },
  { id: "custom", name: "自定义", note: "其他 OpenAI 兼容服务", baseUrl: "", models: [] },
] as const;

type SettingsSection = "appearance" | "layout" | "models" | "creation" | "voice" | "context" | "review" | "learning" | "advanced";
const SETTINGS_SECTIONS: Array<{ id: SettingsSection; label: string; note: string }> = [
  { id: "appearance", label: "外观", note: "主题、配色与密度" },
  { id: "layout", label: "布局", note: "面板、位置与宽度" },
  { id: "models", label: "模型", note: "服务商、模型与密钥" },
  { id: "creation", label: "创作", note: "预设、询问与预填" },
  { id: "voice", label: "语音", note: "输入、朗读与设备" },
  { id: "context", label: "上下文", note: "预算与记忆检索" },
  { id: "review", label: "审查", note: "证据与核验方式" },
  { id: "learning", label: "学习", note: "本地反馈与内测数据" },
  { id: "advanced", label: "高级", note: "角色参数与本机能力" },
];

const AGENT_TUNING_META: Array<{ id: AgentRole; label: string; note: string }> = [
  { id: "coordinator", label: "Coordinator", note: "理解需求、日常交流、拆解派工与汇总，不碰正文和正史。" },
  { id: "writer", label: "Writer", note: "规划、起草与定点修订；默认保留更多表达空间。" },
  { id: "reviewer", label: "Reviewer", note: "证据审查与篇章复核；默认更稳定、少发散。" },
  { id: "memory_keeper", label: "Memory Keeper", note: "提取正史事实；默认最保守。" },
];

function agentGenerationFromProvider(value: unknown): AgentGenerationProfiles {
  const source = value && typeof value === "object" ? value as Partial<Record<AgentRole, Partial<AgentGeneration>>> : {};
  return Object.fromEntries((Object.keys(DEFAULT_AGENT_GENERATION) as AgentRole[]).map((role) => [role, { ...DEFAULT_AGENT_GENERATION[role], ...(source[role] || {}) }])) as AgentGenerationProfiles;
}

function SettingsDialog({ projectRoot, provider, voiceSettings, voiceStatus, layout, preferences, onLayoutChange, onLayoutPreset, onPreferencesChange, onClose, onSaved, onVoiceSaved }: {
  projectRoot: string;
  provider: Record<string, unknown> | null;
  voiceSettings: VoiceSettings | null;
  voiceStatus: VoiceStatus | null;
  layout: WorkspaceLayout;
  preferences: UiPreferences;
  onLayoutChange: (change: Partial<WorkspaceLayout>) => void;
  onLayoutPreset: (preset: WorkspacePreset) => void;
  onPreferencesChange: (value: UiPreferences) => void;
  onClose: () => void;
  onSaved: (value: Record<string, unknown>) => void;
  onVoiceSaved: (settings: VoiceSettings, status: VoiceStatus) => void;
}) {
  const [form, setForm] = useState({ provider_kind: String(provider?.provider_kind || "deepseek"), api_key: "", base_url: String(provider?.base_url || "https://api.deepseek.com"), model: String(provider?.model || "deepseek-v4-flash"), reasoning_effort: String(provider?.reasoning_effort || "high"), inquiry_frequency: String(provider?.inquiry_frequency || "medium"), context_soft_tokens: Number(provider?.context_soft_tokens || 256000), context_hard_tokens: Number(provider?.context_hard_tokens || 512000), max_output_tokens: Number(provider?.max_output_tokens || 16000), input_price_per_million: Number(provider?.input_price_per_million || 0), output_price_per_million: Number(provider?.output_price_per_million || 0), review_verification_mode: String(provider?.review_verification_mode || "evidence"), review_local_nli_model: String(provider?.review_local_nli_model || ""), review_judge_model: String(provider?.review_judge_model || ""), retrieval_embedding_model: String(provider?.retrieval_embedding_model || ""), retrieval_reranker_model: String(provider?.retrieval_reranker_model || ""), powershell_enabled: Boolean(provider?.powershell_enabled), agent_generation: agentGenerationFromProvider(provider?.agent_generation) });
  const [voiceForm, setVoiceForm] = useState<VoiceSettings>(voiceSettings || {
    voice_enabled: false, voice_input_enabled: true, voice_output_enabled: true, voice_auto_read: false, voice_auto_send: false,
    voice_default_profile: "narrator_female", voice_speed: 1, voice_volume: 1, voice_input_device: "", voice_output_device: "",
    voice_compute_device: "auto", voice_engine: "auto", voice_asr_model: "paraformer-zh-streaming", voice_tts_model: "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", voice_clone_model: "Qwen/Qwen3-TTS-12Hz-0.6B-Base", voice_light_asr_model: "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09", voice_light_tts_model: "sherpa-onnx-vits-zh-ll",
    voice_sample_rate: 24000, voice_segment_chars: 360, voice_cache_limit_mb: 1024, voice_debug: false,
  });
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [result, setResult] = useState("");
  const [section, setSection] = useState<SettingsSection>("appearance");
  const [search, setSearch] = useState("");
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [learningSettings, setLearningSettings] = useState({ enabled: true, allow_training_exports: false });
  const [learningNotice, setLearningNotice] = useState("");
  const [trainingExportId, setTrainingExportId] = useState("");
  const [baseModelPath, setBaseModelPath] = useState("");
  const [trainingMethod, setTrainingMethod] = useState<"lora" | "dpo">("lora");
  const [voiceDevices, setVoiceDevices] = useState<MediaDeviceInfo[]>([]);
  const [qwenInstalling, setQwenInstalling] = useState(false);
  const [lightVoiceInstalling, setLightVoiceInstalling] = useState(false);
  useEffect(() => {
    if (!projectRoot) return;
    void window.inkflow.request<{ enabled: boolean; allow_training_exports: boolean }>("learning.settings.get", { project_root: projectRoot }).then(setLearningSettings).catch(() => undefined);
  }, [projectRoot]);
  useEffect(() => { if (voiceSettings) setVoiceForm(voiceSettings); }, [voiceSettings]);
  const updateAgentGeneration = (role: AgentRole, patch: Partial<AgentGeneration>) => setForm((value) => ({ ...value, agent_generation: { ...value.agent_generation, [role]: { ...value.agent_generation[role], ...patch } } }));
  const persist = async () => {
    setWorking(true); setError(""); setResult("");
    try {
      const saved = await withDeadline(window.inkflow.request<Record<string, unknown>>("provider.configure", form), 15000, "本机保存没有及时返回。请重新打开设置核对保存结果后再试。");
      const savedVoice = await window.inkflow.request<VoiceSettings>("voice.settings.configure", voiceForm);
      const latestVoiceStatus = await window.inkflow.request<VoiceStatus>("voice.status", projectRoot ? { project_root: projectRoot } : {});
      if (projectRoot) await window.inkflow.request("learning.settings.update", { project_root: projectRoot, ...learningSettings });
      setForm((value) => ({ ...value, api_key: "" }));
      onSaved({ ...provider, ...saved });
      onVoiceSaved(savedVoice, latestVoiceStatus);
      setResult("设置已保存。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally { setWorking(false); }
  };
  const activePreset = SETTINGS_PRESETS.find((preset) => preset.reasoning_effort === form.reasoning_effort && preset.inquiry_frequency === form.inquiry_frequency && preset.context_soft_tokens === form.context_soft_tokens && preset.context_hard_tokens === form.context_hard_tokens)?.id;
  const inferredProvider = form.provider_kind;
  const settingKeywords: Record<SettingsSection, string> = { appearance: "主题 黑白 系统 配色 颜色 密度", layout: "布局 面板 宽度 左右", models: "模型 服务商 API 密钥 价格 费用 能力 列表", creation: "创作 预设 询问 预填 续写", voice: "语音 普通话 朗读 麦克风 声音 克隆 设备 TTS ASR", context: "上下文 token 检索 RAG embedding reranker top k", review: "审查 Reviewer 证据 NLI 裁判 多维", learning: "学习 反馈 训练 LoRA DPO 导出", advanced: "高级 temperature top p top k PowerShell" };
  const visibleSections = SETTINGS_SECTIONS.filter((item) => `${item.label}${item.note}${settingKeywords[item.id]}`.toLowerCase().includes(search.trim().toLowerCase()));
  const chooseProvider = (id: string) => {
    const choice = PROVIDER_OPTIONS.find((item) => item.id === id);
    if (!choice) return;
    setRemoteModels([]);
    setForm((value) => ({ ...value, provider_kind: id, base_url: choice.baseUrl || value.base_url, model: Array.from(choice.models)[0] || value.model }));
  };
  const applyCreationPreset = (preset: CreationPreset) => setForm((value) => ({
    ...value,
    reasoning_effort: preset.reasoning_effort,
    inquiry_frequency: preset.inquiry_frequency,
    context_soft_tokens: preset.context_soft_tokens,
    context_hard_tokens: preset.context_hard_tokens,
    review_verification_mode: preset.review_verification_mode,
    agent_generation: Object.fromEntries(Object.entries(preset.agent_generation).map(([role, values]) => [role, { ...values }])) as AgentGenerationProfiles,
  }));
  const refreshVoiceDevices = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      setVoiceDevices(await navigator.mediaDevices.enumerateDevices());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取 Windows 音频设备。");
    }
  };
  const installLightVoice = async () => {
    const confirmed = window.confirm("轻量语音包包含普通话朗读与识别模型，预计下载约 360MB，仅保存在本机。确认下载吗？");
    if (!confirmed) return;
    setLightVoiceInstalling(true); setError("");
    try {
      const status = await window.inkflow.request<VoiceStatus>("voice.light.install", { confirmation: "download_light_voice_models" });
      const adapted = { ...voiceForm, voice_engine: "sherpa" as const };
      setVoiceForm(adapted);
      onVoiceSaved(adapted, status);
      setResult("轻量普通话语音已经安装并适配。关闭设置前可继续调整声音参数。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setLightVoiceInstalling(false);
    }
  };
  const installQwen = async () => {
    const qwen = voiceStatus?.qwen;
    const dependencySize = qwen?.estimated_dependency_download_mb || 7000;
    const modelSize = qwen?.estimated_model_download_mb || 7000;
    const confirmed = window.confirm(`Qwen 高品质组件为可选下载：依赖约 ${Math.round(dependencySize / 100) / 10}GB，模型最多还需约 ${Math.round(modelSize / 100) / 10}GB；会占用较多磁盘和显存，安装期间可继续使用其他功能。确认安装并自动适配吗？`);
    if (!confirmed) return;
    setQwenInstalling(true); setError("");
    try {
      const status = await window.inkflow.request<VoiceStatus>("voice.qwen.install", { confirmation: "install_optional_qwen" });
      onVoiceSaved({ ...voiceForm, voice_engine: "qwen" }, status);
      setVoiceForm((value) => ({ ...value, voice_engine: "qwen" }));
      setResult(status.qwen?.model_loaded ? "Qwen 高品质语音已经安装、下载模型并完成适配。" : "Qwen 依赖已经安装；模型仍可在本地模型区域继续准备。");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setQwenInstalling(false);
    }
  };
  return <Modal title="设置" subtitle="布局、外观、模型和创作参数集中在这里。外观与布局即时保存，模型参数点保存后生效。" onClose={onClose} className="settings-modal">
    <form className="settings-shell" onSubmit={(event) => { event.preventDefault(); void persist(); }}>
      <aside className="settings-nav">
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索设置" aria-label="搜索设置" />
        <nav>{visibleSections.map((item) => <button type="button" key={item.id} className={section === item.id ? "active" : ""} onClick={() => setSection(item.id)}><strong>{item.label}</strong><span>{item.note}</span></button>)}</nav>
      </aside>
      <div className="settings-content">
        {section === "appearance" && <SettingsPane title="外观" note="明暗主题与强调色只保存在这台电脑。">
          <SettingGroup title="主题"><div className="choice-row three">{(["dark", "light", "system"] as ThemeMode[]).map((mode) => <button type="button" key={mode} className={preferences.theme === mode ? "active" : ""} onClick={() => onPreferencesChange({ ...preferences, theme: mode })}>{mode === "dark" ? "黑色" : mode === "light" ? "白色" : "跟随系统"}</button>)}</div></SettingGroup>
          <SettingGroup title="配色"><div className="palette-grid">{(["lime", "jade", "blue", "violet", "amber", "rose"] as AccentPalette[]).map((accent) => <button type="button" key={accent} data-palette={accent} className={preferences.accent === accent ? "active" : ""} onClick={() => onPreferencesChange({ ...preferences, accent })}><i />{({ lime: "青柠", jade: "青玉", blue: "湖蓝", violet: "紫罗兰", amber: "琥珀", rose: "胭脂" } as Record<AccentPalette, string>)[accent]}</button>)}</div></SettingGroup>
          <SettingGroup title="界面密度"><div className="choice-row"><button type="button" className={preferences.density === "comfortable" ? "active" : ""} onClick={() => onPreferencesChange({ ...preferences, density: "comfortable" })}>舒适</button><button type="button" className={preferences.density === "compact" ? "active" : ""} onClick={() => onPreferencesChange({ ...preferences, density: "compact" })}>紧凑</button></div></SettingGroup>
        </SettingsPane>}
        {section === "layout" && <SettingsPane title="布局" note="正文始终占据剩余空间；关闭面板不会删除任何内容。"><WorkspaceLayoutPane layout={layout} onChange={onLayoutChange} onPreset={onLayoutPreset} onReset={() => onLayoutChange({ ...defaultWorkspaceLayout })} /></SettingsPane>}
        {section === "models" && <SettingsPane title="模型" note="先选服务商，再填写该服务商当前有效的模型 ID。">
          <div className="provider-grid">{PROVIDER_OPTIONS.map((item) => <button type="button" key={item.id} className={inferredProvider === item.id ? "active" : ""} onClick={() => chooseProvider(item.id)}><strong>{item.name}</strong><span>{item.note}</span></button>)}</div>
          <div className={`provider-status ${provider?.api_key_configured ? "ready" : "missing"}`}><strong>{provider?.api_key_configured ? "模型密钥已保存" : "尚未保存模型密钥"}</strong><span>{provider?.api_key_configured ? `凭据来源：${credentialLabel(String(provider?.api_key_storage || ""))}` : "密钥只交给本机引擎，并保存在系统凭据库。"}</span></div>
          <div className="settings-fields"><label>模型名称<input list="provider-models" value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="填写平台当前模型 ID" /><datalist id="provider-models">{[...PROVIDER_OPTIONS.flatMap((item) => Array.from(item.models)), ...remoteModels].map((model) => <option key={model} value={model} />)}</datalist></label><label>接口地址<input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} /></label><label>更换密钥 <small>{provider?.api_key_configured ? "留空继续使用当前服务商的已有密钥" : "本机模型可以留空"}</small><input type="password" autoComplete="new-password" value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} placeholder="不会回显" /></label></div>
          <div className="settings-inline-actions"><button type="button" onClick={async () => { try { const saved = await window.inkflow.request<Record<string, unknown>>("provider.configure", form); onSaved({ ...provider, ...saved }); const value = await window.inkflow.request<{ models: string[] }>("provider.models", {}); setRemoteModels(value.models); setResult(`读取到 ${value.models.length} 个模型`); } catch (cause) { setError(errorMessage(cause)); } }}>保存并读取模型列表</button><span>这会访问所选服务商，但不会发起内容生成。</span></div>
          <SettingGroup title="费用估算" note="按服务商账单货币填写；留 0 时只统计 Token，不猜价格。"><div className="settings-fields two"><label>输入单价 / 百万 Token<input type="number" min={0} step={0.01} value={form.input_price_per_million} onChange={(event) => setForm({ ...form, input_price_per_million: Number(event.target.value) })} /></label><label>输出单价 / 百万 Token<input type="number" min={0} step={0.01} value={form.output_price_per_million} onChange={(event) => setForm({ ...form, output_price_per_million: Number(event.target.value) })} /></label></div></SettingGroup>
        </SettingsPane>}
        {section === "creation" && <SettingsPane title="创作" note="预设会同时调整上下文、询问策略、审查模式和四个角色的生成参数。">
          <div className="preset-grid">{SETTINGS_PRESETS.map((preset) => <button type="button" key={preset.id} className={activePreset === preset.id ? "active" : ""} onClick={() => applyCreationPreset(preset)}><strong>{activePreset === preset.id ? "✓ " : ""}{preset.name}</strong><span>{preset.note}</span><small>{preset.context_soft_tokens / 10000} 万常用上下文</small></button>)}</div>
          <div className="settings-fields two"><label>思考强度<select value={form.reasoning_effort} onChange={(event) => setForm({ ...form, reasoning_effort: event.target.value })}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="max">最高</option></select></label><label>主动询问<select value={form.inquiry_frequency} onChange={(event) => setForm({ ...form, inquiry_frequency: event.target.value })}><option value="low">只问必需信息</option><option value="medium">把握较低时询问</option><option value="high">重要创作分岔也询问</option><option value="ultra">有明显未知项就询问</option></select></label></div>
          <SettingGroup title="预填续写" note="启用后，编辑停顿会调用当前 Writer 模型，因此可能产生费用。"><label className="setting-check"><input type="checkbox" checked={preferences.prefillEnabled} onChange={(event) => onPreferencesChange({ ...preferences, prefillEnabled: event.target.checked })} />允许在编辑器中手动开启灰字预填候选</label><div className="settings-fields two"><label>等待时间 <small>{preferences.prefillDelayMs} 毫秒</small><input type="range" min="300" max="3000" step="100" value={preferences.prefillDelayMs} onChange={(event) => onPreferencesChange({ ...preferences, prefillDelayMs: Number(event.target.value) })} /></label><label>候选长度<select value={preferences.prefillLength} onChange={(event) => onPreferencesChange({ ...preferences, prefillLength: event.target.value as PrefillLength })}><option value="short">短句</option><option value="medium">一小段</option><option value="long">长段落</option></select></label></div></SettingGroup>
        </SettingsPane>}
        {section === "voice" && <SettingsPane title="本地语音" note="只有一种普通话模式；语音运行时不是新的 Agent，也不会接触小说正史。">
          <div className={`provider-status ${voiceStatus?.ready_for_input && voiceStatus?.ready_for_output ? "ready" : "missing"}`}><strong>{voiceStatus?.message || "正在读取本地语音状态"}</strong><span>{voiceStatus?.backend ? `当前引擎：${voiceStatus.backend}` : "保存设置不会自动下载模型；依赖与模型由安装环节单独处理。"}</span></div>
          <SettingGroup title="总开关"><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_enabled} onChange={(event) => setVoiceForm({ ...voiceForm, voice_enabled: event.target.checked })} />启用本地普通话语音</label><div className="settings-fields two"><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_input_enabled} onChange={(event) => setVoiceForm({ ...voiceForm, voice_input_enabled: event.target.checked })} />语音下达命令与聊天</label><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_output_enabled} onChange={(event) => setVoiceForm({ ...voiceForm, voice_output_enabled: event.target.checked })} />对话与正文朗读</label><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_auto_send} onChange={(event) => setVoiceForm({ ...voiceForm, voice_auto_send: event.target.checked })} />识别后直接发送 <small>关闭时只填入输入框</small></label><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_auto_read} onChange={(event) => setVoiceForm({ ...voiceForm, voice_auto_read: event.target.checked })} />自动朗读 AI 回答</label></div></SettingGroup>
          <SettingGroup title="默认声音与听感"><div className="settings-fields two"><label>默认声音<select value={voiceForm.voice_default_profile} onChange={(event) => setVoiceForm({ ...voiceForm, voice_default_profile: event.target.value })}><option value="narrator_female">女声旁白</option><option value="female_bright">明快女声</option><option value="female_warm">温柔女声</option><option value="narrator_male">男声旁白</option><option value="male_calm">沉静男声</option><option value="male_firm">坚定男声</option></select></label><label>计算设备<select value={voiceForm.voice_compute_device} onChange={(event) => setVoiceForm({ ...voiceForm, voice_compute_device: event.target.value as VoiceSettings["voice_compute_device"] })}><option value="auto">自动（优先 NVIDIA 显卡）</option><option value="cpu">只用 CPU（较慢）</option><option value="cuda">NVIDIA CUDA</option></select></label><label>语速 <output>{voiceForm.voice_speed.toFixed(2)}</output><input type="range" min="0.75" max="1.35" step="0.05" value={voiceForm.voice_speed} onChange={(event) => setVoiceForm({ ...voiceForm, voice_speed: Number(event.target.value) })} /></label><label>音量 <output>{voiceForm.voice_volume.toFixed(2)}</output><input type="range" min="0.25" max="1.5" step="0.05" value={voiceForm.voice_volume} onChange={(event) => setVoiceForm({ ...voiceForm, voice_volume: Number(event.target.value) })} /></label></div></SettingGroup>
          <SettingGroup title="设备偏好" note="不选择时使用 Windows 默认设备。"><div className="settings-inline-actions"><button type="button" onClick={() => void refreshVoiceDevices()}>读取可用设备</button><span>首次读取会触发系统麦克风权限提示。</span></div><div className="settings-fields two"><label>麦克风<select value={voiceForm.voice_input_device} onChange={(event) => setVoiceForm({ ...voiceForm, voice_input_device: event.target.value })}><option value="">Windows 默认麦克风</option>{voiceDevices.filter((item) => item.kind === "audioinput").map((item, index) => <option value={item.deviceId} key={item.deviceId}>{item.label || `麦克风 ${index + 1}`}</option>)}</select></label><label>播放设备<select value={voiceForm.voice_output_device} onChange={(event) => setVoiceForm({ ...voiceForm, voice_output_device: event.target.value })}><option value="">Windows 默认扬声器</option>{voiceDevices.filter((item) => item.kind === "audiooutput").map((item, index) => <option value={item.deviceId} key={item.deviceId}>{item.label || `扬声器 ${index + 1}`}</option>)}</select></label></div></SettingGroup>
          <SettingGroup title="轻量普通话组件" note="默认推荐；约 360MB，使用 sherpa-onnx + ONNX，适合普通 CPU。"><div className="settings-inline-actions"><button type="button" disabled={lightVoiceInstalling || Boolean(voiceStatus?.sherpa?.tts_ready && voiceStatus?.sherpa?.asr_ready)} onClick={() => void installLightVoice()}>{lightVoiceInstalling ? "正在下载轻量组件…" : voiceStatus?.sherpa?.tts_ready && voiceStatus?.sherpa?.asr_ready ? "轻量组件已就绪" : "下载并适配轻量组件"}</button><span>{voiceStatus?.sherpa?.model_size_mb ? `已占用 ${voiceStatus.sherpa.model_size_mb.toFixed(1)}MB` : "只在你点击后下载，不会随保存设置自动执行。"}</span></div></SettingGroup>
          <SettingGroup title="Qwen 高品质组件（可选）" note="用于更自然的声音和用户授权克隆；依赖与模型可能占用数 GB。"><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_engine === "qwen"} disabled={!voiceStatus?.qwen?.installed} onChange={(event) => setVoiceForm({ ...voiceForm, voice_engine: event.target.checked ? "qwen" : "auto" })} />使用 Qwen 高品质模式</label><div className="settings-inline-actions"><button type="button" disabled={qwenInstalling || Boolean(voiceStatus?.qwen?.installed && voiceStatus?.qwen?.model_loaded)} onClick={() => void installQwen()}>{qwenInstalling ? "正在安装并适配 Qwen…" : voiceStatus?.qwen?.installed ? voiceStatus.qwen.model_loaded ? "Qwen 已就绪" : "重新准备 Qwen 模型" : "安装 Qwen 并一键适配"}</button><span>{voiceStatus?.qwen?.installed ? `组件 ${voiceStatus.qwen.package_size_mb.toFixed(1)}MB · 模型 ${voiceStatus.qwen.model_size_mb.toFixed(1)}MB` : `预计依赖约 ${(voiceStatus?.qwen?.estimated_dependency_download_mb || 7000) / 1000}GB，模型另计`}</span></div>{voiceStatus?.qwen?.last_error && <p className="form-error">{voiceStatus.qwen.last_error}</p>}{!voiceStatus?.qwen?.python_available && !voiceStatus?.qwen?.installed && <p className="form-hint">当前安装包没有找到 Python。安装 Qwen 需要本机有 Python 3.12/3.13，或设置 INKFLOW_PYTHON 指向 python.exe。</p>}</SettingGroup>
          <SettingGroup title="声音调试与资源" note="这些参数只影响本机音频生成，不修改正文或正史。"><div className="settings-fields two"><label>输出采样率<select value={voiceForm.voice_sample_rate} onChange={(event) => setVoiceForm({ ...voiceForm, voice_sample_rate: Number(event.target.value) })}><option value={16000}>16 kHz（省空间）</option><option value={22050}>22.05 kHz（轻量模型原生）</option><option value={24000}>24 kHz（推荐）</option><option value={44100}>44.1 kHz（更大文件）</option><option value={48000}>48 kHz（更大文件）</option></select></label><label>每段最多字数 <small>{voiceForm.voice_segment_chars} 字</small><input type="range" min="120" max="800" step="20" value={voiceForm.voice_segment_chars} onChange={(event) => setVoiceForm({ ...voiceForm, voice_segment_chars: Number(event.target.value) })} /></label><label>短音频缓存 <small>{voiceForm.voice_cache_limit_mb} MB</small><input type="range" min="128" max="4096" step="128" value={voiceForm.voice_cache_limit_mb} onChange={(event) => setVoiceForm({ ...voiceForm, voice_cache_limit_mb: Number(event.target.value) })} /></label><label className="setting-check"><input type="checkbox" checked={voiceForm.voice_debug} onChange={(event) => setVoiceForm({ ...voiceForm, voice_debug: event.target.checked })} />开启语音调试日志 <small>只记录运行状态，不记录录音内容</small></label></div><p className="form-hint">分段越短越容易暂停和恢复，但文件数量会增加；缓存达到上限时会自动清理最早的短音频。</p></SettingGroup>
          <details className="voice-technical"><summary>组件与模型信息</summary><p>当前引擎：{voiceForm.voice_engine}</p><p>轻量输入：sherpa-onnx · {voiceForm.voice_light_asr_model}</p><p>轻量朗读：sherpa-onnx · {voiceForm.voice_light_tts_model}</p><p>Qwen 预设：{voiceForm.voice_tts_model}</p><p>声音克隆：Qwen3-TTS · {voiceForm.voice_clone_model}</p><p>本地目录：{voiceStatus?.data_root || "尚未读取"}</p></details>
        </SettingsPane>}
        {section === "context" && <SettingsPane title="上下文与检索" note="按任务分配预算；硬事实固定保留，普通资料按相关度压缩。">
          <div className="settings-fields two"><label>常用上下文<input type="number" min={16000} max={512000} value={form.context_soft_tokens} onChange={(event) => setForm({ ...form, context_soft_tokens: Number(event.target.value) })} /></label><label>最大上下文<input type="number" min={16000} max={1000000} value={form.context_hard_tokens} onChange={(event) => setForm({ ...form, context_hard_tokens: Number(event.target.value) })} /></label></div>
          <SettingGroup title="混合记忆检索" note="精确查询和本地 BM25 始终可用；召回数量按任务、人物、伏笔与剩余预算动态计算。"><div className="settings-fields two"><label>语义召回模型 <small>可选</small><input value={form.retrieval_embedding_model} onChange={(event) => setForm({ ...form, retrieval_embedding_model: event.target.value })} placeholder="BAAI/bge-m3" /></label><label>精排模型 <small>可选</small><input value={form.retrieval_reranker_model} onChange={(event) => setForm({ ...form, retrieval_reranker_model: event.target.value })} placeholder="BAAI/bge-reranker-v2-m3" /></label></div><p className="form-hint">留空不会下载模型。生成参数里的 Top K 默认也留空，只有接口支持且你明确设置时才发送。</p></SettingGroup>
        </SettingsPane>}
        {section === "review" && <SettingsPane title="审查" note="Reviewer 只给证据化报告，不直接改正文。">
          <div className="settings-fields"><label>核验模式<select value={form.review_verification_mode} onChange={(event) => setForm({ ...form, review_verification_mode: event.target.value })}><option value="evidence">基础：本地证据门禁</option><option value="assisted">增强：纠错并逐条核验</option><option value="strict">严格：争议时调用裁判</option></select></label><label>本地中文 NLI <small>留空关闭</small><input value={form.review_local_nli_model} onChange={(event) => setForm({ ...form, review_local_nli_model: event.target.value })} placeholder="本机模型路径或名称" /></label><label>争议裁判模型 <small>留空关闭</small><input value={form.review_judge_model} onChange={(event) => setForm({ ...form, review_judge_model: event.target.value })} placeholder="当前兼容接口中的模型 ID" /></label></div>
        </SettingsPane>}
        {section === "learning" && <SettingsPane title="本地学习" note="当前只在项目内记录可复核的反馈信号，不会把小说正文上传为公共训练数据。">
          <SettingGroup title="反馈记录" note={projectRoot ? "当前项目" : "打开项目后可设置"}><label className="setting-check"><input type="checkbox" disabled={!projectRoot} checked={learningSettings.enabled} onChange={(event) => setLearningSettings({ ...learningSettings, enabled: event.target.checked })} />记录接受、拒绝、撤回、重写、偏好和检索反馈</label></SettingGroup>
          <SettingGroup title="内测训练数据" note="默认关闭"><label className="setting-check"><input type="checkbox" disabled={!projectRoot} checked={learningSettings.allow_training_exports} onChange={(event) => setLearningSettings({ ...learningSettings, allow_training_exports: event.target.checked })} />允许从这个项目导出本地训练数据</label><p className="form-hint">外部参考默认排除，只使用本项目可复核事件；LoRA/DPO 只生成待确认训练单，实际训练仍需再次确认资源影响。</p></SettingGroup>
          <SettingGroup title="本地学习工具" note="不会自动上传"><div className="settings-inline-actions"><button type="button" disabled={!projectRoot} onClick={async () => { try { await window.inkflow.request("learning.settings.update", { project_root: projectRoot, ...learningSettings }); const value = await window.inkflow.request<{ export_id: string; records: number }>("learning.dataset.export", { project_root: projectRoot, include_prose: false }); setTrainingExportId(value.export_id); setLearningNotice(`已导出 ${value.records} 条结构化反馈，不含正文`); } catch (cause) { setError(errorMessage(cause)); } }}>导出结构化反馈</button><button type="button" disabled={!projectRoot} onClick={async () => { try { const value = await window.inkflow.request<{ pairs: number }>("learning.preference.train", { project_root: projectRoot }); setLearningNotice(`本地偏好排序器已更新：${value.pairs} 组比较`); } catch (cause) { setError(errorMessage(cause)); } }}>更新偏好排序器</button></div><div className="settings-fields two"><label>训练方式<select value={trainingMethod} onChange={(event) => setTrainingMethod(event.target.value as "lora" | "dpo")}><option value="lora">LoRA</option><option value="dpo">DPO</option></select></label><label>本地基础模型路径<input value={baseModelPath} onChange={(event) => setBaseModelPath(event.target.value)} placeholder="只接受本机已存在路径" /></label></div><button type="button" disabled={!projectRoot || !trainingExportId || !baseModelPath} onClick={async () => { try { const value = await window.inkflow.request<{ confirmation_token: string }>("learning.training.prepare", { project_root: projectRoot, export_id: trainingExportId, base_model_path: baseModelPath, training_method: trainingMethod }); setLearningNotice(`训练单已准备，尚未运行。确认码：${value.confirmation_token}`); } catch (cause) { setError(errorMessage(cause)); } }}>准备训练单</button>{learningNotice && <p className="form-success">{learningNotice}</p>}</SettingGroup>
        </SettingsPane>}
        {section === "advanced" && <SettingsPane title="高级" note="创作预设已经覆盖常见情况；这里只处理单个角色的细调。">
          <div className="agent-tuning-notice">temperature 与 top_p 会按角色发送；Top K 默认留空。Reviewer 和 Memory Keeper 应保持低随机性。</div>
          <section className="agent-tuning">{AGENT_TUNING_META.map((meta) => { const values = form.agent_generation[meta.id]; return <article key={meta.id}><header><div><strong>{meta.label}</strong><small>{meta.note}</small></div><button type="button" onClick={() => updateAgentGeneration(meta.id, DEFAULT_AGENT_GENERATION[meta.id])}>恢复默认</button></header><div className="agent-tuning-grid"><label>温度 <small>0～2</small><input type="number" min={0} max={2} step={0.05} value={values.temperature} onChange={(event) => updateAgentGeneration(meta.id, { temperature: Number(event.target.value) })} /></label><label>Top P <small>0.01～1</small><input type="number" min={0.01} max={1} step={0.01} value={values.top_p} onChange={(event) => updateAgentGeneration(meta.id, { top_p: Number(event.target.value) })} /></label><label>Top K <small>自动或 1～200</small><input type="number" min={1} max={200} step={1} value={values.top_k ?? ""} placeholder="自动" onChange={(event) => updateAgentGeneration(meta.id, { top_k: event.target.value === "" ? null : Number(event.target.value) })} /></label></div></article>; })}</section>
          <SettingGroup title="本机 PowerShell" note="默认关闭。"><label className="setting-check"><input type="checkbox" checked={form.powershell_enabled} onChange={(event) => setForm({ ...form, powershell_enabled: event.target.checked })} />允许墨流工具在当前小说项目目录内执行 PowerShell</label></SettingGroup>
        </SettingsPane>}
      </div>
      <footer className="settings-actions">{result && <span className="form-success">✓ {result}</span>}{error && <span className="form-error">{error}</span>}<button type="button" onClick={onClose}>关闭</button><button className="primary" type="submit" disabled={working}>{working ? "正在保存…" : "保存设置"}</button></footer>
    </form>
  </Modal>;
}

function SettingsPane({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return <section className="settings-pane"><header><h3>{title}</h3><p>{note}</p></header>{children}</section>;
}

function SettingGroup({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return <section className="setting-group"><header><strong>{title}</strong>{note && <small>{note}</small>}</header>{children}</section>;
}

function WorkspaceLayoutPane({ layout, onChange, onPreset, onReset }: { layout: WorkspaceLayout; onChange: (change: Partial<WorkspaceLayout>) => void; onPreset: (preset: WorkspacePreset) => void; onReset: () => void }) {
  const presets: Array<{ id: WorkspacePreset; name: string; note: string }> = [
    { id: "balanced", name: "均衡", note: "导航、对话和正文同时可见" },
    { id: "writing", name: "专注写作", note: "收起对话与批注，正文最大化" },
    { id: "planning", name: "规划协作", note: "加宽 Coordinator 对话，保留导航" },
    { id: "review", name: "审查对照", note: "AI 置右，批注与版本保持展开" },
  ];
  const setWidth = (target: WorkspaceResizeTarget, value: number) => {
    const key = target === "navigation" ? "navigationWidth" : target === "assistant" ? "assistantWidth" : "inspectorWidth";
    onChange({ [key]: clampWorkspaceWidth(target, value) });
  };
  return <div className="layout-pane">
    <section className="layout-presets"><div><strong>快速布局</strong><small>切换后仍可继续微调</small></div><div className="layout-preset-grid">{presets.map((preset) => <button key={preset.id} type="button" onClick={() => onPreset(preset.id)}><strong>{preset.name}</strong><span>{preset.note}</span></button>)}</div></section>
    <section className="layout-settings-section">
      <div className="layout-settings-heading"><strong>显示哪些面板</strong><small>关闭不会删除内容，随时可从顶部“布局”重新打开。</small></div>
      <div className="layout-toggles">
        <label><input type="checkbox" checked={layout.navigationVisible} onChange={(event) => onChange({ navigationVisible: event.target.checked })} /><span><strong>小说结构</strong><small>文档树、上下文容量与协作看板</small></span></label>
        <label><input type="checkbox" checked={layout.assistantVisible} onChange={(event) => onChange({ assistantVisible: event.target.checked })} /><span><strong>Coordinator 对话</strong><small>自然语言协作与工作流入口</small></span></label>
        <label><input type="checkbox" checked={layout.inspectorVisible} onChange={(event) => onChange({ inspectorVisible: event.target.checked })} /><span><strong>批注与版本</strong><small>正文编辑器右侧的检查面板</small></span></label>
      </div>
    </section>
    <section className="layout-settings-section">
      <div className="layout-settings-heading"><strong>对话位置</strong><small>让对话跟随你的阅读与输入习惯。</small></div>
      <div className="layout-position-switch"><button type="button" className={layout.assistantPosition === "left" ? "active" : ""} onClick={() => onChange({ assistantPosition: "left" })}>AI 在正文左侧</button><button type="button" className={layout.assistantPosition === "right" ? "active" : ""} onClick={() => onChange({ assistantPosition: "right" })}>AI 在正文右侧</button></div>
    </section>
    <section className="layout-settings-section">
      <div className="layout-settings-heading"><strong>精确宽度</strong><small>也可以直接拖拽工作区中的发光分隔线。</small></div>
      <div className="layout-sliders">
        <label>小说结构 <output>{layout.navigationWidth}px</output><input type="range" min="180" max="420" value={layout.navigationWidth} onChange={(event) => setWidth("navigation", Number(event.target.value))} /></label>
        <label>Coordinator 对话 <output>{layout.assistantWidth}px</output><input type="range" min="320" max="760" value={layout.assistantWidth} onChange={(event) => setWidth("assistant", Number(event.target.value))} /></label>
        <label>批注与版本 <output>{layout.inspectorWidth}px</output><input type="range" min="170" max="420" value={layout.inspectorWidth} onChange={(event) => setWidth("inspector", Number(event.target.value))} /></label>
      </div>
    </section>
    <div className="dialog-actions"><button type="button" onClick={onReset}>恢复默认布局</button></div>
  </div>;
}

function ResizeHandle({ className, label, onResizeStart }: { className: string; label: string; onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void }) {
  return <div className={`resize-handle ${className}`} role="separator" aria-orientation="vertical" aria-label={label} onPointerDown={onResizeStart}><span /></div>;
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
  const sourceLabel = local.source === "embedded" ? "发布包内置更新源" : local.source === "github" ? "GitHub Releases" : local.source === "environment" ? "自定义公开更新源" : "尚未配置";
  return <Modal title="软件更新" subtitle="新版会自动下载，并在关闭或重启墨流时安装；小说正文、正史数据库和本地项目不会被删除。" onClose={onClose}><section className={`update-card ${local.status || "ready"}`}><div><small>当前版本</small><strong>{local.currentVersion || "0.5.0"}</strong></div><div><small>可用版本</small><strong>{local.availableVersion || "—"}</strong></div><div><small>更新来源</small><strong>{sourceLabel}</strong></div>{typeof local.progress === "number" && <div className="update-progress"><span style={{ width: `${Math.max(0, Math.min(local.progress, 100))}%` }} /></div>}<p>{local.message || "墨流会自动检查新版本，也可以在这里立即检查。"}</p></section>{local.status === "not_configured" && <p className="form-hint">私密仓库的下载需要账号令牌，不适合写进大众软件。仓库或独立发布仓库公开后，只需在构建时配置发布源即可启用在线更新。</p>}<div className="dialog-actions"><button onClick={onClose}>关闭</button>{!new Set(["available", "downloading", "downloaded"]).has(String(local.status)) && <button className="primary" disabled={working || local.status === "not_configured" || local.status === "checking"} onClick={() => void action("check")}>{local.status === "checking" ? "正在检查…" : "检查新版本"}</button>}{local.status === "available" && <button className="primary" disabled>正在准备自动下载…</button>}{local.status === "downloading" && <button className="primary" disabled>正在下载 {Math.round(Number(local.progress || 0))}%</button>}{local.status === "downloaded" && <button className="primary" disabled={working} onClick={() => void action("install")}>重启并安装</button>}</div></Modal>;
}

function SelectionDialog({ selection, busy, onClose, onSubmit }: { selection: SelectionDraft; busy: boolean; onClose: () => void; onSubmit: (mode: "comment" | "revise", comment: string) => void }) {
  const [comment, setComment] = useState("");
  const quick = [
    "让这段更具体，用可感知的动作或物件替代抽象说明。",
    "收紧节奏，删除重复信息，但保留人物声线。",
    "让对白更自然，并符合人物关系和当下压力。",
    "补强因果衔接，不新增选区外事件。",
  ];
  const revisionBlocked = !selection.isDraft || selection.tooLong;
  return <Modal title="批注或局部修订" subtitle="Writer 只处理锁定选区；选区外正文保持不变。局部修订会保留旧版本，不会自动验收。" onClose={onClose}>
    <div className="selection-dialog">
      <div className="selection-source"><small>{selection.relativePath}</small><blockquote>{selection.quote.length > 1200 ? `${Array.from(selection.quote).slice(0, 1200).join("")}…` : selection.quote}</blockquote></div>
      <div className="selection-chips">{quick.map((item) => <button key={item} type="button" onClick={() => setComment(item)}>{item.slice(0, 6)}</button>)}</div>
      <label>你的意见<textarea autoFocus value={comment} onChange={(event) => setComment(event.target.value)} placeholder="说明只改哪里、保留什么；越具体越容易得到稳定结果。" /></label>
      <p className="form-hint">{selection.tooLong ? "选区超过 8000 字：可以留批注，但请缩小范围后再局部修订。" : selection.isDraft ? "当前是未验收草稿，可以生成新的局部修订版本。" : "当前不是未验收草稿：可以留批注；正史回改仍需先预览影响并确认。"}</p>
      <div className="dialog-actions"><button disabled={busy || !comment.trim()} onClick={() => onSubmit("comment", comment.trim())}>只留批注</button><button className="primary" disabled={busy || !comment.trim() || revisionBlocked} onClick={() => onSubmit("revise", comment.trim())}>让 Writer 局部修订</button></div>
    </div>
  </Modal>;
}

function ConversationHistoryDialog({ entries, onClose, onReuse }: { entries: ConversationHistoryEntry[]; onClose: () => void; onReuse: (value: string) => void }) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("zh-CN");
    const source = [...entries].reverse();
    if (!normalized) return source;
    return source.filter((entry) => `${entry.user}\n${entry.assistant}\n${entry.action_note}`.toLocaleLowerCase("zh-CN").includes(normalized));
  }, [entries, query]);
  return <Modal title="对话历史" subtitle="按项目保留最近 100 轮可见对话；不保存原始思维链、API Key 或模型内部推理。" onClose={onClose}>
    <div className="history-dialog">
      <div className="history-search"><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索问题、回答或工作流状态" /><span>{filtered.length} / {entries.length} 轮</span></div>
      <div className="history-list">
        {filtered.length === 0 && <p className="empty-mini">没有找到匹配的对话。</p>}
        {filtered.map((entry) => <article className="history-entry" key={entry.id}>
          <header><time dateTime={entry.recorded_at}>{entry.recorded_at ? formatTime(entry.recorded_at) : "较早记录"}</time><span>{entry.action_note || "历史对话"}</span></header>
          <div><strong>你</strong><p>{entry.user}</p></div>
          <div><strong>墨流</strong><p>{entry.assistant}</p></div>
          <button onClick={() => onReuse(entry.user)}>继续这个话题</button>
        </article>)}
      </div>
    </div>
  </Modal>;
}

function RevealText({ text, animate }: { text: string; animate: boolean }) {
  const characters = useMemo(() => Array.from(text), [text]);
  const prefersReduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const [count, setCount] = useState(animate && !prefersReduced ? 0 : characters.length);
  useEffect(() => {
    if (!animate || prefersReduced) { setCount(characters.length); return; }
    setCount(0);
    const timer = window.setInterval(() => setCount((value) => {
      const next = Math.min(value + 1, characters.length);
      if (next >= characters.length) window.clearInterval(timer);
      return next;
    }), 50);
    return () => window.clearInterval(timer);
  }, [text, animate, prefersReduced, characters.length]);
  const shown = characters.slice(0, count);
  const last = shown.pop();
  return <p aria-label={text}>{shown.join("")}{last && <span className="reveal-character" key={count}>{last}</span>}{count < characters.length && <button className="skip-reveal" onClick={() => setCount(characters.length)}>立即显示</button>}</p>;
}

function Modal({ title, subtitle, onClose, children, className = "" }: { title: string; subtitle: string; onClose: () => void; children: ReactNode; className?: string }) { return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className={`modal ${className}`} role="dialog" aria-modal="true" aria-label={title}><button className="modal-close" aria-label="关闭" onClick={onClose}>×</button><p className="eyebrow">INKFLOW</p><h2>{title}</h2><p className="modal-subtitle">{subtitle}</p>{children}</section></div>; }
function Toast({ kind, text, onClose }: { kind: "error" | "info"; text: string; onClose: () => void }) { return <div className={`toast ${kind}`}><span>{kind === "error" ? "!" : "i"}</span><p>{text}</p><button onClick={onClose}>×</button></div>; }
function EmptyPanel({ title, text }: { title: string; text: string }) { return <div className="empty-panel"><div>◇</div><h2>{title}</h2><p>{text}</p></div>; }
function InfoCard({ label, value, accent = false }: { label: string; value: unknown; accent?: boolean }) { return <article className={`info-card ${accent ? "accent" : ""}`}><small>{label}</small><p>{String(value || "尚未设置")}</p></article>; }

function visibleResult(result: unknown): { summary: string; details?: string; reasoning: string[] } {
  if (typeof result === "string") return { summary: result, reasoning: [] };
  if (!result || typeof result !== "object") return { summary: "任务完成，可以从右侧工作台查看结果。", reasoning: [] };
  const value = result as Record<string, unknown>;
  const reasoning = collectPublicSummaries(result);
  const safeDetails = JSON.stringify(displaySafeValue(value), null, 2);
  if (value.reply) {
    return { summary: String(value.reply), reasoning, details: safeDetails };
  }
  if (value.annotation_id && value.replacement_excerpt) return { summary: `第 ${String(value.chapter_no || "")} 章选区已生成新草稿版本 v${String(value.version || "")}。${String(value.next_action || "请重新审查当前版本。")}`, reasoning, details: safeDetails };
  if (value.help) return { summary: Array.isArray(value.help) ? value.help.map(String).join("\n") : String(value.help), reasoning };
  for (const key of ["message", "summary", "gate", "next_action"]) if (value[key]) return { summary: String(value[key]), reasoning, details: safeDetails };
  if (value.result && typeof value.result === "object") {
    const nested = visibleResult(value.result);
    return { summary: nested.summary, reasoning: [...new Set([...reasoning, ...nested.reasoning])].slice(0, 10), details: safeDetails };
  }
  return { summary: "工作流已返回结果，请查看右侧文件与过程面板。", reasoning, details: safeDetails };
}
function collectPublicSummaries(value: unknown, target: string[] = [], seen = new Set<unknown>()): string[] {
  if (!value || typeof value !== "object" || seen.has(value)) return target;
  seen.add(value);
  if (Array.isArray(value)) { value.forEach((item) => collectPublicSummaries(item, target, seen)); return target.slice(0, 10); }
  const record = value as Record<string, unknown>;
  const add = (item: unknown) => (Array.isArray(item) ? item : item ? [item] : []).map(String).map((item) => item.trim()).filter(Boolean).forEach((item) => { if (!target.includes(item)) target.push(item); });
  add(record.public_reasoning_summary);
  add(record.decision_summary);
  if (record.session && typeof record.session === "object") add((record.session as Record<string, unknown>).visible_reason);
  Object.entries(record).forEach(([key, nested]) => { if (!isSensitivePresentationKey(key)) collectPublicSummaries(nested, target, seen); });
  return target.slice(0, 10);
}
function displaySafeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(displaySafeValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).filter(([key]) => !isSensitivePresentationKey(key)).map(([key, nested]) => [key, displaySafeValue(nested)]));
}
function isSensitivePresentationKey(key: string): boolean {
  const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
  return /apikey|tracepath|chainofthought|rawthought|hiddenthought|reasoningcontent|providerreasoning|thinkingcontent|internalreasoning/.test(normalized)
    || normalized === "reasoning"
    || normalized === "thoughts"
    || normalized === "thinking";
}
function errorMessage(cause: unknown): string { return cause instanceof Error ? cause.message : String(cause); }
async function withDeadline<T>(request: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([request, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); })]); }
  finally { if (timer) clearTimeout(timer); }
}
function currentChapter(path?: string): number | null { const match = path?.match(/chapter_(\d+)/); return match ? Number(match[1]) : null; }
function tabLabel(tab: Tab): string { return ({ project: "项目", editor: "正文", chapter: "章工位", review: "审查", memory: "记忆", references: "参考", listen: "听读", process: "协作台" })[tab]; }
function eventLabel(value?: string): string { return ({ "run.started": "任务开始", "run.completed": "任务完成", "run.failed": "任务失败", "run.cancelled": "任务已停止", "workflow.started": "工作流启动", "workflow.completed": "工作流完成", "controller.routing": "理解与路由", "writer.started": "写作角色构思", "writer.completed": "写作角色完成", "provider.testing": "模型连接" } as Record<string, string>)[value || ""] || value || "过程"; }
function credentialLabel(value: string): string { return ({ windows_credential_manager: "Windows 凭据库", "environment:INKFLOW_API_KEY": "系统环境变量", "environment:DEEPSEEK_API_KEY": "DeepSeek 环境变量" } as Record<string, string>)[value] || "本机安全存储"; }
function methodLabel(value?: string): string { return ({ "conversation.send": "自然对话", "document.revise_selection": "局部修订", "workflow.run": "小说工作流", "project.ideate": "从零构思", "provider.test": "模型连接测试", "reference.search": "搜索公开写作资料", "reference.fetch": "抓取参考资料", "reference.analyze": "分析参考资料" } as Record<string, string>)[value || ""] || "墨流任务"; }
function processRuns(events: EngineEvent[]) {
  const visibleMethods = new Set(["conversation.send", "document.revise_selection", "workflow.run", "project.ideate", "provider.test", "reference.search", "reference.fetch", "reference.analyze"]);
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
