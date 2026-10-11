import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { Script } from "node:vm";

const require = createRequire(new URL("../desktop/package.json", import.meta.url));
const ts = require("typescript");
const source = readFileSync(new URL("../desktop/src/Mascot.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("Mascot.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const helper = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "boundedGuidePoint");
assert(helper, "Use the actual guide boundary helper.");
const code = ts.transpileModule(helper.getText(tree).replace("export function", "function"), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
const bound = new Script(`${code}\nboundedGuidePoint`).runInNewContext();
for (const width of [96, 320, 768, 1920]) {
  for (const height of [180, 400, 1080]) {
    const bounds = { left: 20, top: 30, right: 20 + width, bottom: 30 + height };
    for (const [x, y] of [[-999, -999], [99999, 99999], [40, 50], [width / 2, height / 2]]) {
      const point = bound(x, y, bounds, 80, 80);
      assert(point.x >= bounds.left + 8 && point.x + 80 <= bounds.right - 8);
      assert(point.y >= bounds.top + 8 && point.y + 80 <= bounds.bottom - 8);
      const unchanged = bound(point.x, point.y, bounds, 80, 80);
      assert.equal(unchanged.x, point.x);
      assert.equal(unchanged.y, point.y);
    }
    const cardWidth = Math.min(240, width - 16);
    const card = bound(99999, 99999, bounds, cardWidth, 100);
    assert(card.x + cardWidth <= bounds.right - 8 && card.y + 100 <= bounds.bottom - 8);
  }
}
console.log("Mobao guide fits narrow/wide work areas, overflow coordinates and result cards.");

const guideNode = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "MobaoGuide");
const guideCode = ts.transpileModule(guideNode.getText(tree).replace("export function", "function"), { compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } }).outputText;
function observeGuide(interruptAt, reduced = false) {
  let now = 0, state = null, previewCount = 0, timerId = 0;
  const timers = new Map(), handlers = new Map(), phases = [];
  class Element {}
  const bounds = { left: 0, top: 0, right: 320, bottom: 400, width: 320, height: 400 };
  const area = { getBoundingClientRect: () => bounds, addEventListener: () => {}, removeEventListener: () => {} };
  const target = { isConnected: true, getBoundingClientRect: () => ({ ...bounds, left: 120, top: 200, right: 260, bottom: 250, width: 140, height: 50 }) };
  const context = {
    boundedGuidePoint: bound, Node: Element,
    useRef: current => ({ current }),
    useState: initial => [initial, value => { state = typeof value === "function" ? value(state) : value; phases.push({ at: now, phase: state?.phase || "closed" }); }],
    useEffect: effect => effect(),
    window: { matchMedia: () => ({ matches: reduced }),
      setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, at: now + delay }); return id; },
      clearTimeout: id => timers.delete(id), addEventListener: () => {}, removeEventListener: () => {} },
    document: { hidden: false, querySelector: () => null,
      addEventListener: (type, listener) => handlers.set(type, listener), removeEventListener: type => handlers.delete(type) },
  };
  const guide = new Script(`${guideCode}\nMobaoGuide`).runInNewContext(context);
  guide({ cue: { id: "actual-output", summary: "本次成果" }, workAreaRef: { current: area }, targetRef: { current: target }, autoPreview: true, onPreview: () => previewCount++ });
  while (timers.size) {
    const [id, timer] = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
    if (interruptAt !== undefined && timer.at >= interruptAt) { now = interruptAt; handlers.get("pointerdown")({ target: new Element() }); break; }
    now = timer.at; timers.delete(id); timer.callback();
  }
  return { phases, previewCount, remainingTimers: timers.size, state, now };
}
const completed = observeGuide();
assert.equal(completed.previewCount, 1);
assert.equal(completed.remainingTimers, 0);
assert.equal(completed.state, null);
assert.deepEqual(completed.phases.filter((item, index, all) => index === 0 || item.phase !== all[index-1].phase).map(item => item.phase), ["move", "point", "tap", "present", "return", "closed"]);
const pointStart = completed.phases.find(item => item.phase === "point").at;
const tapStart = completed.phases.find(item => item.phase === "tap").at;
const presentStart = completed.phases.find(item => item.phase === "present").at;
assert.equal(tapStart - pointStart, 2000);
assert.equal(presentStart - tapStart, 1500);
assert.ok(completed.now <= 9000);
const interrupted = observeGuide(2500);
assert.equal(interrupted.previewCount, 0);
assert.equal(interrupted.remainingTimers, 0);
assert.equal(interrupted.state, null);
const quiet = observeGuide(undefined, true);
assert.equal(quiet.previewCount, 1);
assert.equal(quiet.state, null);
assert.ok(quiet.now <= 3500);
console.log("Actual guide effects pass: full point/tap durations, one preview, finite closure, input interruption and reduced motion.");
