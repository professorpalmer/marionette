import { afterEach, describe, expect, it } from "vitest";
import {
  addToChatShortcutHint,
  appendTerminalMention,
  applyTerminalSelectionsToMessage,
  formatTerminalMention,
  isAddSelectionShortcut,
  isMacNavigator,
  terminalLabelsFromDraft,
  terminalSelectionAnchor,
  terminalSelectionLabel,
} from "../lib/terminalSelection";
import {
  clearTerminalSelectionCache,
  dropTerminalLabels,
  peekTerminalSelections,
  putTerminalSelection,
} from "../components/conversation/terminalSelectionCache";

describe("terminalSelection", () => {
  it("detects Cmd+L on Mac and Ctrl+L elsewhere", () => {
    expect(isMacNavigator({ platform: "MacIntel" })).toBe(true);
    expect(isMacNavigator({ platform: "Win32" })).toBe(false);
    expect(
      isAddSelectionShortcut({ key: "l", metaKey: true, ctrlKey: false, shiftKey: false }, true),
    ).toBe(true);
    expect(
      isAddSelectionShortcut({ key: "l", metaKey: false, ctrlKey: true, shiftKey: false }, false),
    ).toBe(true);
    expect(
      isAddSelectionShortcut({ key: "l", metaKey: true, ctrlKey: false, shiftKey: true }, true),
    ).toBe(false);
    expect(
      isAddSelectionShortcut({ key: "l", metaKey: false, ctrlKey: true, shiftKey: false }, true),
    ).toBe(false);
    expect(addToChatShortcutHint(true)).toBe("Cmd+L");
    expect(addToChatShortcutHint(false)).toBe("Ctrl+L");
  });

  it("labels a selection from buffer coords or line count", () => {
    expect(
      terminalSelectionLabel("ls", "term", { start: { y: 12 }, end: { y: 12 } }),
    ).toBe("term:12");
    expect(
      terminalSelectionLabel("a\nb\n", "term", { start: { y: 3 }, end: { y: 8 } }),
    ).toBe("term:3-8");
    expect(terminalSelectionLabel("one line", "term")).toBe("term:1");
    expect(terminalSelectionLabel("a\nb\nc\n", "agent")).toBe("agent:1-3");
  });

  it("anchors the add button to the last painted selection rect", () => {
    const hostRect = { left: 100, top: 40, width: 400, height: 300 };
    const selRect = { left: 140, top: 80, width: 80, height: 16, bottom: 96 };
    const host = {
      clientWidth: 400,
      clientHeight: 300,
      getBoundingClientRect: () => hostRect,
      querySelectorAll: () =>
        [{
          getBoundingClientRect: () => selRect,
        }] as unknown as NodeListOf<HTMLElement>,
    };
    expect(terminalSelectionAnchor(host)).toEqual({ left: 40, top: 60 });
    expect(terminalSelectionAnchor({
      ...host,
      querySelectorAll: () => [] as unknown as NodeListOf<HTMLElement>,
    })).toBeNull();
  });

  it("appends a mention once and expands it on send", () => {
    expect(formatTerminalMention("term:12")).toBe("@terminal:term:12");
    expect(formatTerminalMention("my shell")).toBe('@terminal:"my shell"');
    expect(appendTerminalMention("", "term:12")).toBe("@terminal:term:12 ");
    expect(appendTerminalMention("look at", "term:12")).toBe("look at @terminal:term:12 ");
    expect(appendTerminalMention("look at @terminal:term:12 ", "term:12")).toBe(
      "look at @terminal:term:12 ",
    );
    expect(terminalLabelsFromDraft('see @terminal:term:12 and @terminal:"my shell"')).toEqual([
      "term:12",
      "my shell",
    ]);
    expect(
      applyTerminalSelectionsToMessage("see @terminal:term:12 please", {
        "term:12": "npm test\n",
      }),
    ).toBe("see ```terminal\nnpm test\n``` please");
    expect(
      applyTerminalSelectionsToMessage("see @terminal:missing", { "term:12": "x" }),
    ).toBe("see @terminal:missing");
  });
});

describe("terminalSelectionCache", () => {
  afterEach(() => {
    clearTerminalSelectionCache();
  });

  it("stores per-session bodies and drops consumed labels", () => {
    putTerminalSelection("s1", "term:12", "ls -la");
    putTerminalSelection("s2", "term:12", "other");
    expect(peekTerminalSelections("s1")).toEqual({ "term:12": "ls -la" });
    dropTerminalLabels("s1", ["term:12"]);
    expect(peekTerminalSelections("s1")).toEqual({});
    expect(peekTerminalSelections("s2")).toEqual({ "term:12": "other" });
  });
});
