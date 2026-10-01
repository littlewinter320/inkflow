// Run with: node tests/conversation-context.test.mjs (no Electron or model calls).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const listeners = new Map();
const calls = [];
let bridge;
const electron = {
  contextBridge: { exposeInMainWorld: (_name, value) => { bridge = value; } },
  ipcRenderer: {
    invoke: async (...args) => { calls.push(args); },
    on: (name, callback) => listeners.set(name, callback),
    removeListener: name => listeners.delete(name),
  },
};
const source = readFileSync(new URL("../electron/preload.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
vm.runInNewContext(compiled, { require: () => electron, exports: {}, URLSearchParams, window: { location: { search: "?conversation_id=chat-one" } } });
await bridge.request("conversation.send", { message: "继续", conversation_id: "other" });
assert.equal(calls[0][2].conversation_id, "chat-one", "A renderer must retain its own conversation identity");
const received = [];
const remove = bridge.onEvent(event => received.push(event));
listeners.get("engine:event")(null, { conversation_id: "chat-other" });
listeners.get("engine:event")(null, { conversation_id: "chat-one" });
listeners.get("engine:event")(null, { type: "voice.global" });
assert.equal(received.length, 2, "Other conversations must not change this window's progress");
assert.equal(received[0].conversation_id, "chat-one");
remove();
assert.equal(listeners.has("engine:event"), false, "Closing a consumer must remove its listener");
