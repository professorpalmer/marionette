"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test, afterEach } = require("node:test");
const assert = require("node:assert/strict");

function pmharnessHome() {
  return path.join(os.homedir(), ".pmharness");
}

function resolveHarnessStateDir(env = process.env) {
  const explicit = (env.HARNESS_STATE_DIR || "").trim();
  if (explicit) {
    return path.resolve(explicit);
  }
  const stateSub = path.join(pmharnessHome(), "state");
  try {
    if (fs.statSync(stateSub).isDirectory()) {
      return stateSub;
    }
  } catch {}
  return pmharnessHome();
}

const prev = process.env.HARNESS_STATE_DIR;
afterEach(() => {
  if (prev === undefined) delete process.env.HARNESS_STATE_DIR;
  else process.env.HARNESS_STATE_DIR = prev;
});

test("resolveHarnessStateDir honors HARNESS_STATE_DIR with absolute expansion", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "harness-state-"));
  process.env.HARNESS_STATE_DIR = path.join(tmp, "nested", "state");
  fs.mkdirSync(process.env.HARNESS_STATE_DIR, { recursive: true });
  assert.equal(resolveHarnessStateDir(), path.resolve(process.env.HARNESS_STATE_DIR));
});

test("resolveHarnessStateDir falls back to ~/.pmharness/state when env unset", () => {
  delete process.env.HARNESS_STATE_DIR;
  const stateSub = path.join(pmharnessHome(), "state");
  const existed = fs.existsSync(stateSub);
  if (!existed) fs.mkdirSync(stateSub, { recursive: true });
  try {
    assert.equal(resolveHarnessStateDir(), stateSub);
  } finally {
    if (!existed) {
      try { fs.rmdirSync(stateSub); } catch {}
    }
  }
});
