import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const parsed = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
assert.equal(parsed.parseDiagnostics.length, 0);
const names = ["normalizeSuggestionShortcut", "suggestionShortcutFromKey", "suggestionShortcutLabel", "matchesSuggestionShortcut", "canFillSuggestionShortcut", "isChatSubmitKey", "prepareSuggestionFill", "loadUiPreferences"];
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text));
assert.equal(functions.length, names.length);
const preferences = parsed.statements.filter(ts.isVariableStatement).flatMap(node => [...node.declarationList.declarations])
  .filter(node => ["uiPreferencesStorageKey", "defaultUiPreferences"].includes(node.name.getText(parsed)));
let stored = null;
const context = { localStorage: { getItem: () => stored } };
const evaluate = code => runInNewContext(ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
evaluate([...preferences.map(node => `const ${node.getText(parsed)};`), ...functions.map(node => node.getText(parsed))].join("\n"));
const event = (patch = {}) => ({ key: "Enter", code: "Enter", altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, repeat: false, isComposing: false, keyCode: 13, ...patch });
assert.equal(context.loadUiPreferences().suggestionShortcut, "Alt+Enter");
assert.equal(context.loadUiPreferences().suggestionShortcutEnabled, true);
assert.equal(context.normalizeSuggestionShortcut("Shift+Ctrl+KeyK"), "Ctrl+Shift+KeyK");
assert.equal(context.suggestionShortcutLabel("Ctrl+Shift+KeyK"), "Ctrl + Shift + K");
for (const shortcut of ["Enter", "Shift+Enter", "Ctrl+Enter", "Meta+Enter", "Alt+Shift+Enter", "Escape", "Ctrl+KeyA", "Ctrl+KeyC", "Ctrl+KeyV", "Ctrl+KeyX", "Ctrl+KeyZ", "Ctrl+Shift+KeyZ", "Meta+KeyS", "Alt+F4", "Alt+Tab", "Alt+Space", "Meta+KeyL", "Meta+KeyK", "Meta+KeyM", "Ctrl+Ctrl+KeyK"]) {
  assert.equal(context.normalizeSuggestionShortcut(shortcut), "", shortcut);
}
assert.equal(context.matchesSuggestionShortcut(event(), "Alt+Enter"), true);
for (const patch of [{ repeat: true }, { isComposing: true }, { keyCode: 229 }, { getModifierState: key => key === "AltGraph" }, { altKey: false }, { ctrlKey: true }, { key: "Dead" }]) {
  assert.equal(context.matchesSuggestionShortcut(event(patch), "Alt+Enter"), false);
}
assert.equal(context.canFillSuggestionShortcut(event(), "Alt+Enter", true, true, false), true);
for (const state of [[false, true, false], [true, false, false], [true, true, true]]) assert.equal(context.canFillSuggestionShortcut(event(), "Alt+Enter", ...state), false);
assert.equal(context.isChatSubmitKey(event({ altKey: false })), true);
for (const patch of [{}, { altKey: false, shiftKey: true }, { altKey: false, ctrlKey: true }, { altKey: false, isComposing: true }, { altKey: false, keyCode: 229 }, { altKey: false, repeat: true }]) assert.equal(context.isChatSubmitKey(event(patch)), false);
for (const draft of ["还没发送的原话", "   "]) {
  const kept = context.prepareSuggestionFill(draft, "建议原话");
  assert.equal(kept.value, draft);
  assert.equal(kept.filled, false);
}
assert.equal(context.prepareSuggestionFill("", undefined).filled, false);
assert.equal(context.prepareSuggestionFill("", "  只填入的建议  ").value, "只填入的建议");
stored = JSON.stringify({ suggestionShortcut: "Ctrl+KeyC", suggestionShortcutEnabled: true });
assert.equal(context.loadUiPreferences().suggestionShortcutEnabled, false);
stored = JSON.stringify({ suggestionShortcut: "", suggestionShortcutEnabled: false });
assert.equal(context.loadUiPreferences().suggestionShortcut, "");

// Execute the actual fill callback: neither a chip nor the key path can send.
let fillCallback, keyEffect, storageEffect;
function visit(node) {
  if (ts.isVariableDeclaration(node) && node.name.getText(parsed) === "fillSuggestedPrompt") fillCallback = node.initializer.arguments[0];
  if (ts.isCallExpression(node) && node.expression.getText(parsed) === "useEffect") {
    if (node.arguments[0]?.getText(parsed).includes('window.addEventListener("keydown", fillFromShortcut')) keyEffect = node.arguments[0];
    if (node.arguments[0]?.getText(parsed).includes('window.addEventListener("storage", sync)')) storageEffect = node.arguments[0];
  }
  ts.forEachChild(node, visit);
}
visit(parsed);
assert.ok(fillCallback && keyEffect && storageEffect);
class ChatElement {
  value = "尚未发送的内容";
  focus() {}
  getClientRects() { return [{}]; }
  closest() { return { contains: target => target === this }; }
}
const input = new ChatElement();
const writes = [], handlers = new Map();
let modal = false;
Object.assign(context, { Element: ChatElement, chatInputRef: { current: input }, chatInput: "", voiceStarting: false, voiceRecording: false, voiceTranscribing: false,
  setSuggestionFillStatus: value => writes.push(["feedback", value]), setChatInput: value => { input.value = value; writes.push(["input", value]); }, setPromptOptimization() {}, setPromptUndo() {},
  request() { throw Error("Filling suggestions must not call the engine"); }, sendChat() { throw Error("Filling suggestions must not send"); },
  showSettings: false, uiPreferences: { suggestedPromptsEnabled: true, suggestionShortcutEnabled: true, suggestionShortcut: "Alt+Enter" },
  visibleSuggestedPrompts: [{ prompt: "此前单次授权的建议也只填入，等用户检查。" }],
  window: { document: { querySelector: () => modal ? {} : null }, addEventListener: (type, handler) => handlers.set(type, handler), removeEventListener: type => handlers.delete(type) },
  setUiPreferences: update => { context.uiPreferences = update(context.uiPreferences); } });
evaluate(`globalThis.fillSuggestedPrompt = ${fillCallback.getText(parsed)}; globalThis.installKey = ${keyEffect.getText(parsed)}; globalThis.installStorage = ${storageEffect.getText(parsed)};`);
const cleanup = context.installKey();
let consumed = 0;
const press = patch => handlers.get("keydown")(event({ target: input, preventDefault: () => consumed++, stopPropagation() {}, ...patch }));
press();
assert.equal(input.value, "尚未发送的内容");
assert.equal(writes.filter(([type]) => type === "input").length, 0);
input.value = "";
press();
assert.equal(input.value, context.visibleSuggestedPrompts[0].prompt);
assert.equal(consumed, 2);
input.value = "";
modal = true; press(); modal = false;
context.showSettings = true; press(); context.showSettings = false;
press({ repeat: true }); press({ isComposing: true }); press({ target: new ChatElement() });
assert.equal(input.value, "");
assert.equal(consumed, 2, "Modal recording, IME, repeats and other editor surfaces retain their keys");
cleanup();
assert.equal(handlers.has("keydown"), false);
const cleanupStorage = context.installStorage();
stored = JSON.stringify({ suggestionShortcut: "Ctrl+Shift+KeyK", suggestionShortcutEnabled: true });
handlers.get("storage")({ key: "inkflow.ui-preferences.v1" });
assert.equal(context.uiPreferences.suggestionShortcut, "Ctrl+Shift+KeyK");
cleanupStorage();
assert.ok(source.includes("onClick={() => fillSuggestedPrompt(item.prompt)}"));
console.log("Suggestion shortcut checks passed: safe recording, shared settings, preserved drafts, fill-only keys and unchanged send/IME behavior.");
