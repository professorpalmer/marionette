import { afterEach, describe, expect, it } from "vitest";
import {
  SLASH_COMMANDS,
  SLASH_MENU_LIMIT,
  isBuiltInSlashCommand,
  mergeSlashCommands,
  slashMenu,
} from "../components/conversation/slashCommands";
import {
  readSlashMenuPrefs,
  recordSlashCommandUse,
  resetSlashMenuPrefsForTests,
  setSlashExpertMode,
} from "../components/conversation/slashMenuPrefs";

const NO_PREFS = { expert: false, uses: {} };
const all = mergeSlashCommands([
  { name: "example", description: "Example custom command", scope: "global" },
  { name: "ship", description: "Ship it", scope: "user" },
]);
const names = (items: { cmd: string }[]) => items.map((c) => c.cmd);

describe("slashMenu", () => {
  it("opens on the top commands in the default order, each with a one-line description", () => {
    const menu = slashMenu(all, "", NO_PREFS);
    expect(names(menu.items)).toEqual(["/new", "/compact", "/clear", "/todo", "/advise", "/routines", "/privacy", "/help"]);
    expect(menu.items).toHaveLength(SLASH_MENU_LIMIT);
    for (const c of menu.items) expect(c.desc).not.toMatch(/\n/);
    // /images-strip, /advice, /model and the custom /ship wait behind a filter.
    expect(menu.moreCount).toBe(4);
    // /refine, /ping and the seeded /example are internal.
    expect(menu.internalCount).toBe(3);
  });

  it("drops the Cmd+K duplicates and internal tools from the open and filtered menu", () => {
    const open = names(slashMenu(all, "", { expert: true, uses: {} }).items);
    for (const typed of ["/swarm", "/terminal", "/settings", "/memory", "/mcp", "/files", "/state"]) {
      expect(open).not.toContain(typed);
      expect(names(slashMenu(all, typed.slice(1, 3), { expert: true, uses: {} }).items)).not.toContain(typed);
    }
    for (const internal of ["/refine", "/ping", "/example"]) {
      expect(open.length).toBe(SLASH_MENU_LIMIT);
      expect(names(slashMenu(all, internal.slice(1, 3), NO_PREFS).items)).not.toContain(internal);
    }
  });

  it("still finds a hidden command on its exact name, so typed commands stay discoverable", () => {
    expect(names(slashMenu(all, "swarm", NO_PREFS).items)).toEqual(["/swarm"]);
    expect(names(slashMenu(all, "ping", NO_PREFS).items)).toEqual(["/ping"]);
    expect(names(slashMenu(all, "sw", NO_PREFS).items)).toEqual([]);
  });

  it("shows internal commands in expert mode", () => {
    const menu = slashMenu(all, "", { expert: true, uses: {} });
    expect(menu.internalCount).toBe(0);
    expect(names(slashMenu(all, "re", { expert: true, uses: {} }).items)).toEqual(["/refine"]);
    expect(names(slashMenu(all, "ex", { expert: true, uses: {} }).items)).toEqual(["/example"]);
  });

  it("ranks by use, then by the default order", () => {
    const uses = { "/ship": 3, "/images-strip": 1 };
    const menu = slashMenu(all, "", { expert: false, uses });
    expect(names(menu.items).slice(0, 3)).toEqual(["/ship", "/images-strip", "/new"]);
    expect(menu.items).toHaveLength(SLASH_MENU_LIMIT);
    expect(names(slashMenu(all, "c", { expert: false, uses: { "/clear": 2 } }).items)).toEqual(["/clear", "/compact"]);
  });

  it("keeps every built-in runnable when typed", () => {
    for (const c of SLASH_COMMANDS) expect(isBuiltInSlashCommand(c.cmd)).toBe(true);
    expect(isBuiltInSlashCommand("/ship")).toBe(false);
  });
});

describe("slashMenuPrefs", () => {
  afterEach(() => resetSlashMenuPrefsForTests());

  it("counts menu picks and stores expert mode across reads", () => {
    recordSlashCommandUse("/todo");
    recordSlashCommandUse("/todo");
    setSlashExpertMode(true);
    expect(readSlashMenuPrefs()).toEqual({ expert: true, uses: { "/todo": 2 } });
    expect(JSON.parse(window.localStorage.getItem("marionette.slashMenu.v1")!)).toEqual({ expert: true, uses: { "/todo": 2 } });
  });

  it("falls back to defaults for a corrupt store and drops bad fields", () => {
    window.localStorage.setItem("marionette.slashMenu.v1", "{not json");
    expect(readSlashMenuPrefs()).toEqual({ expert: false, uses: {} });
    resetSlashMenuPrefsForTests();
    window.localStorage.setItem("marionette.slashMenu.v1", JSON.stringify({ expert: "yes", uses: { "/x": -1, bad: 2, "/ok": 2 } }));
    expect(readSlashMenuPrefs()).toEqual({ expert: false, uses: { "/ok": 2 } });
  });
});
