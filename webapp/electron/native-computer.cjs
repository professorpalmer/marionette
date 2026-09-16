"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const { compileDevelopmentMacHelper, packagedHelperPaths } = require("./build-native-computer.cjs");

const OPERATIONS = new Set(["status", "apps", "snapshot", "click", "type", "keypress", "scroll"]);
const DIRECTIONS = new Set(["up", "down", "left", "right"]);
const MAX_LINE_BYTES = 1024 * 1024;
const MAX_TEXT_BYTES = 64 * 1024;
const REQUEST_TIMEOUT_MS = 20_000;

function fail(message) { throw new Error(`Native computer: ${message}`); }
function optionalString(value, name, max = 512) {
  if (value === undefined) return;
  if (typeof value !== "string" || !value || Buffer.byteLength(value) > max) fail(`${name} is invalid`);
}

function validatePayload(payload, platform = process.platform) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload) || !OPERATIONS.has(payload.operation)) fail("operation is invalid");
  const allowed = new Set(["operation", "app_id", "snapshot_id", "ref", "text", "keys", "direction", "x", "y"]);
  for (const key of Object.keys(payload)) if (!allowed.has(key)) fail(`unknown field ${key}`);
  optionalString(payload.app_id, "app_id");
  optionalString(payload.snapshot_id, "snapshot_id");
  optionalString(payload.ref, "ref", 128);
  if (payload.text !== undefined && (typeof payload.text !== "string" || Buffer.byteLength(payload.text) > MAX_TEXT_BYTES)) fail("text is invalid");
  if (payload.keys !== undefined && (!Array.isArray(payload.keys) || payload.keys.length < 1 || payload.keys.length > 8
      || payload.keys.some(key => typeof key !== "string" || !key || key.length > 32))) fail("keys are invalid");
  if (payload.direction !== undefined && !DIRECTIONS.has(payload.direction)) fail("direction is invalid");
  for (const name of ["x", "y"]) if (payload[name] !== undefined && (!Number.isFinite(payload[name]) || Math.abs(payload[name]) > 100000)) fail(`${name} is invalid`);
  if (["snapshot", "click", "type", "keypress", "scroll"].includes(payload.operation) && !payload.app_id) fail("app_id is required");
  if (["click", "type", "keypress", "scroll"].includes(payload.operation) && !payload.snapshot_id) fail("snapshot_id is required");
  if (payload.operation === "click" && !payload.ref && !(Number.isFinite(payload.x) && Number.isFinite(payload.y))) fail("click requires ref or coordinates");
  if (payload.operation === "type" && payload.text === undefined) fail("type requires text");
  if (payload.operation === "keypress" && !payload.keys) fail("keypress requires keys");
  if (payload.operation === "keypress") {
    const keys = payload.keys.map(key => key.toUpperCase());
    const modifiers = keys.slice(0, -1);
    const final = keys.at(-1);
    const command = platform === "darwin" ? ["CMD", "COMMAND", "META"] : [];
    const editModifiers = ["CTRL", "CONTROL", ...command];
    const editKeys = new Set(["A", "C", "V", "X", "Z", "Y", "F", "L", "R", "W", "T", "N", "S", "O", "P", "LEFT", "RIGHT", "UP", "DOWN", "BACKSPACE"]);
    const selection = modifiers.every(key => key === "SHIFT");
    const editing = modifiers.some(key => editModifiers.includes(key))
      && modifiers.every(key => key === "SHIFT" || editModifiers.includes(key)) && editKeys.has(final);
    const wordNavigation = modifiers.some(key => key === "ALT" || key === "OPTION")
      && modifiers.every(key => ["ALT", "OPTION", "SHIFT"].includes(key)) && ["LEFT", "RIGHT", "BACKSPACE"].includes(final);
    if (!selection && !editing && !wordNavigation) fail("key combination leaves app-scoped editing/navigation; use an approved app target instead");
  }
  if (payload.operation === "scroll" && !payload.direction) fail("scroll requires direction");
  return payload;
}

function unsupportedStatus(platform) {
  return {
    supported: false,
    accessibility: false,
    screenRecording: false,
    setup: `Native computer control is not available on ${platform}. Supported platforms are macOS and Windows.`,
  };
}

function helperCommand({ platform, app, resourcesPath, userDataPath }) {
  const screenshots = path.join(userDataPath, "native-computer", "tmp");
  fs.mkdirSync(screenshots, { recursive: true, mode: 0o700 });
  if (platform === "darwin") {
    const executable = app?.isPackaged
      ? path.join(resourcesPath, packagedHelperPaths.darwin)
      : path.join(userDataPath, "native-computer", "bin", "computer-macos");
    return { file: executable, args: ["--screenshot-dir", screenshots], screenshots };
  }
  if (platform === "win32") {
    const script = app?.isPackaged
      ? path.join(resourcesPath, packagedHelperPaths.win32)
      : path.join(__dirname, "native", "computer-windows.ps1");
    return {
      file: "powershell.exe",
      args: ["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, "-ScreenshotDir", screenshots],
      screenshots,
    };
  }
  return null;
}

function createProtocolClient(command, spawnImpl = spawn) {
  let child;
  let stdout = Buffer.alloc(0);
  let stderr = "";
  let closed = false;
  const pending = new Map();

  const rejectAll = error => {
    for (const item of pending.values()) { clearTimeout(item.timer); item.cleanup(); item.reject(error); }
    pending.clear();
  };
  const stop = error => {
    const previous = child;
    child = undefined;
    previous?.kill();
    stdout = Buffer.alloc(0);
    rejectAll(error || new Error("Native computer helper stopped"));
  };
  const start = () => {
    if (closed) fail("client is closed");
    if (child) return child;
    stdout = Buffer.alloc(0);
    stderr = "";
    const process = spawnImpl(command.file, command.args, { stdio: ["pipe", "pipe", "pipe"], windowsHide: true, shell: false });
    child = process;
    process.stdout.on("data", chunk => {
      if (child !== process) return;
      stdout = Buffer.concat([stdout, chunk]);
      let newline;
      while ((newline = stdout.indexOf(10)) !== -1) {
        if (newline > MAX_LINE_BYTES) return stop(new Error("Native computer helper output exceeded its limit"));
        const line = stdout.subarray(0, newline).toString("utf8");
        stdout = stdout.subarray(newline + 1);
        if (!line) continue;
        let message;
        try { message = JSON.parse(line); } catch { return stop(new Error("Native computer helper returned invalid JSON")); }
        const item = pending.get(message.id);
        if (!item) continue;
        pending.delete(message.id);
        clearTimeout(item.timer);
        item.cleanup();
        if (message.ok === true) item.resolve(message.result);
        else item.reject(new Error(typeof message.error === "string" ? message.error : "Native computer request failed"));
      }
      if (stdout.length > MAX_LINE_BYTES) stop(new Error("Native computer helper output exceeded its limit"));
    });
    process.stderr.on("data", chunk => { if (child === process) stderr = (stderr + chunk.toString("utf8")).slice(-65536); });
    const ioError = error => { if (child === process) stop(new Error(`Native computer helper failed: ${error.message}`)); };
    process.once("error", ioError);
    process.stdin.on("error", ioError);
    process.stdout.on("error", ioError);
    process.stderr.on("error", ioError);
    process.once("exit", code => {
      if (child === process) stop(new Error(`Native computer helper exited (${code ?? "signal"})${stderr ? `: ${stderr.trim()}` : ""}`));
    });
    return process;
  };
  return {
    request(payload, signal) {
      if (signal?.aborted) return Promise.reject(new Error("Native computer request cancelled"));
      return new Promise((resolve, reject) => {
        const id = crypto.randomUUID();
        const abort = () => { stop(new Error("Native computer request cancelled")); };
        const cleanup = () => signal?.removeEventListener("abort", abort);
        signal?.addEventListener("abort", abort, { once: true });
        const timer = setTimeout(() => stop(new Error("Native computer request timed out")), REQUEST_TIMEOUT_MS);
        pending.set(id, { resolve, reject, cleanup, timer });
        try {
          const process = start();
          process.stdin.write(`${JSON.stringify({ id, ...payload })}\n`, error => {
            if (error && child === process && pending.has(id)) stop(new Error(`Native computer helper input failed: ${error.message}`));
          });
        } catch (error) { stop(error); }
      });
    },
    close() { closed = true; stop(new Error("Native computer client closed")); },
  };
}

function createNativeComputer({ app, resourcesPath = process.resourcesPath, userDataPath = app?.getPath?.("userData") }) {
  if (!userDataPath || typeof userDataPath !== "string") fail("userDataPath is required");
  const platform = process.platform;
  let clientPromise;
  const screenshots = [];
  function pruneScreenshots(keep = 0) {
    while (screenshots.length > keep) { try { fs.unlinkSync(screenshots.shift()); } catch { /* Already removed. */ } }
  }
  const getClient = async () => {
    if (!clientPromise) clientPromise = (async () => {
      const command = helperCommand({ platform, app, resourcesPath, userDataPath });
      if (!command) return null;
      if (platform === "darwin" && !app?.isPackaged) await compileDevelopmentMacHelper(command.file);
      if (platform === "darwin" && !fs.existsSync(command.file)) fail("packaged macOS helper is missing");
      if (platform === "win32" && !fs.existsSync(command.args[6])) fail("Windows helper script is missing");
      return createProtocolClient(command);
    })();
    return clientPromise;
  };
  return {
    async dispatch(payload, signal) {
      validatePayload(payload);
      const client = await getClient();
      if (!client) {
        if (payload.operation === "status") return unsupportedStatus(platform);
        fail(`platform ${platform} is unsupported`);
      }
      const result = await client.request(payload, signal);
      if (result?.screenshot_path) {
        const expected = path.join(userDataPath, "native-computer", "tmp");
        if (path.dirname(result.screenshot_path) !== expected || !/^snapshot-[a-f0-9-]+\.png$/i.test(path.basename(result.screenshot_path))) fail("helper returned an invalid screenshot path");
        screenshots.push(result.screenshot_path);
        pruneScreenshots(8);
      }
      return result;
    },
    close() { pruneScreenshots(); if (clientPromise) clientPromise.then(client => client?.close()).catch(() => {}); },
  };
}

module.exports = {
  createNativeComputer,
  _test: Object.freeze({ createProtocolClient, helperCommand, unsupportedStatus, validatePayload }),
};
