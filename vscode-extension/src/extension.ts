import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { applyEdits, modify, parse } from "jsonc-parser";

type RpcResult = Record<string, unknown> | unknown[] | string | number | boolean | null;
type EngineCommand = { executable: string; args: string[]; env: NodeJS.ProcessEnv; kind: "exe" | "python" };

let output: vscode.OutputChannel;
let treeProvider: InkFlowTreeProvider;
let statusDocument = "# 墨流项目状态\n\n尚未读取。\n";

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("墨流 InkFlow", { log: true });
  treeProvider = new InkFlowTreeProvider();
  context.subscriptions.push(
    output,
    vscode.window.registerTreeDataProvider("inkflow.explorer", treeProvider),
    vscode.workspace.registerTextDocumentContentProvider("inkflow-status", {
      provideTextDocumentContent: () => statusDocument,
    }),
    vscode.commands.registerCommand("inkflow.refresh", () => treeProvider.refresh()),
    vscode.commands.registerCommand("inkflow.configureMcp", () => configureMcp(context)),
    vscode.commands.registerCommand("inkflow.commentSelection", () => commentSelection(context, false)),
    vscode.commands.registerCommand("inkflow.reviseSelection", () => commentSelection(context, true)),
    vscode.commands.registerCommand("inkflow.reviewChapter", () => reviewChapter(context)),
    vscode.commands.registerCommand("inkflow.openStatus", () => openStatus(context)),
    vscode.commands.registerCommand("inkflow.createCheckpoint", () => createCheckpoint(context)),
    vscode.commands.registerCommand("inkflow.openTaskHistory", () => openTaskHistory(context)),
    vscode.commands.registerCommand("inkflow.openDesktop", () => openDesktop(context)),
  );
  registerMcpProviderWhenAvailable(context);
  void vscode.commands.executeCommand("setContext", "inkflow.project", Boolean(projectRoot()));
}

export function deactivate(): void {}

class InkFlowItem extends vscode.TreeItem {
  constructor(
    public readonly absolutePath: string | null,
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    public readonly children: InkFlowItem[] = [],
    icon = "file",
  ) {
    super(label, collapsibleState);
    this.iconPath = new vscode.ThemeIcon(icon);
    if (absolutePath) {
      this.resourceUri = vscode.Uri.file(absolutePath);
      this.command = { command: "vscode.open", title: "打开", arguments: [this.resourceUri] };
      this.tooltip = absolutePath;
    }
  }
}

class InkFlowTreeProvider implements vscode.TreeDataProvider<InkFlowItem> {
  private readonly changed = new vscode.EventEmitter<InkFlowItem | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  refresh(): void { this.changed.fire(undefined); }
  getTreeItem(element: InkFlowItem): vscode.TreeItem { return element; }
  getChildren(element?: InkFlowItem): InkFlowItem[] {
    if (element) return element.children;
    const root = projectRoot();
    if (!root) return [new InkFlowItem(null, "请先打开墨流小说项目", vscode.TreeItemCollapsibleState.None, [], "info")];
    const core = ["BOOK.md", "PLAN.md", "STATE.md", "DIALOGUE.md"].filter((name) => fs.existsSync(path.join(root, name))).map((name) => new InkFlowItem(path.join(root, name), coreLabel(name), vscode.TreeItemCollapsibleState.None, [], coreIcon(name)));
    const groups = [
      folderItem(root, "chapters", "章节", "book"),
      folderItem(root, "planning", "规划判断", "map"),
      folderItem(root, "reviews", "审查", "pass"),
      folderItem(root, "batches", "批量草稿", "layers"),
    ];
    return [
      new InkFlowItem(null, "项目状态", vscode.TreeItemCollapsibleState.None, [], "pulse") as InkFlowItem,
      new InkFlowItem(null, "核心文档", vscode.TreeItemCollapsibleState.Expanded, core, "notebook"),
      ...groups,
    ].map((item, index) => {
      if (index === 0) item.command = { command: "inkflow.openStatus", title: "打开项目状态" };
      return item;
    });
  }
}

function folderItem(root: string, folder: string, label: string, icon: string): InkFlowItem {
  const directory = path.join(root, folder);
  const files = fs.existsSync(directory)
    ? fs.readdirSync(directory, { withFileTypes: true })
        .filter((entry) => entry.isFile() && entry.name.toLowerCase().endsWith(".md"))
        .sort((a, b) => a.name.localeCompare(b.name, "zh-CN", { numeric: true }))
        .map((entry) => new InkFlowItem(path.join(directory, entry.name), displayFile(entry.name), vscode.TreeItemCollapsibleState.None, [], folder === "chapters" ? "book" : "markdown"))
    : [];
  return new InkFlowItem(null, label, vscode.TreeItemCollapsibleState.Collapsed, files, icon);
}

async function commentSelection(context: vscode.ExtensionContext, revise: boolean): Promise<void> {
  const root = requireProject();
  const editor = vscode.window.activeTextEditor;
  if (!root || !editor || editor.selection.isEmpty) {
    void vscode.window.showInformationMessage("请先在小说 Markdown 中选中文字。");
    return;
  }
  const absolute = editor.document.uri.fsPath;
  const relative = path.relative(root, absolute).replaceAll("\\", "/");
  if (relative.startsWith("..") || relative.startsWith(".inkflow/")) {
    void vscode.window.showErrorMessage("只能批注墨流项目中的用户文档。");
    return;
  }
  const instruction = await vscode.window.showInputBox({
    title: revise ? "让 Writer 如何修订这段文字？" : "给这段文字添加批注",
    prompt: revise ? "会先保存批注，再通过墨流自然语言主控请求修订。" : "批注本身不会修改正文。",
    ignoreFocusOut: true,
  });
  if (!instruction) return;
  const start = editor.document.offsetAt(editor.selection.start);
  const end = editor.document.offsetAt(editor.selection.end);
  const quote = editor.document.getText(editor.selection);
  await withProgress(revise ? "Writer 正在处理选区" : "正在保存墨流批注", async (token) => {
    const annotation = await requestEngine(context, "document.annotate", {
      project_root: root,
      relative_path: relative,
      start_offset: start,
      end_offset: end,
      comment: instruction,
    }, token);
    if (revise) {
      const chapter = chapterNumber(relative);
      if (!chapter) throw new Error("Writer 定点修订目前只适用于章节文件；普通文档已保留批注。");
      await requestEngine(context, "conversation.send", {
        project_root: root,
        message: `请让 Writer 定点修订第 ${chapter} 章。用户选中的原文是“${quote.slice(0, 800)}”，要求：${instruction}。保留旧版本，修订后不要自动验收。批注记录：${stringField(annotation, "annotation_id")}`,
      }, token);
    }
  });
  treeProvider.refresh();
  void vscode.window.showInformationMessage(revise ? "修订请求已完成；请查看新草稿与审查状态。" : "批注已保存。", "打开墨流输出").then((choice) => { if (choice) output.show(); });
}

async function reviewChapter(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  const editor = vscode.window.activeTextEditor;
  if (!root || !editor) return;
  const chapter = chapterNumber(editor.document.uri.fsPath);
  if (!chapter) { void vscode.window.showInformationMessage("当前文件不是墨流章节。"); return; }
  await withProgress(`Reviewer 正在审查第 ${chapter} 章`, (token) => requestEngine(context, "workflow.run", { project_root: root, action: "review", chapter_no: chapter }, token));
  treeProvider.refresh();
  void vscode.window.showInformationMessage("审查完成。扣分项和证据已写入 reviews。", "查看审查目录").then(() => treeProvider.refresh());
}

async function openStatus(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  if (!root) return;
  const value = await requestEngine(context, "project.status", { project_root: root });
  statusDocument = renderStatus(value);
  const uri = vscode.Uri.parse(`inkflow-status:项目状态.md?${Date.now()}`);
  const document = await vscode.workspace.openTextDocument(uri);
  await vscode.window.showTextDocument(document, { preview: true });
}

async function createCheckpoint(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  if (!root) return;
  const label = await vscode.window.showInputBox({
    title: "创建墨流检查点",
    prompt: "同时保存正史 SQLite 与受管理 Markdown；不会修改正文。",
    value: "手动安全点",
    ignoreFocusOut: true,
  });
  if (!label?.trim()) return;
  const result = await withProgress("正在创建墨流检查点", (token) => requestEngine(context, "workflow.run", {
    project_root: root,
    action: "checkpoint_create",
    label: label.trim(),
  }, token));
  void vscode.window.showInformationMessage(`检查点已创建：${stringField(result, "checkpoint_id") || label}`);
}

async function openTaskHistory(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  if (!root) return;
  const value = await requestEngine(context, "task.list", { project_root: root, limit: 30 });
  statusDocument = renderTaskHistory(value);
  const uri = vscode.Uri.parse(`inkflow-status:任务记录.md?${Date.now()}`);
  const document = await vscode.workspace.openTextDocument(uri);
  await vscode.window.showTextDocument(document, { preview: true });
}

async function configureMcp(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  if (!root) return;
  const command = resolveEngine(context);
  const configPath = path.join(root, ".vscode", "mcp.json");
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const existing = fs.existsSync(configPath) ? fs.readFileSync(configPath, "utf8") : "{\n  \"servers\": {}\n}\n";
  const definition = command.kind === "exe"
    ? { type: "stdio", command: command.executable, args: ["--mcp"], env: { INKFLOW_WORKSPACE: root } }
    : { type: "stdio", command: command.executable, args: [...command.args.filter((arg) => arg !== "--once").slice(0, -1), "inkflow.mcp_server"], env: { INKFLOW_WORKSPACE: root, ...(command.env.PYTHONPATH ? { PYTHONPATH: command.env.PYTHONPATH } : {}) } };
  let updated: string;
  try {
    parse(existing);
    updated = applyEdits(existing, modify(existing, ["servers", "inkflow"], definition, { formattingOptions: { insertSpaces: true, tabSize: 2 } }));
  } catch {
    throw new Error(".vscode/mcp.json 不是有效的 JSON/JSONC；为避免覆盖其他 MCP 配置，墨流没有改写它。");
  }
  fs.writeFileSync(configPath, updated.endsWith("\n") ? updated : `${updated}\n`, "utf8");
  const document = await vscode.workspace.openTextDocument(configPath);
  await vscode.window.showTextDocument(document, { preview: false });
  void vscode.window.showInformationMessage("墨流 MCP 已合并到当前工作区。重新加载窗口后，Claude/Copilot/兼容宿主即可发现它。", "重新加载").then((choice) => { if (choice) void vscode.commands.executeCommand("workbench.action.reloadWindow"); });
}

async function openDesktop(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  if (!root) return;
  const candidates = desktopCandidates(context);
  const executable = candidates.find(fs.existsSync);
  if (!executable) {
    void vscode.window.showWarningMessage("没有找到已安装的墨流桌面版。请先安装墨流 0.2，或在设置中填写引擎路径。", "打开设置").then((choice) => { if (choice) void vscode.commands.executeCommand("workbench.action.openSettings", "inkflow.enginePath"); });
    return;
  }
  const child = spawn(executable, ["--project", root], { detached: true, stdio: "ignore", windowsHide: true });
  child.unref();
}

async function requestEngine(context: vscode.ExtensionContext, method: string, params: Record<string, unknown>, cancellation?: vscode.CancellationToken): Promise<RpcResult> {
  const command = resolveEngine(context);
  const id = randomUUID();
  return new Promise((resolve, reject) => {
    const child: ChildProcessWithoutNullStreams = spawn(command.executable, command.args, { env: command.env, cwd: projectRoot() || undefined, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    let settled = false;
    let stdout = "";
    child.stdout.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
      const lines = stdout.split(/\r?\n/);
      stdout = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const value = JSON.parse(line) as Record<string, unknown>;
          if (value.method === "event") {
            const event = value.params as Record<string, unknown>;
            output.appendLine(`${String(event.type || "event")} · ${String(event.summary || "")}`);
          } else if (String(value.id) === id) {
            settled = true;
            if (value.error) reject(new Error(String((value.error as Record<string, unknown>).message || "墨流请求失败")));
            else resolve(value.result as RpcResult);
          }
        } catch { output.appendLine("[警告] 忽略一行非 JSON 引擎输出。"); }
      }
    });
    child.stderr.on("data", () => output.appendLine("[警告] 本地引擎返回了诊断信息；为避免泄露敏感参数，扩展未复制原文。"));
    child.on("error", reject);
    child.on("exit", (code) => { if (!settled) reject(new Error(`墨流引擎提前退出（${code ?? "unknown"}）。`)); });
    cancellation?.onCancellationRequested(() => { child.kill(); reject(new vscode.CancellationError()); });
    child.stdin.end(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`, "utf8");
  });
}

function resolveEngine(context: vscode.ExtensionContext): EngineCommand {
  const config = vscode.workspace.getConfiguration("inkflow");
  const configuredExe = config.get<string>("enginePath")?.trim();
  const bundled = path.join(context.extensionPath, "bin", "inkflow-engine.exe");
  const installedSidecar = path.join(process.env.LOCALAPPDATA || "", "Programs", "墨流 InkFlow", "resources", "engine", "inkflow-engine.exe");
  const executable = [configuredExe, bundled, installedSidecar].find((item): item is string => Boolean(item && fs.existsSync(item)));
  if (executable) return { executable, args: ["--once"], env: { ...process.env, PYTHONUTF8: "1" }, kind: "exe" };
  const root = projectRoot();
  const configuredPython = config.get<string>("pythonPath")?.trim();
  const workspacePython = root ? path.join(root, ".venv", "Scripts", "python.exe") : "";
  const repositoryPython = path.resolve(context.extensionPath, "..", ".venv", "Scripts", "python.exe");
  const python = [configuredPython, workspacePython, repositoryPython].find((item): item is string => Boolean(item && fs.existsSync(item))) || "python";
  const sourceRoot = path.resolve(context.extensionPath, "..", "src");
  return { executable: python, args: ["-m", "inkflow.app_server", "--once"], env: { ...process.env, PYTHONUTF8: "1", ...(fs.existsSync(sourceRoot) ? { PYTHONPATH: sourceRoot } : {}) }, kind: "python" };
}

function registerMcpProviderWhenAvailable(context: vscode.ExtensionContext): void {
  const lm = vscode.lm as unknown as { registerMcpServerDefinitionProvider?: (id: string, provider: unknown) => vscode.Disposable };
  const Definition = (vscode as unknown as Record<string, unknown>).McpStdioServerDefinition as (new (label: string, command: string, args: string[], env: Record<string, string>) => unknown) | undefined;
  if (!lm.registerMcpServerDefinitionProvider || !Definition) return;
  context.subscriptions.push(lm.registerMcpServerDefinitionProvider("inkflow.vscode", {
    provideMcpServerDefinitions: () => {
      const root = projectRoot();
      if (!root) return [];
      const command = resolveEngine(context);
      const args = command.kind === "exe" ? ["--mcp"] : ["-m", "inkflow.mcp_server"];
      return [new Definition("墨流 InkFlow", command.executable, args, { INKFLOW_WORKSPACE: root })];
    },
  }));
}

function projectRoot(): string | null {
  const folders = vscode.workspace.workspaceFolders || [];
  const direct = folders.find((folder) => fs.existsSync(path.join(folder.uri.fsPath, ".inkflow", "project.json")));
  return direct?.uri.fsPath || null;
}
function requireProject(): string | null { const root = projectRoot(); if (!root) void vscode.window.showErrorMessage("当前工作区不是墨流小说项目：找不到 .inkflow/project.json。"); return root; }
function chapterNumber(value: string): number | null { const match = value.match(/chapter_(\d+)/i); return match ? Number(match[1]) : null; }
function coreLabel(name: string): string { return ({ "BOOK.md": "书籍设定", "PLAN.md": "当前规划", "STATE.md": "正史状态", "DIALOGUE.md": "对话记录" } as Record<string, string>)[name] || name; }
function coreIcon(name: string): string { return ({ "BOOK.md": "notebook", "PLAN.md": "map", "STATE.md": "database", "DIALOGUE.md": "comment-discussion" } as Record<string, string>)[name] || "file"; }
function displayFile(name: string): string { const chapter = chapterNumber(name); if (chapter) return `第 ${chapter} 章${name.includes(".draft.") ? " · 草稿" : ""}`; return name.replace(/\.md$/i, ""); }
function stringField(value: RpcResult, key: string): string { return value && typeof value === "object" && !Array.isArray(value) ? String((value as Record<string, unknown>)[key] || "") : ""; }
function withProgress<T>(title: string, task: (token: vscode.CancellationToken) => Thenable<T>): Thenable<T> { return vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title, cancellable: true }, (_progress, token) => task(token)); }
function desktopCandidates(context: vscode.ExtensionContext): string[] { const configured = vscode.workspace.getConfiguration("inkflow").get<string>("enginePath") || ""; return [configured.endsWith(".exe") && !configured.endsWith("inkflow-engine.exe") ? configured : "", path.join(process.env.LOCALAPPDATA || "", "Programs", "墨流 InkFlow", "墨流 InkFlow.exe"), path.join(context.extensionPath, "InkFlow.exe")].filter(Boolean); }
function renderStatus(value: RpcResult): string { const data = (value || {}) as Record<string, unknown>; const status = (data.status || data) as Record<string, unknown>; return `# 墨流项目状态\n\n> 本页由扩展实时读取，不是模型思维链。\n\n- 项目：\`${String(data.project_id || "未知")}\`\n- 当前规划：${data.current_plan ? "已生成" : "待生成或读取"}\n- 已接受正文：${String(data.accepted_characters || 0)} 字\n- 活跃事实：${String(status.active_facts || 0)}\n- 开放线索：${String(status.open_threads || 0)}\n\n## 原始状态\n\n\`\`\`json\n${JSON.stringify(value, null, 2)}\n\`\`\`\n`; }
function renderTaskHistory(value: RpcResult): string {
  const tasks = value && typeof value === "object" && !Array.isArray(value)
    ? (((value as Record<string, unknown>).tasks || []) as Array<Record<string, unknown>>)
    : [];
  const lines = tasks.map((task) => {
    const title = String(task.action || task.method || "任务");
    const status = String(task.status || "未知");
    const summary = String(task.error_message || task.summary || "无摘要");
    return `## ${title}\n\n- 状态：${status}\n- 时间：${String(task.updated_at || "未知")}\n- 运行：\`${String(task.run_id || "")}\`\n\n${summary}`;
  });
  return `# 墨流任务记录\n\n> 这里只显示状态与可复核摘要，不包含模型原始思维链。\n\n${lines.join("\n\n") || "尚无任务记录。"}\n`;
}
