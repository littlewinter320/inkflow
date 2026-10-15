import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/contextComposition.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
const exports = {};
runInNewContext(compiled.outputText, { exports });
const { groupContextMaterials } = exports;
const item = (key, tokens, title = key) => ({ key, title, estimated_tokens: tokens, source_count: 1, hard: false });
const check = values => {
  const result = groupContextMaterials(values);
  assert.ok(result.groups.length <= 5);
  assert.equal(result.groups.reduce((sum, group) => sum + group.estimated_tokens, 0), result.total);
  assert.equal(result.groups.flatMap(group => group.members).length, result.items.length);
  assert.equal(new Set(result.groups.flatMap(group => group.members.map(member => member.key))).size, result.items.length);
  return result;
};
const grouped = check([50, 10, 9, 9, 8, 8, 6].map((tokens, index) => item(String(index), tokens)));
assert.equal(grouped.total, 100);
assert.equal(grouped.groups.find(group => group.title === "其他").estimated_tokens, 40);
assert.ok(grouped.groups.some(group => group.estimated_tokens === 10 && group.title !== "其他"));
const exactFive = check(Array.from({ length: 5 }, (_, index) => item(String(index), 20)));
assert.equal(exactFive.groups.length, 5);
assert.ok(exactFive.groups.every(group => group.title !== "其他"));
const sixLarge = check([20, 18, 17, 16, 15, 14].map((tokens, index) => item(String(index), tokens)));
assert.equal(sixLarge.groups.length, 5);
assert.equal(sixLarge.groups.find(group => group.title === "其他").estimated_tokens, 29);
const fiveAndSmall = check([25, 20, 20, 15, 15, 5].map((tokens, index) => item(String(index), tokens)));
assert.equal(fiveAndSmall.groups.find(group => group.title === "其他").members.length, 2);
const allSmall = check(Array.from({ length: 11 }, (_, index) => item(String(index), 1)));
assert.equal(allSmall.groups.length, 1);
assert.equal(allSmall.groups[0].estimated_tokens / allSmall.total, 1);
assert.equal(check([item("zero", 0), item("negative", -1), item("invalid", NaN), item("infinite", Infinity)]).groups.length, 0);
assert.equal(check([item("writer-settings", 100, "本书设定")]).groups[0].color,
  check([item("editor-settings", 100, "本书设定")]).groups[0].color);
console.log("Context composition boundaries passed; no model or UI requests.");
