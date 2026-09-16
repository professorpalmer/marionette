"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const { createNativeComputer, _test } = require("./native-computer.cjs");

const echoHelper = String.raw`
const readline = require("node:readline");
readline.createInterface({ input: process.stdin }).on("line", line => {
  const request = JSON.parse(line);
  if (request.operation === "hang") return;
  if (request.operation === "fail") process.stdout.write(JSON.stringify({ id: request.id, ok: false, error: "native refusal" }) + "\n");
  else process.stdout.write(JSON.stringify({ id: request.id, ok: true, result: { operation: request.operation } }) + "\n");
});`;

test("protocol correlates replies, surfaces native errors, and kills work on abort", async () => {
  const client = _test.createProtocolClient({ file: process.execPath, args: ["-e", echoHelper] });
  try {
    assert.deepEqual(await client.request({ operation: "status" }), { operation: "status" });
    await assert.rejects(client.request({ operation: "fail" }), /native refusal/);
    const abort = new AbortController();
    const pending = client.request({ operation: "hang" }, abort.signal);
    abort.abort();
    await assert.rejects(pending, /cancelled/);
    assert.deepEqual(await client.request({ operation: "apps" }), { operation: "apps" });
  } finally { client.close(); }
});

test("payload validation admits only the fixed native operation schema", () => {
  assert.equal(_test.validatePayload({ operation: "status" }).operation, "status");
  assert.equal(_test.validatePayload({ operation: "snapshot", app_id: "bundle:test" }).operation, "snapshot");
  assert.equal(_test.validatePayload({ operation: "click", app_id: "bundle:test", snapshot_id: "one", ref: "r1" }).ref, "r1");
  assert.throws(() => _test.validatePayload({ operation: "eval", script: "whoami" }), /operation/);
  assert.throws(() => _test.validatePayload({ operation: "status", command: "whoami" }), /unknown field/);
  assert.throws(() => _test.validatePayload({ operation: "click", app_id: "a", snapshot_id: "s" }), /requires ref or coordinates/);
  assert.throws(() => _test.validatePayload({ operation: "type", app_id: "a", snapshot_id: "s", text: "x".repeat(70000) }), /text is invalid/);
  const keypress = { operation: "keypress", app_id: "a", snapshot_id: "s" };
  assert.throws(() => _test.validatePayload({ ...keypress, keys: ["WIN", "R"] }, "win32"), /app-scoped/);
  assert.throws(() => _test.validatePayload({ ...keypress, keys: ["CMD", "TAB"] }, "darwin"), /app-scoped/);
  assert.throws(() => _test.validatePayload({ ...keypress, keys: ["CTRL", "ALT", "DELETE"] }, "win32"), /app-scoped/);
  assert.ok(_test.validatePayload({ ...keypress, keys: ["CMD", "SHIFT", "Z"] }, "darwin"));
});

test("late output from a cancelled helper cannot corrupt its replacement", async () => {
  const children = [];
  const client = _test.createProtocolClient({ file: "fixture", args: [] }, () => {
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.write = (line, callback) => { child.request = JSON.parse(line); callback(); };
    child.kill = () => {};
    children.push(child);
    return child;
  });
  try {
    const abort = new AbortController();
    const first = client.request({ operation: "status" }, abort.signal);
    abort.abort();
    await assert.rejects(first, /cancelled/);
    const second = client.request({ operation: "apps" });
    children[0].stdout.emit("data", Buffer.from("not JSON\n"));
    children[1].stdout.emit("data", Buffer.from(JSON.stringify({ id: children[1].request.id, ok: true, result: [] }) + "\n"));
    assert.deepEqual(await second, []);
  } finally { client.close(); }
});

test("a synchronous spawn failure is cleaned up and can be retried", async () => {
  let attempts = 0;
  const { spawn } = require("node:child_process");
  const client = _test.createProtocolClient({ file: process.execPath, args: ["-e", echoHelper] }, (...args) => {
    if (++attempts === 1) throw new Error("spawn fixture failure");
    return spawn(...args);
  });
  try {
    await assert.rejects(client.request({ operation: "status" }), /spawn fixture failure/);
    assert.deepEqual(await client.request({ operation: "apps" }), { operation: "apps" });
  } finally { client.close(); }
});

test("native commands use fixed executable arguments and user-data screenshot storage", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "native-computer-test-"));
  const app = { isPackaged: true };
  const mac = _test.helperCommand({ platform: "darwin", app, resourcesPath: path.join(root, "resources"), userDataPath: root });
  assert.equal(mac.file, path.join(root, "resources", "native-computer", "computer-macos"));
  assert.deepEqual(mac.args, ["--screenshot-dir", path.join(root, "native-computer", "tmp")]);
  const windows = _test.helperCommand({ platform: "win32", app, resourcesPath: path.join(root, "resources"), userDataPath: root });
  assert.equal(windows.file, "powershell.exe");
  assert.deepEqual(windows.args.slice(0, 6), ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]);
  assert.equal(windows.args[6], path.join(root, "resources", "native-computer", "computer-windows.ps1"));
  assert.deepEqual(windows.args.slice(7), ["-ScreenshotDir", path.join(root, "native-computer", "tmp")]);
  assert.equal(_test.helperCommand({ platform: "linux", app, resourcesPath: root, userDataPath: root }), null);
});

test("public wrapper returns truthful unsupported status without launching a helper", async () => {
  if (process.platform === "darwin" || process.platform === "win32") return;
  const computer = createNativeComputer({ app: { getPath: () => os.tmpdir() } });
  assert.deepEqual(await computer.dispatch({ operation: "status" }), _test.unsupportedStatus(process.platform));
  await assert.rejects(computer.dispatch({ operation: "apps" }), /unsupported/);
  computer.close();
});
