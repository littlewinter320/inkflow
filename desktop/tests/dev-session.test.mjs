import assert from "node:assert/strict";
import test from "node:test";
import { resolveConfig } from "vite";

test("writing sessions disable source watching; live development is explicit", async () => {
  const previous = process.env.INKFLOW_LIVE_RELOAD;
  try {
    delete process.env.INKFLOW_LIVE_RELOAD;
    const stable = await resolveConfig({}, "serve");
    assert.equal(stable.server.hmr, false);
    assert.equal(stable.server.watch, null);
    assert.equal(stable.server.strictPort, true);
    process.env.INKFLOW_LIVE_RELOAD = "1";
    const live = await resolveConfig({}, "serve");
    assert.notEqual(live.server.hmr, false);
    assert.notEqual(live.server.watch, null);
  } finally {
    if (previous === undefined) delete process.env.INKFLOW_LIVE_RELOAD;
    else process.env.INKFLOW_LIVE_RELOAD = previous;
  }
});
