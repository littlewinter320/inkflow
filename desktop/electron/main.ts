import { app, BrowserWindow, dialog, ipcMain, Menu, shell } from "electron";
import { AppUpdater, NsisUpdater, autoUpdater } from "electron-updater";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

type Pending = {
  resolve: (value: unknown) => void;
  reject: (reason?: unknown) => void;
  child: ChildProcessWithoutNullStreams;
};

type UpdateState = {
  status: "not_configured" | "ready" | "checking" | "available" | "current" | "downloading" | "downloaded" | "error";
  currentVersion: string;
  availableVersion?: string;
  progress?: number;
  message: string;
  source: "none" | "embedded" | "github" | "environment";
};

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
    this.updater.autoDownload = false;
    this.updater.autoInstallOnAppQuit = false;
    this.updater.on("checking-for-update", () => this.setState({ status: "checking", message: "正在检查新版本…" }));
    this.updater.on("update-available", (info: { version: string }) => this.setState({ status: "available", availableVersion: info.version, message: `发现新版本 ${info.version}，可在软件内下载。` }));
    this.updater.on("update-not-available", () => this.setState({ status: "current", availableVersion: undefined, message: "当前已经是最新版本。" }));
    this.updater.on("download-progress", (progress: { percent: number }) => this.setState({ status: "downloading", progress: Math.round(progress.percent), message: `正在下载更新：${Math.round(progress.percent)}%` }));
    this.updater.on("update-downloaded", (info: { version: string }) => this.setState({ status: "downloaded", availableVersion: info.version, progress: 100, message: "更新已经下载完成；重启墨流即可安装。" }));
    this.updater.on("error", (cause: Error) => this.setState({ status: "error", message: `更新失败：${cause.message}` }));
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

  constructor(window: BrowserWindow) {
    this.window = window;
  }

  start(): void {
    if (this.process && !this.process.killed && this.process.exitCode === null) return;
    const command = this.command();
    const child = spawn(command.executable, command.args, {
      cwd: command.cwd,
      env: { ...process.env, PYTHONUTF8: "1", ...command.env },
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.process = child;
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => this.receive(line));
    child.stderr.on("data", () => {
      this.send("engine:status", {
        level: "warning",
        message: "本地写作引擎报告了诊断信息；如任务失败，可查看过程面板。",
      });
    });
    child.once("error", (cause) => this.failChild(child, new Error(`无法启动墨流本地引擎：${cause.message}`)));
    child.once("exit", (code) => this.failChild(child, new Error(`墨流本地引擎已退出（代码 ${code ?? "unknown"}）。请重试刚才的操作。`)));
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.start();
    const child = this.process;
    if (!child || child.killed || child.exitCode !== null) {
      throw new Error("墨流本地引擎暂时不可用，请重试。");
    }
    const id = randomUUID();
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, child });
      child.stdin.write(`${payload}\n`, "utf8", (error) => {
        if (error) {
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  stop(): void {
    const child = this.process;
    this.process = null;
    child?.kill();
  }

  private failChild(child: ChildProcessWithoutNullStreams, error: Error): void {
    for (const [id, item] of this.pending.entries()) {
      if (item.child !== child) continue;
      item.reject(error);
      this.pending.delete(id);
    }
    if (this.process !== child) return;
    this.process = null;
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
      return;
    }
    const id = String(value.id ?? "");
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    if (value.error) {
      const error = value.error as { message?: string; code?: string };
      pending.reject(new Error(error.message || error.code || "墨流引擎请求失败。"));
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
    const python = path.join(repository, ".venv", "Scripts", "python.exe");
    return {
      executable: existsSync(python) ? python : "python",
      args: ["-m", "inkflow.app_server"],
      cwd: repository,
      env: { PYTHONPATH: path.join(repository, "agent", "src") },
    };
  }
}

let mainWindow: BrowserWindow | null = null;
let bridge: EngineBridge | null = null;
let updates: UpdateManager | null = null;

function projectArgument(argv = process.argv): string | null {
  const index = argv.indexOf("--project");
  return index >= 0 && argv[index + 1] ? path.resolve(argv[index + 1]) : null;
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 960,
    minHeight: 650,
    backgroundColor: "#11110f",
    title: "墨流 InkFlow",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  Menu.setApplicationMenu(null);
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
  ipcMain.handle("dialog:choose-folder", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openDirectory", "createDirectory"],
    });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle("dialog:choose-file", async (_event, title: string) => {
    const result = await dialog.showOpenDialog(mainWindow!, {
      title,
      properties: ["openFile"],
      filters: [{ name: "文本资料", extensions: ["txt", "md", "markdown"] }],
    });
    return result.canceled ? null : result.filePaths[0];
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
  mainWindow.on("closed", () => {
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
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
    const root = projectArgument(argv);
    if (root) mainWindow.webContents.send("app:open-project", root);
  });
  app.whenReady().then(() => {
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
