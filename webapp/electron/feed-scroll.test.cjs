const test = require("node:test");
const assert = require("node:assert/strict");

const { scrollToFeedEnd } = require("./feed-scroll.cjs");

/**
 * Electron-side feed scroll contract (jsdom layout tests live in feedScroll.test.ts).
 * Repro harness: pinned tail + growth should land at scrollHeight - clientHeight.
 */
test("scrollToEnd contract matches Marionette feedScroll helper", () => {
  assert.equal(scrollToFeedEnd(2000, 400), 1600);
  assert.equal(scrollToFeedEnd(350, 400), 0);
});

test("stream-to-fold live tail growth outside the virtual window still pins to the true end", () => {
  const client = 400;
  const virtualHeadHeight = 1600;
  const initialTail = 80;
  const pinnedTop = scrollToFeedEnd(virtualHeadHeight + initialTail, client);
  const grownTail = 320;
  const nextTop = scrollToFeedEnd(virtualHeadHeight + grownTail, client);
  assert.equal(nextTop, pinnedTop + (grownTail - initialTail));
});

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

function electronBin() {
  const resolved = require("electron");
  if (typeof resolved === "string" && fs.existsSync(resolved)) return resolved;
  return "electron";
}

test("Electron VM: stream-to-fold live tail does not lurch", () => {
  const script = path.join(__dirname, "feed-lurch-vm.cjs");
  const bin = electronBin();
  const args = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    script,
  ];
  const result = spawnSync(bin, args, {
    encoding: "utf8",
    env: { ...process.env, ELECTRON_NO_ATTACH_CONSOLE: "1" },
    timeout: 30000,
  });
  if (result.error || result.status !== 0) {
    const detail = [result.error && result.error.message, result.stderr, result.stdout]
      .filter(Boolean)
      .join("\n");
    assert.equal(result.status, 0, detail);
  }
  const payload = JSON.parse((result.stdout || "").trim().split("\n").pop());
  assert.equal(payload.ok, true, JSON.stringify(payload));
  assert.equal(payload.lurches, 0);
});
