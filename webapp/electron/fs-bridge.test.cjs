const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { describe, it } = require("node:test");
const vm = require("node:vm");

describe("fs-bridge revealInFolder", () => {
  it("fs:revealInFolder reveals an allowed path and denies one outside the roots", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "mari-reveal-root-"));
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), "mari-reveal-out-"));
    const inside = path.join(root, "a.txt");
    fs.writeFileSync(inside, "x");
    const revealed = [];
    const handlers = new Map();
    const mod = { exports: {} };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "fs-bridge.cjs"), "utf8"), {
      module: mod,
      require: (name) => (name === "electron"
        ? { shell: { showItemInFolder: (p) => revealed.push(p) } }
        : name.startsWith("./") ? require(path.join(__dirname, name)) : require(name)),
    });
    mod.exports.registerFsBridge(
      { handle: (channel, fn) => handlers.set(channel, fn) },
      { getAllowedRoots: () => [root] },
    );
    const reveal = handlers.get("fs:revealInFolder");
    try {
      assert.equal(reveal({}, inside).ok, true);
      const denied = reveal({}, path.join(outside, "b.txt"));
      assert.equal(denied.ok, false);
      assert.equal(denied.error, "path not allowed");
      const missing = reveal({}, "");
      assert.equal(missing.ok, false);
      assert.equal(missing.error, "missing path");
      assert.deepEqual(revealed, [inside]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
      fs.rmSync(outside, { recursive: true, force: true });
    }
  });

  it("sandboxed preload exposes harnessIPC without requiring Node filesystem access", () => {
    const preload = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
    let exposed = null;
    const invokes = [];
    const ipcRenderer = {
      invoke: (channel, value) => {
        invokes.push([channel, value]);
        return Promise.resolve(channel === "fs:isDirectory");
      },
      send: () => {},
      on: () => {},
      once: () => {},
      removeListener: () => {},
    };
    vm.runInNewContext(preload, {
      require: (name) => {
        if (name !== "electron") throw new Error(`sandbox module not found: ${name}`);
        return {
          contextBridge: { exposeInMainWorld: (_name, api) => { exposed = api; } },
          ipcRenderer,
          webUtils: { getPathForFile: () => "" },
        };
      },
      process: { env: {} },
      Uint8Array,
    });
    assert.ok(exposed, "preload must expose harnessIPC inside Electron's sandbox");
    assert.equal(typeof exposed.getJSON, "function");
    assert.equal(typeof exposed.isDirectory, "function");
    return exposed.isDirectory("/outside/folder").then(async (result) => {
      assert.equal(result, true);
      await exposed.fs.openPath("/outside/file.txt");
      await exposed.fs.revealInFolder("/repo/a.txt");
      assert.deepEqual(invokes, [
        ["fs:isDirectory", "/outside/folder"],
        ["fs:openPath", "/outside/file.txt"],
        ["fs:revealInFolder", "/repo/a.txt"],
      ]);
    });
  });

  it("checks dropped directories in the main process", async () => {
    const { registerFsBridge } = require("./fs-bridge.cjs");
    const listeners = new Map();
    const ipcMain = {
      handle: (channel, listener) => listeners.set(channel, listener),
      on: (channel, listener) => listeners.set(channel, listener),
    };
    registerFsBridge(ipcMain);
    const listener = listeners.get("fs:isDirectory");
    assert.equal(typeof listener, "function");

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "mari-drop-dir-"));
    const file = path.join(dir, "file.txt");
    fs.writeFileSync(file, "x");
    try {
      assert.equal(await listener({}, dir), true);
      assert.equal(await listener({}, file), false);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
