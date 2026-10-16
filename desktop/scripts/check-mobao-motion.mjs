import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/Mascot.tsx", import.meta.url), "utf8");
const parsed = ts.createSourceFile("Mascot.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(parsed.parseDiagnostics.length, 0);
const names = ["spritePosition", "mobaoTravelMs", "mobaoPathClear", "boundedGuidePoint", "nextMobaoAction"];
const helpers = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(helpers.length, names.length);
const context = {};
runInNewContext(ts.transpileModule(helpers.map(node => node.getText(parsed).replace("export ", "")).join("\n"), {}).outputText, context);
const origin = { x: 100, y: 100 };
assert.equal(context.mobaoTravelMs(origin, origin), 240);
assert.ok(context.mobaoTravelMs(origin, { x: 320, y: 100 }) < context.mobaoTravelMs(origin, { x: 540, y: 100 }));
assert.equal(context.mobaoTravelMs(origin, { x: 10000, y: 100 }), 1400);
assert.equal(context.mobaoTravelMs(origin, { x: 320, y: 100 }), context.mobaoTravelMs({ x: 320, y: 100 }, origin));
const input = { left: 250, right: 400, top: 200, bottom: 450 };
assert.equal(context.mobaoPathClear(origin, { x: 450, y: 100 }, [input]), true);
assert.equal(context.mobaoPathClear(origin, { x: 450, y: 300 }, [input]), false);
assert.equal(context.mobaoPathClear({ x: 450, y: 300 }, origin, [input]), false);
assert.equal(context.mobaoPathClear({ x: 260, y: 220 }, { x: 260, y: 220 }, [input]), false);
assert.equal(context.mobaoPathClear({ x: 174, y: 220 }, { x: 174, y: 220 }, [input]), true);
assert.equal(context.spritePosition(0, 4, 4, 4), "0% 33.33333333333333%");
assert.equal(context.spritePosition(3, 4, 4, 12), "100% 100%");
assert.equal(new Set(Array.from({ length: 8 }, (_, frame) => context.spritePosition(frame, 4, 4, 4))).size, 8);
assert.equal(context.spritePosition(0, 1, 1), "0% 0%");
assert.equal(context.nextMobaoAction(["tap", "wave", "smile"], "tap", 0), "wave");
assert.equal(context.nextMobaoAction(["tap", "wave", "smile"], "tap", 1), "smile");
assert.throws(() => context.nextMobaoAction([], null), /动作不能为空/);
const bounds = { left: 20, right: 420, top: 50, bottom: 650 };
assert.deepEqual({ ...context.boundedGuidePoint(-100, 1000, bounds, 76, 76) }, { x: 28, y: 566 });
const fps = parsed.statements.flatMap(node => ts.isVariableStatement(node) ? [...node.declarationList.declarations] : []).find(node => node.name.getText(parsed) === "mascotFramesPerSecond");
assert.equal(Number(fps.initializer.getText(parsed)), 12);
context.mascotFramesPerSecond = 12;
context.flightSheet = "flight-atlas";
context.clips = Object.fromEntries(["flight_start", "move_right", "move_left", "flight_stop"].map(name => [name, {}]));
const flightSetup = parsed.statements.find(node => ts.isForOfStatement(node) && node.initializer.getText(parsed).includes("frameOffset"));
runInNewContext(ts.transpileModule(flightSetup.getText(parsed), {}).outputText, context);
context.flightStartMs = Math.ceil(context.clips.flight_start.duration * 1000);
context.flightStopMs = Math.ceil(context.clips.flight_stop.duration * 1000);
assert.equal(context.clips.flight_start.duration, context.clips.flight_start.frames / 12);
assert.equal(context.clips.flight_stop.duration, context.clips.flight_stop.frames / 12);
assert.deepEqual([context.clips.flight_start.frames, context.clips.move_right.frames, context.clips.flight_stop.frames], [8, 16, 8]);
assert.deepEqual([context.clips.flight_start.frameOffset, context.clips.move_right.frameOffset, context.clips.flight_stop.frameOffset], [0, 8, 24]);
const flightFrames = ["flight_start", "move_right", "flight_stop"].flatMap(name => {
  const clip = context.clips[name];
  assert.equal(clip.columns, 8); assert.equal(clip.rows, 4);
  return Array.from({ length: clip.frames }, (_, frame) => context.spritePosition(frame, clip.columns, clip.rows, clip.frameOffset));
});
assert.equal(new Set(flightFrames).size, 32, "The three phases must use all 32 drawings without overlap");
assert.equal(flightFrames[0], "0% 0%");
assert.equal(flightFrames.at(-1), "100% 100%");
const flightPng = readFileSync(new URL("../src/assets/mascot/animations/mobao-flight-v3.atlas.png", import.meta.url));
assert.equal(flightPng.readUInt32BE(16), 8 * 320);
assert.equal(flightPng.readUInt32BE(20), 4 * 320);
assert.equal(flightPng[25], 6, "The runtime atlas must keep RGBA transparency");

// Exercise the real host with a small clock and DOM boundary, without opening a browser.
let hookIndex = 0, now = 0, nextTimer = 0, layoutEffects = [], effects = [];
const hooks = [], timers = new Map(), events = new Map();
class Element {
  style = {}; attributes = new Map(); listeners = new Map();
  constructor(rect) { this.rect = rect; }
  getBoundingClientRect() { return this.rect; }
  contains(value) { return value === this; }
  closest() { return null; }
  querySelectorAll() { return []; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeEventListener(name) { this.listeners.delete(name); }
  focus() {}
}
const dock = new Element({ left: 1000, top: 100, width: 70, height: 70 });
const workArea = new Element({ left: 0, top: 0, right: 1200, bottom: 900, width: 1200, height: 900 });
const overlay = new Element({ left: 1000, top: 100, width: 76, height: 76 });
const effect = (queue, callback, deps) => {
  const index = hookIndex++, previous = hooks[index];
  if (!previous || deps?.some((value, i) => !Object.is(value, previous.deps?.[i]))) queue.push(() => {
    previous?.cleanup?.(); hooks[index] = { deps, cleanup: callback() };
  });
};
Object.assign(context, {
  useState: initial => {
    const index = hookIndex++;
    hooks[index] ||= { value: typeof initial === "function" ? initial() : initial };
    return [hooks[index].value, value => { hooks[index].value = typeof value === "function" ? value(hooks[index].value) : value; }];
  },
  useRef: initial => { const index = hookIndex++; return hooks[index] ||= { current: initial }; },
  useEffect: (callback, deps) => effect(effects, callback, deps),
  useLayoutEffect: (callback, deps) => effect(layoutEffects, callback, deps),
  createPortal: value => value,
  Mascot: "Mascot", HTMLElement: Element, Node: Element,
  React: { createElement: (type, props, ...children) => {
    if (props?.className === "mobao-companion") {
      const position = /translate3d\(([-\d.]+)px, ([-\d.]+)px, 0\)/.exec(props.style.transform);
      overlay.rect = { ...overlay.rect, left: Number(position[1]), top: Number(position[2]) };
      props.ref.current = overlay;
    }
    return { type, props: props || {}, children };
  } },
  window: {
    innerWidth: 1200,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, due: now + delay }); return id; },
    clearTimeout: id => timers.delete(id),
    addEventListener: (name, callback) => events.set(name, callback), removeEventListener: name => events.delete(name),
  },
  performance: { now: () => now },
  document: { hidden: false, activeElement: null, body: {}, querySelector: () => null, addEventListener() {}, removeEventListener() {} },
});
const companion = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "MobaoCompanion");
runInNewContext(ts.transpileModule(companion.getText(parsed).replace("export ", ""), { compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } }).outputText, context);
const props = { workAreaRef: { current: workArea }, dockRef: { current: dock }, targetRef: { current: null }, cue: null, disabled: false };
const render = () => {
  hookIndex = 0; layoutEffects = []; effects = [];
  const result = context.MobaoCompanion(props);
  if (!result) hooks.filter(value => value?.current === overlay).forEach(value => { value.current = null; });
  layoutEffects.forEach(callback => callback()); effects.forEach(callback => callback());
  return result;
};
const advance = duration => {
  const until = now + duration;
  while (true) {
    const pending = [...timers].filter(([, timer]) => timer.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
    if (!pending) break;
    const [id, timer] = pending; timers.delete(id); now = timer.due; timer.callback(); render();
  }
  now = until;
};
const mascot = host => host.children[0].children[0];
assert.equal(render(), null);
dock.listeners.get("click")();
let host = render();
assert.equal(dock.style.visibility, "hidden", "Only the reacting character may remain visible");
const firstReaction = mascot(host).props.mood;
assert.ok(["tap_result", "show_summary"].includes(firstReaction));
assert.equal(mascot(host).props.repeat, false);
advance(1000); host = render();
assert.equal(mascot(host).props.mood, firstReaction, "A fixed 650 ms timer must not truncate an unfinished drawing sequence");
mascot(host).props.onSettled(); assert.equal(render(), null);
dock.listeners.get("click")(); host = render();
assert.notEqual(mascot(host).props.mood, firstReaction, "Consecutive clicks must not choose the same response");
mascot(host).props.onSettled(); assert.equal(render(), null);
const emptyBackground = new Element({});
workArea.listeners.get("pointerdown")({ button: 0, clientX: 900, clientY: 120, target: emptyBackground });
host = render();
assert.equal(mascot(host).props.mood, "flight_start");
advance(context.flightStartMs); host = render();
assert.equal(mascot(host).props.mood, "move_right");
assert.equal(host.props.style.transform, "translate3d(862px, 82px, 0)");
assert.equal("left" in host.props.style, false, "Flight must use composited transform rather than left/top layout updates");
const travel = context.mobaoTravelMs({ x: 1000, y: 100 }, { x: 862, y: 82 });
advance(travel); host = render();
assert.equal(mascot(host).props.mood, "flight_stop");
advance(context.flightStopMs); host = render();
assert.equal(mascot(host).props.mood, "tap_result");
mascot(host).props.onSettled(); host = render();
assert.equal(mascot(host).props.mood, "flight_start", "A background interaction must fly home after the full response");
advance(context.flightStartMs + travel + context.flightStopMs);
assert.equal(render(), null);
assert.equal(dock.style.visibility, undefined);
// A manual drop keeps one actor, then low-frequency idle work keeps scheduling.
dock.listeners.get("pointerdown")({ button: 0, pointerId: 1, clientX: 1008, clientY: 108 });
events.get("pointermove")({ pointerId: 1, clientX: 920, clientY: 150 });
host = render();
assert.equal(host.props.style.transform, "translate3d(912px, 142px, 0)");
assert.equal(host.props.style.transitionDuration, "0ms", "Dragging must follow the pointer without a travel transition");
events.get("pointerup")({ pointerId: 1, clientX: 920, clientY: 150, target: overlay });
advance(context.flightStopMs); host = render();
assert.equal(mascot(host).props.mood, "idle");
advance(32001); host = render();
assert.ok(host, "A manually placed actor must keep receiving idle actions");
assert.ok(mascot(host).props.repeat === false || ["flight_start", "move_right", "flight_stop"].includes(mascot(host).props.mood));
assert.ok(timers.size > 0, "Idle behavior must schedule its next opportunity instead of running only once");
// Returning interrupts optional idle work but still completes departure, travel and landing.
host.children[0].props.onKeyDown({ key: "Escape" }); host = render();
assert.ok(host, "Return must not teleport straight into the dock");
assert.equal(mascot(host).props.mood, "flight_start");
advance(context.flightStartMs + 2400 + context.flightStopMs);
assert.equal(render(), null);
assert.equal(dock.style.visibility, undefined);
props.disabled = true; render();
assert.equal(timers.size, 0, "Work, recording or editing restrictions must cancel idle and motion timers");
console.log("Mobao motion checks passed: complete varied reactions, composited flight, direct drag, animated return, recurring idle and input avoidance at 12 fps.");
