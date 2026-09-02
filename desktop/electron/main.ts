import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

type Pending = {
  resolve: (value: unknown) => void;
  reject: (reason?: unknown) => void;
};

class EngineBridge {
  private process: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, Pending>();
  private window: BrowserWindow;

  constructor(window: BrowserWindow) {
    this.window = window;
  }

  start(): void {
    if (this.process && !this.process.killed) return;
    const command = this.command();
    this.process = spawn(command.executable, command.args, {
      cwd: command.cwd,
      env: { ...process.env, PYTHONUTF8: "1", ...command.env },
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const lines = readline.createInterface({ input: this.process.stdout });
    lines.on("line", (line) => this.receive(line));
    this.process.stderr.on("data", () => {
      this.send("engine:status", {
        level: "warning",
        message: "本地写作引擎报告了诊断信息；如任务失败，可查看过程面板。",
      });
    });
    this.process.once("exit", (code) => {
      const error = new Error(`墨流本地引擎已退出（代码 ${code ?? "unknown"}）。`);
      for (const item of this.pending.values()) item.reject(error);
      this.pending.clear();
      this.process = null;
      this.send("engine:status", { level: "error", message: error.message });
    });
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    this.start();
    const id = randomUUID();
    const payload = JSON.stringify({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process?.stdin.write(`${payload}\n`, "utf8", (error) => {
        if (error) {
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  stop(): void {
    this.process?.kill();
    this.process = null;
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
      env: { PYTHONPATH: path.join(repository, "src") },
    };
  }
}

let mainWindow: BrowserWindow | null = null;
let bridge: EngineBridge | null = null;

function projectArgument(argv = process.argv): string | null {
  const index = argv.indexOf("--project");
  return index >= 0 && argv[index + 1] ? path.resolve(argv[index + 1]) : null;
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 1120,
    minHeight: 720,
    backgroundColor: "#11110f",
    title: "墨流 InkFlow",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  bridge = new EngineBridge(mainWindow);
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
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  const developmentUrl = process.env.VITE_DEV_SERVER_URL;
  if (developmentUrl) void mainWindow.loadURL(developmentUrl);
  else void mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  mainWindow.on("closed", () => {
    bridge?.stop();
    bridge = null;
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
