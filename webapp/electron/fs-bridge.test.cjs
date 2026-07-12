// Light source-wiring checks for the native fs reveal bridge.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { describe, it } = require("node:test");

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
});
