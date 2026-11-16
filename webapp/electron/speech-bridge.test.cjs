"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const EventEmitter = require("node:events");
const { PassThrough } = require("node:stream");
const { createSpeechBridge, macMajor, MICROPHONE_SETTINGS_URL } = require("./speech-bridge.cjs");

function fakeChild() {
  const child = new EventEmitter();
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.written = [];
  child.stdin.on("data", chunk => child.written.push(Buffer.from(chunk)));
  child.killed = null;
  child.kill = signal => { child.killed = signal; };
  return child;
}

function fakeSender(id = 7) {
  return { id, events: [], isDestroyed: () => false, send(channel, event) { this.events.push([channel, event]); } };
}

function bridge(overrides = {}) {
  const children = [];
  const calls = { spawn: [], compile: [], ask: 0, opened: [] };
  const access = { value: overrides.microphone || "not-determined" };
  const instance = createSpeechBridge({
    app: { isPackaged: false, getPath: () => "/tmp/user-data" },
    platform: "darwin",
    osRelease: "25.4.0",
    systemPreferences: {
      getMediaAccessStatus: () => access.value,
      askForMediaAccess: async () => { calls.ask += 1; access.value = overrides.grant === false ? "denied" : "granted"; return overrides.grant !== false; },
    },
    shell: { openExternal: async url => { calls.opened.push(url); } },
    compile: async (destination, source, frameworks) => {
      calls.compile.push({ destination, source, frameworks });
      if (overrides.compileError) throw new Error(overrides.compileError);
      return destination;
    },
    spawn: (file, args) => {
      calls.spawn.push({ file, args });
      const child = fakeChild();
      children.push(child);
      return child;
    },
    ...overrides.options,
  });
  return { instance, children, calls, access };
}

test("macMajor maps Darwin releases to macOS versions", () => {
  assert.equal(macMajor("25.4.0"), 26);
  assert.equal(macMajor("24.6.0"), 25);
  assert.equal(macMajor("junk"), 0);
});

test("status is unavailable on older macOS and on other platforms", async () => {
  const old = bridge({ options: { osRelease: "24.6.0" } });
  assert.deepEqual(await old.instance.status(), {
    engine: "macos", available: false,
    reason: "On-device dictation needs macOS 26 or later. Use the local Whisper engine.",
    microphone: "not-determined",
  });
  assert.equal(old.calls.compile.length, 0);
  const win = bridge({ options: { platform: "win32" } });
  assert.match((await win.instance.status()).reason, /needs macOS/);
});

test("status compiles the development helper once and reports the microphone", async () => {
  const { instance, calls } = bridge({ microphone: "granted" });
  const status = await instance.status();
  assert.equal(status.available, true);
  assert.equal(status.microphone, "granted");
  await instance.status();
  assert.equal(calls.compile.length, 1);
  assert.match(calls.compile[0].destination, /native-speech\/bin\/speech-macos$/);
  assert.match(calls.compile[0].source, /speech-macos\.swift$/);
});

test("a failed compile is reported and retried on the next call", async () => {
  const { instance, calls } = bridge({ compileError: "xcrun: error: no SDK" });
  const status = await instance.status();
  assert.equal(status.available, false);
  assert.equal(status.reason, "xcrun: error: no SDK");
  await instance.status();
  assert.equal(calls.compile.length, 2);
});

test("a packaged app without the helper says so", async () => {
  const { instance } = bridge({ options: { app: { isPackaged: true }, resourcesPath: "/nonexistent-resources" } });
  const status = await instance.status();
  assert.equal(status.available, false);
  assert.match(status.reason, /does not include the on-device dictation helper/);
});

test("requestMicrophone asks only when the user has not decided", async () => {
  const asked = bridge();
  assert.deepEqual(await asked.instance.requestMicrophone(), { granted: true, status: "granted" });
  assert.equal(asked.calls.ask, 1);
  const denied = bridge({ microphone: "denied" });
  assert.deepEqual(await denied.instance.requestMicrophone(), { granted: false, status: "denied" });
  assert.equal(denied.calls.ask, 0);
  const refused = bridge({ grant: false });
  assert.deepEqual(await refused.instance.requestMicrophone(), { granted: false, status: "denied" });
});

test("openMicrophoneSettings opens the Privacy pane", async () => {
  const { instance, calls } = bridge();
  assert.equal(await instance.openMicrophoneSettings(), true);
  assert.deepEqual(calls.opened, [MICROPHONE_SETTINGS_URL]);
});

test("start streams helper JSON lines to the renderer and stop closes stdin", async () => {
  const { instance, children, calls } = bridge();
  const sender = fakeSender();
  const started = await instance.start(sender, { locale: "en-GB" });
  assert.equal(started.ok, true);
  assert.deepEqual(calls.spawn[0].args, ["--locale", "en-GB"]);
  const child = children[0];
  child.stdout.write('{"type":"ready"}\n{"type":"partial","te');
  child.stdout.write('xt":"hello"}\nnot json\n');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sender.events.map(([, e]) => e), [
    { id: started.id, type: "ready" },
    { id: started.id, type: "partial", text: "hello" },
  ]);
  assert.ok(sender.events.every(([channel]) => channel === "voice:event"));

  instance.write(started.id, new Uint8Array([1, 0, 2, 0]));
  instance.write(started.id, new Uint8Array([1, 0, 2]));
  instance.write("other", new Uint8Array([1, 0]));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Buffer.concat(child.written), Buffer.from([1, 0, 2, 0]));

  let ended = false;
  child.stdin.on("finish", () => { ended = true; });
  instance.stop(started.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ended, true);
  child.emit("exit", 0, null);
  assert.deepEqual(sender.events.at(-1)[1], { id: started.id, type: "exit", code: 0, signal: null, stderr: undefined });
});

test("a second start from the same window kills the first helper", async () => {
  const { instance, children } = bridge();
  const sender = fakeSender();
  await instance.start(sender, {});
  await instance.start(sender, {});
  assert.equal(children[0].killed, "SIGTERM");
  assert.equal(children[1].killed, null);
});

test("start refuses a malformed locale and an unsupported OS", async () => {
  const { instance, calls } = bridge();
  assert.equal((await instance.start(fakeSender(), { locale: "en; rm -rf" })).code, "invalid_locale");
  assert.equal(calls.spawn.length, 0);
  const old = bridge({ options: { osRelease: "23.0.0" } });
  assert.equal((await old.instance.start(fakeSender(), {})).code, "unsupported");
});

test("a fake capture device needs no OS microphone permission", async () => {
  const { instance, calls } = bridge({ microphone: "denied", options: { fakeCaptureDevice: true } });
  assert.deepEqual(await instance.requestMicrophone(), { granted: true, status: "granted" });
  assert.equal(calls.ask, 0);
});
