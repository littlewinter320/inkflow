import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/Mascot.tsx", import.meta.url), "utf8");
const parsed = ts.createSourceFile("Mascot.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(parsed.parseDiagnostics.length, 0);
const context = {};
for (const node of parsed.statements.filter(ts.isImportDeclaration)) {
  const name = node.importClause?.name?.text;
  if (name) context[name] = node.moduleSpecifier.text;
}
context.alphaAtlases = JSON.parse(readFileSync(new URL("../src/assets/mascot/animations/mobao-alpha-atlases.json", import.meta.url), "utf8"));
context.avatarAtlases = JSON.parse(readFileSync(new URL("../src/assets/mascot/animations/mobao-avatar-atlases.json", import.meta.url), "utf8"));
context.animationManifest = JSON.parse(readFileSync(new URL("../src/assets/mascot/animations/mobao-clean.manifest.json", import.meta.url), "utf8"));
context.atlasUrls = Object.fromEntries(Object.values(context.alphaAtlases).map(clip => [`./assets/mascot/animations/${clip.file}`, clip.file]));
Object.assign(context.atlasUrls, Object.fromEntries(Object.values(context.avatarAtlases).map(clip => [`./assets/mascot/animations/${clip.file}`, clip.file])));
context.avatarPosterUrls = Object.fromEntries(Object.values(context.avatarAtlases).map(clip => [`./assets/mascot/animations/${clip.poster}`, clip.poster]));
context.avatarUrls = Object.fromEntries(["calm", "thinking", "frustrated", "angry", "complete", "rest"].map(mood => [`./assets/mascot/avatars/mobao-${mood}.png`, mood]));
const vars = ["sheetMotion", "clips", "mascotFramesPerSecond", "mediaActions", "warmedPosters", "avatarClips"];
const setup = parsed.statements.filter(node => ts.isForOfStatement(node) || ts.isVariableStatement(node) && node.declarationList.declarations.some(item => vars.includes(item.name.getText(parsed))));
const names = ["spritePosition", "spritePlaybackFrame", "warmPoster", "isTransientMediaError", "Mascot", "MobaoAvatar"];
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
const js = ts.transpileModule([...setup, ...functions].map(node => node.getText(parsed).replace(/^export /, "")).join("\n"), { compilerOptions: { jsx: ts.JsxEmit.React, target: ts.ScriptTarget.ES2022 } }).outputText;

let hooks = [], cursor = 0, dirty = false, layout = [], effects = [], now = 0, nextTimer = 0, intersection;
const timers = new Map(), mediaEvents = new Map(), documentEvents = new Map();
const effect = (queue, callback, deps) => {
  const index = cursor++, previous = hooks[index];
  if (!previous || deps?.some((value, i) => !Object.is(value, previous.deps?.[i]))) queue.push(() => {
    previous?.cleanup?.(); hooks[index] = { deps, cleanup: callback() };
  });
};
const media = { matches: false, addEventListener: (name, callback) => mediaEvents.set(name, callback), removeEventListener: name => mediaEvents.delete(name) };
Object.assign(context, {
  Image: class { set src(value) { this.source = value; this.onload?.(); } },
  IntersectionObserver: class { constructor(callback) { intersection = callback; } observe() { intersection([{ isIntersecting: true }]); } disconnect() {} },
  useState: initial => {
    const index = cursor++; hooks[index] ||= { value: typeof initial === "function" ? initial() : initial };
    return [hooks[index].value, incoming => {
      const value = typeof incoming === "function" ? incoming(hooks[index].value) : incoming;
      if (!Object.is(value, hooks[index].value)) { hooks[index].value = value; dirty = true; }
    }];
  },
  useRef: initial => { const index = cursor++; return hooks[index] ||= { current: initial }; },
  useEffect: (callback, deps) => effect(effects, callback, deps),
  useLayoutEffect: (callback, deps) => effect(layout, callback, deps),
  createPortal: () => { throw new Error("A chat avatar must never create a floating host"); },
  React: { createElement: (type, props, ...children) => {
    if (type === "figure" && props.ref) props.ref.current = {};
    return { type, props: props || {}, children };
  } },
  performance: { now: () => now },
  document: { hidden: false, addEventListener: (name, callback) => documentEvents.set(name, callback), removeEventListener: name => documentEvents.delete(name) },
  window: {
    matchMedia: () => media,
    setTimeout: (callback, delay) => { const id = ++nextTimer; timers.set(id, { callback, due: now + delay }); return id; },
    clearTimeout: id => timers.delete(id),
  },
});
runInNewContext(`${js}\nglobalThis.allClips = clips;`, context);
for (const [name, clip] of Object.entries(context.allClips)) {
  assert.ok(clip.sheet, `${name} must have actual animation drawings`);
  assert.equal(clip.fps, 12, `${name} must use the shared 12 fps`);
  assert.equal(clip.duration, clip.frames / 12);
}
assert.equal(context.allClips.frustrated.frameOffset, 0);
assert.equal(context.allClips.angry.frameOffset, 16);
assert.deepEqual([0, 1, 2, 3, 4, 5, 6].map(tick => context.spritePlaybackFrame(tick, 4, true, true)), [0, 1, 2, 3, 2, 1, 0]);
assert.equal(context.spritePlaybackFrame(3, 4, false, true), 3, "A complete host action must keep its original forward sequence");
const emotionPng = readFileSync(new URL("../src/assets/mascot/animations/mobao-emotions-v1.atlas.png", import.meta.url));
assert.equal(emotionPng.readUInt32BE(16), 2560); assert.equal(emotionPng.readUInt32BE(20), 1280); assert.equal(emotionPng[25], 6);
for (const mood of ["calm", "thinking", "frustrated", "angry", "complete", "rest", "writing", "reviewing", "waiting"]) {
  const avatar = context.MobaoAvatar({ mood, animate: true });
  assert.equal(avatar.type, context.Mascot);
  assert.equal(avatar.props.spriteOnly, true);
  assert.equal(avatar.props.repeat, true);
  assert.equal(context.MobaoAvatar({ mood, animate: false }).type, "img", "Old message avatars must not mount animation hooks");
  assert.equal(context.MobaoAvatar({ mood, animate: false }).props.src, avatar.props.poster, "Static and dynamic avatars must use the same drawing source");
}
const appSource = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const app = ts.createSourceFile("App.tsx", appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const avatars = [];
const visit = node => { if ((ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) && node.tagName.getText(app) === "MobaoAvatar") avatars.push(node); ts.forEachChild(node, visit); };
visit(app);
const active = avatars.map(node => node.attributes.properties.find(prop => prop.name?.getText(app) === "animate")).find(prop => prop?.initializer?.getText(app).includes("messages.at"));
assert.ok(active, "The history avatar must have an explicit latest-message gate");
const activeExpression = active.initializer.expression.getText(app);
for (const [messages, busy, expected] of [
  [[{ id: "old", role: "assistant" }, { id: "new", role: "assistant" }], false, ["new"]],
  [[{ id: "old", role: "assistant" }, { id: "user", role: "user" }], false, []],
  [[{ id: "old", role: "assistant" }], true, []],
]) {
  const animated = messages.filter(message => message.role !== "user" && runInNewContext(activeExpression, { messages, message, busy }));
  assert.deepEqual(animated.map(message => message.id), expected);
}
assert.ok(avatars.some(node => node.attributes.properties.some(prop => prop.name?.getText(app) === "animate" && !prop.initializer)), "The current run card must take over the single animated chat avatar");
let props = { mood: "thinking", spriteOnly: true, repeat: true, className: "mobao-avatar" }, tree;
const render = () => {
  let passes = 0;
  do {
    assert.ok(++passes < 10, "The renderer must settle without a state loop");
    dirty = false; cursor = 0; layout = []; effects = [];
    tree = context.Mascot(props); layout.forEach(callback => callback()); effects.forEach(callback => callback());
  } while (dirty);
  return tree;
};
const advance = duration => {
  const until = now + duration;
  while (true) {
    const entry = [...timers].filter(([, timer]) => timer.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
    if (!entry) break;
    const [id, timer] = entry; timers.delete(id); now = timer.due; timer.callback(); if (dirty) render();
  }
  now = until; if (dirty) render();
};
const descendants = node => !node || typeof node !== "object" ? [] : [node, ...(node.children || []).flatMap(child => Array.isArray(child) ? child.flatMap(descendants) : descendants(child))];
const sprite = () => descendants(tree).find(node => node.props.role === "img");
render(); advance(0);
assert.equal(descendants(tree).some(node => node.type === "video"), false);
const first = sprite().props.style.backgroundPosition;
advance(1000);
assert.notEqual(sprite().props.style.backgroundPosition, first);
const pausedAt = sprite().props.style.backgroundPosition;
intersection([{ isIntersecting: false }]); render();
assert.equal(timers.size, 0, "An offscreen avatar must release its frame timer");
advance(1000); assert.equal(sprite().props.style.backgroundPosition, pausedAt);
intersection([{ isIntersecting: true }]); render(); advance(100);
assert.notEqual(sprite().props.style.backgroundPosition, pausedAt);
context.document.hidden = true; documentEvents.get("visibilitychange")(); render();
assert.equal(timers.size, 0, "A hidden page must stop animation");
context.document.hidden = false; documentEvents.get("visibilitychange")(); render();
props = { ...props, mood: "writing" }; render(); advance(0);
assert.equal(tree.props["data-mood"], "idle", "A state change must pass through one neutral pose rather than blend two faces");
assert.equal(descendants(tree).some(node => node.props.className === "mobao-state-transition"), false);
advance(260);
assert.equal(tree.props["data-mood"], "writing");
media.matches = true; mediaEvents.get("change")(); render();
assert.equal(timers.size, 0, "Reduced motion must stop sprite timers");
assert.equal(descendants(tree).some(node => node.type === "img"), true);
media.matches = false; mediaEvents.get("change")(); render(); advance(7000);
let finished = 0;
props = { ...props, repeat: false, onSettled: () => { finished += 1; } }; render();
assert.equal(finished, 0, "Stopping an existing loop after one cycle must not treat its accumulated ticks as an already-finished action");
advance(7000);
assert.equal(finished, 1);
console.log("Mobao avatar checks passed: all states use 12 fps, dynamic emotion frames, no portals/videos, bounded transition and visibility/reduced-motion pause.");
