import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const parsed = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(parsed.parseDiagnostics.length, 0, "App.tsx must preserve balanced JSX tags");
const names = ["batchChapterAccepted", "collaborationPending", "pendingWorkLabel", "BatchProgressStatus", "completionReference", "canPollWorkspace", "chatRefreshScope"];
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(functions.length, names.length);
const code = ts.transpileModule(functions.map(node => node.getText(parsed)).join("\n"), { compilerOptions: { jsx: ts.JsxEmit.React } }).outputText;
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
assert.equal(overview.count, 2); // One chapter handoff plus one distinct acceptance; history and diagnostics are not extra tasks.
assert.equal(overview.work.length, 2); // The deferred task remains visible in its disclosure.
assert.equal(overview.messages.length, 1);
assert.equal(overview.resumableBatches.length, 0);
assert.equal(context.pendingWorkLabel(work, true), "复审这章");
assert.equal(context.pendingWorkLabel({ ...work, status: "needs_writer" }, true), "修订并复审这章");
const oldBatch = { run_id: "old", status: "failed", start_chapter_no: 14, end_chapter_no: 15, chapters: [{ chapter_no: 14, status: "accepted" }, { chapter_no: 15, status: "accepted" }] };
assert.equal(context.BatchProgressStatus({ batches: [oldBatch], events: [], activeRunId: "inspect-plan-31" }), null);
assert.equal(context.BatchProgressStatus({ batches: [oldBatch], events: [], activeRunId: null }), null);
const references = [{ run_id: "now", status: "completed", stage: "context.compiled", references: [{ exists: true, relative_path: "OUTLINE.md" }] },
  { run_id: "now", status: "completed", stage: "draft.saved", references: [{ exists: true, relative_path: "chapters/31.draft.md" }] },
  { run_id: "other", status: "completed", stage: "draft.saved", references: [{ exists: true, relative_path: "chapters/other.md" }] },
  { run_id: "now", status: "failed", stage: "review.failed", references: [{ exists: true, relative_path: "reviews/failed.md" }] }];
assert.equal(context.completionReference(references, "now")?.relative_path, "chapters/31.draft.md");
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
