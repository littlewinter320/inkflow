import { app, BrowserWindow, dialog, ipcMain, Menu, Notification, shell } from "electron";
import { AppUpdater, NsisUpdater, autoUpdater } from "electron-updater";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { appendFileSync, cpSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

const applicationId = app.isPackaged ? "cn.inkflow.desktop" : "cn.inkflow.desktop.dev";
const applicationName = "墨流 InkFlow";
if (process.platform === "win32") app.setAppUserModelId(applicationId);

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

function redactEngineDiagnostics(value: string): string {
  return value
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

class UpdateManager {
  private updater: AppUpdater | null = null;
  private state: UpdateState;

  constructor(private readonly window: BrowserWindow) {
    const configuredUrl = String(process.env.INKFLOW_UPDATE_URL || "").trim();
    const embeddedConfig = path.join(process.resourcesPath, "app-update.yml");
    const source: UpdateState["source"] = configuredUrl
      ? "environment"
      : app.isPackaged && existsSync(embeddedConfig)
        ? "embedded"
        : app.isPackaged
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
    this.updater.on("update-downloaded", (info: { version: string }) => this.setState({ status: "downloaded", availableVersion: info.version, progress: 100, message: "更新已经下载完成；关闭墨流或点击重启后会自动安装。" }));
    this.updater.on("error", (cause: Error) => this.setState({ status: "error", message: `更新失败：${cause.message}` }));
    this.window.webContents.once("did-finish-load", () => {
      setTimeout(() => {
        if (!this.window.isDestroyed()) void this.check().catch(() => undefined);
      }, 1200);
    });
  }

  status(): UpdateState { return { ...this.state }; }

  async check(): Promise<UpdateState> {
    if (!app.isPackaged) return this.setState({ status: "not_configured", message: "开发模式不执行在线更新；请构建安装版后检查。" });
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
  private window: BrowserWindow;
  private compatibilityCheck: Promise<void> | null = null;
  private shuttingDown = false;
  private shutdownPromise: Promise<void> | null = null;

  constructor(window: BrowserWindow) {
    this.window = window;
  }

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
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => this.receive(line));
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
    const runIds = [...runRequests.keys()];
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
        const result = await this.requestOnChild(child, "run.cancel", { run_id: runId }) as { cancelled?: unknown } | null;
        cancelResults.push({ run_id: runId, cancelled: result?.cancelled === true });
      } catch (cause) {
        cancelResults.push({ run_id: runId, error: cause instanceof Error ? cause.message : String(cause) });
      }
    }));
    const requestsSettled = Promise.all(waitForRequests.map((item) => item.settled));
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
    if (["conversation.send", "document.revise_selection", "task.retry"].includes(item.method)) return true;
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

  pendingCount(): number {
    return this.pending.size;
  }

  pendingMethods(): string[] {
    // Method names are enough to diagnose a stale read request without
    // logging user text, file paths, API credentials, or request parameters.
    return [...new Set([...this.pending.values()].map((item) => item.method))].sort();
  }

  private failChild(child: ChildProcessWithoutNullStreams, error: Error): void {
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

  private receive(line: string): void {
    let value: Record<string, unknown>;
    try {
      value = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return;
    }
    if (value.method === "event") {
      this.send("engine:event", value.params);
      const event = value.params as { type?: string; summary?: string } | undefined;
      if (/^voice\.(moss|qwen|asr)\.(ready|models_failed|install\.failed)$/.test(event?.type || "") && Notification.isSupported()) {
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
    if (!pending) return;
    this.pending.delete(id);
    pending.markSettled();
    if (value.error) {
      const error = value.error as {
        title?: string;
        message?: string;
        code?: string;
        impact?: string;
        preserved?: string;
        actions?: Array<{ label?: string }>;
      };
      const lines = [
        error.title && error.title !== error.message ? error.title : "",
        error.message || error.code || "墨流引擎请求失败。",
        error.impact ? `影响：${error.impact}` : "",
        error.preserved ? `已保留：${error.preserved}` : "",
        error.actions?.length ? `下一步：${error.actions.map((item) => item.label).filter(Boolean).join("；")}` : "",
      ].filter(Boolean);
      pending.reject(new Error(lines.join("\n")));
    } else {
      pending.resolve(value.result);
    }
  }

  private send(channel: string, payload: unknown): void {
    if (this.window.isDestroyed() || this.window.webContents.isDestroyed()) return;
    this.window.webContents.send(channel, payload);
  }

  private command(): { executable: string; args: string[]; cwd: string; env?: Record<string, string> } {
    if (app.isPackaged) {
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
let bridge: EngineBridge | null = null;
let updates: UpdateManager | null = null;
let confirmationPending = false;
let closeConfirmationPending = false;
let closeAuthorized = false;

function applicationIconPath(): string {
  return app.isPackaged
    ? path.join(process.resourcesPath, "icon.ico")
    : path.resolve(__dirname, "..", "resources", "icon.ico");
}

function developmentLaunchDetails(): { target: string; args: string } {
  const wscript = path.join(process.env.SystemRoot || "C:\\Windows", "System32", "wscript.exe");
  const launcher = path.resolve(app.getAppPath(), "..", "scripts", "start-desktop-hidden.vbs");
  return { target: wscript, args: `"${launcher}"` };
}

function applicationRelaunchCommand(): string {
  if (app.isPackaged) return `"${process.execPath}"`;
  // A pinned development window must start Vite as well as the application.
  // Launching electron.exe alone opens Electron's default welcome screen.
  const launch = developmentLaunchDetails();
  return `"${launch.target}" ${launch.args}`;
}

function updateDevelopmentShortcut(): void {
  if (app.isPackaged || process.platform !== "win32") return;
  try {
    const shortcutPath = path.join(app.getPath("appData"), "Microsoft", "Windows", "Start Menu", "Programs", "墨流（本地开发）.lnk");
    if (!existsSync(shortcutPath)) return;
    const shortcut = shell.readShortcutLink(shortcutPath);
    const launch = developmentLaunchDetails();
    // Only update the entry registered by this checkout's hidden launcher.
    if (path.resolve(shortcut.target).toLowerCase() !== path.resolve(launch.target).toLowerCase() || shortcut.args !== launch.args) return;
    const updated = shell.writeShortcutLink(shortcutPath, "update", {
      target: shortcut.target,
      appUserModelId: applicationId,
      icon: applicationIconPath(),
      iconIndex: 0,
      description: "墨流 · 墨宝小说工作台（本机开发版）",
    });
    if (!updated) logLifecycle("app.shortcut-branding-failed", { reason: "shortcut-update-returned-false" });
  } catch (cause) {
    logLifecycle("app.shortcut-branding-failed", { reason: cause instanceof Error ? cause.message : String(cause) });
  }
}

function projectArgument(argv = process.argv): string | null {
  const index = argv.indexOf("--project");
  return index >= 0 && argv[index + 1] ? path.resolve(argv[index + 1]) : null;
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
    show: false,
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
    const pending = bridge?.pendingCount() || 0;
    logLifecycle("window.close-requested", { pending, pending_methods: bridge?.pendingMethods() || [] });
    if (closeAuthorized || pending === 0) return;
    event.preventDefault();
    if (closeConfirmationPending) return;
    closeConfirmationPending = true;
    const closingWindow = affectedWindow;
    if (!closingWindow || closingWindow.isDestroyed()) {
      closeConfirmationPending = false;
      return;
    }
    void dialog.showMessageBox(closingWindow, {
      type: "warning",
      title: "墨流有请求尚未返回",
      message: `还有 ${pending} 项本地请求尚未返回。退出会取消这些请求。`,
      detail: "请求未返回不一定代表仍在写作。若当前工作里有正在运行的创作或安装，请先核对进度；已保存的正文、草稿和候选会保留。返回墨流不会重复发送请求。",
      buttons: ["返回墨流，继续运行", "停止并退出"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    }).then(async ({ response }) => {
      if (response !== 1 || closingWindow.isDestroyed()) return;
      const stoppingBridge = bridge;
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
    }).catch((cause) => {
      logLifecycle("window.close-confirmation-failed", { reason: cause instanceof Error ? cause.message : String(cause) });
    }).finally(() => {
      closeConfirmationPending = false;
    });
  });
  bridge = new EngineBridge(mainWindow);
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
  ipcMain.handle("engine:request", (_event, method: string, params: Record<string, unknown>) =>
    bridge?.request(method, params),
  );
  ipcMain.handle("dialog:confirm", async (event, message: string) => {
    const window = mainWindow;
    if (!window || window.isDestroyed() || event.sender !== window.webContents || confirmationPending || typeof message !== "string" || !message.trim()) return false;
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
  ipcMain.handle("project:trash", async (_event, rootValue: string) => {
    const root = resolveInkFlowProject(rootValue);
    await shell.trashItem(root);
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
    return { source, destination };
  });
  ipcMain.handle("dialog:choose-file", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openFile"],
      filters: [{ name: "文本资料与训练样本", extensions: ["txt", "md", "markdown", "jsonl"] }],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("dialog:choose-audio", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openFile"],
      filters: [{ name: "语音文件", extensions: ["wav", "mp3", "m4a", "flac", "ogg", "webm"] }],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("voice:save-recording", (_event, bytes: Uint8Array, extension = "webm") => {
    const safeExtension = ["wav", "webm", "ogg", "mp3", "m4a"].includes(extension) ? extension : "webm";
    const folder = path.join(app.getPath("userData"), "voice-input");
    mkdirSync(folder, { recursive: true });
    const target = path.join(folder, `recording-${randomUUID()}.${safeExtension}`);
    writeFileSync(target, Buffer.from(bytes));
    return target;
  });
  ipcMain.handle("voice:audio-url", (_event, target: string) => {
    const resolved = path.resolve(target);
    const allowedRoots = [
      app.getPath("userData"),
      app.getPath("temp"),
      path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "InkFlow"),
      path.join(process.env.APPDATA || app.getPath("userData"), "InkFlow"),
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
    return `data:${mime};base64,${readFileSync(resolved).toString("base64")}`;
  });
  ipcMain.handle("shell:open-path", (_event, target: string) => shell.openPath(target));
  ipcMain.handle("shell:show-item", (_event, target: string) => shell.showItemInFolder(target));
  ipcMain.handle("app:launch-context", () => ({ projectRoot: projectArgument() }));
  ipcMain.handle("app:update-status", () => updates?.status());
  ipcMain.handle("app:update-check", () => updates?.check());
  ipcMain.handle("app:update-download", () => updates?.download());
  ipcMain.handle("app:update-install", () => updates?.install());
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  const developmentUrl = process.env.VITE_DEV_SERVER_URL;
  if (developmentUrl) void mainWindow.loadURL(developmentUrl);
  else void mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
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
    updateDevelopmentShortcut();
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
    const root = projectArgument(argv);
    if (root) mainWindow.webContents.send("app:open-project", root);
  });
  app.whenReady().then(() => {
    updateDevelopmentShortcut();
    logLifecycle("app.started", { version: app.getVersion(), packaged: app.isPackaged });
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
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
