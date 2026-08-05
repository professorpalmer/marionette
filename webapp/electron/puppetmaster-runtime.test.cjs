"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const runtime = require("./puppetmaster-runtime.cjs");
const pm = require("./update-pm.cjs");

const REPO = "/home/u/.marionette/marionette";
const PYTHON = "/home/u/.marionette/marionette/.venv/bin/python";

function pinAt(version) {
  return () => ({
    pinnedSpec: `puppetmaster-ai==${version}`,
    distName: "puppetmaster-ai",
    planPuppetmasterUpgrade: pm.planPuppetmasterUpgrade,
  });
}

function showOutput(body) {
  return `Name: puppetmaster-ai\n${body}`;
}

/**
 * Fake child runner: `uv --version` succeeds unless uv is disabled, `pip show`
 * returns the given text, and installs succeed unless installFails is set.
 */
function fakeRun({ show = "", hasUv = true, installFails = "" } = {}) {
  const calls = [];
  const run = async (cmd, args) => {
    calls.push([cmd, ...args].join(" "));
    if (cmd === "uv" && args[0] === "--version") {
      return hasUv ? { ok: true, out: "uv 0.5.0", err: "" } : { ok: false, out: "", err: "ENOENT" };
    }
    if (args.includes("show")) return { ok: true, out: show, err: "" };
    if (args.includes("install")) {
      return installFails
        ? { ok: false, out: "", err: installFails }
        : { ok: true, out: "Installed 1 package", err: "" };
    }
    return { ok: true, out: "", err: "" };
  };
  return { run, calls, installs: () => calls.filter((c) => c.includes(" install ")) };
}

function ensure(opts, extra = {}) {
  return runtime.ensurePuppetmasterRuntime({
    repoRoot: REPO,
    python: PYTHON,
    exists: () => true,
    env: {},
    ...opts,
    ...extra,
  });
}

test("runtime parity: a venv already at the checkout pin is left alone", async () => {
  const fake = fakeRun({ show: showOutput("Version: 1.21.11\nLocation: /venv/site-packages") });
  const res = await ensure({ run: fake.run, resolvePin: pinAt("1.21.11") });
  assert.equal(res.status, "current");
  assert.equal(res.version, "1.21.11");
  assert.deepEqual(fake.installs(), []);
  assert.equal(runtime.isRuntimeStale(res), false);
});

test("runtime parity: a venv behind the checkout pin is upgraded to the CHECKOUT pin", async () => {
  const fake = fakeRun({ show: showOutput("Version: 1.20.10\nLocation: /venv/site-packages") });
  // The pin the checkout asks for, deliberately newer than this shell's frozen
  // DEFAULT_PUPPETMASTER_SPEC -- installing the frozen one would stay stale.
  const res = await ensure({ run: fake.run, resolvePin: pinAt("9.9.9") });
  assert.equal(res.status, "upgraded");
  assert.equal(res.from, "1.20.10");
  assert.equal(res.to, "9.9.9");
  assert.deepEqual(fake.installs(), [
    `uv pip install --python ${PYTHON} --upgrade puppetmaster-ai==9.9.9`,
  ]);
});

test("runtime parity: an unreachable index reports stale instead of claiming current", async () => {
  const fake = fakeRun({
    show: showOutput("Version: 1.20.10"),
    installFails: "Could not resolve host: pypi.org",
  });
  const res = await ensure({ run: fake.run, resolvePin: pinAt("1.21.11") });
  assert.equal(res.status, "stale");
  assert.equal(res.have, "1.20.10");
  assert.equal(res.want, "1.21.11");
  assert.match(res.reason, /pypi\.org/);
  assert.equal(runtime.isRuntimeStale(res), true);
  assert.match(runtime.describeRuntimeParity(res), /1\.21\.10/);
});

test("runtime parity: an editable dev checkout is never clobbered", async () => {
  const fake = fakeRun({
    show: showOutput("Version: 1.20.10\nEditable project location: /Users/dev/Puppetmaster"),
  });
  const res = await ensure({ run: fake.run, resolvePin: pinAt("1.21.11") });
  assert.equal(res.status, "skipped");
  assert.match(res.reason, /editable/);
  assert.deepEqual(fake.installs(), []);
});

test("runtime parity: a custom MARIONETTE_PUPPETMASTER_SPEC is honored", async () => {
  const fake = fakeRun({ show: showOutput("Version: 1.20.10") });
  const res = await ensure({
    run: fake.run,
    resolvePin: pinAt("1.21.11"),
    env: { MARIONETTE_PUPPETMASTER_SPEC: "/Users/dev/Puppetmaster" },
  });
  assert.equal(res.status, "skipped");
  assert.match(res.reason, /MARIONETTE_PUPPETMASTER_SPEC/);
  assert.deepEqual(fake.installs(), []);
});

test("runtime parity: without uv it falls back to the venv's own pip", async () => {
  const fake = fakeRun({ show: showOutput("Version: 1.20.10"), hasUv: false });
  const res = await ensure({ run: fake.run, resolvePin: pinAt("1.21.11") });
  assert.equal(res.status, "upgraded");
  assert.deepEqual(fake.installs(), [
    `${PYTHON} -m pip install --upgrade puppetmaster-ai==1.21.11 --quiet`,
  ]);
});

test("runtime parity: a missing venv interpreter is stale, not silently current", async () => {
  const fake = fakeRun({ show: showOutput("Version: 1.21.11") });
  const res = await ensure({ run: fake.run, resolvePin: pinAt("1.21.11"), exists: () => false });
  assert.equal(res.status, "stale");
  assert.match(res.reason, /no venv interpreter/);
  assert.deepEqual(fake.calls, []);
});

test("runtime parity: a source/dev run with no checkout is a no-op", async () => {
  const res = await runtime.ensurePuppetmasterRuntime({ repoRoot: "" });
  assert.equal(res.status, "skipped");
  assert.equal(runtime.isRuntimeStale(res), false);
});

test("runtimeParityFields: only a stale runtime annotates the update-check payload", async () => {
  const fake = fakeRun({ show: showOutput("Version: 1.20.10"), installFails: "offline" });
  const stale = await ensure({ run: fake.run, resolvePin: pinAt("1.21.11") });
  const fields = runtime.runtimeParityFields(stale);
  assert.equal(fields.runtimeStale, true);
  assert.equal(fields.runtimeHave, "1.20.10");
  assert.equal(fields.runtimeWant, "1.21.11");
  assert.match(fields.runtimeNote, /1\.21\.10/);

  const ok = fakeRun({ show: showOutput("Version: 1.21.11") });
  const current = await ensure({ run: ok.run, resolvePin: pinAt("1.21.11") });
  assert.deepEqual(runtime.runtimeParityFields(current), {});
});

test("resolveCheckoutPin: the checkout's pin wins over this shell's frozen pin", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mn-pin-"));
  const dir = path.join(root, "webapp", "electron");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(
    path.join(dir, "update-pm.cjs"),
    'module.exports = { DEFAULT_PUPPETMASTER_SPEC: "puppetmaster-ai==9.9.9" };\n',
  );
  try {
    const pin = pm.resolveCheckoutPin(root);
    assert.equal(pin.pinnedSpec, "puppetmaster-ai==9.9.9");
    assert.equal(pin.distName, pm.PUPPETMASTER_DIST_NAME);
    assert.equal(typeof pin.planPuppetmasterUpgrade, "function");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("resolveCheckoutPin: a missing checkout falls back to the packaged pin", () => {
  const pin = pm.resolveCheckoutPin(path.join(os.tmpdir(), "mn-pin-absent"));
  assert.equal(pin.pinnedSpec, pm.DEFAULT_PUPPETMASTER_SPEC);
  assert.equal(pin.planPuppetmasterUpgrade, pm.planPuppetmasterUpgrade);
});
