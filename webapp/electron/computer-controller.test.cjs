"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createComputerController } = require("./computer-controller.cjs");

function fixture() {
  const calls = [];
  const decisions = [];
  let pid = 90001;
  let failSnapshot = false;
  const native = { close() {}, async dispatch(args) {
    calls.push(args);
    if (args.operation === "apps") return { apps: [{ app_id: "fixture.app", name: "Fixture", pid }] };
    if (args.operation === "snapshot" && failSnapshot) throw new Error("window closed");
    return args.operation === "snapshot" ? { snapshot_id: "fresh", app_id: args.app_id } : { ok: true };
  } };
  const controller = createComputerController({ createNative: () => native, requestApproval: () => new Promise(resolve => decisions.push(resolve)) });
  controller.setSession("session");
  const dispatch = (operation = "snapshot", session_id = "session") => controller.dispatch({ session_id, arguments: { operation, app_id: "fixture.app", snapshot_id: "old", ref: "r1" } });
  return { controller, dispatch, calls, decisions, restart: () => pid++, failVerification: () => { failSnapshot = true; } };
}

test("computer access requires a session and human app approval", async () => {
  const f = fixture();
  f.controller.setSession("");
  await assert.rejects(f.dispatch("snapshot", ""), /active conversation/);
  assert.equal(f.calls.length, 0);
  f.controller.setSession("session");
  assert.equal((await f.dispatch()).approval, "pending");
  assert.deepEqual(f.calls.map(call => call.operation), ["apps"]);
  f.decisions[0](true);
  await new Promise(setImmediate);
  assert.equal((await f.dispatch()).snapshot_id, "fresh");
  const result = await f.dispatch("click");
  assert.equal(result.state.snapshot_id, "fresh");
  assert.deepEqual(f.calls.slice(-3).map(call => call.operation), ["apps", "click", "snapshot"]);
});

test("another conversation steals computer control", async () => {
  const f = fixture();
  assert.equal((await f.dispatch()).approval, "pending");
  f.decisions[0](true);
  await new Promise(setImmediate);
  assert.equal((await f.dispatch()).snapshot_id, "fresh");
  const stolen = await f.dispatch("snapshot", "other");
  assert.equal(f.controller.getSession(), "other");
  assert.equal(stolen.approval, "pending");
});

test("releaseSession only clears the owning conversation", () => {
  const f = fixture();
  f.controller.setSession("other");
  f.controller.releaseSession("session");
  assert.equal(f.controller.getSession(), "other");
  f.controller.releaseSession("other");
  assert.equal(f.controller.getSession(), "");
});

test("session switches revoke access and ignore late permission decisions", async () => {
  const f = fixture();
  await f.dispatch();
  f.controller.setSession("other");
  f.decisions[0](true);
  await new Promise(setImmediate);
  f.controller.setSession("session");
  assert.equal((await f.dispatch()).approval, "pending");
  assert.equal(f.decisions.length, 2);
});

test("denial does not nag; revoke and app restart invalidate grants", async () => {
  const f = fixture();
  await f.dispatch();
  f.decisions[0](false);
  await new Promise(setImmediate);
  assert.equal((await f.dispatch()).approval, "denied");
  assert.equal(f.decisions.length, 1);
  f.controller.revoke();
  await f.dispatch();
  f.decisions[1](true);
  await new Promise(setImmediate);
  f.restart();
  assert.equal((await f.dispatch()).approval, "pending");
});

test("verification failure reports sent input instead of inviting blind retry", async () => {
  const f = fixture();
  await f.dispatch();
  f.decisions[0](true);
  await new Promise(setImmediate);
  f.failVerification();
  const result = await f.dispatch("click");
  assert.equal(result.action.ok, true);
  assert.match(result.verification_error, /window closed/);
});
