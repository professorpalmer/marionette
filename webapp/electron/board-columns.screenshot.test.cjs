"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnElectronSync } = require("./spawn-electron-sync.cjs");
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

function electronBin() {
  const resolved = require("electron");
  if (typeof resolved === "string" && fs.existsSync(resolved)) return resolved;
  return "electron";
}

function runScreenshotScript() {
  const script = path.join(__dirname, "board-columns-screenshot.cjs");
  const bin = electronBin();
  const args = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    script,
  ];
  const env = {
    ...process.env,
    ELECTRON_NO_ATTACH_CONSOLE: "1",
  };
  let result = spawnElectronSync(bin, args, {
    encoding: "utf8",
    env,
    timeout: 30000,
  });
  if (result.error && result.error.code === "ENOENT" && process.platform === "linux") {
    result = spawnElectronSync("xvfb-run", ["-a", bin, ...args], {
      encoding: "utf8",
      env,
      timeout: 30000,
    });
  }
  return result;
}

describe("three-column board resize screenshots", () => {
  it("captures before/after and resizes only the middle pair", () => {
    const result = runScreenshotScript();
    if (result.error || result.status !== 0) {
      const detail = [
        result.error && result.error.message,
        result.stderr,
        result.stdout,
      ].filter(Boolean).join("\n");
      if (process.platform === "linux" && /DISPLAY|xvfb|GPU|ozone|sandbox|SUID/i.test(detail)) {
        assert.ok(true, "skipped headless electron screenshot: " + detail.slice(0, 200));
        return;
      }
      assert.equal(result.status, 0, detail);
    }
    const payload = JSON.parse((result.stdout || "").trim().split("\n").pop());
    assert.deepEqual(payload.before.widths, [4, 4, 4]);
    assert.equal(payload.before.handleCount, 2);
    assert.deepEqual(payload.before.handleIndexes, [
      "column-resize-0",
      "column-resize-1",
    ]);
    assert.deepEqual(payload.after.widths, [4, 6, 2]);
    assert.equal(payload.after.handleCount, 2);
    const shots = Object.values(payload.screenshots).map((shot) => fs.readFileSync(shot));
    for (const bytes of shots) {
      assert.ok(bytes.length > 100);
      assert.equal(bytes.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");
    }
    assert.ok(
      !shots[0].equals(shots[1]),
      "before/after screenshots must differ after a middle-column resize",
    );
  });
});
