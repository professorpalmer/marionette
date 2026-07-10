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

function browserClientHintHeaders(versionsChrome, platform) {
  const chrome = versionsChrome || "130.0.0.0";
  const major = String(chrome).split(".")[0] || "130";
  let plat = "Windows";
  if (platform === "darwin") plat = "macOS";
  else if (platform === "linux") plat = "Linux";
  return {
    "User-Agent": browserUserAgent(chrome, platform),
    "Sec-CH-UA": `"Not_A Brand";v="8", "Chromium";v="${major}", "Google Chrome";v="${major}"`,
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": `"${plat}"`,
  };
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

  it("Client Hints major version matches the UA Chromium build on Windows", () => {
    const chrome = "130.0.6723.191";
    const ua = browserUserAgent(chrome, "win32");
    const hints = browserClientHintHeaders(chrome, "win32");
    assert.match(ua, /Chrome\/130\.0\.6723\.191/);
    assert.match(hints["Sec-CH-UA"], /Chromium";v="130"/);
    assert.match(hints["Sec-CH-UA"], /Google Chrome";v="130"/);
    assert.equal(hints["Sec-CH-UA-Platform"], `"Windows"`);
    assert.equal(hints["Sec-CH-UA-Mobile"], "?0");
    assert.equal(hints["User-Agent"], ua);
  });

  it("main.cjs wires AutomationControlled off and browser-preload", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /disable-blink-features.*AutomationControlled/);
    assert.match(main, /browser-preload\.cjs/);
    assert.match(main, /process\.versions\.chrome/);
    assert.ok(fs.existsSync(path.join(__dirname, "browser-preload.cjs")));
  });

  it("main.cjs aligns Sec-CH-UA Client Hints on persist:browser", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /browserClientHintHeaders/);
    assert.match(main, /Sec-CH-UA/);
    assert.match(main, /onBeforeSendHeaders/);
    assert.match(main, /persist:browser/);
    assert.match(main, /setUserAgent\([^,]+,\s*"en-US,en"\)/);
  });

  it("main.cjs keeps trusted preload on will-attach-webview and OAuth popups", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /will-attach-webview/);
    assert.match(main, /browserPreloadPath/);
    assert.match(main, /did-create-window/);
    assert.match(main, /wireBrowserContentsAutomation/);
    assert.match(main, /overrideBrowserWindowOptions[\s\S]*preload:\s*browserPreloadPath\(\)/);
    assert.match(main, /function openPopoutWindow[\s\S]*preload:\s*browserPreloadPath\(\)/);
    assert.match(main, /browser:openExternal/);
  });

  it("main.cjs logs fingerprint diagnostics to electron.log", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /\[browser\] fingerprint/);
    assert.match(main, /process\.versions\.electron/);
    assert.match(main, /AutomationControlled=disabled/);
    assert.match(main, /will-attach-webview preload=/);
    assert.match(main, /client-hints aligned/);
  });

  it("browser-preload.cjs patches chrome/plugins/languages/mimeTypes early", () => {
    const preload = fs.readFileSync(
      path.join(__dirname, "browser-preload.cjs"),
      "utf8"
    );
    assert.match(preload, /webFrame\.executeJavaScript/);
    assert.match(preload, /navigator,\s*"webdriver"/);
    assert.match(preload, /window\.chrome/);
    assert.match(preload, /navigator,\s*"plugins"/);
    assert.match(preload, /navigator,\s*"languages"/);
    assert.match(preload, /navigator,\s*"mimeTypes"/);
    assert.match(preload, /userAgentData/);
    assert.match(preload, /__pmAutomationHidden/);
  });

  it("main.cjs never injects unsolicited high-entropy Client Hints", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    // High-entropy hints must only OVERWRITE headers Chromium already sends
    // (post Accept-CH). Unsolicited injection is itself a bot signal.
    assert.match(main, /hints\.low/);
    assert.match(main, /hints\.high/);
    assert.match(main, /if \(existing\) headers\[existing\] = v;/);
    // Grease version pair must be consistent between low/high entropy lists.
    assert.match(main, /"Not_A Brand";v="8\.0\.0\.0"/);
    assert.doesNotMatch(main, /"Not_A Brand";v="10\.0\.0\.0"/);
  });

  it("browser-preload.cjs overrides userAgentData when Google Chrome brand missing", () => {
    const preload = fs.readFileSync(
      path.join(__dirname, "browser-preload.cjs"),
      "utf8"
    );
    // Electron 33 ships non-empty Chromium-only brands; headers claim Google
    // Chrome, so navigator.userAgentData must agree or Google rejects.
    assert.match(preload, /b\.brand === "Google Chrome"/);
    assert.match(preload, /version: "8\.0\.0\.0"/);
    assert.doesNotMatch(preload, /version: "10\.0\.0\.0"/);
  });

  it("renderer preload exposes openExternal escape hatch", () => {
    const preload = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
    assert.match(preload, /openExternal/);
    assert.match(preload, /browser:openExternal/);
  });
});
