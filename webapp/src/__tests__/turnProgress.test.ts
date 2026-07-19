import { describe, expect, it } from "vitest";
import {
  aggregateExplorationSummary,
  deriveBusyProgress,
  formatBusyElapsed,
  investigatingHeadline,
  itemsInCurrentTurn,
  shortenGoal,
  shouldShowBusyFooter,
  toolFocusPhrase,
  toolRowLabel,
  isRedundantToolGoal,
  quietWorkingCueVisible,
  turnHasVisibleBusySurface,
  turnHasLiveInvestigation,
  turnLooksAnswerComplete,
} from "../lib/turnProgress";
import {
  clearToolPrepPlaceholders,
  deduplicateAssistantNarration,
  upsertToolPrep,
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

  it("surfaces tool_prep while waiting for the first card", () => {
    const items: Item[] = [
      msg("user", "diagnose"),
      { kind: "tool_prep", name: "read_file" },
    ];
    const p = deriveBusyProgress(items, "thinking", 12_000);
    expect(p.label).toContain("thinking");
    expect(p.label).toContain("read file");
    expect(p.label).toContain("12s");
    expect(p.pill).toContain("read file");
  });

  it("says Waiting on provider before first token or tool (T3)", () => {
    const items: Item[] = [msg("user", "diagnose")];
    const p = deriveBusyProgress(items, "thinking", 8_000);
    expect(p.phase).toBe("waiting");
    expect(p.label).toBe("Waiting on provider… · 8s");
    expect(p.pill).toBe("Waiting on provider… · 8s");
    expect(p.label.toLowerCase()).not.toContain("thinking");
  });

  it("names the pilot model while waiting on provider", () => {
    const p = deriveBusyProgress([msg("user", "hi")], "thinking", 5_000, {
      modelLabel: "openai-codex:gpt-5.6-luna",
    });
    expect(p.label).toBe("Waiting on gpt-5.6-luna… · 5s");
  });

  it("appends a wait hint for Codex continuation", () => {
    const p = deriveBusyProgress([msg("user", "hi")], "thinking", 2_000, {
      modelLabel: "openai-codex:gpt-5.6-luna",
      waitHint: "asking it to continue (1/3)",
    });
    expect(p.label).toContain("gpt-5.6-luna");
    expect(p.label).toContain("asking it to continue (1/3)");
  });

  it("omits elapsed under 1s while waiting on provider", () => {
    const p = deriveBusyProgress([msg("user", "hi")], "thinking", 400);
    expect(p.label).toBe("Waiting on provider…");
  });

  it("uses thinking once reasoning tokens arrive", () => {
    const items: Item[] = [
      msg("user", "go"),
      { kind: "thinking", text: "Let me check", streaming: true },
    ];
    const p = deriveBusyProgress(items, "thinking", 3_000);
    expect(p.phase).toBe("thinking");
    expect(p.label).toContain("thinking");
    expect(p.label).not.toContain("Waiting on provider");
  });

  it("clears busy labels when pure-chat answer is complete despite lagging status (T5)", () => {
    const items: Item[] = [
      msg("user", "hi"),
      msg("assistant", "Hello — here is a quick answer."),
    ];
    const p = deriveBusyProgress(items, "thinking", 20_000);
    expect(p.phase).toBe("idle");
    expect(p.label).toBe("");
    expect(p.pill).toBe("idle");
    expect(p.label.toLowerCase()).not.toContain("thinking");
  });

  it("keeps busy labels after tools when status lags (no idle blink)", () => {
    const items: Item[] = [
      msg("user", "diagnose"),
      card("1", "a.ts", "read_file", false),
      msg("assistant", "Here is the fix."),
    ];
    const p = deriveBusyProgress(items, "thinking", 20_000);
    expect(p.phase).not.toBe("idle");
    expect(p.pill).not.toBe("idle");
  });

  it("includes thinking and tool_prep kinds in the current turn", () => {
    const items: Item[] = [
      msg("user", "go"),
      { kind: "thinking", text: "Let me check", streaming: true },
      { kind: "tool_prep", name: "grep" },
    ];
    const turn = itemsInCurrentTurn(items);
    expect(turn).toHaveLength(2);
    expect(turn[0].kind).toBe("thinking");
    expect((turn[0] as { streaming?: boolean }).streaming).toBe(true);
    expect(turn[1].kind).toBe("tool_prep");
    expect((turn[1] as { name: string }).name).toBe("grep");
  });
});

describe("investigatingHeadline / exploration summary", () => {
  it("shows Investigating focus while tools run", () => {
    expect(
      investigatingHeadline(3, true, "read", "config.txt", "3 files"),
    ).toBe("Investigating · read config.txt");
  });

  it("does not paint tool tool when kind and goal are the same", () => {
    expect(
      investigatingHeadline(1, true, "tool", "tool", "1 step"),
    ).toBe("Investigating · tool");
  });

  it("falls back to kind counts while live without a focus tool", () => {
    expect(
      investigatingHeadline(3, true, "", "", "2 files, 1 search"),
    ).toBe("Investigating · 2 files, 1 search");
  });

  it("aggregates Explored summary when done", () => {
    expect(
      investigatingHeadline(4, false, "", "", "3 files, 1 search"),
    ).toBe("Explored 3 files, 1 search");
  });

  it("buckets kinds Cursor-style", () => {
    expect(
      aggregateExplorationSummary([
        "read_file",
        "read_file",
        "read_file",
        "grep",
        "run_command",
      ]),
    ).toBe("3 files, 1 search, 1 command");
  });

  it("maps tool kinds to row labels", () => {
    expect(toolRowLabel("read_file")).toBe("Read");
    expect(toolRowLabel("grep")).toBe("Grep");
    expect(toolRowLabel("run_command")).toBe("Run");
    expect(toolRowLabel("query_wiki")).toBe("Query wiki");
    expect(toolRowLabel("call_mcp")).toBe("MCP");
    expect(toolFocusPhrase("run_command")).toBe("run");
    expect(toolRowLabel("readToolCall")).toBe("Read");
    expect(toolRowLabel("ShellToolCall")).toBe("Run");
    expect(toolRowLabel("execute")).toBe("Run");
  });

  it("treats kind-echo goals as redundant", () => {
    expect(isRedundantToolGoal("read_file", "read file")).toBe(true);
    expect(isRedundantToolGoal("read_file", "tool")).toBe(true);
    expect(isRedundantToolGoal("call_mcp", "MCP")).toBe(true);
    expect(isRedundantToolGoal("read_file", "harness/server.py")).toBe(false);
  });

  it("shortens path tails", () => {
    expect(shortenGoal("a/b/c/very-long-name-that-exceeds-limit.lua", 20).endsWith("…")).toBe(true);
  });
});

describe("turnHasLiveInvestigation", () => {
  it("is true while a card is running", () => {
    const items: Item[] = [
      msg("user", "go"),
      card("1", "a.ts", "read_file", true),
    ];
    expect(turnHasLiveInvestigation(items)).toBe(true);
  });

  it("is true while thinking streams", () => {
    const items: Item[] = [
      msg("user", "go"),
      { kind: "thinking", text: "hmm", streaming: true },
    ];
    expect(turnHasLiveInvestigation(items)).toBe(true);
  });

  it("is false when tools finished and the agent loop is closed", () => {
    const items: Item[] = [
      msg("user", "go"),
      card("1", "a.ts", "read_file", false),
    ];
    expect(turnHasLiveInvestigation(items)).toBe(false);
    expect(turnHasLiveInvestigation(items, false)).toBe(false);
  });

  it("stays live between tool steps while the agent loop is open", () => {
    const items: Item[] = [
      msg("user", "go"),
      card("1", "a.ts", "read_file", false),
    ];
    expect(turnHasLiveInvestigation(items, true)).toBe(true);
  });

  it("is true while nested worker actions are still running", () => {
    const items: Item[] = [
      msg("user", "go"),
      {
        kind: "card",
        card: {
          id: "p1",
          goal: "parallel",
          kind: "run_parallel",
          running: false,
          open: false,
          actions: [
            { action_id: "n1", kind: "read_file", goal: "a.py", status: "running" },
          ],
        },
      },
    ];
    expect(turnHasLiveInvestigation(items)).toBe(true);
  });
});

describe("turnLooksAnswerComplete / shouldShowBusyFooter (T5)", () => {
  it("is true for pure chat when assistant text is done (T5)", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "Final answer."),
    ];
    expect(turnLooksAnswerComplete(items)).toBe(true);
    expect(shouldShowBusyFooter(items, "thinking")).toBe(false);
    expect(shouldShowBusyFooter(items, "streaming")).toBe(false);
  });

  it("is false after tools even when assistant narration looks final", () => {
    // Same shape as a finished tool turn — must NOT early-idle between steps.
    const items: Item[] = [
      msg("user", "go"),
      card("1", "a.ts", "read_file", false),
      { kind: "thinking", text: "done thinking", streaming: false },
      msg("assistant", "Final answer."),
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "thinking")).toBe(true);
    expect(shouldShowBusyFooter(items, "executing")).toBe(true);
  });

  it("is false when narration follows finished cards (gap before next tool)", () => {
    const items: Item[] = [
      msg("user", "fix"),
      card("1", "server.py", "edit_file", false),
      msg("assistant", "Now the bigger fix — never use the queue."),
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(turnHasLiveInvestigation(items, true)).toBe(true);
    expect(turnHasLiveInvestigation(items, false)).toBe(false);
    const p = deriveBusyProgress(items, "thinking", 30_000);
    expect(p.pill).not.toBe("idle");
  });

  it("is false while the assistant bubble is still streaming", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "partial…", true),
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "streaming")).toBe(true);
  });

  it("is false while a card is running", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "Working on it."),
      card("1", "a.ts", "read_file", true),
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "executing")).toBe(true);
  });

  it("is false between tool steps after mid-turn narration (no idle blink)", () => {
    // Narration + finished write, next tool not started yet — header used to
    // flash idle because nothing was card.running.
    const items: Item[] = [
      msg("user", "scaffold"),
      msg("assistant", "Let me lay down the core files:"),
      card("1", "README.md", "write_file", false),
      card("2", "src/index.ts", "write_file", false),
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "executing")).toBe(true);
    const p = deriveBusyProgress(items, "executing", 90_000);
    expect(p.phase).not.toBe("idle");
    expect(p.pill).not.toBe("idle");
  });

  it("is false while thinking is streaming", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "Earlier note."),
      { kind: "thinking", text: "more", streaming: true },
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "thinking")).toBe(true);
  });

  it("is false while tool_prep is active", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "Next I will grep."),
      { kind: "tool_prep", name: "grep" },
    ];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "thinking")).toBe(true);
  });

  it("is false with no assistant text yet (T3 waiting still shows)", () => {
    const items: Item[] = [msg("user", "go")];
    expect(turnLooksAnswerComplete(items)).toBe(false);
    expect(shouldShowBusyFooter(items, "thinking")).toBe(true);
  });

  it("returns false for shouldShowBusyFooter when status is idle", () => {
    const items: Item[] = [msg("user", "go"), msg("assistant", "done")];
    expect(shouldShowBusyFooter(items, "idle")).toBe(false);
  });
});

describe("upsertToolPrep promotes into ActivityGroup cards", () => {
  it("adds a provisional running card as soon as tool_prep arrives", () => {
    const items: Item[] = [msg("user", "diagnose")];
    const out = upsertToolPrep(items, "run_command");
    const cards = out.filter((i) => i.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards).toHaveLength(1);
    expect(cards[0].card.id).toBe("tool-prep:run_command");
    expect(cards[0].card.running).toBe(true);
    expect(cards[0].card.kind).toBe("run_command");
    expect(out.some((i) => i.kind === "tool_prep")).toBe(true);
    expect(turnHasLiveInvestigation(out)).toBe(true);
  });

  it("keeps unrelated legacy prep cards instead of stealing their slot", () => {
    const once = upsertToolPrep([msg("user", "go")], "read_file");
    const twice = upsertToolPrep(once, "grep");
    const cards = twice.filter((i) => i.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards).toHaveLength(2);
    expect(cards.map((c) => c.card.id)).toEqual([
      "tool-prep:read_file",
      "tool-prep:grep",
    ]);
  });

  it("accumulates Cursor tools by call id instead of wiping the fold", () => {
    const once = upsertToolPrep([msg("user", "go")], "read_file", {
      id: "c1",
      goal: "a.py",
      status: "in_progress",
    });
    const twice = upsertToolPrep(once, "run_command", {
      id: "c2",
      goal: "pytest",
      status: "in_progress",
    });
    const done = upsertToolPrep(twice, "read_file", {
      id: "c1",
      status: "completed",
    });
    const cards = done.filter((i) => i.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards).toHaveLength(2);
    expect(cards[0].card.id).toBe("tool-prep:c1");
    expect(cards[0].card.running).toBe(false);
    expect(cards[0].card.goal).toBe("a.py");
    expect(cards[1].card.id).toBe("tool-prep:c2");
    expect(cards[1].card.running).toBe(true);
    expect(cards[1].card.goal).toBe("pytest");
  });

  it("clears prep placeholders before a real action_start card", () => {
    const prepped = upsertToolPrep([msg("user", "go")], "read_file");
    const cleared = clearToolPrepPlaceholders(prepped);
    expect(cleared.some((i) => i.kind === "tool_prep")).toBe(false);
    expect(cleared.some((i) => i.kind === "card")).toBe(false);
    const withReal = [
      ...cleared,
      card("real-1", "src/a.ts", "read_file", true),
    ];
    expect(withReal.filter((i) => i.kind === "card")).toHaveLength(1);
  });
});

describe("formatBusyElapsed", () => {
  it("formats seconds and minutes", () => {
    expect(formatBusyElapsed(4_000)).toBe("4s");
    expect(formatBusyElapsed(65_000)).toBe("1m 5s");
  });
});

describe("quietWorkingCueVisible / turnHasVisibleBusySurface (no idle flicker)", () => {
  it("shows immediately between tool batches (finished cards, loop busy)", () => {
    // Card just finished, next tool not started — the exact gap that used to
    // blink idle for up to 2s.
    const items: Item[] = [
      msg("user", "audit"),
      card("1", "server.py", "read_file", false),
    ];
    expect(turnHasVisibleBusySurface(items)).toBe(false);
    expect(quietWorkingCueVisible(items, "thinking", false, false)).toBe(true);
    expect(quietWorkingCueVisible(items, "executing", false, false)).toBe(true);
  });

  it("hides while a running card owns the busy surface", () => {
    const items: Item[] = [
      msg("user", "audit"),
      card("1", "server.py", "read_file", true),
    ];
    expect(turnHasVisibleBusySurface(items)).toBe(true);
    expect(quietWorkingCueVisible(items, "executing", false, false)).toBe(false);
  });

  it("hides while thinking or assistant text streams", () => {
    const thinkItems: Item[] = [
      msg("user", "go"),
      { kind: "thinking", text: "hm", streaming: true },
    ];
    expect(quietWorkingCueVisible(thinkItems, "thinking", false, false)).toBe(false);
    const streamItems: Item[] = [
      msg("user", "go"),
      msg("assistant", "partial…", true),
    ];
    expect(quietWorkingCueVisible(streamItems, "streaming", false, false)).toBe(false);
  });

  it("hides on tool_prep (Investigating placeholder already shows)", () => {
    const items: Item[] = [
      msg("user", "go"),
      { kind: "tool_prep", name: "grep" },
    ];
    expect(quietWorkingCueVisible(items, "thinking", false, false)).toBe(false);
  });

  it("hides when idle/done, compacting, or the busy footer is up", () => {
    const items: Item[] = [
      msg("user", "audit"),
      card("1", "server.py", "read_file", false),
    ];
    expect(quietWorkingCueVisible(items, "idle", false, false)).toBe(false);
    expect(quietWorkingCueVisible(items, "done", false, false)).toBe(false);
    expect(quietWorkingCueVisible(items, "thinking", true, false)).toBe(false);
    expect(quietWorkingCueVisible(items, "thinking", false, true)).toBe(false);
  });

  it("only inspects the current turn (prior-turn cards do not suppress)", () => {
    const items: Item[] = [
      msg("user", "first"),
      card("old", "a.ts", "read_file", true),
      msg("assistant", "done"),
      msg("user", "second"),
    ];
    expect(turnHasVisibleBusySurface(items)).toBe(false);
    expect(quietWorkingCueVisible(items, "thinking", false, false)).toBe(true);
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
