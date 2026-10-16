import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/settingsPatch.ts", import.meta.url), "utf8");
const api = {};
runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { exports: api });
if (process.argv.includes("--voice-migration-only")) {
  const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
  const handler = app.slice(app.indexOf("  const moveVoiceStorage = async () => {"), app.indexOf("  const installMoss = async () => {"));
  assert.ok(handler.includes("voice.storage.move"));
  for (const dirty of [true, false]) {
    const baseline = { voice_data_dir: "D:/old", voice_speed: 1, voice_name: "old" };
    const saved = { ...baseline, voice_data_dir: "D:/new/InkFlow-voice", voice_name: "updated-elsewhere" };
    let current = { ...baseline, voice_speed: dirty ? 1.1 : 1 };
    const baselineRef = { current: baseline };
    const dirtyRef = { current: dirty };
    const context = {
      ...api, initialVoiceSettingsRef: baselineRef, voiceFormDirtyRef: dirtyRef,
      voiceSettings: baseline, voiceForm: current,
      window: { inkflow: {
        chooseFolder: async () => "D:/new", confirm: async () => true,
        request: async () => {
          if (dirty) current = { ...current, voice_speed: 1.2 };
          return { settings: saved, status: {}, message: "moved" };
        },
      } },
      setVoiceForm: update => { current = typeof update === "function" ? update(current) : update; },
      onVoiceSaved: settings => assert.equal(settings, saved),
      setVoiceMoving() {}, setError(message) { assert.fail(message); }, setResult() {},
      errorMessage: String,
    };
    // Clearing the error is expected; a caught migration error must fail this check.
    context.setError = message => { if (message) assert.fail(message); };
    await runInNewContext(ts.transpileModule(`${handler}\nmoveVoiceStorage();`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText, context);
    assert.equal(current.voice_data_dir, saved.voice_data_dir);
    assert.equal(current.voice_name, saved.voice_name);
    assert.equal(current.voice_speed, dirty ? 1.2 : 1);
    assert.equal(baselineRef.current, saved);
    assert.equal(dirtyRef.current, dirty);
    assert.equal(JSON.stringify(api.settingsDiff(current, baselineRef.current)), JSON.stringify(dirty ? { voice_speed: 1.2 } : {}));
  }
  console.log("Voice migration preserves pending/in-flight edits and refreshes the saved baseline (2 cases).");
  process.exit(0);
}
const baseline = { model: "previous", api_key: "", agent_generation: { writer: { temperature: 1, top_p: .9 } }, role_models: { writer: "custom-writer" } };
const edited = { ...baseline, agent_generation: { writer: { temperature: .8, top_p: .9 } } };
const patch = api.settingsDiff(edited, baseline);
assert.equal(JSON.stringify(patch), JSON.stringify({ agent_generation: { writer: { temperature: .8 } } }));
const concurrentlySaved = { ...baseline, model: "mimo-v2.6-flash", agent_generation: { writer: { temperature: 1, top_p: .95 } } };
const reconciled = api.mergeSettings(concurrentlySaved, patch);
assert.equal(reconciled.model, "mimo-v2.6-flash");
assert.equal(reconciled.agent_generation.writer.top_p, .95);
assert.equal(reconciled.agent_generation.writer.temperature, .8);
assert.equal(api.settingsDiff({ ...baseline, role_models: { writer: "" } }, baseline).role_models.writer, "");
assert.equal(Object.keys(api.settingsDiff(baseline, baseline)).length, 0);
const editedDuringSave = { ...edited, api_key: "tp-newly-typed-local-value" };
assert.equal(api.mergeSettings(concurrentlySaved, api.settingsDiff(editedDuringSave, edited)).api_key, "tp-newly-typed-local-value");
console.log("Settings change-only submission and concurrent-edit reconciliation passed.");
