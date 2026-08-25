import { describe, expect, it } from "vitest";
import {
  classifyCloseTabTarget,
  focusedCloseSurface,
  isCloseTabKey,
} from "../lib/closeTabShortcut";

describe("close-tab shortcut classifier", () => {
  it("recognizes Cmd/Ctrl+W and ignores shifted/alted variants", () => {
    expect(isCloseTabKey({ key: "w", metaKey: true, ctrlKey: false })).toBe(true);
    expect(isCloseTabKey({ key: "W", metaKey: false, ctrlKey: true })).toBe(true);
    expect(isCloseTabKey({ key: "w", metaKey: true, ctrlKey: false, shiftKey: true })).toBe(false);
    expect(isCloseTabKey({ key: "w", metaKey: false, ctrlKey: false })).toBe(false);
  });

  it("closes the focused editor tab first", () => {
    expect(
      classifyCloseTabTarget({
        activeEditorTab: "src/App.tsx",
        focusedSelector: "editor",
      }),
    ).toEqual({ kind: "editor", path: "src/App.tsx" });
  });

  it("closes an extra in-app browser tab, or the browser card when it is the last tab", () => {
    expect(
      classifyCloseTabTarget({
        activeEditorTab: "chat",
        focusedSelector: "browser",
        browserTabCount: 3,
      }),
    ).toEqual({ kind: "browser-tab" });
    expect(
      classifyCloseTabTarget({
        activeEditorTab: "chat",
        focusedSelector: "browser",
        browserTabCount: 1,
      }),
    ).toEqual({ kind: "right-card", tab: "browser" });
  });

  it("closes the focused right-pane card", () => {
    expect(
      classifyCloseTabTarget({
        activeEditorTab: "chat",
        focusedSelector: "right-card",
        focusedRightCard: "swarm",
      }),
    ).toEqual({ kind: "right-card", tab: "swarm" });
  });

  it("does not close the window target while a file tab is showing", () => {
    expect(
      classifyCloseTabTarget({
        activeEditorTab: "README.md",
        focusedSelector: "other",
      }),
    ).toEqual({ kind: "editor", path: "README.md" });
  });

  it("returns none when chat is focused and no tab can close", () => {
    expect(
      classifyCloseTabTarget({
        activeEditorTab: "chat",
        focusedSelector: "other",
      }),
    ).toEqual({ kind: "none" });
  });

  it("reads editor / browser / right-card surfaces from the focused node", () => {
    const root = document.createElement("div");
    root.innerHTML = `
      <div data-close-surface="editor" id="ed"></div>
      <div data-close-surface="browser" data-browser-tab-count="2" id="br"></div>
      <section id="right-pane-card-terminal"></section>
    `;
    document.body.appendChild(root);
    expect(focusedCloseSurface(root.querySelector("#ed"))).toEqual({ selector: "editor" });
    expect(focusedCloseSurface(root.querySelector("#br"))).toEqual({
      selector: "browser",
      browserTabCount: 2,
    });
    expect(focusedCloseSurface(root.querySelector("#right-pane-card-terminal"))).toEqual({
      selector: "right-card",
      rightCard: "terminal",
    });
    root.remove();
  });
});
