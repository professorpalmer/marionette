import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import CommandPalette from "../components/CommandPalette";
import {
  COMMAND_PALETTE_ACTIONS,
  filterCommandPaletteActions,
  runCommandPaletteAction,
} from "../lib/commandPalette";

function pressPaletteShortcut(target: Window | Document = window) {
  fireEvent.keyDown(target, { key: "k", metaKey: true });
}

describe("commandPalette filter", () => {
  it("narrows curated actions by fuzzy query", () => {
    const narrowed = filterCommandPaletteActions(COMMAND_PALETTE_ACTIONS, "clea");
    expect(narrowed.map((a) => a.id)).toEqual(["clear-transcript"]);
    const swarm = filterCommandPaletteActions(COMMAND_PALETTE_ACTIONS, "swarm");
    expect(swarm.map((a) => a.id)).toEqual(["open-swarm"]);
  });
});

describe("runCommandPaletteAction open-memory", () => {
  it("focuses Advanced and dispatches harness-expand-memory", () => {
    const focusSettingsPage = vi.fn();
    const seen: string[] = [];
    const onExpand = () => seen.push("harness-expand-memory");
    const onTab = (e: Event) => seen.push(`tab:${String((e as CustomEvent).detail)}`);
    window.addEventListener("harness-expand-memory", onExpand);
    window.addEventListener("harness-focus-tab", onTab as EventListener);
    try {
      runCommandPaletteAction("open-memory", {
        toggleLeft: () => {},
        toggleRight: () => {},
        focusSettingsPage,
      });
      expect(focusSettingsPage).toHaveBeenCalledWith("advanced");
      expect(seen).toEqual(["tab:settings", "harness-expand-memory"]);
    } finally {
      window.removeEventListener("harness-expand-memory", onExpand);
      window.removeEventListener("harness-focus-tab", onTab as EventListener);
    }
  });
});

describe("runCommandPaletteAction clear vs new", () => {
  it("Clear transcript does not createSession or start a new session", () => {
    const createSession = vi.fn();
    const seen: string[] = [];
    const onNew = () => {
      seen.push("harness-new-session");
      createSession();
    };
    const onClear = () => {
      seen.push("harness-clear-transcript");
    };
    window.addEventListener("harness-new-session", onNew);
    window.addEventListener("harness-clear-transcript", onClear);
    try {
      runCommandPaletteAction("clear-transcript", {
        toggleLeft: () => {},
        toggleRight: () => {},
        focusSettingsPage: () => {},
      });
      expect(seen).toEqual(["harness-clear-transcript"]);
      expect(createSession).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("harness-new-session", onNew);
      window.removeEventListener("harness-clear-transcript", onClear);
    }
  });

  it("New session dispatches harness-new-session only", () => {
    const seen: string[] = [];
    const onNew = () => seen.push("harness-new-session");
    const onClear = () => seen.push("harness-clear-transcript");
    window.addEventListener("harness-new-session", onNew);
    window.addEventListener("harness-clear-transcript", onClear);
    try {
      runCommandPaletteAction("new-session", {
        toggleLeft: () => {},
        toggleRight: () => {},
        focusSettingsPage: () => {},
      });
      expect(seen).toEqual(["harness-new-session"]);
    } finally {
      window.removeEventListener("harness-new-session", onNew);
      window.removeEventListener("harness-clear-transcript", onClear);
    }
  });
});

describe("CommandPalette UI", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // Close any leftover open palette from a prior assertion.
    if (screen.queryByTestId("command-palette")) {
      fireEvent.keyDown(window, { key: "Escape" });
    }
  });

  it("opens on Cmd/Ctrl-K and closes on Escape", () => {
    render(
      <CommandPalette onToggleLeft={() => {}} onToggleRight={() => {}} />,
    );
    expect(screen.queryByTestId("command-palette")).toBeNull();
    pressPaletteShortcut();
    expect(screen.getByTestId("command-palette")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("command-palette")).toBeNull();
  });

  it("filters the list as the query narrows", () => {
    render(
      <CommandPalette onToggleLeft={() => {}} onToggleRight={() => {}} />,
    );
    pressPaletteShortcut();
    const input = screen.getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "term" } });
    expect(screen.getByTestId("command-palette-item-open-terminal")).toBeTruthy();
    expect(screen.queryByTestId("command-palette-item-new-session")).toBeNull();
  });

  it("selecting Clear does not createSession", () => {
    const createSession = vi.fn();
    const onNew = () => {
      createSession();
    };
    window.addEventListener("harness-new-session", onNew);
    const cleared: string[] = [];
    const onClear = () => cleared.push("clear");
    window.addEventListener("harness-clear-transcript", onClear);
    try {
      render(
        <CommandPalette onToggleLeft={() => {}} onToggleRight={() => {}} />,
      );
      pressPaletteShortcut();
      fireEvent.change(screen.getByTestId("command-palette-input"), {
        target: { value: "clear" },
      });
      fireEvent.click(screen.getByTestId("command-palette-item-clear-transcript"));
      expect(cleared).toEqual(["clear"]);
      expect(createSession).not.toHaveBeenCalled();
      expect(screen.queryByTestId("command-palette")).toBeNull();
    } finally {
      window.removeEventListener("harness-new-session", onNew);
      window.removeEventListener("harness-clear-transcript", onClear);
    }
  });

  it("closes when clicking the backdrop", () => {
    render(
      <CommandPalette onToggleLeft={() => {}} onToggleRight={() => {}} />,
    );
    pressPaletteShortcut();
    expect(screen.getByTestId("command-palette")).toBeTruthy();
    fireEvent.mouseDown(screen.getByTestId("command-palette-backdrop"));
    expect(screen.queryByTestId("command-palette")).toBeNull();
  });
});
