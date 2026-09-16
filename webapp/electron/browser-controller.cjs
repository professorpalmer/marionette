"use strict";
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const WORLD = 1001;

// Private isolated-world functions: no model-supplied JavaScript or public eval.
function readPage(prefix) {
  const refs = new Map(), rows = [];
  for (const element of document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"],[contenteditable="true"]')) {
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height || getComputedStyle(element).visibility === "hidden") continue;
    const ref = prefix + (rows.length + 1);
    refs.set(ref, element);
    const label = element.getAttribute("aria-label") || element.labels?.[0]?.innerText || element.innerText || element.getAttribute("placeholder") || "";
    rows.push(ref + " " + (element.getAttribute("role") || element.tagName.toLowerCase()) + " " + label.trim().slice(0, 160));
    if (rows.length === 120) break;
  }
  globalThis.__marionetteRefs = { url: location.href, refs };
  return { rows, text: (document.body?.innerText || "").slice(0, 12000) };
}

function locateElement(ref, typing) {
  const book = globalThis.__marionetteRefs;
  const element = book?.url === location.href && book.refs.get(ref);
  if (!element || !element.isConnected) throw new Error("Stale browser ref; take a new snapshot.");
  if (element.disabled || element.readOnly) throw new Error("Element is disabled or read-only.");
  if (typing && !(element.matches('input:not([type="file"]),textarea') || element.isContentEditable)) throw new Error("Element is not a text field.");
  element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  const rect = element.getBoundingClientRect();
  const x = Math.floor(rect.left + rect.width / 2), y = Math.floor(rect.top + rect.height / 2);
  const hit = document.elementFromPoint(x, y);
  if (!rect.width || !rect.height || !hit || !(hit === element || element.contains(hit))) throw new Error("Element is hidden or covered.");
  return { x, y };
}

function focusTextField(ref) {
  const book = globalThis.__marionetteRefs;
  const element = book?.url === location.href && book.refs.get(ref);
  if (!element?.isConnected) throw new Error("Stale browser ref; take a new snapshot.");
  element.focus();
  if (document.activeElement !== element) throw new Error("Browser field did not accept focus.");
}

function createBrowserController({ resolveGuest, activateTab, focus, screenshotDir }) {
  let context = { sessionId: "", activeTabId: "", tabs: new Map(), revision: 0 };
  let busy = false;
  const screenshots = [];
  function pruneScreenshots(keep = 0) {
    while (screenshots.length > keep) { try { fs.unlinkSync(screenshots.shift()); } catch { /* Already removed. */ } }
  }
  function clear() {
    pruneScreenshots();
    for (const tab of context.tabs.values()) tab.dispose();
    context = { sessionId: "", activeTabId: "", tabs: new Map(), revision: context.revision + 1 };
  }
  function setContext(payload) {
    if (!payload || typeof payload.sessionId !== "string" || typeof payload.activeTabId !== "string"
        || !Array.isArray(payload.tabs) || payload.tabs.length > 100) throw new Error("Invalid browser context.");
    const guests = new Map();
    for (const row of payload.tabs) {
      if (!row || typeof row.tabId !== "string" || !row.tabId || guests.has(row.tabId) || !Number.isSafeInteger(row.webContentsId)) throw new Error("Invalid browser tab.");
      const contents = resolveGuest(row.webContentsId);
      if (!contents || contents.isDestroyed()) throw new Error("Browser tab is not owned by this window.");
      guests.set(row.tabId, contents);
    }
    if (context.sessionId !== payload.sessionId) pruneScreenshots();
    const next = new Map();
    for (const [id, contents] of guests) {
      const previous = context.tabs.get(id);
      if (previous?.contents === contents) next.set(id, previous);
      else {
        const tab = { contents, refs: new Set(), epoch: 0, observation: null };
        const invalidate = () => { tab.epoch++; tab.refs.clear(); tab.observation = null; };
        const navigate = (_event, _url, _inPlace, isMainFrame) => { if (isMainFrame) invalidate(); };
        contents.on("did-start-navigation", navigate);
        contents.on("did-navigate-in-page", invalidate);
        contents.on("destroyed", invalidate);
        tab.dispose = () => {
          invalidate();
          contents.removeListener("did-start-navigation", navigate);
          contents.removeListener("did-navigate-in-page", invalidate);
          contents.removeListener("destroyed", invalidate);
        };
        next.set(id, tab);
      }
    }
    const changed = context.sessionId !== payload.sessionId || context.activeTabId !== payload.activeTabId
      || next.size !== context.tabs.size || [...next].some(([id, tab]) => context.tabs.get(id) !== tab);
    for (const [id, tab] of context.tabs) if (next.get(id) !== tab) tab.dispose();
    if (changed) for (const tab of next.values()) { tab.refs.clear(); tab.observation = null; }
    context = { sessionId: payload.sessionId, activeTabId: payload.activeTabId, tabs: next, revision: context.revision + Number(changed) };
    return { ok: true };
  }
  const script = (contents, fn, ...args) => contents.executeJavaScriptInIsolatedWorld(WORLD, [{ code: "(" + fn.toString() + ")(" + args.map(arg => JSON.stringify(arg)).join(",") + ")" }], true);
  async function run({ session_id: sessionId, action, arguments: args }, signal) {
    if (!sessionId || sessionId !== context.sessionId) throw new Error("Browser is not attached to this active conversation. Select it and open the Browser pane.");
    if (action === "tabs") return [...context.tabs].filter(([, tab]) => !tab.contents.isDestroyed()).map(([id, tab]) => ({ tab_id: id, active: id === context.activeTabId, url: tab.contents.getURL(), title: tab.contents.getTitle() }));
    if (action === "tab_activate") {
      const tab = context.tabs.get(args.tab_id);
      if (!tab || tab.contents.isDestroyed()) throw new Error("Unknown or closed browser tab.");
      if (context.activeTabId !== args.tab_id) {
        activateTab({ sessionId, tabId: args.tab_id });
        while (context.sessionId === sessionId && context.activeTabId !== args.tab_id && !signal.aborted) await new Promise(resolve => setTimeout(resolve, 10));
      }
      if (signal.aborted || context.sessionId !== sessionId || context.tabs.get(args.tab_id) !== tab) throw new Error("Browser tab activation cancelled.");
      return "Activated " + args.tab_id;
    }
    const tab = context.tabs.get(context.activeTabId);
    if (!tab || tab.contents.isDestroyed()) throw new Error("Open the Browser pane and wait for the selected tab to load.");
    const contents = tab.contents, revision = context.revision, epoch = tab.epoch;
    const current = (navigation = false) => {
      if (signal.aborted || contents.isDestroyed() || context.revision !== revision || (!navigation && tab.epoch !== epoch)) throw new Error("Browser target changed or request expired; take a new snapshot.");
    };
    current();
    if (action === "navigate") {
      const url = new URL(args.url);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Only HTTP(S) browser URLs without credentials are allowed.");
      tab.refs.clear();
      await contents.loadURL(url.href);
      current(true);
      return "Navigated to " + contents.getURL() + ". Take a new browser_snapshot.";
    }
    if (action === "back") {
      if (!contents.navigationHistory.canGoBack()) return "No previous page.";
      tab.refs.clear();
      contents.navigationHistory.goBack();
      return "Back navigation started. Take a new browser_snapshot after loading.";
    }
    if (contents.isLoadingMainFrame()) throw new Error("Browser page is still loading; retry the observation shortly.");
    if (action === "snapshot") {
      const prefix = "@" + crypto.randomBytes(6).toString("hex") + "-e";
      const page = await script(contents, readPage, prefix);
      current();
      tab.refs = new Set(page.rows.map(row => row.split(" ")[0]));
      tab.observation = { id: crypto.randomUUID(), visual: false };
      return "Snapshot: " + tab.observation.id + "\nTab: " + context.activeTabId + "\nPage: " + contents.getTitle() + "\nURL: " + contents.getURL() + "\n" + page.rows.join("\n") + "\n\n" + page.text + "\n(Main document only; use screenshot for embedded frames or canvas.)";
    }
    if (action === "get_text") {
      const text = await script(contents, () => (document.body?.innerText || "").slice(0, 20000));
      current();
      return text;
    }
    if (action === "screenshot") {
      const image = await contents.capturePage();
      current();
      const viewport = await script(contents, () => ({ width: innerWidth, height: innerHeight }));
      current();
      fs.mkdirSync(screenshotDir, { recursive: true });
      const file = path.join(screenshotDir, "browser-" + crypto.randomUUID() + ".png");
      fs.writeFileSync(file, image.toPNG());
      screenshots.push(file);
      pruneScreenshots(8);
      const size = image.getSize();
      tab.observation = { id: crypto.randomUUID(), visual: true, ...size, viewport };
      return { screenshot_path: file, snapshot_id: tab.observation.id, ...size, coordinates: "image pixels relative to this browser viewport" };
    }
    if (action === "input") {
      const observation = tab.observation;
      if (!observation || args.snapshot_id !== observation.id) throw new Error("Stale browser snapshot; take a new snapshot or screenshot.");
      const operation = args.operation;
      if (!["click", "drag", "keypress"].includes(operation)) throw new Error("Unknown browser input operation.");
      let points;
      if (operation !== "keypress") {
        if (!observation.visual) throw new Error("Coordinate input requires a fresh screenshot.");
        points = [{ x: args.x, y: args.y }];
        if (operation === "drag") points.push({ x: args.to_x, y: args.to_y });
        if (points.some(p => !Number.isFinite(p.x) || !Number.isFinite(p.y) || p.x < 0 || p.y < 0 || p.x >= observation.width || p.y >= observation.height)) throw new Error("Point is outside the observed browser viewport.");
        const viewport = await script(contents, () => ({ width: innerWidth, height: innerHeight }));
        current();
        if (viewport.width !== observation.viewport.width || viewport.height !== observation.viewport.height) throw new Error("Browser viewport changed; take a new screenshot.");
        points = points.map(p => ({ x: Math.floor(p.x * viewport.width / observation.width), y: Math.floor(p.y * viewport.height / observation.height) }));
      }
      const keys = args.keys;
      const keyNames = new Set(["Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Space"]);
      if (operation === "keypress" && (!Array.isArray(keys) || keys.length < 1 || keys.length > 4
          || keys.slice(0, -1).some(k => !["Control", "Meta", "Alt", "Shift"].includes(k))
          || !(keyNames.has(keys.at(-1)) || /^[a-z0-9]$/i.test(keys.at(-1))))) throw new Error("Invalid browser key combination.");
      if (operation === "keypress" && keys.slice(0, -1).some(k => k === "Meta" || k === "Control")
          && !["a", "c", "v", "x", "z", "y", "f"].includes(keys.at(-1).toLowerCase())) throw new Error("This browser shortcut could affect app chrome; use the browser tools instead.");
      focus(contents);
      current();
      tab.refs.clear();
      tab.observation = null;
      if (operation === "keypress") {
        const event = { keyCode: keys.at(-1), modifiers: keys.slice(0, -1).map(k => k.toLowerCase()) };
        contents.sendInputEvent({ type: "keyDown", ...event });
        contents.sendInputEvent({ type: "keyUp", ...event });
      } else {
        const start = points[0], end = points.at(-1);
        contents.sendInputEvent({ type: "mouseMove", ...start });
        contents.sendInputEvent({ type: "mouseDown", ...start, button: "left", clickCount: 1 });
        if (operation === "drag") contents.sendInputEvent({ type: "mouseMove", ...end, button: "left" });
        contents.sendInputEvent({ type: "mouseUp", ...end, button: "left", clickCount: 1 });
      }
      return "Input sent. Take a fresh snapshot or screenshot to verify the result before deciding another action.";
    }
    if (action === "scroll") {
      if (!["up", "down", "left", "right"].includes(args.direction)) throw new Error("Invalid scroll direction.");
      focus(contents);
      tab.observation = null;
      const horizontal = ["left", "right"].includes(args.direction);
      const delta = ["up", "left"].includes(args.direction) ? 600 : -600;
      contents.sendInputEvent({ type: "mouseWheel", x: 20, y: 20, deltaX: horizontal ? delta : 0, deltaY: horizontal ? 0 : delta, canScroll: true });
      return "Scrolled. Take a new snapshot.";
    }
    if (action === "click" || action === "type") {
      if (!tab.refs.has(args.ref)) throw new Error("Stale or invalid browser ref; take a new snapshot.");
      if (action === "type" && (typeof args.text !== "string" || args.text.length > 10000)) throw new Error("Invalid browser text.");
      const point = await script(contents, locateElement, args.ref, action === "type");
      current();
      focus(contents);
      tab.observation = null;
      contents.sendInputEvent({ type: "mouseDown", ...point, button: "left", clickCount: 1 });
      contents.sendInputEvent({ type: "mouseUp", ...point, button: "left", clickCount: 1 });
      if (action === "type") {
        await script(contents, focusTextField, args.ref);
        current();
        await contents.insertText(args.text);
        current();
      }
      return action === "type" ? "Typed into " + args.ref : "Clicked " + args.ref + ". Take a new snapshot.";
    }
    throw new Error("Unknown browser action.");
  }
  async function dispatch(payload, signal) {
    if (signal.aborted) throw new Error("Browser request cancelled.");
    if (busy) throw new Error("Another browser action is in progress; wait for its result.");
    busy = true;
    let abort;
    try {
      return await Promise.race([run(payload, signal), new Promise((_, reject) => {
        abort = () => reject(new Error("Browser request cancelled."));
        signal.addEventListener("abort", abort, { once: true });
      })]);
    } finally { signal.removeEventListener("abort", abort); busy = false; }
  }
  return { setContext, clear, dispatch, readyForSession: id => context.sessionId === id && context.tabs.has(context.activeTabId) };
}
module.exports = { createBrowserController };
