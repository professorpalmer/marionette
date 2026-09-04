// Light source-wiring checks for the native fs reveal bridge.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { describe, it } = require("node:test");
const vm = require("node:vm");

describe("fs-bridge revealInFolder", () => {
  it("registers shell.showItemInFolder via fs:revealInFolder", () => {
    const src = fs.readFileSync(path.join(__dirname, "fs-bridge.cjs"), "utf8");
    assert.match(src, /shell\.showItemInFolder/);
    assert.match(src, /fs:revealInFolder/);
  });

  it("preload exposes harnessIPC.fs.revealInFolder", () => {
    const preload = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
    assert.match(preload, /revealInFolder:\s*\(absPath\)\s*=>\s*ipcRenderer\.invoke\("fs:revealInFolder"/);
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
    return exposed.isDirectory("/outside/folder").then((result) => {
      assert.equal(result, true);
      assert.deepEqual(invokes, [["fs:isDirectory", "/outside/folder"]]);
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
