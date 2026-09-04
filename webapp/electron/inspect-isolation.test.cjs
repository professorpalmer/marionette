"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  isInspectMode,
  resolveHarnessStateDir,
  resolveInspectUserDataDir,
  stateFileSearchDirs,
} = require("./inspect-isolation.cjs");

test("isInspectMode accepts 1/true/yes only", () => {
  assert.equal(isInspectMode({}), false);
  assert.equal(isInspectMode({ HARNESS_INSPECT: "0" }), false);
  assert.equal(isInspectMode({ HARNESS_INSPECT: "1" }), true);
  assert.equal(isInspectMode({ HARNESS_INSPECT: "TRUE" }), true);
  assert.equal(isInspectMode({ HARNESS_INSPECT: "yes" }), true);
});

test("inspect mode never falls back to ~/.pmharness for markers", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-inspect-"));
  const state = path.join(tmp, "inspect-state");
  fs.mkdirSync(state, { recursive: true });
  const env = {
    HARNESS_INSPECT: "1",
    HARNESS_STATE_DIR: state,
  };
  assert.deepEqual(stateFileSearchDirs(env), [path.resolve(state)]);
  assert.equal(resolveHarnessStateDir(env), path.resolve(state));
});

test("non-inspect still searches product home after the explicit state dir", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-state-"));
  const state = path.join(tmp, "state");
  fs.mkdirSync(state, { recursive: true });
  const dirs = stateFileSearchDirs({ HARNESS_STATE_DIR: state });
  assert.equal(dirs[0], path.resolve(state));
  assert.equal(dirs[1], path.join(os.homedir(), ".pmharness"));
});

test("resolveInspectUserDataDir honors an explicit override", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "marionette-udata-"));
  assert.equal(
    resolveInspectUserDataDir({ HARNESS_USER_DATA_DIR: tmp }),
    path.resolve(tmp),
  );
  assert.equal(resolveInspectUserDataDir({}), "");
});
