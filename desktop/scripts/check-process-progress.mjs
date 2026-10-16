import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const helper = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "publicProcessSteps");
assert.ok(helper);
const compiled = ts.transpileModule(helper.getText(tree) + "\nexports.project = publicProcessSteps;", {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const exports = {};
runInNewContext(compiled.outputText, { exports });
const event = (phase, elapsed_ms, role = "writer", run_id = "same-run") => ({
  type: "model.stream.progress", summary: phase, role, run_id, model: "same-model", metadata: { phase, elapsed_ms },
});
const events = [event("thinking", 1), event("thinking", 30), event("thinking", 60), event("content", 61), event("content", 90)];
const original = JSON.stringify(events);
const compacted = exports.project(events);
assert.equal(compacted.length, 2);
assert.equal(compacted[0], events[2]);
assert.equal(compacted[1], events[4]);
assert.equal(JSON.stringify(events), original);
for (const different of [event("thinking", 2, "editor"), event("thinking", 2, "writer", "other-run"), event("thinking", 0)]) {
  assert.equal(exports.project([events[0], different]).length, 2);
}
const failed = { type: "model.stream.interrupted", summary: "connection interrupted" };
assert.equal(exports.project([events[0], failed, events[1]]).length, 3);
assert.equal(exports.project([{ type: "provider.reasoning", summary: "private" }, { type: "usage.updated", summary: "count" }]).length, 0);
console.log("Stream progress projection passed; no engine or model requests.");
