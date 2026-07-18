"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  shouldUnlinkBackendMarker,
  classifyBackendExit,
  shouldRespawnAfterBackendExit,
  isFreshIntentionalRestartSignal,
  shouldCountTowardCrashLoop,
  windowsBackendShutdownPlan,
  childProcessStillAlive,
  shutdownOwnedBackendTree,
  WINDOWS_SHUTDOWN_GRACE_MS,
} = require("./backend-lifecycle.cjs");

test("shouldUnlinkBackendMarker: only when owned", () => {
  assert.equal(shouldUnlinkBackendMarker(true), true);
  assert.equal(shouldUnlinkBackendMarker(false), false);
});

test("marker lifecycle: captured ownership unlinks after global clear", () => {
  // Mirrors main.cjs exit handler: capture owned, clear global, unlink via snapshot.
  let globalOwned = true;
  const ownedSnapshot = globalOwned;
  globalOwned = false;
  assert.equal(shouldUnlinkBackendMarker(globalOwned), false);
  assert.equal(shouldUnlinkBackendMarker(ownedSnapshot), true);
});

test("classifyBackendExit: unexpected vs intentional restart vs ignore", () => {
  assert.equal(
    classifyBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: false,
      restarting: false,
      intentionalRestart: false,
    }),
    "unexpected",
  );
  assert.equal(
    classifyBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: false,
      restarting: false,
      intentionalRestart: true,
    }),
    "intentional_restart",
  );
  assert.equal(
    classifyBackendExit({
      backendRef: {},
      backendOwned: false,
      quitting: false,
      restarting: false,
      intentionalRestart: false,
    }),
    "ignore",
  );
  assert.equal(
    classifyBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: false,
      restarting: true,
      intentionalRestart: false,
    }),
    "ignore",
  );
});

test("shouldRespawnAfterBackendExit: owned unexpected exit", () => {
  assert.equal(
    shouldRespawnAfterBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: false,
      restarting: false,
    }),
    true,
  );
});

test("shouldRespawnAfterBackendExit: intentional /api/restart still respawns", () => {
  assert.equal(
    shouldRespawnAfterBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: false,
      restarting: false,
      intentionalRestart: true,
    }),
    true,
  );
  assert.equal(shouldCountTowardCrashLoop("intentional_restart"), false);
  assert.equal(shouldCountTowardCrashLoop("unexpected"), true);
});

test("shouldRespawnAfterBackendExit: skip when adopted or intentional Electron teardown", () => {
  assert.equal(
    shouldRespawnAfterBackendExit({
      backendRef: {},
      backendOwned: false,
      quitting: false,
      restarting: false,
    }),
    false,
  );
  assert.equal(
    shouldRespawnAfterBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: true,
      restarting: false,
    }),
    false,
  );
  assert.equal(
    shouldRespawnAfterBackendExit({
      backendRef: {},
      backendOwned: true,
      quitting: false,
      restarting: true,
    }),
    false,
  );
  assert.equal(
    shouldRespawnAfterBackendExit({
      backendRef: null,
      backendOwned: true,
      quitting: false,
      restarting: false,
    }),
    false,
  );
});

test("isFreshIntentionalRestartSignal: accepts fresh, rejects stale/corrupt", () => {
  const now = 1_000_000;
  assert.equal(
    isFreshIntentionalRestartSignal(JSON.stringify({ at: now - 1000, pid: 1 }), { nowMs: now }),
    true,
  );
  assert.equal(
    isFreshIntentionalRestartSignal(JSON.stringify({ at: now - 60_000, pid: 1 }), { nowMs: now }),
    false,
  );
  assert.equal(isFreshIntentionalRestartSignal("not-json", { nowMs: now }), false);
  assert.equal(isFreshIntentionalRestartSignal(null, { nowMs: now }), false);
});

test("windowsBackendShutdownPlan: soft then force after grace", () => {
  const plan = windowsBackendShutdownPlan();
  assert.equal(plan.graceMs, WINDOWS_SHUTDOWN_GRACE_MS);
  assert.deepEqual(plan.softArgs(4242), ["/pid", "4242", "/T"]);
  assert.deepEqual(plan.forceArgs(4242), ["/pid", "4242", "/T", "/F"]);
});

test("shutdownOwnedBackendTree windows: awaits grace before force; no replacement race", async () => {
  const calls = [];
  let clock = 0;
  const child = { pid: 99, exitCode: null, killed: false, kill() { this.killed = true; } };
  const spawnSync = (cmd, args) => {
    calls.push([cmd, ...args]);
  };
  const sleep = async (ms) => {
    calls.push(["sleep", ms]);
    clock += ms;
    // Still alive through the grace window so force must run after awaits.
  };

  const result = await shutdownOwnedBackendTree({
    platform: "win32",
    child,
    spawnSync,
    sleep,
    graceMs: 100,
    now: () => clock,
  });

  assert.equal(result.forced, true);
  assert.equal(result.softSignaled, true);
  assert.deepEqual(calls[0], ["taskkill", "/pid", "99", "/T"]);
  assert.ok(calls.some((c) => c[0] === "sleep"), "must await grace before force");
  const forceIdx = calls.findIndex(
    (c) => c[0] === "taskkill" && c.includes("/F"),
  );
  const firstSleepIdx = calls.findIndex((c) => c[0] === "sleep");
  assert.ok(forceIdx > firstSleepIdx, "force must come after awaited grace sleeps");
  assert.deepEqual(calls[forceIdx], ["taskkill", "/pid", "99", "/T", "/F"]);
});

test("shutdownOwnedBackendTree windows: skips force when child exits during grace", async () => {
  const calls = [];
  let clock = 0;
  const child = { pid: 77, exitCode: null, killed: false, kill() {} };
  const spawnSync = (cmd, args) => { calls.push([cmd, ...args]); };
  const sleep = async (ms) => {
    calls.push(["sleep", ms]);
    clock += ms;
    child.exitCode = 0; // drained during grace
  };

  const result = await shutdownOwnedBackendTree({
    platform: "win32",
    child,
    spawnSync,
    sleep,
    graceMs: 100,
    now: () => clock,
  });

  assert.equal(result.forced, false);
  assert.ok(!calls.some((c) => c.includes("/F")), "must not force-kill a drained tree");
  assert.equal(childProcessStillAlive(child), false);
});
