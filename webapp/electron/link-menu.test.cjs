const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { isAllowedExternalUrl } = require("./browser-url.cjs");

// Objects from a vm context have a foreign prototype. Copy them before deepEqual.
const plain = (value) => JSON.parse(JSON.stringify(value));

function fakeContents() {
  const listeners = new Map();
  const sent = [];
  return {
    sent,
    on: (name, fn) => listeners.set(name, fn),
    emit: (name, ...args) => listeners.get(name)(...args),
    isDestroyed: () => false,
    send: (...args) => sent.push(args),
    replaceMisspelling: (word) => sent.push(["replaceMisspelling", word]),
    session: { addWordToSpellCheckerDictionary: (word) => sent.push(["addWord", word]) },
  };
}

// Load the context-menu and close-tab wiring from main.cjs with fake Electron parts.
function loadMenuWiring({ platform = "darwin" } = {}) {
  const source = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  const mainContents = fakeContents();
  const menus = [];
  const calls = [];
  const handlers = new Map();
  const ctx = vm.createContext({
    process: { platform },
    win: { isDestroyed: () => false, webContents: mainContents, close: () => calls.push(["win.close"]) },
    ipcMain: { handle: (channel, fn) => handlers.set(channel, fn) },
    isAllowedSender: (event) => Boolean(event && event.allowed),
    BrowserWindow: { fromWebContents: () => null },
    Menu: {
      buildFromTemplate: (template) => {
        menus.push(template);
        return { popup: () => calls.push(["popup"]) };
      },
    },
    shell: { openExternal: (url) => { calls.push(["openExternal", url]); return Promise.resolve(); } },
    clipboard: { writeText: (text) => calls.push(["writeText", text]) },
    isAllowedExternalUrl,
    logMain: (msg) => calls.push(["log", msg]),
  });
  vm.runInContext(
    source.slice(source.indexOf("const MAX_SPELLCHECK_WORD = 128;"), source.indexOf("function createWindow()")),
    ctx,
  );
  // The IPC handlers for openExternal, context menus and window close sit together.
  vm.runInContext(
    source.slice(source.indexOf('ipcMain.handle("browser:openExternal"'), source.indexOf("\n\napp.whenReady()")),
    ctx,
  );
  return { ctx, mainContents, menus, calls, handlers };
}

describe("link context menu and Cmd+W intercept", () => {
  it("a link right-click pops one menu whose items open, open in app, and copy the URL", () => {
    const { ctx, mainContents, menus, calls } = loadMenuWiring();
    const guest = fakeContents();
    ctx.wireContextMenu(guest);
    const url = "https://example.com/a?b=1";

    guest.emit("context-menu", {}, { linkURL: "javascript:alert(1)" });
    guest.emit("context-menu", {}, { linkURL: "" });
    assert.equal(menus.length, 0);

    guest.emit("context-menu", {}, { linkURL: url });
    assert.equal(menus.length, 1);
    const items = menus[0];
    assert.deepEqual(Array.from(items, (i) => i.label), ["Open in system browser", "Open in-app browser", "Copy link"]);
    for (const item of items) item.click();
    assert.deepEqual(plain(calls), [["popup"], ["openExternal", url], ["writeText", url]]);
    assert.deepEqual(plain(mainContents.sent), [["browser:openInApp", url]]);
  });

  it("an editable right-click goes to the renderer in the main window and to a native spelling menu elsewhere", () => {
    const { ctx, mainContents, menus } = loadMenuWiring();
    const params = {
      isEditable: true,
      x: 10,
      y: 20,
      misspelledWord: "teh",
      dictionarySuggestions: ["the", "ten", "tech", "tea", "tee", "toe"],
    };

    ctx.wireContextMenu(mainContents);
    mainContents.emit("context-menu", {}, params);
    assert.equal(menus.length, 0);
    assert.deepEqual(plain(mainContents.sent), [
      ["context-menu:open", { x: 10, y: 20, misspelledWord: "teh", suggestions: params.dictionarySuggestions }],
    ]);

    const guest = fakeContents();
    ctx.wireContextMenu(guest);
    guest.emit("context-menu", {}, params);
    assert.equal(menus.length, 1);
    const items = menus[0];
    assert.deepEqual(
      Array.from(items, (i) => i.label || i.role || i.type),
      ["the", "ten", "tech", "tea", "tee", "Add to dictionary", "separator", "cut", "copy", "paste", "separator", "selectAll"],
    );
    items[0].click();
    items[5].click();
    assert.deepEqual(plain(guest.sent), [["replaceMisspelling", "the"], ["addWord", "teh"]]);

    guest.emit("context-menu", {}, { ...params, misspelledWord: "x".repeat(129) });
    assert.deepEqual(Array.from(menus[1], (i) => i.role || i.type), ["cut", "copy", "paste", "separator", "selectAll"]);
  });

  it("context-menu IPC handlers pop the stored native menu once, edit, spellcheck and close the window", async () => {
    const { ctx, mainContents, menus, calls, handlers } = loadMenuWiring();
    const edits = [];
    mainContents.copy = () => edits.push("copy");
    mainContents.cut = () => edits.push("cut");
    mainContents.paste = () => edits.push("paste");
    const event = { sender: mainContents };

    await handlers.get("context-menu:native")(event);
    assert.equal(menus.length, 0);
    ctx.wireContextMenu(mainContents);
    mainContents.emit("context-menu", {}, { isEditable: true, x: 1, y: 2, misspelledWord: "teh", dictionarySuggestions: ["the"] });
    await handlers.get("context-menu:native")(event);
    await handlers.get("context-menu:native")(event);
    assert.equal(menus.length, 1);
    assert.deepEqual(Array.from(menus[0], (i) => i.label || i.role || i.type),
      ["the", "Add to dictionary", "separator", "cut", "copy", "paste", "separator", "selectAll"]);

    for (const command of ["copy", "cut", "paste", "delete"]) await handlers.get("context-menu:edit")(event, command);
    assert.deepEqual(edits, ["copy", "cut", "paste"]);

    mainContents.sent.length = 0;
    await handlers.get("context-menu:spellcheck")(event, { kind: "replace", word: " the " });
    await handlers.get("context-menu:spellcheck")(event, { kind: "add", word: "teh" });
    await handlers.get("context-menu:spellcheck")(event, { kind: "add", word: "x".repeat(129) });
    assert.deepEqual(plain(mainContents.sent), [["replaceMisspelling", "the"], ["addWord", "teh"]]);

    assert.deepEqual(plain(await handlers.get("window:close")()), { ok: true });
    assert.deepEqual(plain(calls.filter((c) => c[0] === "win.close")), [["win.close"]]);
  });

  it("browser:openExternal opens only http(s) or mailto URLs from the trusted sender", async () => {
    const { handlers, calls } = loadMenuWiring();
    const open = handlers.get("browser:openExternal");
    assert.deepEqual(plain(await open({}, "https://example.com/")), { ok: false, error: "untrusted sender" });
    assert.deepEqual(plain(await open({ allowed: true }, "file:///etc/passwd")),
      { ok: false, error: "only http(s) or mailto URLs allowed" });
    assert.deepEqual(plain(await open({ allowed: true }, "javascript:alert(1)")),
      { ok: false, error: "only http(s) or mailto URLs allowed" });
    assert.deepEqual(plain(await open({ allowed: true }, " https://example.com/a ")), { ok: true });
    assert.deepEqual(plain(calls.filter((c) => c[0] === "openExternal")), [["openExternal", "https://example.com/a"]]);
  });

  it("Cmd+W on macOS and Ctrl+W elsewhere close the tab, not the window", () => {
    for (const [platform, mod] of [["darwin", "meta"], ["win32", "control"]]) {
      const { ctx, mainContents } = loadMenuWiring({ platform });
      const guest = fakeContents();
      ctx.wireCloseTabShortcut(guest);
      const press = (input) => {
        let prevented = 0;
        guest.emit("before-input-event", { preventDefault: () => { prevented += 1; } }, input);
        return prevented;
      };

      assert.equal(press({ type: "keyDown", [mod]: true, key: "W" }), 1, platform);
      assert.equal(press({ type: "keyUp", [mod]: true, key: "w" }), 0, platform);
      assert.equal(press({ type: "keyDown", [mod]: true, shift: true, key: "w" }), 0, platform);
      assert.equal(press({ type: "keyDown", [mod]: true, key: "q" }), 0, platform);
      assert.equal(press({ type: "keyDown", key: "w" }), 0, platform);
      assert.deepEqual(plain(mainContents.sent), [["app:closeTab"]], platform);
    }
  });

  it("preload close-tab and in-app open listeners receive events and unsubscribe", () => {
    const preload = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
    const listeners = new Map();
    let exposed = null;
    const invokes = [];
    const ipcRenderer = {
      invoke: (...args) => { invokes.push(args); return Promise.resolve(); },
      send: () => {},
      on: (channel, fn) => listeners.set(channel, fn),
      once: () => {},
      removeListener: (channel, fn) => { if (listeners.get(channel) === fn) listeners.delete(channel); },
    };
    vm.runInNewContext(preload, {
      require: () => ({
        contextBridge: { exposeInMainWorld: (name, api) => { if (name === "harnessIPC") exposed = api; } },
        ipcRenderer,
        webUtils: { getPathForFile: () => "" },
      }),
      process: { env: {} },
      Uint8Array,
    });
    const got = [];
    const offClose = exposed.onCloseTab(() => got.push("close"));
    const offOpen = exposed.onOpenInApp((url) => got.push(url));
    listeners.get("app:closeTab")({});
    listeners.get("browser:openInApp")({}, "https://example.com/");
    assert.deepEqual(got, ["close", "https://example.com/"]);
    offClose();
    offOpen();
    assert.equal(listeners.has("app:closeTab"), false);
    assert.equal(listeners.has("browser:openInApp"), false);

    const offMenu = exposed.onContextMenuOpen((payload) => got.push(payload));
    listeners.get("context-menu:open")({}, { x: 1 });
    assert.deepEqual(got[2], { x: 1 });
    offMenu();
    assert.equal(listeners.has("context-menu:open"), false);

    exposed.closeWindow();
    exposed.contextMenuEdit("copy");
    exposed.contextMenuSpellcheck({ kind: "add", word: "teh" });
    exposed.contextMenuNative();
    assert.deepEqual(invokes, [
      ["window:close"],
      ["context-menu:edit", "copy"],
      ["context-menu:spellcheck", { kind: "add", word: "teh" }],
      ["context-menu:native"],
    ]);
  });
});
