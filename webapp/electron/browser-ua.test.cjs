"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");

// browserUserAgent lives inside main.cjs (not exported). Mirror the formula
// here so a UA/Chromium mismatch cannot regress silently -- Google OAuth
// rejects when Sec-CH-UA and the UA string disagree.
function browserUserAgent(versionsChrome, platform) {
  const chrome = versionsChrome || "130.0.0.0";
  let platformBit;
  if (platform === "darwin") {
    platformBit = "Macintosh; Intel Mac OS X 10_15_7";
  } else if (platform === "linux") {
    platformBit = "X11; Linux x86_64";
  } else {
    platformBit = "Windows NT 10.0; Win64; x64";
  }
  return (
    `Mozilla/5.0 (${platformBit}) AppleWebKit/537.36 ` +
    `(KHTML, like Gecko) Chrome/${chrome} Safari/537.36`
  );
}

describe("browserUserAgent", () => {
  it("embeds the real Chromium version, not a hardcoded newer Chrome", () => {
    const ua = browserUserAgent("130.0.6723.191", "win32");
    assert.match(ua, /Chrome\/130\.0\.6723\.191/);
    assert.doesNotMatch(ua, /Chrome\/132/);
    assert.match(ua, /Windows NT 10\.0/);
  });

  it("uses a Mac platform token on darwin", () => {
    const ua = browserUserAgent("130.0.0.0", "darwin");
    assert.match(ua, /Macintosh; Intel Mac OS X/);
  });

  it("main.cjs wires AutomationControlled off and browser-preload", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /disable-blink-features.*AutomationControlled/);
    assert.match(main, /browser-preload\.cjs/);
    assert.match(main, /process\.versions\.chrome/);
    assert.ok(fs.existsSync(path.join(__dirname, "browser-preload.cjs")));
  });
});
