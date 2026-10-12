import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { EventEmitter } from "node:events";
import ts from "typescript";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const parsed = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(parsed.parseDiagnostics.length, 0, "App.tsx must preserve balanced JSX tags");
assert.ok(!source.includes('"0.7.0"'), "Version lookup failure must not invent a stale version");
const mainSource = readFileSync(new URL("../electron/main.ts", import.meta.url), "utf8");
const mainParsed = ts.createSourceFile("main.ts", mainSource, ts.ScriptTarget.Latest, true);
const mode = mainParsed.statements.flatMap(node => ts.isVariableStatement(node) ? [...node.declarationList.declarations] : []).find(node => node.name.getText(mainParsed) === "isDevelopment");
const modeCode = ts.transpileModule(`globalThis.isDevelopment = ${mode.initializer.getText(mainParsed)}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
for (const [packaged, url, sources, expected] of [[true, "http://localhost:5000", true, true], [true, "", false, false], [true, "http://localhost:5000", false, false], [false, "", true, true]]) {
  const launch = { app: { isPackaged: packaged }, process: { env: { VITE_DEV_SERVER_URL: url } }, existsSync: () => sources, path: { resolve: () => "source.py" }, __dirname: "source" };
  runInNewContext(modeCode, launch);
  assert.equal(launch.isDevelopment, expected, "Branded dev processes must use source engines; installed apps keep packaged engines");
}
const pipeGuard = mainParsed.statements.find(node => ts.isForOfStatement(node) && node.getText(mainParsed).includes("stdio.unavailable"));
const pipe = { process: { stdout: new EventEmitter(), stderr: new EventEmitter() }, recorded: [] };
pipe.logLifecycle = (event, details) => pipe.recorded.push([event, details.code]);
runInNewContext(ts.transpileModule(pipeGuard.getText(mainParsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, pipe);
pipe.process.stdout.emit("error", { code: "EPIPE" });
pipe.process.stderr.emit("error", { code: "EPIPE" });
assert.equal(pipe.recorded.length, 2, "Detached output pipes must not crash the Electron main process");
const bridgeClass = mainParsed.statements.find(node => ts.isClassDeclaration(node) && node.name?.text === "EngineBridge");
const workCounter = bridgeClass.members.find(node => ts.isMethodDeclaration(node) && node.name.getText(mainParsed) === "pendingWorkCount");
const pendingProbe = {};
runInNewContext(ts.transpileModule(`globalThis.PendingProbe = class { constructor(entries) { this.pending = new Map(entries.map((item, i) => [i, item])); } ${workCounter.getText(mainParsed)} }`, {}).outputText, pendingProbe);
const readProbe = new pendingProbe.PendingProbe([{method:"project.status"}, {method:"collaboration.overview"}, {method:"computer.actions.list"}]);
assert.equal(readProbe.pendingWorkCount(), 0, "Background polling must not display unfinished work on exit");
const workProbe = new pendingProbe.PendingProbe([{method:"conversation.send",conversationId:"a"}, {method:"document.save",conversationId:"b"}, {method:"voice.moss.install",conversationId:"b"}]);
assert.equal(workProbe.pendingWorkCount(), 3, "Generation, saving and installation still protect exit");
assert.equal(workProbe.pendingWorkCount("a"), 1, "Closing one conversation must not count another conversation's work");
const names = ["busyMascotMood", "batchChapterAccepted", "batchChapterReviewed", "batchChapterWritten", "batchMilestones", "chapterMilestones", "collaborationPending", "pendingWorkLabel", "pendingDecisionPayload", "BatchProgressStatus", "completionReference", "canPollWorkspace", "chatRefreshScope", "audioPlaybackFailure", "workflowStageState", "processElapsed", "publicProcessSteps", "collectPublicSummaries", "isSensitivePresentationKey"];
names.push("readableWorkMessage", "readableWorkReason");
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(functions.length, names.length);
const code = ts.transpileModule(functions.map(node => node.getText(parsed)).join("\n"), { compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } }).outputText;
const context = {};
runInNewContext(code, context);
const selectedWork = {id:"ab3cc97ec6d768a97dc701e3",kind:"workflow",chapter_no:41,intent:{action:"continue_until",end_chapter_no:50}};
assert.equal(context.readableWorkMessage("处理待办 ab3cc97ec6d768a97dc701e3", [selectedWork]), "继续处理第41—50章的未完成任务。");
assert.equal(context.readableWorkMessage("钥匙编号731仍在原处。", [selectedWork]), "钥匙编号731仍在原处。");
assert.ok(!context.readableWorkMessage("处理待办 ab3cc97ec6d768a97dc701e3", []).includes(selectedWork.id));
assert.ok(context.readableWorkReason({chapter_no:42,reason:"引用待核 @quote:6；@quote:8"}).includes("2条记忆引用"));
const messageFunction = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "errorMessage");
runInNewContext(ts.transpileModule(messageFunction.getText(parsed), {}).outputText, context);
assert.equal(context.errorMessage("原步骤暂时不能完成\n已保留：已经保存的正文保持当前状态。"), "原步骤暂时不能完成");
assert.ok(context.errorMessage("'stop_reason'").includes("任务衔接的程序问题"));
assert.equal(context.errorMessage("第42章审查暂停。\n服务商拒绝了当前请求（403）。\n下一步：检查账号权限"), "第42章审查暂停。\n服务商拒绝了当前请求（403）。\n下一步：检查账号权限");
assert.equal(context.busyMascotMood([{ run_id: "now", role: "editor", stage: "chapter.review" }], "now"), "reviewing");
assert.equal(context.busyMascotMood([{ run_id: "now", role: "writer", stage: "outline.writer" }], "now"), "thinking");
assert.equal(context.busyMascotMood([{ run_id: "now", role: "writer", stage: "chapter.write" }], "now"), "writing");
assert.equal(context.busyMascotMood([{ run_id: "old", role: "writer", stage: "chapter.write" }], "now"), "thinking");
assert.ok(!source.includes("conversation-layout-select"), "Conversation docking must not require a position setting");
assert.ok(!readFileSync(new URL("../src/ProjectCenter.tsx", import.meta.url), "utf8").includes("planning.cleanup.preview"), "Opening the project center must not scan planning history");
const mascotSource = readFileSync(new URL("../src/Mascot.tsx", import.meta.url), "utf8");
const mascotParsed = ts.createSourceFile("Mascot.tsx", mascotSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const sprite = mascotParsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "spritePosition");
runInNewContext(ts.transpileModule(sprite.getText(mascotParsed).replace("export ", ""), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
const manifest = JSON.parse(readFileSync(new URL("../src/assets/mascot/animations/mobao-clean.manifest.json", import.meta.url), "utf8"));
for (const clip of manifest.clips.filter(clip => clip.drawn_frames)) {
  const [columns, rows] = clip.sheet_grid;
  assert.equal(clip.drawn_frames, columns * rows);
  assert.equal(clip.drawn_frames, clip.decoded_frames, "Encoded frames must not inflate the drawing count");
  assert.equal(clip.drawn_fps, 20);
  assert.equal(clip.duration, clip.drawn_frames / clip.drawn_fps);
  assert.equal(new Set(Array.from({ length: clip.drawn_frames }, (_, index) => context.spritePosition(index, columns, rows))).size, clip.drawn_frames);
  assert.equal(context.spritePosition(clip.drawn_frames + 5, columns, rows), "100% 100%");
  assert.equal(readdirSync(new URL(`../src/assets/mascot/animations/mobao-clean-${clip.action}-frames/`, import.meta.url)).filter(name => /^frame-\d+\.png$/.test(name)).length, clip.drawn_frames);
}
assert.equal(context.processElapsed("2026-10-12T10:00:00Z", Date.parse("2026-10-12T10:01:08Z")), "1分8秒");
assert.equal(context.processElapsed("invalid", Date.now()), "0秒");
assert.equal(context.publicProcessSteps([{ stage: "outline.provider_reasoning", summary: "private" }, { type: "workflow.stage", stage: "outline.review", summary: "公开依据" }]).length, 1);
assert.deepEqual([...context.collectPublicSummaries({ decision_summary: "已定位正文依据", reasoning_content: "private", raw_thought: "private" })], ["已定位正文依据"]);
const work = { id: "review-20", kind: "plan_revision", status: "awaiting_review", chapter_no: 20, can_process: true, intent: { batch_id: "linked" } };
const overview = context.collaborationPending({
  pending_work: [work, { id: "diagnosis", parent_id: work.id, status: "blocked" }, { id: "later", status: "deferred" }, { id: "done", status: "completed" }],
  batches: [
    { batch_id: "accepted", status: "failed", chapters: [{ status: "accepted" }] },
    { batch_id: "linked", status: "failed", resume_available: true, chapters: [{ status: "draft" }] },
    { batch_id: "ready", status: "ready_for_acceptance", chapters: [{ status: "draft", review_status: "passed" }] },
  ],
  messages: [{ status: "escalated" }, { status: "resolved" }],
});
assert.equal(overview.count, 1); // Count current source work; batch handoffs and linked diagnostics are separate views.
assert.equal(overview.work.length, 2); // The deferred task remains visible in its disclosure.
assert.equal(overview.messages.length, 1);
assert.equal(overview.resumableBatches.length, 0);
const historical = context.collaborationPending({ batches: [
  { batch_id: "old-empty", status: "failed", historical: true, chapters: [] },
  { batch_id: "live-missing", status: "failed", chapters: [] },
], messages: [] });
assert.equal(historical.historicalBatches.length, 1);
assert.equal(historical.pendingBatches.length, 1);
assert.equal(historical.count, 1);
const suggestions = context.collaborationPending({ pending_work: [work,
  { id: "summary", kind: "memory_summary", status: "waiting_condition", can_reject: true, can_process: false },
  { id: "required", kind: "review_gate", status: "waiting_condition", can_reject: false, can_process: false },
], batches: [], messages: [] });
assert.equal(suggestions.suggestions.length, 1);
assert.equal(suggestions.count, 2, "Non-executable optional suggestions must not inflate required review counts");
assert.equal(context.pendingWorkLabel(work, true), "复核这章");
assert.equal(context.pendingWorkLabel({ ...work, status: "needs_writer" }, true), "核对并按需修订");
assert.equal(context.batchChapterAccepted({ status: "draft", memory_status: "canon" }), false);
assert.equal(context.batchChapterAccepted({ status: "accepted", chapter_status: "draft" }), false);
assert.equal(context.batchChapterReviewed({ status: "accepted", review_status: "needs_review" }), false);
const draftBatch = { start_chapter_no: 31, end_chapter_no: 40, chapters: Array.from({ length: 10 }, (_, index) => ({ chapter_no: index + 31, status: "draft", version: 2, review_status: "needs_review" })) };
const batchProgress = context.batchMilestones(draftBatch);
assert.equal(batchProgress.written, 10);
assert.equal(batchProgress.awaitingReview, 10);
assert.equal(batchProgress.accepted, 0);
const bookProgress = context.chapterMilestones({ status: { chapters: { draft: 10, accepted: 30 } }, chapter_progress: { written: 40, awaiting_review: 10, accepted: 30, written_through: 40, accepted_through: 30 } });
assert.equal(bookProgress.written, 40);
assert.equal(bookProgress.awaitingReview, 10);
assert.equal(bookProgress.accepted, 30);
assert.equal(context.chapterMilestones({ status: { chapters: { draft: 10, accepted: 30 } } }).awaitingReview, null);
const rejected = context.collaborationPending({ pending_work: [work, work, { ...work, id: "refused", status: "rejected" }], rejected_work: [{ ...work, id: "refused", status: "rejected" }], batches: [], messages: [] });
assert.equal(rejected.count, 1);
assert.equal(rejected.rejected.length, 1);
const rejectPayload = context.pendingDecisionPayload("book", { ...work, updated_at: "source-v1" }, "reject", "   ");
assert.equal(rejectPayload.reason, "用户选择不执行此项工作，保留已有成果和必要依赖");
assert.equal(rejectPayload.expected_updated_at, "source-v1");
assert.equal(context.pendingDecisionPayload("book", work, "reject", "  保留当前版本  ").reason, "保留当前版本");
const reopenPayload = context.pendingDecisionPayload("book", { ...work, status: "rejected", processing_attempts: 2 }, "reopen");
assert.equal(reopenPayload.decision, "reopen");
assert.equal(reopenPayload.work_id, work.id);
assert.equal("reason" in reopenPayload, false);
assert.equal("processing_attempts" in reopenPayload, false, "restoring scheduling must not reset task attempts");
assert.ok(source.includes('onReopen={(item)') && source.includes('decision: "reopen"'));
assert.ok(source.includes("恢复安排保留原任务处理次数，不立即执行，也不表示问题已经解决。"));
assert.equal(context.workflowStageState([{ type: "chapter.accepting" }], "accept"), "active");
assert.equal(context.workflowStageState([{ type: "chapter.accepted" }], "accept"), "done");
assert.equal(context.workflowStageState([{ type: "reviewer.started" }], "review"), "active");
assert.equal(context.audioPlaybackFailure(new Error("Failed to load because no supported source was found.")).includes("已有文件保留"), true);
assert.equal(context.audioPlaybackFailure(undefined, 3).includes("无法解码"), true);
const deniedSpeech = context.audioPlaybackFailure(new Error("403 url='wss://speech.platform.bing.com/?Sec-MS-GEC=private'"));
assert.equal(deniedSpeech.includes("403"), true);
assert.equal(deniedSpeech.includes("Sec-MS-GEC"), false);
const app = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "App");
const speak = app.body.statements.flatMap(node => ts.isVariableStatement(node) ? [...node.declarationList.declarations] : []).find(node => node.name.getText(parsed) === "speakText");
let speechRequests = 0;
const speechContext = { ...context, autoReadPausedRef: { current: false }, speakingMessageIdRef: { current: null },
  audioPlaybackSequenceRef: { current: 0 }, notices: [], errors: [],
  async request() { if (++speechRequests === 1) throw new Error("403 private speech URL"); return { audio_path: "kept.mp3" }; },
  async playAudioPath() {},
  setSpeakingMessageId() {},
};
speechContext.stopAudioPlayback = () => { speechContext.audioPlaybackSequenceRef.current++; speechContext.speakingMessageIdRef.current = null; };
speechContext.setNotice = message => speechContext.notices.push(message);
speechContext.setError = message => speechContext.errors.push(message);
runInNewContext(ts.transpileModule(`globalThis.speakText = ${speak.initializer.getText(parsed)}`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, speechContext);
await speechContext.speakText("reply-1", "文字保留", true);
await speechContext.speakText("reply-2", "继续写作", true);
assert.equal(speechRequests, 1, "Automatic speech must pause after failure");
assert.equal(speechContext.notices.length, 1);
assert.equal(speechContext.errors.length, 0, "Speech failure must not mark the writing task failed");
await speechContext.speakText("reply-2", "手动重试");
await speechContext.speakText("reply-3", "继续自动朗读", true);
assert.equal(speechRequests, 3, "Successful manual retry restores automatic speech");
assert.ok(!source.includes("ContextTopbarBadge") && !source.includes("context-topbar-badge"));
const footer = source.match(/<footer className="status-strip project-body-total"[\s\S]*?<\/footer>/)?.[0];
assert.ok(footer?.includes("written_characters"));
assert.ok(!/对白|分钟|段|tokens|Context/.test(footer));
assert.ok(source.includes('"project.pending_decide"') && source.includes('item.can_reject === true'));
assert.ok(source.includes('id: "mimo"') && source.includes('"mimo-v2.6-flash"'));
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
assert.ok(/media-src[^;]*\bdata:/.test(html), "authorized data:audio must be allowed by the renderer media CSP");
const oldBatch = { run_id: "old", status: "failed", start_chapter_no: 14, end_chapter_no: 15, chapters: [{ chapter_no: 14, status: "accepted" }, { chapter_no: 15, status: "accepted" }] };
assert.equal(context.BatchProgressStatus({ batches: [oldBatch], events: [], activeRunId: "inspect-plan-31" }), null);
assert.equal(context.BatchProgressStatus({ batches: [oldBatch], events: [], activeRunId: null }), null);
const references = [{ run_id: "now", status: "completed", stage: "context.compiled", references: [{ exists: true, relative_path: "OUTLINE.md" }] },
  { run_id: "now", status: "completed", stage: "draft.saved", references: [{ exists: true, relative_path: "chapters/31.draft.md" }] },
  { run_id: "other", status: "completed", stage: "draft.saved", references: [{ exists: true, relative_path: "chapters/other.md" }] },
  { run_id: "now", status: "failed", stage: "review.failed", references: [{ exists: true, relative_path: "reviews/failed.md" }] }];
assert.equal(context.completionReference(references, "now")?.relative_path, "chapters/31.draft.md");
const diagnostics=[{run_id:"now",status:"completed",stage:"mode.editor",references:[
  {exists:true,relative_path:".inkflow/runs/review/output.json"},
  {exists:true,relative_path:".inkflow\\runs\\review\\trace.md"},
  {exists:true,relative_path:"reviews/output.json"}]}];
assert.equal(context.completionReference(diagnostics,"now"),undefined);
assert.equal(context.completionReference([...references,...diagnostics],"now")?.relative_path,"chapters/31.draft.md");
assert.equal(context.completionReference([...diagnostics,{run_id:"now",status:"completed",stage:"review.saved",references:[
  {exists:true,relative_path:"reviews/current.review.md"}]}],"now")?.relative_path,"reviews/current.review.md");
assert.ok(source.includes("autoPreview={Boolean(completionReference(events, mobaoCompletion?.runId))}"));
assert.ok(source.includes('"document.read_reference"') && source.includes("text !== savedText"));
assert.equal(context.canPollWorkspace("visible", true), true);
assert.equal(context.canPollWorkspace("visible", false), false);
assert.equal(context.canPollWorkspace("hidden", true), false);
assert.equal(context.chatRefreshScope({ intent: { action: "pending_work", pending_work_decision: "inspect" } }), "pending");
assert.equal(context.chatRefreshScope({ intent: { action: "pending_work", pending_work_decision: "defer" } }), "pending");
assert.equal(context.chatRefreshScope({ intent: { action: "pending_work", pending_work_decision: "process_all" } }), "project");
assert.equal(context.chatRefreshScope({ intent: { action: "discuss" } }), "none");
assert.equal(context.chatRefreshScope({ intent: { action: "write" } }), "project");
assert.equal(context.chatRefreshScope({}), "project");
let refreshCallback;
function findRefresh(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(parsed) === "refresh" && ts.isCallExpression(node.initializer)) refreshCallback = node.initializer.arguments[0];
  ts.forEachChild(node, findRefresh);
}
findRefresh(parsed);
assert.ok(refreshCallback);
const reads = [];
Object.assign(context, { projectRoot: "book", projectRootRef: { current: "book" }, projectRefreshRef: { current: null },
  window: { inkflow: { request: async method => { reads.push(method); return { dashboard: {}, tree: {} }; } } },
  setDashboard() {}, setTree() {}, setCollaboration() {}, setCanonMigration() {} });
runInNewContext(ts.transpileModule(`globalThis.testRefresh = ${refreshCallback.getText(parsed)};`, {}).outputText, context);
await Promise.all([context.testRefresh(), context.testRefresh()]);
assert.deepEqual(reads, ["project.open", "collaboration.overview"]);
console.log("Pending layout checks passed: source task counts, chapter actions, retained deferred items.");
