const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

describe("v0.9.344 link menu and Cmd+W intercept", () => {
  it("main.cjs builds a linkURL context menu with the three polish items", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /params\.linkURL/);
    assert.match(main, /Open in system browser/);
    assert.match(main, /Open in-app browser/);
    assert.match(main, /Copy link/);
    assert.match(main, /wireContextMenu/);
    assert.match(main, /browser:openInApp/);
  });

  it("main.cjs wires spelling and link items through one context menu", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    const preload = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
    assert.match(main, /wireContextMenu/);
    assert.match(main, /context-menu:open/);
    assert.match(main, /MAX_SPELLCHECK_WORD/);
    assert.match(main, /context-menu:native/);
    assert.match(main, /editableContextMenuTemplate/);
    assert.match(main, /addWordToSpellCheckerDictionary/);
    assert.match(preload, /onContextMenuOpen/);
    assert.match(preload, /contextMenuEdit/);
    assert.match(preload, /contextMenuNative/);
  });

  it("main.cjs intercepts Cmd\/Ctrl+W so Close Window is not first", () => {
    const main = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
    assert.match(main, /wireCloseTabShortcut/);
    assert.match(main, /app:closeTab/);
    assert.match(main, /before-input-event/);
    assert.match(main, /window:close/);
    assert.match(main, /event\.preventDefault\(\)/);
  });

  it("preload exposes close-tab and in-app open listeners", () => {
    const preload = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
    assert.match(preload, /onCloseTab/);
    assert.match(preload, /app:closeTab/);
    assert.match(preload, /onOpenInApp/);
    assert.match(preload, /browser:openInApp/);
    assert.match(preload, /window:close/);
  });
});
