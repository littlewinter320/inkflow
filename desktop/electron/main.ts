import { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, Notification, screen, shell } from "electron";
import { AppUpdater, NsisUpdater, autoUpdater } from "electron-updater";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { appendFileSync, cpSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import * as asyncFs from "node:fs/promises";
import readline from "node:readline";

const isDevelopment = !app.isPackaged || Boolean(process.env.VITE_DEV_SERVER_URL
  && existsSync(path.resolve(__dirname, "..", "..", "agent", "src", "inkflow", "app_server.py")));
const applicationId = isDevelopment ? "cn.inkflow.desktop.dev" : "cn.inkflow.desktop";
const applicationName = "墨流 InkFlow";
if (process.platform === "win32") app.setAppUserModelId(applicationId);
for (const [name, stream] of [["stdout", process.stdout], ["stderr", process.stderr]] as const) {
  stream.on("error", (cause: NodeJS.ErrnoException) => {
    logLifecycle("stdio.unavailable", { stream: name, code: cause.code || "unknown" });
  });
}

// Lifecycle metadata only; never record prompts, novel text, keys or request payloads.
function logLifecycle(event: string, details: Record<string, unknown> = {}): void {
  try {
    const directory = path.join(app.getPath("userData"), "logs");
    mkdirSync(directory, { recursive: true });
    const file = path.join(directory, "desktop-lifecycle.jsonl");
    if (existsSync(file) && statSync(file).size > 512_000) renameSync(file, `${file}.previous`);
    appendFileSync(file, JSON.stringify({ timestamp: new Date().toISOString(), pid: process.pid, event, ...details }) + "\n", "utf8");
  } catch {
    // A diagnostic disk failure must not terminate the application.
  }
}

type Pending = {
  resolve: (value: unknown) => void;
  reject: (reason?: unknown) => void;
  child: ChildProcessWithoutNullStreams;
  method: string;
  runId: string;
  conversationId: string;
  action: string;
  settled: Promise<void>;
  markSettled: () => void;
};

type UpdateState = {
  status: "not_configured" | "ready" | "checking" | "available" | "current" | "downloading" | "downloaded" | "error";
  currentVersion: string;
  availableVersion?: string;
  progress?: number;
  message: string;
  source: "none" | "embedded" | "github" | "environment";
};

function redactEngineDiagnostics(value: unknown): string {
  // 引擎的校验诊断是{type,loc,msg}数组，其他错误也可能只返回文本；不能只用TS断言当运行时校验。
  // 只读取明确的诊断字段，不序列化整个对象，避免把input、上下文、书稿或密钥附带写进日志。
  const diagnosticText = (item: unknown, depth = 0): string => {
    if (typeof item === "string") return item;
    if (typeof item === "number" && Number.isFinite(item)) return String(item);
    if (typeof item === "boolean") return item ? "true" : "false";
    if (item == null) return "";
    if (depth >= 3) return "（更深层诊断请查看任务记录）";
    if (Array.isArray(item)) {
      const parts = item.slice(0, 20).map(entry => diagnosticText(entry, depth + 1)).filter(Boolean);
      if (item.length > 20) parts.push(`另有 ${item.length - 20} 项诊断，请查看任务记录。`);
      return parts.join("\n");
    }
    if (typeof item === "object") {
      const detail = item as Record<string, unknown>;
      const message = typeof detail.msg === "string" ? detail.msg : typeof detail.message === "string" ? detail.message : "";
      const kind = typeof detail.type === "string" ? detail.type : "";
      const location = Array.isArray(detail.loc) ? detail.loc
        .filter(part => typeof part === "string" || (typeof part === "number" && Number.isFinite(part)))
        .join(".") : typeof detail.loc === "string" ? detail.loc : "";
      return [location, kind, message].filter(Boolean).join("：") || "（诊断格式暂不能展开，请查看任务记录）";
    }
    return "（诊断格式暂不能展开，请查看任务记录）";
  };
  // 先提取允许的文本，再脱敏和限长；数组、Error和未知对象都不能绕过密钥隐藏。
  return diagnosticText(value)
    .replace(/(bearer\s+|api[_ -]?key\s*[:=]\s*)[^\s,;]+/gi, "$1[已隐藏]")
    .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[已隐藏密钥]")
    .slice(-1_200);
}

function resolveInkFlowProject(rootValue: string): string {
  const root = realpathSync(path.resolve(String(rootValue || "")));
  const stat = lstatSync(root);
  if (!stat.isDirectory() || !existsSync(path.join(root, ".inkflow", "project.json"))) {
    throw new Error("这不是可识别的墨流项目文件夹，未执行操作。");
  }
  return root;
}

function pathContains(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

type RecentProject = { root: string; title: string; openedAt: string };

function recentProjectsFile(): string {
  return path.join(app.getPath("userData"), "recent-projects.json");
}

function recentProjects(): RecentProject[] {
  try {
    const saved: unknown = JSON.parse(readFileSync(recentProjectsFile(), "utf8"));
    if (!Array.isArray(saved)) return [];
    return saved.filter((item): item is RecentProject =>
      item && typeof item.root === "string" && path.isAbsolute(item.root)
      && typeof item.title === "string" && typeof item.openedAt === "string",
    ).slice(0, 5);
  } catch {
    return [];
  }
}

function saveRecentProjects(items: RecentProject[]): RecentProject[] {
  const file = recentProjectsFile();
  mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  writeFileSync(temporary, JSON.stringify(items.slice(0, 5)), "utf8");
  renameSync(temporary, file);
  return items.slice(0, 5);
}

function sameProjectRoot(left: string, right: string): boolean {
  return path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();
}

function workspaceViewsFile(): string {
  return path.join(app.getPath("userData"), "workspace-views.json");
}

function workspaceViews(): Array<Record<string, unknown>> {
  try {
    const saved: unknown = JSON.parse(readFileSync(workspaceViewsFile(), "utf8"));
    if (!Array.isArray(saved) || saved.some(item => !item || typeof item.projectRoot !== "string" || !path.isAbsolute(item.projectRoot))) {
      throw new Error("上次界面记录格式异常，原文件已保留，未重置或覆盖。");
    }
    return saved;
  } catch (cause) {
    if ((cause as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw cause;
  }
}

let lastWorkspaceView = "";
const deletedConversationViews = new Set<string>();
/** 仅在本进程阻止已授权删除的旧页卸载写回；重开后的目录身份由共享服务核对。 */
const conversationViewKey = (root: string, identity: string) => `${path.resolve(root).toLowerCase()}\n${identity}`;
/** 所有界面记录共用原子写入，删除和普通保存不维护第二份草稿文件。 */
function writeWorkspaceViews(views: Array<Record<string, unknown>>): void {
  const file = workspaceViewsFile();
  mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try { writeFileSync(temporary, JSON.stringify(views), "utf8"); renameSync(temporary, file); }
  finally { if (existsSync(temporary)) unlinkSync(temporary); }
}
function saveWorkspaceView(value: unknown): void {
  if (!value || typeof value !== "object") return;
  const view = value as Record<string, unknown>;
  if (typeof view.projectRoot !== "string" || !path.isAbsolute(view.projectRoot)) return;
  const serialized = JSON.stringify(view);
  if (serialized === lastWorkspaceView) return;
  // 旧记录没有conversationId时归入原聊天；各聊天的输入、队列和未保存正文候选独立保留。
  const identity = typeof view.conversationId === "string" ? view.conversationId : "main";
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(identity)) throw new Error("聊天界面身份无效，原记录未覆盖。");
  if (deletedConversationViews.has(conversationViewKey(view.projectRoot, identity))) return;
  const views = [{ ...view, conversationId: identity }, ...workspaceViews().filter(item =>
    !sameProjectRoot(String(item.projectRoot), view.projectRoot as string) || (item.conversationId || "main") !== identity)];
  writeWorkspaceViews(views);
  lastWorkspaceView = serialized;
}

type Appearance = {
  theme: "dark" | "light" | "system";
  accent: "lime" | "jade" | "blue" | "violet" | "amber" | "rose";
  density: "comfortable" | "compact";
  graphNodeLimit?: number | null;
  prefillEnabled?: boolean;
  prefillDelayMs?: number;
  prefillLength?: "short" | "medium" | "long";
  suggestedPromptsEnabled?: boolean;
  suggestionShortcutEnabled?: boolean;
  suggestionShortcut?: string;
};

// 只接受界面偏好白名单。旧记录缺少的字段保留为缺失，以便从旧工作台记录恢复。
function checkedAppearance(value: unknown): Appearance | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const saved = value as Record<string, unknown>;
  if (typeof saved.theme !== "string" || !["dark", "light", "system"].includes(saved.theme)
    || typeof saved.accent !== "string" || !["lime", "jade", "blue", "violet", "amber", "rose"].includes(saved.accent)
    || typeof saved.density !== "string" || !["comfortable", "compact"].includes(saved.density)) return null;
  const limit = saved.graphNodeLimit;
  const preferences: Appearance = { theme: saved.theme as Appearance["theme"], accent: saved.accent as Appearance["accent"], density: saved.density as Appearance["density"],
    ...(limit === null || (typeof limit === "number" && Number.isSafeInteger(limit) && limit > 0) ? { graphNodeLimit: limit } : {}) };
  for (const key of ["prefillEnabled", "suggestedPromptsEnabled", "suggestionShortcutEnabled"] as const) {
    if (typeof saved[key] === "boolean") preferences[key] = saved[key];
  }
  if (typeof saved.prefillDelayMs === "number" && Number.isInteger(saved.prefillDelayMs)
    && saved.prefillDelayMs >= 300 && saved.prefillDelayMs <= 3000) preferences.prefillDelayMs = saved.prefillDelayMs;
  if (saved.prefillLength === "short" || saved.prefillLength === "medium" || saved.prefillLength === "long") preferences.prefillLength = saved.prefillLength;
  if (typeof saved.suggestionShortcut === "string" && saved.suggestionShortcut.length <= 80) preferences.suggestionShortcut = saved.suggestionShortcut;
  return preferences;
}

// 外观跟随桌面应用保存，避免开发端口改变后首页读不到原来的白色主题。
function appearanceFile(): string {
  return path.join(app.getPath("userData"), "appearance.json");
}

// 旧版原生文件只存了部分字段，升级时借用最近一次工作台快照补齐。
function readAppearance(): Appearance | null {
  let saved: Appearance | null = null;
  try {
    saved = checkedAppearance(JSON.parse(readFileSync(appearanceFile(), "utf8")));
    if (!saved) throw new Error("外观记录格式异常");
    if (saved.graphNodeLimit !== undefined && saved.prefillEnabled !== undefined && saved.prefillDelayMs !== undefined
      && saved.prefillLength !== undefined && saved.suggestedPromptsEnabled !== undefined
      && saved.suggestionShortcutEnabled !== undefined && saved.suggestionShortcut !== undefined) return saved;
  } catch (cause) {
    if ((cause as NodeJS.ErrnoException).code !== "ENOENT") logLifecycle("appearance.read.failed");
  }
  try {
    const legacy = checkedAppearance(workspaceViews()[0]?.uiPreferences);
    return saved && legacy ? { ...legacy, ...saved } : saved || legacy;
  } catch { logLifecycle("appearance.legacy.read.failed"); return saved; }
}

// 先写完整临时文件再替换；保存失败保留旧设置。
function saveAppearance(value: unknown): void {
  const saved = checkedAppearance(value);
  if (!saved) throw new Error("外观设置无效，请重新选择主题、配色和显示密度。");
  const file = appearanceFile();
  mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
  try {
    writeFileSync(temporary, JSON.stringify(saved), "utf8");
    renameSync(temporary, file);
  } finally { if (existsSync(temporary)) unlinkSync(temporary); }
  // 原生菜单/下拉弹层也要跟随应用主题，不能只改网页CSS后仍沿用系统黑色主题。
  nativeTheme.themeSource = saved.theme;
}

function rememberRecentProject(rootValue: string, titleValue: string): RecentProject[] {
  const root = resolveInkFlowProject(rootValue);
  const title = String(titleValue || "未命名小说").trim().slice(0, 160) || "未命名小说";
  return saveRecentProjects([
    { root, title, openedAt: new Date().toISOString() },
    ...recentProjects().filter((item) => !sameProjectRoot(item.root, root)),
  ]);
}

function forgetRecentProject(root: string): RecentProject[] {
  return saveRecentProjects(recentProjects().filter((item) => !sameProjectRoot(item.root, root)));
}

class UpdateManager {
  private updater: AppUpdater | null = null;
  private state: UpdateState;

  constructor(private readonly window: BrowserWindow) {
    const configuredUrl = String(process.env.INKFLOW_UPDATE_URL || "").trim();
    const embeddedConfig = path.join(process.resourcesPath, "app-update.yml");
    const source: UpdateState["source"] = configuredUrl
      ? "environment"
      : !isDevelopment && existsSync(embeddedConfig)
        ? "embedded"
        : !isDevelopment
          ? "github"
          : "none";
    this.state = {
      status: source === "none" ? "not_configured" : "ready",
      currentVersion: app.getVersion(),
      message: source === "none" ? "当前私密预览版尚未配置公开更新源。" : "可以检查是否有新版本。",
      source,
    };
    if (source === "environment") {
      this.updater = new NsisUpdater({ provider: "generic", url: configuredUrl });
    } else if (source === "embedded") {
      this.updater = autoUpdater;
    } else if (source === "github") {
      // Keep already-installed packages updateable even if an older build was
      // accidentally produced without app-update.yml. Future releases still
      // need a published GitHub Release with latest.yml and the NSIS assets.
      this.updater = new NsisUpdater({
        provider: "github",
        owner: "littlewinter320",
        repo: "inkflow",
        releaseType: "release",
      });
    }
    if (!this.updater) return;
    // Reuse unchanged blocks from the cached installer. electron-updater falls
    // back to a full download when the old installer or blockmap is unavailable.
    this.updater.disableDifferentialDownload = false;
    this.updater.autoDownload = true;
    // Installing an update must be an explicit user action. Automatic
    // install-on-quit can restart the desktop while a Writer request is still
    // waiting on the local engine.
    this.updater.autoInstallOnAppQuit = false;
    this.updater.on("checking-for-update", () => this.setState({ status: "checking", message: "正在检查新版本…" }));
    this.updater.on("update-available", (info: { version: string }) => this.setState({ status: "available", availableVersion: info.version, message: `发现新版本 ${info.version}，正在自动下载。` }));
    this.updater.on("update-not-available", () => this.setState({ status: "current", availableVersion: undefined, message: "当前已经是最新版本。" }));
    this.updater.on("download-progress", (progress: { percent: number }) => this.setState({ status: "downloading", progress: Math.round(progress.percent), message: `正在下载更新：${Math.round(progress.percent)}%` }));
    this.updater.on("update-downloaded", (info: { version: string }) => this.setState({ status: "downloaded", availableVersion: info.version, progress: 100, message: "更新已经下载完成；点击重启安装后应用更新，关闭墨流会保留下载结果。" }));
    this.updater.on("error", (cause: Error) => this.setState({ status: "error", message: `更新失败：${cause.message}` }));
    this.window.webContents.once("did-finish-load", () => {
      setTimeout(() => {
        if (!this.window.isDestroyed()) void this.check().catch(() => undefined);
      }, 1200);
    });
  }

  status(): UpdateState { return { ...this.state }; }

  async check(): Promise<UpdateState> {
    if (isDevelopment) return this.setState({ status: "not_configured", message: "开发模式不执行在线更新；请构建安装版后检查。" });
    if (!this.updater) return this.state;
    await this.updater.checkForUpdates();
    return this.state;
  }

  async download(): Promise<UpdateState> {
    if (!this.updater || this.state.status !== "available") return this.state;
    await this.updater.downloadUpdate();
    return this.state;
  }

  install(): UpdateState {
    if (!this.updater || this.state.status !== "downloaded") return this.state;
    this.updater.quitAndInstall(false, true);
    return this.state;
  }

  private setState(update: Partial<UpdateState>): UpdateState {
    this.state = { ...this.state, ...update };
    if (!this.window.isDestroyed() && !this.window.webContents.isDestroyed()) {
      this.window.webContents.send("app:update-status", this.state);
    }
    return this.state;
  }
}

class EngineBridge {
  private process: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, Pending>();
  private backgroundRuns = new Map<string, Pick<Pending, "child" | "conversationId" | "settled" | "markSettled">>();
  private compatibilityCheck: Promise<void> | null = null;
  private shuttingDown = false;
  private shutdownPromise: Promise<void> | null = null;

  start(): void {
    if (this.shuttingDown) return;
    if (this.process && !this.process.killed && this.process.exitCode === null) return;
    const command = this.command();
    const child = spawn(command.executable, command.args, {
      cwd: command.cwd,
      env: { ...process.env, PYTHONUTF8: "1", ...command.env },
      // Give the engine its own hidden Windows process group. If it shares the
      // developer launcher's console, unrelated Ctrl+C/control events can
      // surface as KeyboardInterrupt in the middle of a model call. Electron
      // still owns the child handle and stops it explicitly when the app exits.
      detached: true,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.process = child;
    child.stdin.on("error", (cause) => this.failChild(child, new Error(`墨流本地引擎通信中断：${cause.message}`)));
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => this.receive(line, child));
    let stderrTail = "";
    child.stderr.on("data", (chunk: Buffer) => {
      stderrTail = redactEngineDiagnostics(`${stderrTail}${chunk.toString("utf8")}`);
      this.send("engine:status", {
        level: "warning",
        message: "本地写作引擎报告了诊断信息；如任务失败，可查看过程面板。",
        details: stderrTail,
      });
    });
    child.once("error", (cause) => this.failChild(child, new Error(`无法启动墨流本地引擎：${cause.message}`)));
    child.once("exit", (code, signal) => {
      logLifecycle("engine.exited", { enginePid: child.pid, code, signal, requested: this.shuttingDown || this.process !== child });
      const suffix = stderrTail ? `\n诊断摘要：${stderrTail}` : "";
      this.failChild(child, new Error(`墨流本地引擎已退出（代码 ${code ?? "unknown"}${signal ? `，信号 ${signal}` : ""}）。原任务可能已保存部分进度，请先核对任务记录，不要直接重发整项创作请求。${suffix}`));
    });
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (method !== "app.initialize") await this.ensureCompatible();
    return this.requestRaw(method, params);
  }

  private async ensureCompatible(): Promise<void> {
    if (!this.compatibilityCheck) {
      this.compatibilityCheck = this.requestRaw("app.initialize").then((value) => {
        const details = value as { version?: string; protocol_version?: number };
        const desktopVersion = app.getVersion();
        const engineVersion = String(details.version || "");
        const protocolVersion = Number(details.protocol_version || 0);
        if (engineVersion !== desktopVersion || protocolVersion !== 1) {
          throw new Error(`桌面程序 ${desktopVersion} 与内置引擎 ${engineVersion || "未知"} 不兼容。为避免损坏配置，已停止本次请求；请重新安装当前版本。`);
        }
      });
    }
    const check = this.compatibilityCheck;
    try {
      await check;
    } catch (cause) {
      // A transient startup/pipe failure must not poison every later request.
      // The next request rechecks the newly started engine's version.
      if (this.compatibilityCheck === check) this.compatibilityCheck = null;
      throw cause;
    }
  }

  private async requestRaw(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (this.shuttingDown) throw new Error("墨流正在保存退出状态，请稍后再试。");
    this.start();
    const child = this.process;
    if (!child || child.killed || child.exitCode !== null) {
      throw new Error("墨流本地引擎暂时不可用，请重试。");
    }
    return this.requestOnChild(child, method, params);
  }

  private requestOnChild(child: ChildProcessWithoutNullStreams, method: string, params: Record<string, unknown>): Promise<unknown> {
    if (child.killed || child.exitCode !== null || child.signalCode !== null) {
      return Promise.reject(new Error("墨流本地引擎已停止，未能发送本次取消请求。"));
    }
    const id = randomUUID();
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => {
      let markSettled: () => void = () => {};
      const settled = new Promise<void>((done) => { markSettled = done; });
      const pending: Pending = {
        resolve,
        reject,
        child,
        method,
        runId: typeof params.run_id === "string" ? params.run_id.trim() : "",
        conversationId: typeof params.conversation_id === "string" ? params.conversation_id : "main",
        action: typeof params.action === "string" ? params.action.trim() : "",
        settled,
        markSettled,
      };
      this.pending.set(id, pending);
      const handleWriteError = (error: Error | null | undefined) => {
        if (error) {
          if (this.pending.get(id) === pending) {
            this.pending.delete(id);
            pending.markSettled();
            reject(error);
          }
        }
      };
      try {
        child.stdin.write(`${payload}\n`, "utf8", handleWriteError);
      } catch (cause) {
        handleWriteError(cause instanceof Error ? cause : new Error(String(cause)));
      }
    });
  }

  stop(): void {
    const child = this.process;
    this.finishBackgroundRuns(child || undefined);
    if (!child) return;
    if (child.killed || child.exitCode !== null || child.signalCode !== null) {
      this.process = null;
      this.compatibilityCheck = null;
      return;
    }
    logLifecycle("engine.stop-requested", { enginePid: child?.pid });
    this.shuttingDown = true;
    this.process = null;
    this.compatibilityCheck = null;
    for (const [id, item] of this.pending.entries()) {
      if (item.child !== child) continue;
      item.reject(new Error("墨流桌面已关闭，本次引擎任务被中断；已写入的文件会保留。"));
      item.markSettled();
      this.pending.delete(id);
    }
    child?.kill();
  }

  shutdownForExit(): Promise<void> {
    if (this.shutdownPromise) return this.shutdownPromise;
    this.shuttingDown = true;
    this.shutdownPromise = this.shutdownChildForExit(this.process);
    return this.shutdownPromise;
  }

  private async shutdownChildForExit(child: ChildProcessWithoutNullStreams | null): Promise<void> {
    if (!child || child.killed || child.exitCode !== null || child.signalCode !== null) {
      logLifecycle("engine.shutdown.no-live-child", { pending_count: this.pending.size });
      return;
    }

    // Seal background dispatch before taking the final active-work snapshot.
    const sealed = await this.waitForCompletion(this.requestOnChild(child, "run.cancel", {
      run_id: "", source: "app_exit", reason: "桌面退出；保留原配置、已用次数和未完成断点。",
    }), 5_000);
    if (!sealed) {
      logLifecycle("engine.shutdown.seal-timeout");
      this.stop();
      return;
    }
    const pending = [...this.pending.values()].filter((item) => item.child === child);
    const existingCancelRunIds = new Set(
      pending.filter((item) => item.method === "run.cancel" && item.runId).map((item) => item.runId),
    );
    const workRequests = pending.filter((item) => this.isCancellableWork(item));
    const runRequests = new Map<string, Pending[]>();
    const unidentifiableWork = workRequests
      .filter((item) => !item.runId)
      .map((item) => ({ method: item.method, action: item.action || undefined, run_id: null, reason: "missing_run_id" }));
    for (const item of workRequests) {
      if (!item.runId) continue;
      const entries = runRequests.get(item.runId) || [];
      entries.push(item);
      runRequests.set(item.runId, entries);
    }
    const background = [...this.backgroundRuns.entries()].filter(([, item]) => item.child === child);
    const runIds = [...new Set([...runRequests.keys(), ...background.map(([runId]) => runId)])];
    const requestsToCancel = runIds.filter((runId) => !existingCancelRunIds.has(runId));
    const existingCancelRequests = pending.filter((item) => item.method === "run.cancel");
    const waitForRequests = [
      ...workRequests,
      ...existingCancelRequests.filter((item) => !workRequests.includes(item)),
    ];
    const uncancelledMethods = [...new Set(
      pending.filter((item) => !workRequests.includes(item) && item.method !== "run.cancel")
        .map((item) => item.method),
    )].sort();

    logLifecycle("engine.shutdown.cancel-started", {
      run_ids: runIds,
      already_cancelling_run_ids: [...existingCancelRunIds],
      run_ids_missing: unidentifiableWork,
      not_cancelled_pending_methods: uncancelledMethods,
      wait_timeout_ms: 5_000,
    });

    const cancelResults: Array<{ run_id: string; cancelled?: boolean; error?: string }> = [];
    const cancelAcknowledgements = Promise.all(requestsToCancel.map(async (runId) => {
      try {
        const result = await this.requestOnChild(child, "run.cancel", {
          run_id: runId, source: "app_exit", reason: "桌面退出；保留原配置、已用次数和未完成断点。",
        }) as { cancelled?: unknown } | null;
        cancelResults.push({ run_id: runId, cancelled: result?.cancelled === true });
      } catch (cause) {
        cancelResults.push({ run_id: runId, error: cause instanceof Error ? cause.message : String(cause) });
      }
    }));
    const requestsSettled = Promise.all([...waitForRequests.map((item) => item.settled),
      ...background.map(([, item]) => item.settled)]);
    const completed = await this.waitForCompletion(Promise.all([cancelAcknowledgements, requestsSettled]), 5_000);
    if (!completed) {
      logLifecycle("engine.shutdown.cancel-timeout", {
        run_ids: runIds,
        run_ids_missing: unidentifiableWork,
        timeout_ms: 5_000,
        cancel_results: cancelResults,
      });
      this.stop();
      return;
    }

    logLifecycle("engine.shutdown.cancel-complete", {
      run_ids: runIds,
      run_ids_missing: unidentifiableWork,
      cancel_results: cancelResults,
      waited_for: uncancelledMethods,
    });
    child.stdin.end();
    const exited = await this.waitForChildExit(child, 2_000);
    if (!exited) {
      const remaining = [...this.pending.values()].filter((item) => item.child === child);
      logLifecycle("engine.shutdown.exit-timeout", {
        run_ids: runIds,
        run_ids_missing: unidentifiableWork,
        timeout_ms: 2_000,
        pending_methods: [...new Set(remaining.map((item) => item.method))].sort(),
      });
      this.stop();
      return;
    }
    logLifecycle("engine.shutdown.graceful-exit", { run_ids: runIds });
  }

  private isCancellableWork(item: Pending): boolean {
    if (["conversation.send", "document.revise_selection", "task.retry", "manual_edits.review"].includes(item.method)) return true;
    if (item.method !== "workflow.run") return false;
    return !["checkpoint_list", "rollback_preview"].includes(item.action);
  }

  private async waitForCompletion(completion: Promise<unknown>, timeoutMs: number): Promise<boolean> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const completed = await Promise.race([
      completion.then(() => true),
      new Promise<boolean>((resolve) => { timer = setTimeout(() => resolve(false), timeoutMs); }),
    ]);
    if (timer !== undefined) clearTimeout(timer);
    return completed;
  }

  private waitForChildExit(child: ChildProcessWithoutNullStreams, timeoutMs: number): Promise<boolean> {
    if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      const finish = (exited: boolean) => {
        if (timer !== undefined) clearTimeout(timer);
        child.removeListener("exit", onExit);
        child.removeListener("error", onError);
        resolve(exited);
      };
      const onExit = () => finish(true);
      const onError = () => finish(child.exitCode !== null || child.signalCode !== null);
      child.once("exit", onExit);
      child.once("error", onError);
      timer = setTimeout(() => finish(child.exitCode !== null || child.signalCode !== null), timeoutMs);
    });
  }

  pendingCount(conversationId?: string): number {
    return conversationId ? [...this.pending.values()].filter(item => item.conversationId === conversationId).length : this.pending.size;
  }

  pendingWorkCount(conversationId?: string): number {
    // Passive preloading and status requests cannot turn an idle window into a running task.
    const reads = new Set(["project.preload", "project.open", "project.status", "project.tree", "collaboration.overview", "collaboration.list", "batch.progress",
      "context.status", "context.details", "usage.overview", "task.status", "task.list", "document.read", "document.read_reference", "document.version.read", "document.search",
      "settings.get", "computer.actions.list", "voice.status", "voice.background", "voice.settings.get", "voice.job.list", "voice.job.status", "voice.profile.list", "voice.roles.get", "project.trash.list", "project.canon_migration.status", "conversation.history", "conversation.list", "memory.index",
      "story_settings.list", "story_settings.maintenance", "story_settings.records", "story_settings.templates", "manual_edits.scan", "manual_edits.status", "memory.audit",
      "knowledge.index", "knowledge.page", "knowledge.source", "knowledge.search", "knowledge.graph", "knowledge.status"]);
    // Unknown requests still protect exit; only known status and read requests are passive.
    const work = [...this.pending.values()].filter(item => (!conversationId || item.conversationId === conversationId)
      && !reads.has(item.method) && item.method !== "run.cancel"
      && !(item.method === "preferences.manage" && ["list", "usage", "history"].includes(item.action))
      && !(item.method === "workflow.run" && ["checkpoint_list", "rollback_preview"].includes(item.action)));
    return work.length + [...this.backgroundRuns].filter(([runId, item]) =>
      (!conversationId || item.conversationId === conversationId) && !work.some(request => request.runId === runId)).length;
  }

  pendingMethods(): string[] {
    // Method names are enough to diagnose a stale read request without
    // logging user text, file paths, API credentials, or request parameters.
    return [...new Set([...this.pending.values()].map((item) => item.method))].sort();
  }

  private failChild(child: ChildProcessWithoutNullStreams, error: Error): void {
    this.finishBackgroundRuns(child);
    for (const [id, item] of this.pending.entries()) {
      if (item.child !== child) continue;
      item.reject(error);
      item.markSettled();
      this.pending.delete(id);
    }
    if (this.process !== child) return;
    this.process = null;
    this.compatibilityCheck = null;
    if (this.shuttingDown) return;
    this.send("engine:status", { level: "error", message: error.message });
  }

  private finishBackgroundRuns(child?: ChildProcessWithoutNullStreams): void {
    for (const [runId, item] of this.backgroundRuns) {
      if (child && item.child !== child) continue;
      item.markSettled();
      this.backgroundRuns.delete(runId);
    }
  }

  private receive(line: string, child: ChildProcessWithoutNullStreams): void {
    let value: Record<string, unknown>;
    try {
      value = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return;
    }
    // 损坏的JSON-RPC行不能触发主进程异常；只接受对象封包，原请求继续等有效回执。
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    if (value.method === "event") {
      if (child !== this.process) return;
      const event = value.params as { type?: string; summary?: string; run_id?: string; conversation_id?: string; _background?: boolean } | undefined;
      if (event?._background === true && event.run_id) {
        if (event.type === "run.started" && !this.backgroundRuns.has(event.run_id)) {
          let markSettled: () => void = () => {};
          const settled = new Promise<void>(done => { markSettled = done; });
          this.backgroundRuns.set(event.run_id, { child, conversationId: event.conversation_id || "main", settled, markSettled });
        } else if (/^run\.(completed|failed|cancelled|interrupted|waiting_user|waiting_condition)$/.test(event.type || "")) {
          this.backgroundRuns.get(event.run_id)?.markSettled();
          this.backgroundRuns.delete(event.run_id);
        }
      }
      this.send("engine:event", value.params);
      // 素材内容和原文只在应用内展示，系统通知仅提示回到设置查看。
      if (event?.run_id?.startsWith("preference-material-") &&
          /^run\.(waiting_user|waiting_condition|failed|interrupted)$/.test(event.type || "") && Notification.isSupported()) {
        try {
          new Notification({
            title: event.type === "run.waiting_user" ? "写作素材有结果待查看" : "写作素材分析未完成",
            body: "打开墨流的「作者习惯与记忆」查看结果、原因或待答问题。",
            icon: applicationIconPath(),
          }).show();
        } catch { /* 系统通知不可用时，应用内仍保留任务回执。 */ }
      }
      if (/^voice\.(edge|asr)\.(ready|models_failed|install\.failed)$/.test(event?.type || "") && Notification.isSupported()) {
        try {
          new Notification({
            title: event?.type?.endsWith("ready") ? "语音组件安装完成" : "语音组件安装未完成",
            body: event?.type?.endsWith("ready") ? "可以回到墨流使用语音功能。" : "已有下载保留，请在墨流的设置 → 语音中查看原因并重试。",
            icon: applicationIconPath(),
          }).show();
        } catch {
          // In-app status remains available when system notifications are unavailable.
        }
      }
      return;
    }
    const id = String(value.id ?? "");
    const pending = this.pending.get(id);
    if (!pending || pending.child !== child) return;
    this.pending.delete(id);
    pending.markSettled();
    if (value.error) {
      const error = typeof value.error === "object" && !Array.isArray(value.error)
        ? value.error as Record<string, unknown> : {};
      const title = typeof error.title === "string" ? redactEngineDiagnostics(error.title) : "";
      const message = redactEngineDiagnostics(error.message ?? (
        typeof value.error === "string" || Array.isArray(value.error) ? value.error : ""));
      const code = typeof error.code === "string" || (typeof error.code === "number" && Number.isFinite(error.code))
        ? redactEngineDiagnostics(error.code) : "unknown";
      const impact = typeof error.impact === "string" ? redactEngineDiagnostics(error.impact) : "";
      const details = redactEngineDiagnostics(error.details);
      const actions = Array.isArray(error.actions) ? error.actions.flatMap(item => {
        if (!item || typeof item !== "object" || typeof item.label !== "string") return [];
        return [redactEngineDiagnostics(item.label)];
      }).filter(Boolean) : [];
      // 非文本details先规范化再记录；此处不再可能因.replace/.map失败让已移出pending的Promise悬空。
      logLifecycle("engine.request.failed", { method: pending.method, code, ...(details ? { details } : {}) });
      const lines = [
        title && title !== message ? title : "",
        message || (code !== "unknown" ? code : "墨流引擎请求失败。"),
        details ? `具体原因：${details}` : "",
        impact,
        actions.length ? `下一步：${actions.join("；")}` : "",
      ].filter(Boolean);
      pending.reject(new Error(lines.join("\n")));
    } else {
      pending.resolve(value.result);
    }
  }

  private send(channel: string, payload: unknown): void {
    for (const window of BrowserWindow.getAllWindows()) {
      if (!window.isDestroyed() && !window.webContents.isDestroyed()) window.webContents.send(channel, payload);
    }
  }

  private command(): { executable: string; args: string[]; cwd: string; env?: Record<string, string> } {
    if (!isDevelopment) {
      const executable = path.join(process.resourcesPath, "engine", "inkflow-engine.exe");
      if (!existsSync(executable)) throw new Error(`找不到墨流本地引擎：${executable}`);
      return { executable, args: [], cwd: path.dirname(executable) };
    }
    const repository = path.resolve(__dirname, "..", "..");
    const python = path.join(repository, ".venv", "Scripts", "pythonw.exe");
    const consolePython = path.join(repository, ".venv", "Scripts", "python.exe");
    return {
      // pythonw keeps the long-running JSON-RPC engine completely in the
      // background. Its stdio pipes still belong to Electron, so diagnostics
      // and structured events remain available without opening a terminal.
      executable: existsSync(python) ? python : existsSync(consolePython) ? consolePython : "python",
      args: ["-m", "inkflow.app_server"],
      cwd: repository,
      env: { PYTHONPATH: path.join(repository, "agent", "src") },
    };
  }
}

let mainWindow: BrowserWindow | null = null;
type WorkspacePanelId = "navigation" | "assistant" | "inspector" | "workbench";
const conversationWindows = new Map<number, { projectRoot: string; conversationId: string; panel?: WorkspacePanelId; ownerId?: number; relativePath?: string; shared?: boolean; returnHidden?: boolean }>();
const conversationSwitches = new Set<number>();
const workspaceStates = new Map<number, Record<string, unknown>>();
let bridge: EngineBridge | null = null;
let voiceDataDirectory = "";

// Keep recording management inside the same directory used by microphone capture.
function voiceInputDirectory(): string {
  return voiceDataDirectory ? path.join(voiceDataDirectory, "cache", "voice-input") : process.platform === "win32" && existsSync("D:/") ? "D:/墨流/cache/voice-input" : path.join(app.getPath("userData"), "voice-input");
}

// Only app-owned recording names in the current storage root can be changed or recycled.
async function managedVoiceRecording(target: string): Promise<string> {
  if (typeof target !== "string") throw new Error("录音路径无效。");
  const resolved = path.resolve(target);
  const folder = await asyncFs.realpath(voiceInputDirectory());
  if (await asyncFs.realpath(path.dirname(resolved)) !== folder || !/^recording-[a-f0-9-]{36}\.(wav|webm|ogg|mp3|m4a)$/.test(path.basename(resolved))) throw new Error("只能管理墨流当前录音目录中的录音。");
  const info = await asyncFs.lstat(resolved);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error("录音文件指向外部位置，未操作。");
  return resolved;
}
let updates: UpdateManager | null = null;
let confirmationPending = false;
let closeConfirmationPending = false;
let closeAuthorized = false;

function applicationIconPath(): string {
  return !isDevelopment
    ? path.join(process.resourcesPath, "icon.ico")
    : path.resolve(__dirname, "..", "resources", "icon.ico");
}

function developmentLaunchDetails(): { target: string; args: string } {
  return { target: "D:\\墨流\\dev-launcher\\墨流 InkFlow.exe", args: "" };
}

function applicationRelaunchCommand(): string {
  if (!isDevelopment) return `"${process.execPath}"`;
  // A pinned development window must start Vite as well as the application.
  // Launching electron.exe alone opens Electron's default welcome screen.
  const launch = developmentLaunchDetails();
  return `"${launch.target}" ${launch.args}`;
}

function projectArgument(argv = process.argv): string | null {
  const index = argv.indexOf("--project");
  return index >= 0 && argv[index + 1] ? path.resolve(argv[index + 1]) : null;
}

/** 等待真实页面载入；失败保留可恢复入口，聊天切换可据此回滚窗口绑定。 */
function loadWorkspace(window: BrowserWindow, conversationId?: string, panel?: WorkspacePanelId): Promise<boolean> {
  const developmentUrl = process.env.VITE_DEV_SERVER_URL;
  const query: Record<string, string> = { ...(conversationId ? { conversation_id: conversationId } : {}), ...(panel ? { workspace_panel: panel } : {}) };
  const url = developmentUrl ? new URL(developmentUrl) : null;
  for (const [key, value] of Object.entries(query)) url?.searchParams.set(key, value);
  const loading = developmentUrl
    ? window.loadURL(url!.toString())
    : window.loadFile(path.join(__dirname, "..", "dist", "index.html"), { query });
  return loading.then(() => {
    if (window.isDestroyed()) return false;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
    logLifecycle("window.loaded", { windowId: window.id });
    return true;
  }).catch((cause) => {
    logLifecycle("window.load-failed", { windowId: window.id, reason: redactEngineDiagnostics(cause) });
    if (window.isDestroyed()) return false;
    window.show();
    void dialog.showMessageBox(window, {
      type: "error", title: "墨流界面加载失败", message: "窗口已经打开，但工作台未能载入。",
      detail: `${redactEngineDiagnostics(cause)}\n启动记录保存在 ${path.join(app.getPath("userData"), "logs")}。`,
      buttons: ["重新载入", "查看启动日志"], defaultId: 0, cancelId: 1,
    }).then(({ response }) => {
      if (response === 0 && !window.isDestroyed()) void loadWorkspace(window, conversationWindows.get(window.id)?.conversationId || (window === mainWindow ? undefined : conversationId), panel);
      else void shell.openPath(path.join(app.getPath("userData"), "logs"));
    }).catch(() => undefined);
    return false;
  });
}
/** 保存门禁阻止导航时也结束等待；正常切换和删除后的重载共用此结果。 */
async function loadConversation(window: BrowserWindow, identity: string): Promise<boolean> {
  let blocked = () => {};
  const prevented = new Promise<boolean>(resolve => { blocked = () => resolve(false); window.webContents.once("will-prevent-unload", blocked); });
  try { return await Promise.race([loadWorkspace(window, identity), prevented]); }
  finally { window.webContents.removeListener("will-prevent-unload", blocked); }
}
function createWorkspacePanel(owner: BrowserWindow, projectRoot: string, panel: WorkspacePanelId, relativePath?: string, requested?: Electron.Rectangle): void {
  const existing = [...conversationWindows].find(([, item]) => item.ownerId === owner.id && item.panel === panel && sameProjectRoot(item.projectRoot, projectRoot));
  const opened = existing ? BrowserWindow.fromId(existing[0]) : null;
  if (opened && !opened.isDestroyed()) { if (opened.isMinimized()) opened.restore(); opened.focus(); return; }
  const display = screen.getDisplayMatching(requested || owner.getBounds()).workArea;
  const minimumWidth = panel === "assistant" ? 280 : panel === "navigation" ? 180 : 200;
  const width = Math.min(Math.max(minimumWidth, requested?.width || (panel === "assistant" ? 620 : 460)), display.width);
  const height = Math.min(Math.max(240, requested?.height || 760), display.height);
  const bounds = owner.getBounds();
  const window = new BrowserWindow({
    x: Math.max(display.x, Math.min(display.x + display.width - width, requested?.x ?? bounds.x + 48)), y: Math.max(display.y, Math.min(display.y + display.height - height, requested?.y ?? bounds.y + 48)),
    width, height, minWidth: Math.min(minimumWidth, display.width), minHeight: Math.min(240, display.height), title: `墨流 · ${{ navigation: "小说结构", assistant: "墨宝对话", inspector: "批注与版本", workbench: "工程工作台" }[panel]}`,
    icon: applicationIconPath(), backgroundColor: "#11110f", show: true, autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  const conversationId = conversationWindows.get(owner.id)?.conversationId || "main";
  conversationWindows.set(window.id, { projectRoot, conversationId, panel, ownerId: owner.id, relativePath, shared: true });
  let dragOrigin: Electron.Rectangle | null = null;
  const notifyDrag = (phase: "move" | "drop" | "cancel") => {
    if (owner.isDestroyed() || window.isDestroyed() || owner.isMinimized() && phase === "move") return;
    owner.webContents.send("workspace:event", { type: "native-drag", panel, projectRoot, phase: owner.isMinimized() ? "cancel" : phase,
      rect: window.getContentBounds(), origin: owner.getContentBounds(), point: screen.getCursorScreenPoint(), scale: owner.webContents.getZoomFactor() });
  };
  const cancelDrag = () => { if (dragOrigin) { dragOrigin = null; if (!window.isDestroyed()) window.setOpacity(1); notifyDrag("cancel"); } };
  // Windows 'moved' ends the native drag; a pause during 'move' is not a drop.
  window.on("will-move", () => { if (!dragOrigin && process.platform === "win32") { dragOrigin = window.getBounds(); window.setOpacity(.84); } });
  window.on("move", () => { if (dragOrigin) notifyDrag("move"); });
  window.on("moved", () => {
    if (!dragOrigin) return;
    const current = window.getBounds(), original = dragOrigin; dragOrigin = null; window.setOpacity(1);
    const cancelled = current.x === original.x && current.y === original.y || window.isMaximized() || window.snapped;
    notifyDrag(cancelled ? "cancel" : "drop");
  });
  window.on("will-resize", cancelDrag); window.on("blur", cancelDrag); window.on("minimize", cancelDrag);
  const returned = () => {
    const hidden = conversationWindows.get(window.id)?.returnHidden === true;
    conversationWindows.delete(window.id);
    owner.removeListener("closed", ownerClosed);
    if (!owner.isDestroyed()) owner.webContents.send("workspace:event", { type: "returned", panel, projectRoot, hidden });
  };
  const ownerClosed = () => { if (!window.isDestroyed()) window.close(); workspaceStates.delete(owner.id); };
  window.once("closed", returned);
  owner.once("closed", ownerClosed);
  loadWorkspace(window, conversationId, panel);
}

function createWindow(): void {
  const windowIcon = applicationIconPath();
  mainWindow = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 960,
    minHeight: 650,
    backgroundColor: "#11110f",
    title: applicationName,
    icon: windowIcon,
    show: true,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  const affectedWindow = mainWindow;
  if (process.platform === "win32") {
    // Window icons and the taskbar's pinned/relaunch identity are separate.
    mainWindow.setAppDetails({
      appId: applicationId,
      appIconPath: windowIcon,
      appIconIndex: 0,
      relaunchCommand: applicationRelaunchCommand(),
      relaunchDisplayName: applicationName,
    });
  }
  Menu.setApplicationMenu(null);
  affectedWindow.webContents.on("render-process-gone", (_event, details) => {
    logLifecycle("renderer.gone", { reason: details.reason, exitCode: details.exitCode });
    if (details.reason === "clean-exit" || affectedWindow.isDestroyed()) return;
    void dialog.showMessageBox(affectedWindow, {
      type: "error",
      title: "墨流界面意外退出",
      message: "界面进程已退出，已保存的小说文件不会删除。",
      detail: `原因：${details.reason}（${details.exitCode}）。后台任务可能仍在运行；重新载入不会重复执行生成，但未保存的界面输入可能丢失。`,
      buttons: ["重新载入界面", "暂不处理"],
      defaultId: 1,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0 && !affectedWindow.isDestroyed()) affectedWindow.reload();
    }).catch(() => undefined);
  });
  const unresponsivePromptDelayMs = 20_000;
  let unresponsiveSince: number | null = null;
  let unresponsiveTimer: ReturnType<typeof setTimeout> | null = null;
  let unresponsiveDialogOpen = false;
  let unresponsivePromptShown = false;

  const clearUnresponsiveTimer = () => {
    if (unresponsiveTimer !== null) clearTimeout(unresponsiveTimer);
    unresponsiveTimer = null;
  };

  const scheduleUnresponsivePrompt = () => {
    clearUnresponsiveTimer();
    if (unresponsiveSince === null || unresponsivePromptShown || affectedWindow.isDestroyed()) return;
    const elapsedMs = Date.now() - unresponsiveSince;
    unresponsiveTimer = setTimeout(() => {
      unresponsiveTimer = null;
      if (unresponsiveSince === null || unresponsivePromptShown || affectedWindow.isDestroyed() || mainWindow !== affectedWindow) return;
      if (Date.now() - unresponsiveSince < unresponsivePromptDelayMs) {
        scheduleUnresponsivePrompt();
        return;
      }
      if (unresponsiveDialogOpen) return;

      const durationMs = Date.now() - unresponsiveSince;
      unresponsivePromptShown = true;
      unresponsiveDialogOpen = true;
      logLifecycle("window.unresponsive.prompt", { duration_ms: durationMs });
      void dialog.showMessageBox(affectedWindow, {
        type: "warning",
        title: "墨流界面暂时无响应",
        message: `界面已持续无响应 ${Math.max(1, Math.round(durationMs / 1000))} 秒。`,
        detail: "墨流会继续等待界面恢复；后台任务不会因这条提示自动停止。你可以继续等待，或手动重新载入界面。重新载入可能丢失尚未保存的界面输入，请勿因此重复提交当前任务。",
        buttons: ["继续等待", "手动重新载入"],
        defaultId: 0,
        cancelId: 0,
        noLink: true,
      }).then(({ response }) => {
        if (response !== 1 || affectedWindow.isDestroyed() || mainWindow !== affectedWindow) return;
        const reloadDurationMs = unresponsiveSince === null ? durationMs : Date.now() - unresponsiveSince;
        logLifecycle("window.unresponsive.reload-requested", {
          duration_ms: reloadDurationMs,
        });
        clearUnresponsiveTimer();
        unresponsiveSince = null;
        unresponsivePromptShown = false;
        affectedWindow.reload();
      }).catch((cause) => {
        logLifecycle("window.unresponsive.prompt-failed", {
          reason: cause instanceof Error ? cause.message : String(cause),
        });
      }).finally(() => {
        unresponsiveDialogOpen = false;
        // If a distinct stall began while the first prompt was still open,
        // offer its prompt after the first one is dismissed (never stack dialogs).
        if (unresponsiveSince !== null && !unresponsivePromptShown && !affectedWindow.isDestroyed()) {
          scheduleUnresponsivePrompt();
        }
      });
    }, Math.max(0, unresponsivePromptDelayMs - elapsedMs));
  };

  affectedWindow.on("unresponsive", () => {
    if (unresponsiveSince !== null) return;
    unresponsiveSince = Date.now();
    unresponsivePromptShown = false;
    logLifecycle("window.unresponsive", { started_at: new Date(unresponsiveSince).toISOString() });
    scheduleUnresponsivePrompt();
  });
  affectedWindow.on("responsive", () => {
    if (unresponsiveSince === null) return;
    const startedAt = unresponsiveSince;
    const recoveredAt = Date.now();
    clearUnresponsiveTimer();
    logLifecycle("window.responsive", {
      started_at: new Date(startedAt).toISOString(),
      recovered_at: new Date(recoveredAt).toISOString(),
      duration_ms: recoveredAt - startedAt,
      prompt_shown: unresponsivePromptShown,
    });
    unresponsiveSince = null;
    unresponsivePromptShown = false;
  });
  affectedWindow.on("close", (event) => {
    // 关闭主窗口会终止共享后台；这里统计所有窗口的实际工作，不把被动读取当成任务。
    const pending = bridge?.pendingWorkCount() || 0;
    const allPending = bridge?.pendingCount() || 0;
    logLifecycle("window.close-requested", { pending: allPending, active_pending: pending, pending_methods: bridge?.pendingMethods() || [] });
    if (closeAuthorized || !bridge) return;
    event.preventDefault();
    if (closeConfirmationPending) return;
    closeConfirmationPending = true;
    const closingWindow = affectedWindow;
    if (!closingWindow || closingWindow.isDestroyed()) {
      closeConfirmationPending = false;
      return;
    }
    void (async () => {
      const stoppingBridge = bridge;
      // 必须先取得退出决定，再取消任务；关闭提示或按 Esc 都等同于继续工作。
      if ((stoppingBridge?.pendingWorkCount() || 0) > 0) {
        const decision = await dialog.showMessageBox(closingWindow, {
          type: "warning",
          title: "退出墨流？",
          message: "墨流还有任务正在运行，确定要退出吗？",
          detail: "退出会中断当前工作。已保存的内容会保留，尚未完成的输出可能无法找回；已经发送的模型请求仍可能产生费用。",
          buttons: ["继续工作", "退出软件"],
          defaultId: 0,
          cancelId: 0,
          noLink: true,
        });
        if (decision.response !== 1 || closingWindow.isDestroyed()) return;
      }
      // 用户确认后沿用原取消与限时收尾，不重置预算，也不把未完成任务标成成功。
      try {
        await stoppingBridge?.shutdownForExit();
      } catch (cause) {
        logLifecycle("engine.shutdown.failed", {
          reason: cause instanceof Error ? cause.message : String(cause),
          pending_count: stoppingBridge?.pendingCount() || 0,
        });
        stoppingBridge?.stop();
      }
      if (closingWindow.isDestroyed()) return;
      closeAuthorized = true;
      closingWindow.close();
    })().catch((cause) => {
      logLifecycle("window.close-save-failed", { reason: cause instanceof Error ? cause.message : String(cause) });
    }).finally(() => {
      closeConfirmationPending = false;
    });
  });
  bridge = new EngineBridge();
  try {
    updates = new UpdateManager(mainWindow);
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause);
    updates = null;
    const updateWindow = mainWindow;
    updateWindow.webContents.once("did-finish-load", () => {
      if (!updateWindow.isDestroyed()) {
        updateWindow.webContents.send("app:update-status", {
          status: "error",
          currentVersion: app.getVersion(),
          message: `更新模块暂时不可用：${message}`,
          source: "none",
        } satisfies UpdateState);
      }
    });
  }
  ipcMain.handle("engine:request", async (event, method: string, params: Record<string, unknown>) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || (source !== mainWindow && !conversationWindows.has(source.id))) throw new Error("这个窗口没有本地引擎访问权限。");
    const conversationId = conversationWindows.get(source.id)?.conversationId || "main";
    // 旧页面卸载期间的迟到调用不能被悄悄改绑到刚切换的新聊天。
    if (params.conversation_id !== undefined && params.conversation_id !== conversationId) throw new Error("聊天已切换，这项旧界面请求没有发送。请在当前聊天继续。");
    const result = await bridge?.request(method, { ...params, conversation_id: conversationId });
    if (method === "voice.settings.get" || method === "voice.settings.configure" || method === "voice.storage.move") {
      const settings = (method === "voice.storage.move" ? (result as { settings?: unknown })?.settings : result) as { voice_data_dir?: string } | undefined;
      voiceDataDirectory = settings?.voice_data_dir || "";
    }
    const context = conversationWindows.get(source.id);
    if (context && method === "project.open" && typeof params.project_root === "string" && context.projectRoot !== params.project_root) {
      context.projectRoot = params.project_root;
    }
    return result;
  });
  ipcMain.handle("conversation:switch", async (event, rootValue: string, identity?: string) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || source !== mainWindow || !bridge) throw new Error("请回到主窗口切换聊天。");
    if (conversationSwitches.has(source.id)) throw new Error("正在切换聊天，请等待本次界面加载。");
    conversationSwitches.add(source.id);
    try {
      const root = resolveInkFlowProject(rootValue);
      const current = conversationWindows.get(source.id)?.conversationId || "main";
      const previousState = workspaceStates.get(source.id);
      const previous = conversationWindows.get(source.id) || { projectRoot: String(previousState?.projectRoot || root), conversationId: current };
      if (identity !== undefined && !/^[A-Za-z0-9_-]{1,80}$/.test(identity)) throw new Error("要打开的聊天身份无效。");
      // 目录由共享服务确认；不能用调用方传来的任意身份冒充已保存聊天。
      const catalog = await bridge.request("conversation.list", { project_root: root, conversation_id: current }) as { items: Array<{ id: string }> };
      if (identity !== undefined && !catalog.items.some(item => item.id === identity)) throw new Error("这份聊天不在当前小说中，已有聊天未改变。");
      const next = identity || (await bridge.request("conversation.create", { project_root: root, conversation_id: current }) as { id: string }).id;
      // 原请求继续持有原聊天、快照和预算；切换只重建界面，不取消任务或转移迟到事件。
      // 拆出的面板属于原聊天，返回后重建；不把其迟到状态同步给新聊天。
      for (const [id, context] of conversationWindows) if (context.ownerId === source.id) BrowserWindow.fromId(id)?.close();
      workspaceStates.delete(source.id);
      conversationWindows.set(source.id, { projectRoot: root, conversationId: next });
      try {
        // 保存门禁阻止卸载时也须结束等待，不能只依赖页面完成/网络失败事件。
        if (!await loadConversation(source, next)) throw new Error("新聊天界面没有成功载入，原聊天和任务已保留。可重新载入原聊天后继续。");
      } catch (cause) {
        // 导航被旧页保存门禁阻止或加载失败时，身份必须跟随可恢复的原聊天，不能锁死旧页面。
        if (!source.isDestroyed()) { conversationWindows.set(source.id, previous); if (previousState) workspaceStates.set(source.id, previousState); }
        throw cause;
      }
    } finally { conversationSwitches.delete(source.id); }
  });
  ipcMain.handle("conversation:delete", async (event, rootValue: string, identity: string) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || source !== mainWindow || !bridge) throw new Error("请回到主窗口删除本书聊天。");
    if (conversationSwitches.has(source.id)) throw new Error("正在调整聊天，请等待本次操作完成。");
    if (typeof identity !== "string" || !/^[A-Za-z0-9_-]{1,80}$/.test(identity)) throw new Error("要删除的聊天身份无效。");
    const root = resolveInkFlowProject(rootValue), current = conversationWindows.get(source.id)?.conversationId || "main";
    const displayedRoot = conversationWindows.get(source.id)?.projectRoot || workspaceStates.get(source.id)?.projectRoot;
    if (typeof displayedRoot !== "string" || !sameProjectRoot(displayedRoot, root)) throw new Error("小说已切换，请重新选择要删除的聊天。");
    if (bridge.pendingWorkCount(identity)) throw new Error("这份聊天还有请求正在处理，请先停止并等待收尾。");
    if (identity === current && [...conversationWindows].some(([id, context]) => context.ownerId === source.id && !BrowserWindow.fromId(id)?.isDestroyed())) throw new Error("请先把本聊天的独立面板返回主界面，再删除聊天。");
    conversationSwitches.add(source.id);
    const key = conversationViewKey(root, identity);
    let originalViews: Array<Record<string, unknown>> | undefined, deleted = false;
    try {
      originalViews = workspaceViews();
      const targetView = originalViews.find(item => sameProjectRoot(String(item.projectRoot), root) && (item.conversationId || "main") === identity);
      if (targetView?.editorDraft) throw new Error("这份聊天还保留未保存的正文，请先打开它保存或处理正文候选，再删除聊天。");
      deletedConversationViews.add(key); lastWorkspaceView = "";
      writeWorkspaceViews(originalViews.filter(item => !sameProjectRoot(String(item.projectRoot), root) || (item.conversationId || "main") !== identity));
      await bridge.request("conversation.delete", { project_root: root, conversation_id: current, target_conversation_id: identity });
      deleted = true;
      if (identity !== current) return { deleted: true, reloaded: false };
      // 物理删除成功后不能回滚到已删身份；加载失败也保留新的有效聊天，给出手动重载入口。
      const catalog = await bridge.request("conversation.list", { project_root: root, conversation_id: current }) as { items: Array<{ id: string }> };
      const next = catalog.items[0]?.id || (await bridge.request("conversation.create", { project_root: root, conversation_id: current }) as { id: string }).id;
      workspaceStates.delete(source.id);
      conversationWindows.set(source.id, { projectRoot: root, conversationId: next });
      if (!await loadConversation(source, next)) throw new Error("新聊天界面未载入，请重新加载窗口。");
      return { deleted: true, reloaded: true };
    } catch (cause) {
      if (!deleted) {
        deletedConversationViews.delete(key); lastWorkspaceView = "";
        if (originalViews) writeWorkspaceViews([...workspaceViews().filter(item => !sameProjectRoot(String(item.projectRoot), root) || (item.conversationId || "main") !== identity),
          ...originalViews.filter(item => sameProjectRoot(String(item.projectRoot), root) && (item.conversationId || "main") === identity)]);
      }
      throw new Error(`${deleted ? "聊天已经删除，请重新打开本书或加载窗口。" : ""}${cause instanceof Error ? cause.message : String(cause)}`);
    } finally { conversationSwitches.delete(source.id); }
  });
  ipcMain.handle("workspace:open", (event, projectRoot: string, panel: WorkspacePanelId, relativePath?: string, value?: unknown) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || (source !== mainWindow && !conversationWindows.has(source.id)) || !["navigation", "assistant", "inspector", "workbench"].includes(panel) || typeof projectRoot !== "string" || !projectRoot) throw new Error("请先打开小说，再移动这个面板。");
    if (conversationWindows.get(source.id)?.shared) throw new Error("请先回到主工作台，再打开独立面板。");
    let requested: Electron.Rectangle | undefined;
    if (value !== undefined) {
      const rect = value as Electron.Rectangle;
      if (!rect || typeof rect !== "object" || ![rect.x, rect.y, rect.width, rect.height].every(item => typeof item === "number" && Number.isFinite(item)) || rect.width <= 0 || rect.height <= 0) throw new Error("面板的落位尺寸无效，请重新拖动。");
      const origin = source.getContentBounds(), scale = source.webContents.getZoomFactor();
      requested = { x: Math.round(origin.x + rect.x * scale), y: Math.round(origin.y + rect.y * scale), width: Math.round(rect.width * scale), height: Math.round(rect.height * scale) };
    }
    createWorkspacePanel(source, resolveInkFlowProject(projectRoot), panel, typeof relativePath === "string" ? relativePath : undefined, requested);
  });
  ipcMain.handle("workspace:return", (event, panel?: WorkspacePanelId, hide = false) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (panel && source && (source === mainWindow || conversationWindows.has(source.id))) {
      if (!["navigation", "assistant", "inspector", "workbench"].includes(panel)) throw new Error("这个面板不能返回工作台。");
      const opened = [...conversationWindows].find(([, item]) => item.ownerId === source.id && item.panel === panel);
      if (opened) opened[1].returnHidden = hide === true;
      const window = opened ? BrowserWindow.fromId(opened[0]) : null;
      if (window && !window.isDestroyed()) window.close();
      return;
    }
    const context = source ? conversationWindows.get(source.id) : null;
    if (!source || !context?.panel) return;
    context.returnHidden = hide === true;
    const owner = context.ownerId ? BrowserWindow.fromId(context.ownerId) : mainWindow;
    if (owner && !owner.isDestroyed()) { if (owner.isMinimized()) owner.restore(); owner.focus(); }
    source.close();
  });
  ipcMain.handle("workspace:state", (event, value: unknown) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || (source !== mainWindow && !conversationWindows.has(source.id)) || conversationWindows.get(source.id)?.shared || !value || typeof value !== "object") return;
    const state = value as Record<string, unknown>;
    if (typeof state.projectRoot !== "string" || !state.projectRoot || !Array.isArray(state.messages) || state.messages.length > 200) return;
    if (((state.view as Record<string, unknown> | undefined)?.conversationId || "main") !== (conversationWindows.get(source.id)?.conversationId || "main")) return;
    saveWorkspaceView(state.view);
    // 待答的未提交输入只属于主窗口本机草稿，不随共享阅读面板广播。
    const sharedState = { ...state, view: { ...(state.view as Record<string, unknown>), questionDrafts: undefined } };
    workspaceStates.set(source.id, sharedState);
    for (const [id, context] of conversationWindows) {
      if (context.ownerId !== source.id || !context.shared) continue;
      const window = BrowserWindow.fromId(id);
      if (!window || window.isDestroyed()) continue;
      if (!sameProjectRoot(context.projectRoot, state.projectRoot)) window.close();
      else window.webContents.send("workspace:event", { type: "state", state: sharedState });
    }
  });
  ipcMain.handle("workspace:view", (event, root: string) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || (source !== mainWindow && !conversationWindows.has(source.id))) throw new Error("界面记录读取窗口不匹配。");
    const identity = conversationWindows.get(source.id)?.conversationId || "main";
    return workspaceViews().find(item => typeof root === "string" && sameProjectRoot(String(item.projectRoot), root) && (item.conversationId || "main") === identity) || null;
  });
  ipcMain.on("workspace:view:save", (event, value: unknown) => {
    const source = BrowserWindow.fromWebContents(event.sender);
    try {
      if (!source || source !== mainWindow || conversationWindows.get(source.id)?.shared) throw new Error("状态保存窗口不匹配。");
      // 卸载前保存仍归属于发出它的旧页面，不能因主进程已切换身份而覆盖新聊天。
      const identity = new URL(source.webContents.getURL()).searchParams.get("conversation_id") || "main";
      if (((value as Record<string, unknown> | null)?.conversationId || "main") !== identity) throw new Error("界面记录与聊天不对应。");
      saveWorkspaceView(value);
      event.returnValue = { saved: true };
    } catch { event.returnValue = { saved: false }; }
  });
  // 同步读取仅用于首屏外观；限制为本应用窗口，不能扩展为通用文件访问。
  ipcMain.on("appearance:read", event => {
    const source = BrowserWindow.fromWebContents(event.sender);
    event.returnValue = source && (source === mainWindow || conversationWindows.has(source.id)) ? readAppearance() : null;
    if (event.returnValue) nativeTheme.themeSource = event.returnValue.theme;
  });
  // 和工作台快照一样同步确认落盘，退出时无需等待未完成的 IPC。
  ipcMain.on("appearance:save", (event, value: unknown) => {
    event.returnValue = { saved: false };
    const source = BrowserWindow.fromWebContents(event.sender);
    if (!source || (source !== mainWindow && !conversationWindows.has(source.id))) return;
    try {
      saveAppearance(value);
      event.returnValue = { saved: true };
    } catch { logLifecycle("appearance.save.failed"); }
  });
  ipcMain.handle("workspace:command", (event, command: string, value: string, relativePath?: string) => {
    const source = BrowserWindow.fromWebContents(event.sender), context = source ? conversationWindows.get(source.id) : null;
    const allowed = context?.panel === "assistant" ? ["chat", "document", "navigate", "conversation-layout"] : context?.panel === "navigation" ? ["document", "navigate"] : ["compare", "annotation"];
    if (!context?.shared || !context.ownerId || !allowed.includes(command) || typeof value !== "string" || value.length > 100_000) throw new Error("这个面板不能执行这项操作，请回到主工作台处理。");
    if (command === "navigate" && !/^(?:pending_work(?::[1-9]\d{0,5})?|library:(?:draft|accepted|threads))$/.test(value)) throw new Error("这个导航目标不可用，请回到主工作台查看。");
    if (command === "conversation-layout" && (!Number.isFinite(Number(value)) || Number(value) < .15 || Number(value) > .75)) throw new Error("对话布局比例无效，请重新调整。");
    const owner = BrowserWindow.fromId(context.ownerId);
    if (!owner || owner.isDestroyed()) throw new Error("主工作台已关闭，请重新打开小说。");
    if (["document", "compare", "navigate"].includes(command)) { if (owner.isMinimized()) owner.restore(); owner.focus(); }
    owner.webContents.send("workspace:event", { type: "command", command, value, relativePath, projectRoot: context.projectRoot });
  });
  ipcMain.handle("dialog:confirm", async (event, message: string) => {
    const window = BrowserWindow.fromWebContents(event.sender);
    if (!window || window.isDestroyed() || (window !== mainWindow && !conversationWindows.has(window.id)) || confirmationPending || typeof message !== "string" || !message.trim()) return false;
    confirmationPending = true;
    try {
      // Keep app.getName/userData/update identity unchanged; only brand the dialog.
      const result = await dialog.showMessageBox(window, {
        type: "question",
        title: applicationName,
        message,
        buttons: ["确定", "取消"],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      return result.response === 0;
    } finally {
      confirmationPending = false;
    }
  });
  ipcMain.handle("dialog:choose-folder", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openDirectory", "createDirectory"],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("project:recent-list", () => recentProjects());
  ipcMain.handle("project:recent-remember", (_event, root: string, title: string) => rememberRecentProject(root, title));
  ipcMain.handle("project:recent-forget", (_event, root: string) => forgetRecentProject(root));
  ipcMain.handle("project:trash", async (_event, rootValue: string) => {
    const root = resolveInkFlowProject(rootValue);
    await shell.trashItem(root);
    try { forgetRecentProject(root); } catch { logLifecycle("recent-projects.write.failed", { action: "trash" }); }
    return { root, recoverable: true };
  });
  ipcMain.handle("project:move", async (_event, rootValue: string, targetParentValue: string) => {
    const source = resolveInkFlowProject(rootValue);
    const targetParent = realpathSync(path.resolve(String(targetParentValue || "")));
    if (!lstatSync(targetParent).isDirectory()) throw new Error("目标位置不是文件夹，项目没有移动。");
    if (pathContains(source, targetParent)) throw new Error("不能把项目移动到自己的文件夹内。");
    const destination = path.join(targetParent, path.basename(source));
    if (existsSync(destination)) throw new Error(`目标位置已经存在“${path.basename(source)}”文件夹，请先选择其他位置。`);
    try {
      renameSync(source, destination);
    } catch (cause) {
      const code = cause && typeof cause === "object" && "code" in cause ? String((cause as { code?: unknown }).code || "") : "";
      if (code !== "EXDEV") throw cause;
      cpSync(source, destination, { recursive: true, errorOnExist: true, force: false });
      rmSync(source, { recursive: true, force: true });
    }
    try {
      const current = recentProjects();
      const entry = current.find((item) => sameProjectRoot(item.root, source));
      saveRecentProjects([
        { root: destination, title: entry?.title || path.basename(destination), openedAt: new Date().toISOString() },
        ...current.filter((item) => !sameProjectRoot(item.root, source) && !sameProjectRoot(item.root, destination)),
      ]);
    } catch { logLifecycle("recent-projects.write.failed", { action: "move" }); }
    return { source, destination };
  });
  ipcMain.handle("dialog:choose-file", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openFile"],
      filters: [{ name: "文本资料", extensions: ["txt", "md", "markdown", "jsonl"] }],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  // 原生选取文件，后台只接收路径，避免把大文件内容编码后经 IPC 转发。
  ipcMain.handle("dialog:choose-preference-materials", async () => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title: "选择写作素材",
      properties: ["openFile", "multiSelections"],
      filters: [{ name: "写作素材", extensions: ["txt", "md", "markdown", "docx", "pdf", "epub", "html", "htm"] }],
    });
    return result.canceled ? [] : result.filePaths;
  });
  ipcMain.handle("dialog:choose-audio", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openFile"],
      filters: [{ name: "语音文件", extensions: ["wav", "mp3", "m4a", "flac", "ogg", "webm"] }],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("voice:save-recording", async (_event, bytes: Uint8Array, extension = "webm", snapshotId?: string) => {
    if (!bytes?.byteLength || bytes.byteLength > 50 * 1024 * 1024) throw new Error("录音为空或过长，请分段录制。");
    if (snapshotId !== undefined && !/^[a-f0-9-]{36}$/.test(snapshotId)) throw new Error("本次录音的身份无效，请重新开始录音。");
    const safeExtension = ["wav", "webm", "ogg", "mp3", "m4a"].includes(extension) ? extension : "webm";
    const folder = voiceInputDirectory();
    await asyncFs.mkdir(folder, { recursive: true });
    const target = path.join(folder, `${snapshotId ? "snapshot" : "recording"}-${snapshotId || randomUUID()}.${safeExtension}`);
    // Atomic publication keeps a decoder from reading a half-written WAV.
    const temporary = `${target}.${randomUUID()}.partial`;
    try { await asyncFs.writeFile(temporary, Buffer.from(bytes)); await asyncFs.rename(temporary, target); }
    finally { await asyncFs.rm(temporary, { force: true }).catch(() => undefined); }
    return target;
  });
  ipcMain.handle("voice:recording-list", async () => {
    const folder = voiceInputDirectory();
    const entries = await asyncFs.readdir(folder, { withFileTypes: true }).catch((cause: NodeJS.ErrnoException) => {
      if (cause.code === "ENOENT") return [];
      throw cause;
    });
    const records = await Promise.all(entries.filter(entry => entry.isFile() && /^recording-[a-f0-9-]{36}\.(wav|webm|ogg|mp3|m4a)$/.test(entry.name)).map(async entry => {
      const audioPath = path.join(folder, entry.name);
      const info = await asyncFs.stat(audioPath);
      let transcript = "";
      try {
        const sidecar = await asyncFs.lstat(`${audioPath}.json`);
        if (sidecar.isFile() && !sidecar.isSymbolicLink() && sidecar.size <= 512_000) transcript = String(JSON.parse(await asyncFs.readFile(`${audioPath}.json`, "utf8")).transcript || "");
      }
      catch { /* Older recordings remain playable without a transcript sidecar. */ }
      return { path: audioPath, name: entry.name, bytes: info.size, createdAt: info.birthtime.toISOString(), transcript };
    }));
    return { directory: folder, recordings: records.sort((a, b) => b.createdAt.localeCompare(a.createdAt)) };
  });
  ipcMain.handle("voice:recording-transcript", async (_event, target: string, transcript: string) => {
    const audioPath = await managedVoiceRecording(target);
    if (typeof transcript !== "string" || transcript.length > 100_000) throw new Error("转写文字过长，未保存记录。");
    const sidecar = `${audioPath}.json`, temporary = `${sidecar}.${randomUUID()}.partial`;
    try { await asyncFs.writeFile(temporary, JSON.stringify({ transcript, updatedAt: new Date().toISOString() }), "utf8"); await asyncFs.rename(temporary, sidecar); }
    finally { await asyncFs.rm(temporary, { force: true }).catch(() => undefined); }
  });
  ipcMain.handle("voice:recording-delete", async (_event, target: string) => {
    const audioPath = await managedVoiceRecording(target);
    await shell.trashItem(audioPath);
    if (existsSync(`${audioPath}.json`)) await shell.trashItem(`${audioPath}.json`);
    return { deleted: true, recoverable: true };
  });
  ipcMain.handle("voice:audio-url", async (_event, target: string) => {
    const resolved = path.resolve(target);
    const voiceSettings = await bridge?.request("voice.settings.get") as { voice_data_dir?: string } | undefined;
    const allowedRoots = [
      app.getPath("userData"),
      app.getPath("temp"),
      path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "InkFlow"),
      path.join(process.env.APPDATA || app.getPath("userData"), "InkFlow"),
      "D:/墨流/cache/voice-input",
      ...(voiceSettings?.voice_data_dir ? [voiceSettings.voice_data_dir] : []),
    ].map((item) => path.resolve(item));
    const allowed = allowedRoots.some((root) => {
      const relative = path.relative(root, resolved);
      return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
    });
    if (!allowed || !existsSync(resolved)) throw new Error("音频文件不在墨流允许播放的本地目录中。");
    // A file:// URL loaded from the Vite http:// renderer is rejected by
    // Chromium as an unsupported source even though the WAV itself is valid.
    // Returning the already-authorized local bytes avoids that origin split
    // in development and packaged builds without exposing arbitrary files.
    const mimeByExtension: Record<string, string> = {
      ".wav": "audio/wav",
      ".mp3": "audio/mpeg",
      ".m4a": "audio/mp4",
      ".flac": "audio/flac",
      ".ogg": "audio/ogg",
      ".webm": "audio/webm",
    };
    const mime = mimeByExtension[path.extname(resolved).toLowerCase()];
    if (!mime) throw new Error("这个音频格式暂不支持播放。");
    return `data:${mime};base64,${(await asyncFs.readFile(resolved)).toString("base64")}`;
  });
  ipcMain.handle("shell:open-path", (_event, target: string) => shell.openPath(target));
  ipcMain.handle("shell:show-item", (_event, target: string) => shell.showItemInFolder(target));
  ipcMain.handle("app:launch-context", event => {
    const source = BrowserWindow.fromWebContents(event.sender);
    const context = source ? conversationWindows.get(source.id) : undefined;
    const nativePanels = source ? [...conversationWindows.values()].filter(item => item.ownerId === source.id && item.shared && item.panel).map(item => item.panel!) : [];
    // Normal launches stay on Home; explicit file opens and detached panels retain their target.
    return { projectRoot: context ? context.projectRoot || null : projectArgument() || null, conversationId: context?.conversationId || "main", panel: context?.panel, relativePath: context?.relativePath, shared: context?.shared, sharedState: context?.shared && context.ownerId ? workspaceStates.get(context.ownerId) : undefined, nativePanels };
  });
  ipcMain.handle("app:update-status", () => updates?.status());
  ipcMain.handle("app:update-check", () => updates?.check());
  ipcMain.handle("app:update-download", () => updates?.download());
  ipcMain.handle("app:update-install", () => updates?.install());
  loadWorkspace(affectedWindow);
  affectedWindow.on("closed", () => {
    clearUnresponsiveTimer();
    if (unresponsiveSince !== null) {
      logLifecycle("window.unresponsive.closed", {
        started_at: new Date(unresponsiveSince).toISOString(),
        duration_ms: Date.now() - unresponsiveSince,
        prompt_shown: unresponsivePromptShown,
      });
      unresponsiveSince = null;
    }
    for (const id of conversationWindows.keys()) BrowserWindow.fromId(id)?.destroy();
    conversationWindows.clear();
    bridge?.stop();
    bridge = null;
    updates = null;
    mainWindow = null;
  });
}

const singleInstance = app.requestSingleInstanceLock();
if (!singleInstance) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    if (!mainWindow) return;
    mainWindow.show();
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
    const root = projectArgument(argv);
    if (root) mainWindow.webContents.send("app:open-project", root);
  });
  app.whenReady().then(() => {
    logLifecycle("app.started", { version: app.getVersion(), packaged: !isDevelopment });
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  }).catch((cause) => {
    logLifecycle("app.startup-failed", { reason: redactEngineDiagnostics(cause) });
    dialog.showErrorBox("墨流启动失败", `未能创建工作台：${redactEngineDiagnostics(cause)}\n请查看 ${path.join(app.getPath("userData"), "logs")} 中的启动记录。`);
    app.quit();
  });
}

app.on("before-quit", () => logLifecycle("app.before-quit"));
app.on("child-process-gone", (_event, details) => logLifecycle("child.gone", {
  type: details.type, reason: details.reason, exitCode: details.exitCode,
}));
process.on("uncaughtExceptionMonitor", (error) => logLifecycle("main.uncaught", {
  name: error.name, message: redactEngineDiagnostics(error.message),
}));

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
