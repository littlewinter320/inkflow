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
