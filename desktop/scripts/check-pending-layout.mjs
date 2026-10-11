import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const parsed = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(parsed.parseDiagnostics.length, 0, "App.tsx must preserve balanced JSX tags");
const names = ["batchChapterAccepted", "batchChapterReviewed", "batchChapterWritten", "batchMilestones", "chapterMilestones", "collaborationPending", "pendingWorkLabel", "pendingDecisionPayload", "BatchProgressStatus", "completionReference", "canPollWorkspace", "chatRefreshScope", "audioPlaybackFailure", "workflowStageState"];
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(functions.length, names.length);
const code = ts.transpileModule(functions.map(node => node.getText(parsed)).join("\n"), { compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } }).outputText;
const context = {};
runInNewContext(code, context);
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
