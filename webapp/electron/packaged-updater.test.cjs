"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  compareVersions,
  shellBehindCheckout,
  packagedUpdaterEnabled,
  mergeUpdateAvailability,
  shouldRelaunchAfterSourceUpdate,
  registerPackagedUpdater,
} = require("./packaged-updater.cjs");

test("compareVersions: basic ordering", () => {
  assert.equal(compareVersions("0.9.161", "0.9.154"), 1);
  assert.equal(compareVersions("0.9.154", "0.9.161"), -1);
  assert.equal(compareVersions("v0.9.161", "0.9.161"), 0);
});

test("shellBehindCheckout: detects frozen shell older than checkout", () => {
  assert.equal(shellBehindCheckout({ shellVersion: "0.9.154", checkoutVersion: "0.9.161" }), true);
  assert.equal(shellBehindCheckout({ shellVersion: "0.9.161", checkoutVersion: "0.9.161" }), false);
  assert.equal(shellBehindCheckout({ shellVersion: "", checkoutVersion: "0.9.161" }), false);
});

test("packagedUpdaterEnabled: only packaged + not explicitly disabled", () => {
  assert.equal(packagedUpdaterEnabled({ isPackaged: false, env: {} }), false);
  assert.equal(packagedUpdaterEnabled({ isPackaged: true, env: {} }), true);
  assert.equal(packagedUpdaterEnabled({ isPackaged: true, env: { MARIONETTE_PACKAGED_UPDATER: "0" } }), false);
});

test("mergeUpdateAvailability: shell skew alone surfaces an installer update", () => {
  const merged = mergeUpdateAvailability({
    gitResult: { available: false, behind: 0, latest: "0.9.161" },
    packagedResult: { available: false },
    isPackaged: true,
    shellVersion: "0.9.154",
    checkoutVersion: "0.9.161",
  });
  assert.equal(merged.available, true);
  assert.equal(merged.installerUpdateRequired, true);
  assert.equal(merged.source, "shell-skew");
  assert.equal(merged.latest, "0.9.161");
});

test("mergeUpdateAvailability: packaged download wins source label", () => {
  const merged = mergeUpdateAvailability({
    gitResult: { available: true, behind: 2, latest: "0.9.160" },
    packagedResult: { available: true, downloaded: true, latest: "0.9.161" },
    isPackaged: true,
    shellVersion: "0.9.154",
    checkoutVersion: "0.9.161",
  });
  assert.equal(merged.available, true);
  assert.equal(merged.downloaded, true);
  assert.equal(merged.source, "packaged-downloaded");
  assert.equal(merged.latest, "0.9.161");
});

test("shouldRelaunchAfterSourceUpdate: packaged installer requirement blocks relaunch", () => {
  assert.equal(
    shouldRelaunchAfterSourceUpdate({
      ok: true,
      isPackaged: true,
      installerUpdateRequired: true,
      packagedInstallPending: false,
    }),
    false,
  );
  assert.equal(
    shouldRelaunchAfterSourceUpdate({
      ok: true,
      isPackaged: false,
      installerUpdateRequired: false,
      packagedInstallPending: false,
    }),
    true,
  );
  assert.equal(
    shouldRelaunchAfterSourceUpdate({
      ok: true,
      isPackaged: true,
      installerUpdateRequired: false,
      packagedInstallPending: true,
    }),
    false,
  );
});

test("registerPackagedUpdater: disabled outside packaged builds", async () => {
  const handle = registerPackagedUpdater(
    { handle() {} },
    { isPackaged: false },
    { createAutoUpdater: () => { throw new Error("should not load"); } },
  );
  assert.equal(handle.enabled, false);
  const res = await handle.check();
  assert.equal(res.disabled, true);
});

test("registerPackagedUpdater: progress + install path via injected autoUpdater", async () => {
  const events = {};
  const sent = [];
  const fakeUpdater = {
    autoDownload: true,
    autoInstallOnAppQuit: false,
    allowDowngrade: true,
    allowPrerelease: true,
    disableWebInstaller: false,
    on(name, cb) { events[name] = cb; },
    async checkForUpdates() {
      events["update-available"]({ version: "0.9.162" });
      return { updateInfo: { version: "0.9.162" } };
    },
    async downloadUpdate() {
      events["download-progress"]({ percent: 50 });
      events["update-downloaded"]({ version: "0.9.162" });
    },
    quitAndInstall() { fakeUpdater._quitCalled = true; },
  };
  const handle = registerPackagedUpdater(
    { handle() {} },
    { isPackaged: true },
    {
      createAutoUpdater: () => fakeUpdater,
      broadcast: (channel, payload) => sent.push({ channel, payload }),
      log: () => {},
    },
  );
  assert.equal(handle.enabled, true);
  assert.equal(fakeUpdater.autoDownload, false);
  assert.equal(fakeUpdater.disableWebInstaller, true);

  const checked = await handle.check();
  assert.equal(checked.available, true);
  assert.equal(checked.latest, "0.9.162");

  const installed = await handle.downloadAndInstall();
  assert.equal(installed.ok, true);
  assert.equal(installed.packagedInstallPending, true);
  assert.ok(sent.some((s) => s.channel === "updates:progress" && s.payload.stage === "download"));
  // quitAndInstall is deferred; wait for the timeout
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(fakeUpdater._quitCalled, true);
});

test("registerPackagedUpdater: emits idle when no update is available", async () => {
  const events = {};
  const sent = [];
  const fakeUpdater = {
    autoDownload: true,
    autoInstallOnAppQuit: false,
    allowDowngrade: true,
    allowPrerelease: true,
    disableWebInstaller: false,
    on(name, cb) { events[name] = cb; },
    async checkForUpdates() {
      events["checking-for-update"]();
      events["update-not-available"]({ version: "0.9.162" });
      return { updateInfo: { version: "0.9.162" } };
    },
    async downloadUpdate() {},
    quitAndInstall() {},
  };
  const handle = registerPackagedUpdater(
    { handle() {} },
    { isPackaged: true },
    {
      createAutoUpdater: () => fakeUpdater,
      broadcast: (channel, payload) => sent.push({ channel, payload }),
      log: () => {},
    },
  );

  const checked = await handle.check();
  assert.equal(checked.available, false);
  assert.ok(sent.some((s) => s.channel === "updates:progress" && s.payload.stage === "check"));
  assert.ok(sent.some((s) => s.channel === "updates:progress" && s.payload.stage === "idle"));
});

test("registerPackagedUpdater: concurrent check returns busy without duplicate work", async () => {
  const events = {};
  const sent = [];
  let resolveFirst;
  const firstDone = new Promise((r) => { resolveFirst = r; });
  const fakeUpdater = {
    autoDownload: true,
    autoInstallOnAppQuit: false,
    allowDowngrade: true,
    allowPrerelease: true,
    disableWebInstaller: false,
    on(name, cb) { events[name] = cb; },
    async checkForUpdates() {
      events["checking-for-update"]();
      await firstDone;
      events["update-not-available"]({ version: "0.9.162" });
      return { updateInfo: { version: "0.9.162" } };
    },
    async downloadUpdate() {},
    quitAndInstall() {},
  };
  const handle = registerPackagedUpdater(
    { handle() {} },
    { isPackaged: true },
    {
      createAutoUpdater: () => fakeUpdater,
      broadcast: (channel, payload) => sent.push({ channel, payload }),
      log: () => {},
    },
  );

  const first = handle.check();
  const second = await handle.check();
  assert.equal(second.busy, true);

  resolveFirst();
  await first;
  assert.ok(sent.some((s) => s.channel === "updates:progress" && s.payload.stage === "idle"));
});

test("mergeUpdateAvailability: forwards packaged busy flag", () => {
  const merged = mergeUpdateAvailability({
    gitResult: { available: false, behind: 0 },
    packagedResult: { available: false, busy: true },
    isPackaged: true,
    shellVersion: "0.9.161",
    checkoutVersion: "0.9.161",
  });
  assert.equal(merged.busy, true);
});
