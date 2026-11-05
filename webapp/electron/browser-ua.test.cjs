"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");

const MAIN = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
const CHROME = "130.0.6723.191";
const WIN_UA =
  `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ` +
  `(KHTML, like Gecko) Chrome/${CHROME} Safari/537.36`;

// Objects from a vm context have a foreign prototype. Copy them before deepEqual.
const plain = (value) => JSON.parse(JSON.stringify(value));

function between(start, end) {
  const from = MAIN.indexOf(start);
  const to = MAIN.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `main.cjs slice ${start} .. ${end}`);
  return MAIN.slice(from, to);
}

// Load the UA, Client Hints and persist:browser session code from main.cjs.
function loadBrowserSession({ chrome = CHROME, platform = "win32" } = {}) {
  const partitions = [];
  const userAgents = [];
  let beforeSendHeaders = null;
  const ses = {
    setUserAgent: (...args) => userAgents.push(args),
    webRequest: { onBeforeSendHeaders: (fn) => { beforeSendHeaders = fn; } },
  };
  const ctx = vm.createContext({
    process: { versions: { chrome, electron: "33.0.0" }, platform },
    path,
    __dirname,
    session: { fromPartition: (name) => { partitions.push(name); return ses; } },
    logMain: () => {},
  });
  vm.runInContext(between("function browserUserAgent()", "function applyChromeFingerprint("), ctx);
  vm.runInContext(between("function configureBrowserSession()", "function _dbg2("), ctx);
  return {
    ctx,
    partitions,
    userAgents,
    send(requestHeaders) {
      let result = null;
      beforeSendHeaders({ requestHeaders }, (r) => { result = r; });
      return plain(result);
    },
  };
}

// Run the page-world script that browser-preload.cjs injects, against a fake navigator.
async function runBrowserPreload({ userAgentData }) {
  let stealth = null;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "browser-preload.cjs"), "utf8"), {
    require: () => ({ webFrame: { executeJavaScript: (code) => { stealth = code; return Promise.resolve(); } } }),
  });
  assert.equal(typeof stealth, "string");
  const page = vm.createContext({ existingUaData: userAgentData });
  vm.runInContext(`
    globalThis.window = globalThis;
    globalThis.Navigator = class Navigator {};
    Object.defineProperty(Navigator.prototype, "userAgentData", {
      get: () => existingUaData, configurable: true,
    });
    globalThis.navigator = new Navigator();
    navigator.userAgent = ${JSON.stringify(WIN_UA)};
    navigator.webdriver = true;
    navigator.plugins = [];
    navigator.languages = [];
    navigator.mimeTypes = [];
  `, page);
  vm.runInContext(stealth, page);
  return JSON.parse(await vm.runInContext(`
    navigator.userAgentData.getHighEntropyValues(["fullVersionList", "bogus"]).then((high) => JSON.stringify({
      webdriver: navigator.webdriver === undefined ? "undefined" : navigator.webdriver,
      plugins: Array.from(navigator.plugins, (p) => p.name),
      languages: Array.from(navigator.languages),
      mimeTypes: Array.from(navigator.mimeTypes, (m) => m.type),
      chrome: [typeof window.chrome.runtime, typeof window.chrome.loadTimes, typeof window.chrome.csi],
      hidden: window.__pmAutomationHidden,
      uaData: { brands: navigator.userAgentData.brands, platform: navigator.userAgentData.platform },
      high,
    }))
  `, page));
}

describe("browserUserAgent", () => {
  it("embeds the real Chromium version, not a hardcoded newer Chrome", () => {
    const { ctx } = loadBrowserSession();
    assert.equal(ctx.browserUserAgent(), WIN_UA);
  });

  it("uses a Mac platform token on darwin", () => {
    const { ctx } = loadBrowserSession({ chrome: "131.0.1.2", platform: "darwin" });
    assert.equal(
      ctx.browserUserAgent(),
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/131.0.1.2 Safari/537.36",
    );
  });

  it("Client Hints major version matches the UA Chromium build on Windows", () => {
    const { ctx } = loadBrowserSession();
    assert.deepEqual(plain(ctx.browserClientHintHeaders().low), {
      "User-Agent": WIN_UA,
      "Sec-CH-UA": `"Not_A Brand";v="8", "Chromium";v="130", "Google Chrome";v="130"`,
      "Sec-CH-UA-Mobile": "?0",
      "Sec-CH-UA-Platform": `"Windows"`,
    });
  });

  it("main.cjs wires AutomationControlled off and browser-preload", () => {
    // The switch runs at module load. No seam exists, so check the source line.
    assert.match(MAIN, /appendSwitch\("disable-blink-features", "AutomationControlled"\)/);
    const { ctx } = loadBrowserSession();
    assert.equal(ctx.browserPreloadPath(), path.join(__dirname, "browser-preload.cjs"));
    assert.ok(fs.existsSync(ctx.browserPreloadPath()));
  });

  it("main.cjs aligns Sec-CH-UA Client Hints on persist:browser", () => {
    const session = loadBrowserSession();
    session.ctx.configureBrowserSession();
    assert.deepEqual(session.partitions, ["persist:browser"]);
    assert.deepEqual(plain(session.userAgents), [[WIN_UA, "en-US,en"]]);
    assert.deepEqual(
      session.send({ "user-agent": "Electron/33", "sec-ch-ua": `"Chromium";v="130"`, Accept: "*/*" }),
      {
        cancel: false,
        requestHeaders: {
          "user-agent": WIN_UA,
          "sec-ch-ua": `"Not_A Brand";v="8", "Chromium";v="130", "Google Chrome";v="130"`,
          Accept: "*/*",
          "Sec-CH-UA-Mobile": "?0",
          "Sec-CH-UA-Platform": `"Windows"`,
        },
      },
    );
  });

  it("main.cjs keeps trusted preload on will-attach-webview and OAuth popups", () => {
    const handlerStart = MAIN.indexOf('win.webContents.on("will-attach-webview", ') +
      'win.webContents.on("will-attach-webview", '.length;
    const handler = MAIN.slice(handlerStart, MAIN.indexOf("\n  });", handlerStart) + 4);
    const { ctx } = loadBrowserSession();
    const attach = vm.runInContext(`(${handler})`, ctx);
    const prefs = { preload: "/attacker/preload.js", nodeIntegration: true, contextIsolation: false };
    attach({}, prefs);
    assert.deepEqual(prefs, {
      preload: path.join(__dirname, "browser-preload.cjs"),
      nodeIntegration: false,
      contextIsolation: true,
      partition: "persist:browser",
    });
    const kept = { partition: "persist:other" };
    attach({}, kept);
    assert.equal(kept.partition, "persist:other");
    // Popup options live inside a large handler with no seam. Check the source.
    assert.match(MAIN, /overrideBrowserWindowOptions[\s\S]*preload:\s*browserPreloadPath\(\)/);
    assert.match(MAIN, /function openPopoutWindow[\s\S]*preload:\s*browserPreloadPath\(\)/);
    assert.match(MAIN, /sandbox:\s*true/);
    assert.match(MAIN, /hardenWebPreferences/);
    assert.match(MAIN, /webviewTag:\s*true/);
    assert.match(MAIN, /preload:\s*path\.join\(__dirname,\s*"preload\.cjs"\)/);
    assert.match(
      MAIN,
      /contents\.on\("did-create-window"[\s\S]*?wireBrowserContentsAutomation\(childWindow\.webContents\)/,
    );
  });

  it("wireBrowserContentsAutomation sets the Chrome UA once and hides automation on load", () => {
    const { ctx } = loadBrowserSession();
    vm.runInContext(between("function applyChromeFingerprint(", "// Configure the in-app browser's PERSISTENT"), ctx);
    const events = [];
    const contents = {
      isDestroyed: () => false,
      setUserAgent: (ua) => events.push(["ua", ua]),
      on: (name) => events.push(["on", name]),
    };
    ctx.wireBrowserContentsAutomation(contents);
    ctx.wireBrowserContentsAutomation(contents);
    assert.deepEqual(events, [["ua", WIN_UA], ["on", "dom-ready"], ["on", "did-finish-load"]]);
  });

  it("browser-preload.cjs patches chrome/plugins/languages/mimeTypes early", async () => {
    const out = await runBrowserPreload({ userAgentData: undefined });
    assert.equal(out.webdriver, "undefined");
    assert.deepEqual(out.plugins, ["Chrome PDF Plugin", "Chrome PDF Viewer", "Native Client"]);
    assert.deepEqual(out.languages, ["en-US", "en"]);
    assert.deepEqual(out.mimeTypes, ["application/pdf", "application/x-google-chrome-pdf"]);
    assert.deepEqual(out.chrome, ["object", "function", "function"]);
    assert.equal(out.hidden, true);
  });

  it("main.cjs never injects unsolicited high-entropy Client Hints", () => {
    const session = loadBrowserSession();
    session.ctx.configureBrowserSession();
    const unsolicited = session.send({ Accept: "*/*" }).requestHeaders;
    assert.deepEqual(Object.keys(unsolicited).sort(), [
      "Accept", "Sec-CH-UA", "Sec-CH-UA-Mobile", "Sec-CH-UA-Platform", "User-Agent",
    ]);
    const optedIn = session.send({ "sec-ch-ua-full-version-list": "old", "Sec-CH-UA-Arch": `"arm"` }).requestHeaders;
    assert.equal(
      optedIn["sec-ch-ua-full-version-list"],
      `"Not_A Brand";v="8.0.0.0", "Chromium";v="${CHROME}", "Google Chrome";v="${CHROME}"`,
    );
    assert.equal(optedIn["Sec-CH-UA-Arch"], `"x86"`);
    assert.equal("Sec-CH-UA-Full-Version" in optedIn, false);
    assert.equal("Sec-CH-UA-Bitness" in optedIn, false);
    assert.equal("Sec-CH-UA-Full-Version-List" in optedIn, false);
  });

  it("browser-preload.cjs overrides userAgentData when Google Chrome brand missing", async () => {
    const chromiumOnly = await runBrowserPreload({
      userAgentData: { brands: [{ brand: "Chromium", version: "130" }], mobile: false, platform: "Windows" },
    });
    assert.deepEqual(chromiumOnly.uaData, {
      brands: [
        { brand: "Not_A Brand", version: "8" },
        { brand: "Chromium", version: "130" },
        { brand: "Google Chrome", version: "130" },
      ],
      platform: "Windows",
    });
    assert.deepEqual(chromiumOnly.high.fullVersionList, [
      { brand: "Not_A Brand", version: "8.0.0.0" },
      { brand: "Chromium", version: CHROME },
      { brand: "Google Chrome", version: CHROME },
    ]);
    assert.equal("bogus" in chromiumOnly.high, false);

    const real = { brands: [{ brand: "Google Chrome", version: "130" }], platform: "kept", getHighEntropyValues: async () => ({}) };
    const untouched = await runBrowserPreload({ userAgentData: real });
    assert.deepEqual(untouched.uaData, { brands: [{ brand: "Google Chrome", version: "130" }], platform: "kept" });
  });

  it("renderer preload exposes openExternal escape hatch", async () => {
    const invokes = [];
    let exposed = null;
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8"), {
      require: () => ({
        contextBridge: { exposeInMainWorld: (name, api) => { if (name === "harnessIPC") exposed = api; } },
        ipcRenderer: {
          invoke: (channel, value) => { invokes.push([channel, value]); return Promise.resolve({ ok: true }); },
          send: () => {},
          on: () => {},
          once: () => {},
          removeListener: () => {},
        },
        webUtils: { getPathForFile: () => "" },
      }),
      process: { env: {} },
      Uint8Array,
    });
    assert.deepEqual(await exposed.openExternal("https://accounts.google.com/"), { ok: true });
    assert.deepEqual(invokes, [["browser:openExternal", "https://accounts.google.com/"]]);
  });
});
