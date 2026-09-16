const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { createBrowserBridge } = require("./browser-bridge.cjs");
const { createBrowserController } = require("./browser-controller.cjs");

test("bridge authenticates, validates, bounds input, and expires unfinished work", async () => {
  let calls = 0, signal;
  const bridge = createBrowserBridge({ timeoutMs: 100, dispatch: async (_, s) => {
    calls++; signal = s; return new Promise(() => {});
  } });
  const port = await bridge.listen();
  const request = (body, headers = {}) => fetch("http://127.0.0.1:" + port + "/browser", {
    method: "POST", headers: { Authorization: "Bearer " + bridge.token, ...headers }, body: JSON.stringify(body),
  });
  try {
    const payload = { session_id: "a", action: "snapshot", arguments: {} };
    assert.equal((await request(payload, { Authorization: "bad" })).status, 403);
    assert.equal((await request(payload, { Origin: "https://untrusted.example" })).status, 403);
    assert.equal((await request({ ...payload, action: "eval" })).status, 400);
    assert.equal((await request({ ...payload, arguments: [] })).status, 400);
    assert.equal((await request({ ...payload, padding: "x".repeat(70000) })).status, 413);
    assert.equal(calls, 0);
    assert.equal((await request(payload)).status, 408);
    assert.equal(calls, 1);
    assert.equal(signal.aborted, true);
  } finally { bridge.close(); }
});

class Guest extends EventEmitter {
  destroyed = false;
  isDestroyed() { return this.destroyed; }
  getURL() { return "http://localhost/fixture"; }
  getTitle() { return "Fixture"; }
  isLoadingMainFrame() { return false; }
  async executeJavaScriptInIsolatedWorld() { return { rows: ["@test button Go"], text: "Fixture" }; }
}

test("controller binds tabs to active session, acknowledges selection, and revokes refs", async () => {
  const one = new Guest(), two = new Guest();
  const tabs = [{ tabId: "one", webContentsId: 1 }, { tabId: "two", webContentsId: 2 }];
  const controller = createBrowserController({
    resolveGuest: id => [one, two][id - 1],
    activateTab: ({ sessionId, tabId }) => controller.setContext({ sessionId, activeTabId: tabId, tabs }),
    focus: () => { throw new Error("stale click reached native input"); },
  });
  const call = (action, args = {}, session = "a", signal = new AbortController().signal) => controller.dispatch({ session_id: session, action, arguments: args }, signal);
  controller.setContext({ sessionId: "a", activeTabId: "one", tabs });
  assert.equal((await call("tabs")).length, 2);
  await assert.rejects(call("snapshot", {}, "b"), /active conversation/);
  await call("snapshot");
  one.emit("did-navigate-in-page");
  await assert.rejects(call("click", { ref: "@test" }), /Stale/);
  await call("snapshot");
  await call("tab_activate", { tab_id: "two" });
  assert.equal((await call("tabs"))[1].active, true);
  await assert.rejects(call("click", { ref: "@test" }), /Stale/);
  two.destroyed = true;
  two.emit("destroyed");
  await assert.rejects(call("snapshot"), /selected tab/);
  controller.clear();
  await assert.rejects(call("tabs"), /active conversation/);
});

test("late page work cannot act after cancellation or session switch", async () => {
  const guest = new Guest();
  let complete;
  guest.executeJavaScriptInIsolatedWorld = () => new Promise(resolve => { complete = resolve; });
  const controller = createBrowserController({ resolveGuest: () => guest });
  const context = { sessionId: "a", activeTabId: "one", tabs: [{ tabId: "one", webContentsId: 1 }] };
  controller.setContext(context);
  const abort = new AbortController();
  const work = controller.dispatch({ session_id: "a", action: "snapshot", arguments: {} }, abort.signal);
  abort.abort();
  await assert.rejects(work, /cancelled/);
  complete({ rows: ["@old button Go"], text: "Old" });
  controller.setContext({ ...context, sessionId: "b" });
  await assert.rejects(controller.dispatch({ session_id: "b", action: "click", arguments: { ref: "@old" } }, new AbortController().signal), /Stale/);
  controller.clear();
});
