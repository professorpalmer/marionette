import { describe, expect, it } from "vitest";
import {
  deriveBusyProgress,
  formatBusyElapsed,
  investigatingHeadline,
  shortenGoal,
} from "../lib/turnProgress";
import {
  deduplicateAssistantNarration,
} from "../components/Conversation";
import type { Item } from "../components/TranscriptList";

function card(id: string, goal: string, kind = "read_file", running = false): Item {
  return {
    kind: "card",
    card: { id, goal, cwd: null, kind, running, open: false },
  };
}

function msg(role: "user" | "assistant", text: string, streaming = false): Item {
  return { kind: "msg", msg: { role, text, streaming } };
}

describe("deriveBusyProgress", () => {
  it("surfaces running tool kind, step count, and elapsed", () => {
    const items: Item[] = [
      msg("user", "diagnose"),
      card("1", "addons/foo.lua", "read_file", false),
      card("2", "translator_config.txt", "read_file", true),
    ];
    const p = deriveBusyProgress(items, "executing", 75_000);
    expect(p.label).toContain("running");
    expect(p.label).toContain("read file");
    expect(p.label).toContain("step 2");
    expect(p.label).toContain("1m 15s");
    expect(p.pill).toContain("running");
    expect(p.step).toBe(2);
  });

  it("returns empty label when idle", () => {
    const p = deriveBusyProgress([msg("user", "hi")], "idle", null);
    expect(p.label).toBe("");
  });
});

describe("investigatingHeadline / shortenGoal", () => {
  it("shows current tool while investigating", () => {
    expect(
      investigatingHeadline(3, true, "read file", "config.txt", "3 reads"),
    ).toBe("step 3 · read file config.txt");
  });

  it("shortens path tails", () => {
    expect(shortenGoal("a/b/c/very-long-name-that-exceeds-limit.lua", 20).endsWith("…")).toBe(true);
  });
});

describe("formatBusyElapsed", () => {
  it("formats seconds and minutes", () => {
    expect(formatBusyElapsed(4_000)).toBe("4s");
    expect(formatBusyElapsed(65_000)).toBe("1m 5s");
  });
});

describe("deduplicateAssistantNarration", () => {
  it("drops near-duplicate diagnosis across tool cards in a turn", () => {
    const diagnosis =
      "Found the root causes. Two issues: API key is invalid and Unicode crash on cp1252.";
    const items: Item[] = [
      msg("user", "diagnose"),
      msg("assistant", diagnosis),
      card("a1", "config.txt"),
      msg("assistant", diagnosis + " Also logging."),
      card("a2", "spawn.lua"),
      msg("assistant", diagnosis),
    ];
    const out = deduplicateAssistantNarration(items);
    const assistants = out.filter((i) => i.kind === "msg" && i.msg.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect((assistants[0] as { msg: { text: string } }).msg.text).toContain("Also logging");
  });

  it("does not collapse streaming bubbles", () => {
    const items: Item[] = [
      msg("user", "hi"),
      msg("assistant", "partial answer", true),
      msg("assistant", "partial answer done"),
    ];
    const out = deduplicateAssistantNarration(items);
    expect(out.filter((i) => i.kind === "msg" && i.msg.role === "assistant")).toHaveLength(2);
  });
});
