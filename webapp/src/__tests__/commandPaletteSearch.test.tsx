import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import CommandPalette from "../components/CommandPalette";
import { api, type Config, type Session } from "../lib/api";
import {
  buildPaletteSections,
  scoreFilePath,
  shortcutCaps,
  shortcutText,
  type PaletteSources,
} from "../lib/commandPalette";
import { SWITCH_SESSION_EVENT } from "../lib/sessionNav";

const NOW = 1_800_000_000_000;

function session(id: string, title: string, ageSeconds: number, extra: Partial<Session> = {}): Session {
  return { id, title, created: NOW / 1000 - ageSeconds, workspace_root: "/w/marionette", ...extra };
}

const SESSIONS = [
  session("s-current", "Current work", 10),
  session("s-cache", "Fix cache eviction", 120),
  session("s-glass", "Window glass audit", 3600, { workspace_root: "/w/site" }),
  session("s-old", "Archived idea", 60, { archived: true }),
  session("s-new", "New session", 5),
];

const MODELS = ["openrouter:deepseek/deepseek-v4.1-flash", "openrouter:anthropic/claude-opus-5.5", "local:qwen3-coder-30b"];

function sources(extra: Partial<PaletteSources> = {}): PaletteSources {
  return {
    sessions: SESSIONS,
    sessionHits: [],
    currentSessionId: "s-current",
    files: ["src/App.tsx", "src/legacy/old/App.tsx", "src/lib/cacheStore.ts", "docs/cache.md"],
    models: MODELS,
    modelLabels: { "openrouter:deepseek/deepseek-v4.1-flash": "DeepSeek V4.1 Flash" },
    currentModel: MODELS[1],
    modelsEnabled: true,
    nowMs: NOW,
    ...extra,
  };
}

const keys = (query: string, extra: Partial<PaletteSources> = {}) =>
  buildPaletteSections(query, sources(extra)).map((section) => [section.id, section.items.map((item) => item.key)]);

describe("buildPaletteSections", () => {
  it("shows recent sessions, then commands, with no query", () => {
    const sections = buildPaletteSections("", sources());
    expect(sections.map((s) => s.title)).toEqual(["Recent sessions", "Commands"]);
    expect(sections[0].items.map((item) => item.key)).toEqual(["session:s-cache", "session:s-glass"]);
    const glass = sections[0].items[1];
    expect(glass.kind === "session" && glass.detail).toBe("site · 1 h ago");
  });

  it("puts the kind with the best match first", () => {
    expect(keys("deepseek")[0]).toEqual(["models", ["model:openrouter:deepseek/deepseek-v4.1-flash"]]);
    expect(keys("app.tsx")[0]).toEqual(["files", ["file:src/App.tsx", "file:src/legacy/old/App.tsx"]]);
    const cache = keys("cache");
    expect(cache.map(([id]) => id)).toEqual(["files", "sessions"]);
    // Equal name prefix: the shorter path wins.
    expect(cache[0][1]).toEqual(["file:docs/cache.md", "file:src/lib/cacheStore.ts"]);
  });

  it("finds a session by its transcript and says why", () => {
    const sections = buildPaletteSections("eviction policy", sources({
      sessionHits: [
        { session_id: "s-glass", snippet: "user: ...the eviction policy drops...", rank: 1 },
        { session_id: "s-old", snippet: "archived", rank: 2 },
        { session_id: "gone", snippet: "deleted", rank: 3 },
      ],
    }));
    const found = sections.find((s) => s.id === "sessions");
    expect(found?.items.map((item) => item.kind === "session" && [item.sessionId, item.detail])).toEqual([
      ["s-glass", "...the eviction policy drops..."],
    ]);
  });

  it("never offers models while a swap runs, and marks the current one", () => {
    expect(keys("opus", { modelsEnabled: false }).some(([id]) => id === "models")).toBe(false);
    const opus = buildPaletteSections("opus", sources()).find((s) => s.id === "models")?.items[0];
    expect(opus?.kind === "model" && opus.current).toBe(true);
  });

  it("ranks the file name over the folder path", () => {
    expect(scoreFilePath("src/App.tsx", "app.tsx")).toBeGreaterThan(scoreFilePath("app.tsx.d/readme.md", "app.tsx") ?? 0);
    expect(scoreFilePath("src/lib/x.ts", "")).toBeNull();
  });
});

describe("shortcut hints", () => {
  it("uses the platform key names", () => {
    expect(shortcutCaps(["mod", "shift", "J"], true)).toEqual(["⌘", "⇧", "J"]);
    expect(shortcutCaps(["mod", "shift", "J"], false)).toEqual(["Ctrl", "Shift", "J"]);
    expect(shortcutText(["mod", "B"], true)).toBe("Command+B");
  });
});

describe("CommandPalette search", () => {
  const config = {
    driver: MODELS[1],
    reach: "",
    budget: 0,
    models: MODELS,
    model_labels: { "openrouter:deepseek/deepseek-v4.1-flash": "DeepSeek V4.1 Flash" },
  } as Config;

  beforeEach(() => {
    vi.spyOn(api, "sessionsBank").mockResolvedValue(SESSIONS);
    vi.spyOn(api, "getWorkspaceFiles").mockResolvedValue({ files: ["src/App.tsx", "README.md"] });
    vi.spyOn(api, "searchSessions").mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  function openPalette(onPickModel = vi.fn()) {
    render(
      <CommandPalette
        onToggleLeft={() => {}}
        onToggleRight={() => {}}
        config={config}
        sessionId="s-current"
        onPickModel={onPickModel}
      />,
    );
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    return onPickModel;
  }

  it("shows shortcut hints on commands that have one", () => {
    openPalette();
    const row = screen.getByTestId("command-palette-item-toggle-left-rail");
    expect(within(row).getAllByText(/^(B|⌘|Ctrl)$/).length).toBe(2);
    expect(screen.getByTestId("command-palette-item-clear-transcript").querySelector("kbd")).toBeNull();
  });

  it("switches to a recent session through the rail", async () => {
    const switched = vi.fn();
    window.addEventListener(SWITCH_SESSION_EVENT, switched);
    openPalette();
    fireEvent.click(await screen.findByRole("option", { name: /Fix cache eviction/ }));
    expect((switched.mock.calls[0][0] as CustomEvent).detail).toEqual({ sessionId: "s-cache" });
    await waitFor(() => expect(screen.queryByTestId("command-palette")).toBeNull());
    window.removeEventListener(SWITCH_SESSION_EVENT, switched);
  });

  it("opens a file as the file tree does", async () => {
    const opened = vi.fn();
    window.addEventListener("harness-open-file", opened);
    openPalette();
    await waitFor(() => expect(api.getWorkspaceFiles).toHaveBeenCalled());
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "app" } });
    fireEvent.click(await screen.findByRole("option", { name: /App\.tsx/ }));
    expect((opened.mock.calls[0][0] as CustomEvent).detail).toEqual({ path: "src/App.tsx", trusted: true });
    window.removeEventListener("harness-open-file", opened);
  });

  it("moves across sections with the arrow keys and picks a model with Enter", async () => {
    const onPickModel = openPalette();
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "deepseek" } });
    const option = await screen.findByRole("option", { name: /DeepSeek V4\.1 Flash/ });
    expect(option.getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(window, { key: "Enter" });
    expect(onPickModel).toHaveBeenCalledWith("openrouter:deepseek/deepseek-v4.1-flash");
  });

  it("does not swap to the model that already runs", () => {
    const onPickModel = openPalette();
    fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: "opus" } });
    fireEvent.click(screen.getByRole("option", { name: /Current/ }));
    expect(onPickModel).not.toHaveBeenCalled();
  });
});
