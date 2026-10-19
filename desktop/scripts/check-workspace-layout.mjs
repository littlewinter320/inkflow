import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const text = readFileSync(new URL("../src/workspaceLayout.ts", import.meta.url), "utf8");
const exports = {};
runInNewContext(ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports });
const layout = exports.readWorkspaceLayout(null, { assistantPosition: "floating", assistantX: 0, assistantY: 0, assistantWidth: 1200 });
assert.equal(layout.panels.assistant.x, 0, "Zero positions must survive reopening");
assert.equal(layout.panels.assistant.width, 1200, "Large desktops must not be capped at 760px");
for (const size of [{ width: 2200, height: 1300 }, { width: 960, height: 650 }, { width: 280, height: 180 }]) {
  const fitted = exports.fitFloatingRect("assistant", { x: 1900, y: 900, width: 1200, height: 800 }, size);
  assert.ok(fitted.x >= 0 && fitted.y >= 0 && fitted.x + fitted.width <= size.width && fitted.y + fitted.height <= size.height);
  const current = exports.readWorkspaceLayout(exports.defaultWorkspaceLayout, null);
  current.panels.inspector.visible = true;
  const geometry = exports.workspaceGeometry(current, size);
  for (const panel of Object.values(geometry.panels)) assert.ok(panel.width > 0 && panel.height > 0 && panel.x >= 0 && panel.y >= 0 && panel.x + panel.width <= size.width + .01 && panel.y + panel.height <= size.height + .01);
  assert.ok(geometry.editor.width >= Math.min(340, size.width));
  assert.equal(current.panels.assistant.width, 350, "Viewport adaptation must not overwrite saved sizes");
}
const bottomLayout = exports.readWorkspaceLayout(exports.defaultWorkspaceLayout, null);
for (const panel of Object.values(bottomLayout.panels)) { panel.visible = true; panel.position = "bottom"; }
const bottom = exports.workspaceGeometry(bottomLayout, { width: 1400, height: 800 });
assert.equal(bottom.editor.height, 514);
assert.ok(Object.values(bottom.panels).every(panel => !panel.floating));
assert.ok(Object.values(exports.workspaceGeometry(bottomLayout, { width: 1400, height: 300 }).panels).every(panel => panel.floating), "Small windows must retain a readable editor instead of forcing a bottom row");
assert.equal(exports.dockedWidth("assistant", 2000, { x: 0, y: 0, width: 350, height: 700 }, { x: 356, y: 0, width: 600, height: 700 }, { width: 1000, height: 700 }), 610);
assert.equal(exports.dockTarget(500, 640, { width: 1000, height: 650 }), "bottom");
assert.equal(exports.dockTarget(10, -5, { width: 1000, height: 650 }), null);
console.log("Workspace boundary and legacy-layout checks passed.");
