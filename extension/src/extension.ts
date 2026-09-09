import * as vscode from "vscode";
import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { applyEdits, modify, parse } from "jsonc-parser";
import { controlHtml as buildControlHtml } from "./controlHtml";

type RpcResult = Record<string, unknown> | unknown[] | string | number | boolean | null;
type EngineCommand = { executable: string; args: string[]; env: NodeJS.ProcessEnv; kind: "exe" | "python" };
type QuestionOption = { id: string; label: string; description?: string; recommended?: boolean; kind?: string };
type QuestionCard = { id: string; header?: string; question: string; why_it_matters?: string; selection?: "single" | "multiple"; options: QuestionOption[] };
type PromptPreset = { id: string; label: string; prompt: string; builtin?: boolean };
type SelectionSnapshot = {
  relativePath: string;
  label: string;
  quote: string;
  startOffset: number;
  endOffset: number;
  expectedHash: string;
  dirty: boolean;
  isDraft: boolean;
  tooLong: boolean;
};

const BUILTIN_PRESETS: PromptPreset[] = [
  { id: "builtin-constraints", label: "续写前体检", prompt: "先核对当前正史、人物知识边界、未结伏笔和下一章章节卡，只列出真正会影响续写的风险，不要修改正文。", builtin: true },
  { id: "builtin-scene", label: "场景推进", prompt: "检查当前章每个场景是否形成‘压力—选择—后果’的连续推进，指出重复功能或无因跳变，并给出最小修改建议。", builtin: true },
  { id: "builtin-dialogue", label: "对白声线", prompt: "检查当前章主要人物的对白是否能区分身份、关系和当下目的；引用证据，只建议需要改的部分。", builtin: true },
  { id: "builtin-hook", label: "章末钩子", prompt: "检查当前章钩子是否由本章决定和代价自然产生，读者具体在等什么答案；不要为了悬念新增无来源事件。", builtin: true },
];

let output: vscode.OutputChannel;
let treeProvider: InkFlowTreeProvider;
let controlProvider: InkFlowControlProvider;
let statusDocument = "# 墨流项目状态\n\n尚未读取。\n";

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("墨流 InkFlow", { log: true });
  treeProvider = new InkFlowTreeProvider();
  controlProvider = new InkFlowControlProvider(context);
  context.subscriptions.push(
    output,
    vscode.window.registerTreeDataProvider("inkflow.explorer", treeProvider),
    vscode.window.registerWebviewViewProvider(InkFlowControlProvider.viewType, controlProvider),
    vscode.workspace.registerTextDocumentContentProvider("inkflow-status", {
      provideTextDocumentContent: () => statusDocument,
    }),
    vscode.commands.registerCommand("inkflow.refresh", () => { treeProvider.refresh(); void controlProvider.refresh(); }),
    vscode.commands.registerCommand("inkflow.configureMcp", () => configureMcp(context)),
    vscode.commands.registerCommand("inkflow.commentSelection", () => commentSelection(context, false)),
    vscode.commands.registerCommand("inkflow.reviseSelection", () => commentSelection(context, true)),
    vscode.commands.registerCommand("inkflow.reviewChapter", () => reviewChapter(context)),
    vscode.commands.registerCommand("inkflow.openStatus", () => openStatus(context)),
    vscode.commands.registerCommand("inkflow.createCheckpoint", () => createCheckpoint(context)),
    vscode.commands.registerCommand("inkflow.openTaskHistory", () => openTaskHistory(context)),
    vscode.commands.registerCommand("inkflow.openDesktop", () => openDesktop(context)),
    vscode.commands.registerCommand("inkflow.ask", () => askInkFlow(context)),
    vscode.commands.registerCommand("inkflow.generatePlan", () => runQuickWorkflow(context, "plan")),
    vscode.commands.registerCommand("inkflow.writeChapter", () => runQuickWorkflow(context, "write")),
    vscode.commands.registerCommand("inkflow.acceptChapter", () => runQuickWorkflow(context, "accept")),
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

class InkFlowControlProvider implements vscode.WebviewViewProvider {
  static readonly viewType = "inkflow.control";
  private static readonly presetKey = "inkflow.customPresets.v1";
  private view: vscode.WebviewView | undefined;
  private activeCancellation: vscode.CancellationTokenSource | undefined;

  constructor(private readonly context: vscode.ExtensionContext) {
    context.subscriptions.push(
      vscode.window.onDidChangeTextEditorSelection(() => void this.pushSelection()),
      vscode.window.onDidChangeActiveTextEditor(() => void this.pushSelection()),
    );
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, "media")],
    };
    const revealSpeed = vscode.workspace.getConfiguration("inkflow").get<number>("revealCharactersPerSecond", 10);
    view.webview.html = buildControlHtml(view.webview, this.context.extensionUri, revealSpeed);
    this.context.subscriptions.push(view.webview.onDidReceiveMessage((message: Record<string, unknown>) => void this.handleMessage(message)));
    this.context.subscriptions.push(view.onDidChangeVisibility(() => {
      if (view.visible) {
        void this.pushSelection();
        void this.pushPresets();
      }
    }));
    void this.refresh();
  }

  async refresh(note = ""): Promise<void> {
    const root = projectRoot();
    if (!this.view) return;
    if (!root) {
      await this.view.webview.postMessage({ type: "state", ready: false, note: note || "请先用 VS Code 打开一本墨流小说项目。" });
      return;
    }
    try {
      const data = await requestEngine(this.context, "project.status", { project_root: root });
      const value = data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : {};
      const status = (value.status || {}) as Record<string, unknown>;
      const chapters = (status.chapters || {}) as Record<string, unknown>;
      const brief = (value.brief || {}) as Record<string, unknown>;
      await this.view.webview.postMessage({
        type: "state",
        ready: true,
        title: String(brief.title || path.basename(root)),
        root,
        acceptedCharacters: Number(value.accepted_characters || 0),
        draftChapters: Number(chapters.draft || 0),
        acceptedChapters: Number(chapters.accepted || 0),
        openThreads: Number(status.open_threads || 0),
        planned: Boolean(value.current_plan),
        note,
      });
    } catch (cause) {
      await this.view.webview.postMessage({ type: "state", ready: true, title: path.basename(root), root, error: errorMessage(cause), note });
    }
  }

  private async handleMessage(message: Record<string, unknown>): Promise<void> {
    const command = String(message.command || "");
    if (command === "ready") {
      await Promise.all([this.pushSelection(), this.pushPresets()]);
      return;
    }
    if (command === "openFolder") {
      await vscode.commands.executeCommand("workbench.action.files.openFolder");
      return;
    }
    if (command === "openDesktop") { await openDesktop(this.context); return; }
    if (command === "configureMcp") { await configureMcp(this.context); return; }
    if (command === "openStatus") { await openStatus(this.context); return; }
    if (command === "openTaskHistory") { await openTaskHistory(this.context); return; }
    if (command === "createCheckpoint") { await createCheckpoint(this.context); return; }
    if (command === "refresh") { treeProvider.refresh(); await this.refresh("状态已刷新。"); return; }
    if (command === "cancel") {
      this.activeCancellation?.cancel();
      return;
    }
    if (command === "savePreset") {
      await this.savePreset(String(message.label || ""), String(message.prompt || ""));
      return;
    }
    if (command === "deletePreset") {
      await this.deletePreset(String(message.id || ""));
      return;
    }
    if (command === "selectionAction") {
      await this.selectionAction(Boolean(message.revise), String(message.instruction || ""));
      return;
    }
    if (command === "send") {
      const text = String(message.text || "").trim();
      if (text) await this.run("conversation.send", { message: text }, "正在理解你的自然语言需求");
      return;
    }
    if (command === "workflow") {
      const action = String(message.action || "");
      if (!new Set(["plan", "write", "review", "accept"]).has(action)) return;
      const chapter = Number(message.chapter || 0);
      if (action !== "plan" && chapter < 1) {
        await this.view?.webview.postMessage({ type: "result", error: "请先填写要处理的章节号。" });
        return;
      }
      if (action === "accept") {
        const confirmed = await vscode.window.showWarningMessage(`确认验收第 ${chapter} 章并交给记忆角色提交正史吗？`, { modal: true }, "确认验收");
        if (confirmed !== "确认验收") return;
      }
      await this.run("workflow.run", { action, ...(chapter > 0 ? { chapter_no: chapter } : {}) }, workflowProgress(action, chapter));
    }
  }

  private async run(method: string, params: Record<string, unknown>, label: string): Promise<void> {
    const root = requireProject();
    if (!root) return;
    if (this.activeCancellation) {
      await this.view?.webview.postMessage({ type: "result", text: "已有任务正在运行。可以先停止它，再开始下一项。", error: true });
      return;
    }
    const cancellation = new vscode.CancellationTokenSource();
    this.activeCancellation = cancellation;
    await this.view?.webview.postMessage({ type: "busy", label });
    let result: RpcResult | undefined;
    try {
      result = await requestEngine(
        this.context,
        method,
        { project_root: root, ...params },
        cancellation.token,
        (event) => void this.view?.webview.postMessage({
          type: "progress",
          eventType: String(event.type || "event"),
          summary: String(event.summary || "正在处理"),
        }),
      );
      treeProvider.refresh();
      const presentation = enginePresentation(result);
      await this.view?.webview.postMessage({ type: "result", ...presentation });
      await this.refresh("任务完成；文件树与项目状态已同步。");
    } catch (cause) {
      if (cause instanceof vscode.CancellationError) {
        await this.view?.webview.postMessage({ type: "cancelled" });
      } else {
        await this.view?.webview.postMessage({ type: "result", text: errorMessage(cause), error: true });
      }
    } finally {
      cancellation.dispose();
      if (this.activeCancellation === cancellation) this.activeCancellation = undefined;
    }
    if (result !== undefined) {
      const answer = await askQuestionCards(result);
      if (answer) await this.run("conversation.send", { message: answer }, "正在结合你的选择继续理解原任务");
    }
  }

  private async pushSelection(): Promise<void> {
    await this.view?.webview.postMessage({ type: "selection", selection: activeSelection() });
  }

  private customPresets(): PromptPreset[] {
    const stored = this.context.workspaceState.get<unknown>(InkFlowControlProvider.presetKey, []);
    if (!Array.isArray(stored)) return [];
    return stored.filter((item): item is PromptPreset => Boolean(
      item && typeof item === "object"
      && typeof (item as PromptPreset).id === "string"
      && typeof (item as PromptPreset).label === "string"
      && typeof (item as PromptPreset).prompt === "string",
    )).slice(0, 20);
  }

  private async pushPresets(): Promise<void> {
    await this.view?.webview.postMessage({ type: "presets", presets: [...BUILTIN_PRESETS, ...this.customPresets()] });
  }

  private async savePreset(labelValue: string, promptValue: string): Promise<void> {
    const label = labelValue.trim().slice(0, 20);
    const prompt = promptValue.trim().slice(0, 2_000);
    if (!label || !prompt) {
      await this.view?.webview.postMessage({ type: "result", text: "预设需要名称和提示内容。", error: true });
      return;
    }
    const current = this.customPresets();
    const same = current.find((item) => item.label.toLocaleLowerCase("zh-CN") === label.toLocaleLowerCase("zh-CN"));
    const next = same
      ? current.map((item) => item.id === same.id ? { ...item, label, prompt } : item)
      : [...current, { id: `preset-${randomUUID()}`, label, prompt }].slice(-20);
    await this.context.workspaceState.update(InkFlowControlProvider.presetKey, next);
    await this.pushPresets();
    await this.view?.webview.postMessage({ type: "result", text: same ? `预设“${label}”已更新。` : `预设“${label}”已添加。`, reasoning: [] });
  }

  private async deletePreset(id: string): Promise<void> {
    const current = this.customPresets();
    const target = current.find((item) => item.id === id);
    if (!target) return;
    const confirmed = await vscode.window.showWarningMessage(
      `删除自定义预设“${target.label}”？只会删除当前工作区的这条快捷提示，不影响小说正文。`,
      { modal: true },
      "删除预设",
    );
    if (confirmed !== "删除预设") return;
    await this.context.workspaceState.update(InkFlowControlProvider.presetKey, current.filter((item) => item.id !== id));
    await this.pushPresets();
  }

  private async selectionAction(revise: boolean, instructionValue: string): Promise<void> {
    const selection = activeSelection();
    const instruction = instructionValue.trim();
    if (!selection || !instruction) {
      await this.view?.webview.postMessage({ type: "result", text: "请先在小说 Markdown 中选择文字并填写意见。", error: true });
      return;
    }
    if (selection.dirty) {
      await this.view?.webview.postMessage({ type: "result", text: "正文还有未保存修改。请先保存，再对稳定版本批注或修订。", error: true });
      return;
    }
    if (selection.tooLong && revise) {
      await this.view?.webview.postMessage({ type: "result", text: "一次局部修订最多处理 8000 个字符；请缩小选区或改用整章修订。", error: true });
      return;
    }
    if (revise && !selection.isDraft) {
      await this.view?.webview.postMessage({ type: "result", text: "这不是未验收草稿。正史正文必须先预览后续影响并确认分支修订；当前没有直接覆盖。", error: true });
      return;
    }
    const method = revise ? "document.revise_selection" : "document.annotate";
    await this.run(method, {
      relative_path: selection.relativePath,
      start_offset: selection.startOffset,
      end_offset: selection.endOffset,
      comment: instruction,
      ...(revise ? { expected_hash: selection.expectedHash } : {}),
    }, revise ? "Writer 正在只修改选中的文字" : "正在保存批注");
    await this.pushSelection();
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

async function askInkFlow(context: vscode.ExtensionContext): Promise<void> {
  const root = requireProject();
  if (!root) return;
  const message = await vscode.window.showInputBox({
    title: "和墨流讨论或安排任务",
    prompt: "可以用自然语言讨论、规划、写作、审查或查询记忆，不需要背命令。",
    placeHolder: "例如：先把第 8～12 章的章节卡一次给我看，不要修改规划。",
    ignoreFocusOut: true,
  });
  if (!message?.trim()) return;
  let result = await withProgress("墨流正在理解你的需求", (token) => requestEngine(context, "conversation.send", {
    project_root: root,
    message: message.trim(),
  }, token));
  for (let round = 0; round < 3; round += 1) {
    const answer = await askQuestionCards(result);
    if (!answer) break;
    result = await withProgress("墨流正在结合你的选择继续理解", (token) => requestEngine(context, "conversation.send", {
      project_root: root,
      message: answer,
    }, token));
  }
  statusDocument = `# 墨流答复\n\n${visibleEngineResult(result)}\n`;
  const document = await vscode.workspace.openTextDocument(vscode.Uri.parse(`inkflow-status:墨流答复.md?${Date.now()}`));
  await vscode.window.showTextDocument(document, { preview: true });
  treeProvider.refresh();
  await controlProvider.refresh("自然语言任务已完成。");
}

async function runQuickWorkflow(context: vscode.ExtensionContext, action: "plan" | "write" | "accept"): Promise<void> {
  const root = requireProject();
  if (!root) return;
  let chapter = vscode.window.activeTextEditor ? chapterNumber(vscode.window.activeTextEditor.document.uri.fsPath) : null;
  if (action !== "plan" && !chapter) {
    const value = await vscode.window.showInputBox({ title: "选择章节", prompt: "填写要处理的章节号。", validateInput: (text) => Number(text) >= 1 ? undefined : "请输入大于 0 的章节号。" });
    if (!value) return;
    chapter = Number(value);
  }
  if (action === "accept") {
    const confirmed = await vscode.window.showWarningMessage(`确认验收第 ${chapter} 章并提交正史吗？`, { modal: true }, "确认验收");
    if (confirmed !== "确认验收") return;
  }
  const result = await withProgress(workflowProgress(action, chapter || 0), (token) => requestEngine(context, "workflow.run", {
    project_root: root,
    action,
    ...(chapter ? { chapter_no: chapter } : {}),
  }, token));
  treeProvider.refresh();
  await controlProvider.refresh("快捷任务已完成。");
  void vscode.window.showInformationMessage(visibleEngineResult(result).slice(0, 240));
}

function workflowProgress(action: string, chapter: number): string {
  if (action === "plan") return "写作角色正在生成四级规划";
  if (action === "write") return `写作角色正在创作第 ${chapter} 章草稿`;
  if (action === "review") return `审查角色正在检查第 ${chapter} 章`;
  if (action === "accept") return `记忆角色正在提交第 ${chapter} 章正史`;
  return "墨流正在处理任务";
}

function visibleEngineResult(result: RpcResult): string {
  return enginePresentation(result).text;
}

function enginePresentation(result: RpcResult): { text: string; reasoning: string[]; details?: string } {
  const text = primaryResultText(result);
  const reasoning = collectPublicSummaries(result);
  const safe = displaySafeValue(result);
  const serialized = typeof safe === "object" && safe !== null ? JSON.stringify(safe, null, 2) : "";
  return {
    text,
    reasoning,
    ...(serialized && serialized !== "{}" ? { details: serialized.slice(0, 14_000) } : {}),
  };
}

function primaryResultText(result: unknown): string {
  if (typeof result === "string") return result;
  if (Array.isArray(result)) return result.map((item) => primaryResultText(item)).filter(Boolean).join("\n");
  if (!result || typeof result !== "object") return "任务已完成，请从小说结构中查看新文件。";
  const value = result as Record<string, unknown>;
  if (value.reply) return String(value.reply);
  if (Array.isArray(value.help)) return value.help.map(String).map((item) => `• ${item}`).join("\n");
  if (value.help) return String(value.help);
  if (value.gate) return String(value.gate);
  if (value.annotation_id && value.replacement_excerpt) {
    return `第 ${String(value.chapter_no || "")} 章选区已生成新草稿版本 v${String(value.version || "")}。${String(value.next_action || "请重新审查当前版本。")}`;
  }
  if (value.annotation_id) return "批注已保存。正文没有被修改。";
  if (value.verdict && value.chapter_no) {
    return `第 ${String(value.chapter_no)} 章审查完成：${String(value.summary || value.verdict)}${value.score_total !== undefined ? `（${String(value.score_total)} 分）` : ""}`;
  }
  if (value.draft_path && value.chapter_no) {
    return `第 ${String(value.chapter_no)} 章草稿 v${String(value.version || "1")} 已生成。${value.next_action ? `下一步：${String(value.next_action)}。` : ""}`;
  }
  if (Array.isArray(value.steps) && value.steps.length) {
    const last = value.steps[value.steps.length - 1] as Record<string, unknown>;
    const nested = primaryResultText(last?.result ?? last);
    return value.next_action ? `${nested}\n\n下一步：${String(value.next_action)}` : nested;
  }
  if (value.result !== undefined) return primaryResultText(value.result);
  for (const key of ["message", "summary", "next_action"]) {
    if (value[key]) return String(value[key]);
  }
  return "任务已完成，文件树和项目状态已经更新。";
}

function collectPublicSummaries(value: unknown, target: string[] = [], seen = new Set<unknown>()): string[] {
  if (!value || typeof value !== "object" || seen.has(value)) return target;
  seen.add(value);
  if (Array.isArray(value)) {
    for (const item of value) collectPublicSummaries(item, target, seen);
    return target.slice(0, 10);
  }
  const record = value as Record<string, unknown>;
  const add = (item: unknown) => {
    for (const text of Array.isArray(item) ? item.map(String) : item ? [String(item)] : []) {
      const trimmed = text.trim();
      if (trimmed && !target.includes(trimmed)) target.push(trimmed);
    }
  };
  add(record.public_reasoning_summary);
  add(record.decision_summary);
  if (record.session && typeof record.session === "object") add((record.session as Record<string, unknown>).visible_reason);
  for (const [key, nested] of Object.entries(record)) {
    if (isSensitivePresentationKey(key)) continue;
    collectPublicSummaries(nested, target, seen);
  }
  return target.slice(0, 10);
}

function displaySafeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(displaySafeValue);
  if (!value || typeof value !== "object") return value;
  const result: Record<string, unknown> = {};
  for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
    if (isSensitivePresentationKey(key)) continue;
    result[key] = displaySafeValue(nested);
  }
  return result;
}

function isSensitivePresentationKey(key: string): boolean {
  const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
  return /apikey|tracepath|chainofthought|rawthought|hiddenthought|reasoningcontent|providerreasoning|thinkingcontent|internalreasoning/.test(normalized)
    || normalized === "reasoning"
    || normalized === "thoughts"
    || normalized === "thinking";
}

async function askQuestionCards(result: RpcResult): Promise<string | null> {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const raw = (result as Record<string, unknown>).questions;
  if (!Array.isArray(raw) || raw.length === 0) return null;
  const questions = raw as QuestionCard[];
  const answers: string[] = [];
  for (let index = 0; index < questions.length; index += 1) {
    const question = questions[index];
    const items = question.options.map((option) => ({
      label: option.recommended ? `$(star-full) ${option.label}（建议）` : option.label,
      description: option.description,
      detail: question.why_it_matters ? `为什么问：${question.why_it_matters}` : undefined,
      option,
    }));
    const picked = await vscode.window.showQuickPick(items, {
      title: `${question.header || "需要确认"} · ${index + 1}/${questions.length}`,
      placeHolder: question.question,
      canPickMany: question.selection === "multiple",
      ignoreFocusOut: true,
    });
    if (!picked || (Array.isArray(picked) && picked.length === 0)) return null;
    const selected = Array.isArray(picked) ? picked : [picked];
    const labels: string[] = [];
    for (const item of selected) {
      if (item.option.kind === "other") {
        const custom = await vscode.window.showInputBox({
          title: question.question,
          prompt: "其他：请用自己的话回答。",
          ignoreFocusOut: true,
          validateInput: (value) => value.trim() ? undefined : "请填写你的答案。",
        });
        if (!custom) return null;
        labels.push(`其他：${custom.trim()}`);
      } else {
        labels.push(item.option.label);
      }
    }
    answers.push(`${index + 1}. ${question.question}\n我的回答：${labels.join("；")}`);
  }
  return `我来回答刚才的问题：\n${answers.join("\n")}\n请结合这些答案继续理解原来的目标；如果此前已经明确要求执行且信息足够，就继续原任务，否则先总结你理解到的方案。`;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause || "墨流遇到未知错误。");
}

function controlHtml(webview: vscode.Webview): string {
  const nonce = randomUUID().replaceAll("-", "");
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  *{box-sizing:border-box}body{padding:0 10px 16px;color:var(--vscode-foreground);font-family:var(--vscode-font-family);font-size:12px}button,textarea,input{font:inherit}button{border:1px solid var(--vscode-button-border,transparent);padding:7px 9px;color:var(--vscode-button-foreground);background:var(--vscode-button-background);cursor:pointer}button:hover{background:var(--vscode-button-hoverBackground)}button.secondary{color:var(--vscode-foreground);background:var(--vscode-button-secondaryBackground)}button:disabled{opacity:.55;cursor:default}.hero{padding:10px 0 8px}.hero h2{margin:0;font-size:17px}.hero p{margin:5px 0;color:var(--vscode-descriptionForeground);line-height:1.5}.status{padding:10px;border:1px solid var(--vscode-widget-border);background:var(--vscode-sideBarSectionHeader-background)}.status strong{display:block;font-size:13px}.status small{display:block;margin-top:4px;overflow:hidden;color:var(--vscode-descriptionForeground);text-overflow:ellipsis;white-space:nowrap}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-top:8px}.metrics div{padding:7px;border:1px solid var(--vscode-widget-border)}.metrics span{display:block;color:var(--vscode-descriptionForeground);font-size:10px}.metrics b{display:block;margin-top:3px}.section{margin-top:14px}.section h3{margin:0 0 7px;font-size:12px}.flow{display:grid;grid-template-columns:1fr 1fr;gap:5px}.chapter{display:grid;grid-template-columns:1fr 72px;gap:5px;margin-bottom:5px}.chapter input,textarea{width:100%;border:1px solid var(--vscode-input-border);padding:7px;color:var(--vscode-input-foreground);background:var(--vscode-input-background)}textarea{min-height:92px;resize:vertical}.send{display:flex;justify-content:flex-end;margin-top:5px}.result{margin-top:9px;padding:9px;border-left:3px solid var(--vscode-focusBorder);background:var(--vscode-textBlockQuote-background);line-height:1.55;white-space:pre-wrap}.result.error{border-color:var(--vscode-errorForeground);color:var(--vscode-errorForeground)}.note{margin-top:8px;color:var(--vscode-descriptionForeground);font-size:10px;line-height:1.5}.empty{padding:14px 0}.empty button{width:100%;margin-top:7px}
</style></head><body>
<header class="hero"><h2>墨流小说工作台</h2><p>自然语言主控，写作、审查、记忆三个角色各守边界。</p></header>
<section id="empty" class="empty" hidden><p id="emptyNote">请先打开墨流小说项目。</p><button id="openFolder">打开项目文件夹</button><button id="openDesktopEmpty" class="secondary">打开桌面版新建小说</button></section>
<main id="workspace" hidden>
  <section class="status"><strong id="title">正在读取项目…</strong><small id="root"></small><div class="metrics"><div><span>已接受正文</span><b id="characters">0 字</b></div><div><span>开放线索</span><b id="threads">0</b></div><div><span>草稿章节</span><b id="drafts">0</b></div><div><span>正史章节</span><b id="accepted">0</b></div></div></section>
  <section class="section"><h3>常用工作流</h3><div class="chapter"><input id="chapter" type="number" min="1" value="1" aria-label="章节号"><button id="status" class="secondary">项目状态</button></div><div class="flow"><button data-action="plan">生成四级规划</button><button data-action="write">写本章草稿</button><button data-action="review" class="secondary">审查本章</button><button data-action="accept" class="secondary">验收进正史</button></div></section>
  <section class="section"><h3>自然语言</h3><textarea id="message" placeholder="例如：先比较第 8～12 章的章节卡和当前正史，只指出风险，不要修改。"></textarea><div class="send"><button id="askFirst" class="secondary">让墨流先问我</button><button id="send">发送给墨流</button></div></section>
  <section class="section"><h3>工具</h3><div class="flow"><button id="refresh" class="secondary">刷新状态</button><button id="desktop" class="secondary">桌面版打开</button><button id="mcp" class="secondary">配置模型工具连接</button></div></section>
  <div id="result" class="result" hidden aria-live="polite"></div><p id="note" class="note"></p>
</main>
<script nonce="${nonce}">
  const vscode=acquireVsCodeApi();const previous=vscode.getState()||{};const q=id=>document.getElementById(id);const buttons=()=>[...document.querySelectorAll('button')];
  q('chapter').value=previous.chapter||1;q('message').value=previous.message||'';
  function post(command,extra={}){vscode.postMessage({command,...extra})}function busy(label){buttons().forEach(b=>b.disabled=true);q('result').hidden=false;q('result').className='result';q('result').textContent=label+'…'}
  q('openFolder').onclick=()=>post('openFolder');q('openDesktopEmpty').onclick=()=>post('openDesktop');q('status').onclick=()=>post('openStatus');q('refresh').onclick=()=>post('refresh');q('desktop').onclick=()=>post('openDesktop');q('mcp').onclick=()=>post('configureMcp');
  document.querySelectorAll('[data-action]').forEach(button=>button.onclick=()=>{const chapter=Number(q('chapter').value||0);vscode.setState({chapter,message:q('message').value});busy('任务已提交');post('workflow',{action:button.dataset.action,chapter})});
  q('askFirst').onclick=()=>{const text='先不要执行任务。请根据当前项目状态和最近讨论，用选项主动问我一到三个最值得确认的问题；说明答案会影响什么，最后保留其他选项。';busy('墨流正在准备关键问题');post('send',{text})};
  q('send').onclick=()=>{const text=q('message').value.trim();if(!text)return;vscode.setState({chapter:q('chapter').value,message:text});busy('墨流正在理解你的需求');post('send',{text})};
  window.addEventListener('message',event=>{const data=event.data;if(data.type==='busy'){busy(data.label);return}if(data.type==='result'){buttons().forEach(b=>b.disabled=false);q('result').hidden=false;q('result').className=data.error?'result error':'result';q('result').textContent=data.error||data.text;return}if(data.type==='state'){buttons().forEach(b=>b.disabled=false);q('empty').hidden=Boolean(data.ready);q('workspace').hidden=!data.ready;if(!data.ready){q('emptyNote').textContent=data.note||'请先打开墨流小说项目。';return}q('title').textContent=data.title||'墨流小说';q('root').textContent=data.root||'';q('characters').textContent=Number(data.acceptedCharacters||0).toLocaleString()+' 字';q('threads').textContent=String(data.openThreads||0);q('drafts').textContent=String(data.draftChapters||0);q('accepted').textContent=String(data.acceptedChapters||0);q('note').textContent=data.error||data.note||(data.planned?'四级规划已就绪。':'尚未生成四级规划。')}});
</script></body></html>`;
}

async function commentSelection(context: vscode.ExtensionContext, revise: boolean): Promise<void> {
  const root = requireProject();
  const editor = vscode.window.activeTextEditor;
  if (!root || !editor || editor.selection.isEmpty) {
    void vscode.window.showInformationMessage("请先在小说 Markdown 中选中文字。");
    return;
  }
  const selection = activeSelection();
  if (!selection) {
    void vscode.window.showErrorMessage("只能批注墨流项目中的用户文档。");
    return;
  }
  if (selection.dirty) {
    void vscode.window.showWarningMessage("正文还有未保存修改。请先保存，再对稳定版本批注或修订。");
    return;
  }
  if (revise && !selection.isDraft) {
    void vscode.window.showWarningMessage("这不是未验收草稿。正史正文必须先预览影响并确认分支修订；墨流没有直接覆盖。");
    return;
  }
  if (revise && selection.tooLong) {
    void vscode.window.showWarningMessage("一次局部修订最多处理 8000 个字符；请缩小选区或改用整章修订。");
    return;
  }
  const instruction = await vscode.window.showInputBox({
    title: revise ? "让写作角色如何修订这段文字？" : "给这段文字添加批注",
    prompt: revise ? "Writer 只替换这个选区，并保存为新的未验收草稿版本。" : "批注本身不会修改正文。",
    ignoreFocusOut: true,
  });
  if (!instruction) return;
  await withProgress(revise ? "写作角色正在处理选区" : "正在保存墨流批注", async (token) => {
    await requestEngine(context, revise ? "document.revise_selection" : "document.annotate", {
      project_root: root,
      relative_path: selection.relativePath,
      start_offset: selection.startOffset,
      end_offset: selection.endOffset,
      comment: instruction,
      ...(revise ? { expected_hash: selection.expectedHash } : {}),
    }, token);
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
  await withProgress(`审查角色正在检查第 ${chapter} 章`, (token) => requestEngine(context, "workflow.run", { project_root: root, action: "review", chapter_no: chapter }, token));
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
    void vscode.window.showWarningMessage("没有找到已安装的墨流桌面版。请先安装墨流桌面版，或在设置中填写引擎路径。", "打开设置").then((choice) => { if (choice) void vscode.commands.executeCommand("workbench.action.openSettings", "inkflow.enginePath"); });
    return;
  }
  const child = spawn(executable, ["--project", root], { detached: true, stdio: "ignore", windowsHide: true });
  child.unref();
}

async function requestEngine(
  context: vscode.ExtensionContext,
  method: string,
  params: Record<string, unknown>,
  cancellation?: vscode.CancellationToken,
  onEvent?: (event: Record<string, unknown>) => void,
): Promise<RpcResult> {
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
            onEvent?.(event);
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
  const sourceRoot = path.resolve(context.extensionPath, "..", "agent", "src");
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
function activeSelection(): SelectionSnapshot | null {
  const root = projectRoot();
  const editor = vscode.window.activeTextEditor;
  if (!root || !editor || editor.selection.isEmpty || editor.document.uri.scheme !== "file") return null;
  const relativePath = path.relative(root, editor.document.uri.fsPath).replaceAll("\\", "/");
  if (relativePath.startsWith("../") || relativePath === ".." || relativePath.startsWith(".inkflow/") || !relativePath.toLowerCase().endsWith(".md")) return null;
  const content = editor.document.getText();
  const prefix = editor.document.getText(new vscode.Range(new vscode.Position(0, 0), editor.selection.start));
  const selected = editor.document.getText(editor.selection);
  // Python indexes Unicode code points, while VS Code offsets are UTF-16 code units.
  // Counting code points keeps selections after emoji and astral characters aligned.
  const startOffset = Array.from(prefix).length;
  const selectedCharacters = Array.from(selected).length;
  const endOffset = startOffset + selectedCharacters;
  const quote = selectedCharacters > 600 ? `${Array.from(selected).slice(0, 600).join("")}…` : selected;
  return {
    relativePath,
    label: `${path.basename(relativePath)} · ${selectedCharacters.toLocaleString("zh-CN")} 字`,
    quote,
    startOffset,
    endOffset,
    expectedHash: createHash("sha256").update(content, "utf8").digest("hex"),
    dirty: editor.document.isDirty,
    isDraft: relativePath.toLowerCase().endsWith(".draft.md"),
    tooLong: selectedCharacters > 8_000,
  };
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
