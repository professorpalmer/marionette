import { describe, expect, it, afterEach } from "vitest";
import {
  clearTranscriptCache,
  peekTranscriptCache,
  resolveSwitchTranscript,
  writeTranscriptCache,
} from "../components/conversation/transcriptCache";
import {
  deduplicateAssistantNarration,
  getSimilarity,
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "../components/conversation/transcriptItems";
import {
  clearToolPrepPlaceholders,
  finalizeStreamingThinking,
  upsertStreamingThinking,
  upsertToolPrep,
} from "../components/conversation/thinkingToolPrep";
import {
  CHAT_EVENTS_POLL_MS,
  chatFrameToStreamEvent,
  cursorAfterReplayMiss,
  isChatEventReplayMiss,
  isTerminalStreamKind,
  nextAppliedCursor,
  ringGenerationAfterReplayMiss,
  shouldAdvanceReplayCursor,
  shouldArmChatEventsFromRunners,
  shouldHydrateTranscriptOnReplayMiss,
  shouldPollChatEvents,
} from "../components/conversation/chatEvents";
import {
  formatWorkspaceOpenLeaseExhaustedMessage,
  isWorkspaceOpenLeaseExhausted,
} from "../components/conversation/leaseExhausted";
import { composerStatusFromRunner } from "../components/conversation/composerStatus";
import {
  SLASH_COMMANDS,
  formatMentionListingCapMessage,
  isBuiltInSlashCommand,
  mergeSlashCommands,
} from "../components/conversation/slashCommands";
import {
  filterTabsAfterDelete,
  normalizeTabPath,
  pathIsUnder,
  remapActiveTabAfterRename,
  remapTabsAfterRename,
} from "../components/conversation/tabPaths";
import {
  appendStreamingTextToItems,
  findStreamingBubbleIdx,
  typewriterCharsPerFrame,
} from "../components/conversation/streamBubbles";
import { derivePillStatus } from "../components/conversation/pillStatus";
import { workspaceLeafName } from "../components/conversation/workspaceDisplay";
import {
  statusPillDotClass,
  statusPillLabel,
  statusPillTextClass,
} from "../components/conversation/StatusPill";
import type { Item } from "../components/TranscriptList";

function msg(role: "user" | "assistant", text: string, streaming = false): Item {
  return { kind: "msg", msg: { role, text, streaming } };
}

describe("transcriptCache module", () => {
  afterEach(() => clearTranscriptCache());

  it("write/peek isolates sessions and copies arrays", () => {
    const rows = [msg("user", "a")];
    writeTranscriptCache("s1", rows);
    rows.push(msg("assistant", "b"));
    expect(peekTranscriptCache("s1")).toEqual([msg("user", "a")]);
    expect(peekTranscriptCache("s2")).toBeUndefined();
  });

  it("resolveSwitchTranscript blanks on miss", () => {
    expect(
      resolveSwitchTranscript({
        nextId: "x",
        cached: undefined,
        priorItems: [msg("user", "leak")],
      }),
    ).toEqual({ items: [], stale: true, blank: false });
  });
});

describe("transcriptItems module", () => {
  it("getSimilarity treats prefix matches as identity", () => {
    expect(getSimilarity("Found the root cause", "Found the root cause here")).toBe(1);
    expect(getSimilarity("", "x")).toBe(0);
  });

  it("deduplicateAssistantNarration keeps longer near-duplicate across cards", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "Found the issue in foo"),
      {
        kind: "card",
        card: {
          id: "c1",
          goal: "read",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      msg("assistant", "Found the issue in foo.ts and fixed it"),
    ];
    const out = deduplicateAssistantNarration(items);
    const assistants = out.filter((i) => i.kind === "msg" && i.msg.role === "assistant");
    expect(assistants).toHaveLength(1);
    if (assistants[0].kind === "msg") {
      expect(assistants[0].msg.text).toContain("fixed it");
    }
  });

  it("deduplicateAssistantNarration never collapses streaming bubbles", () => {
    const items: Item[] = [
      msg("user", "go"),
      msg("assistant", "hello", true),
      msg("assistant", "hello world", true),
    ];
    expect(deduplicateAssistantNarration(items)).toHaveLength(3);
  });

  it("transcriptFingerprint distinguishes thinking and tool_prep", () => {
    const base: Item[] = [msg("user", "hi")];
    const withThink: Item[] = [
      ...base,
      { kind: "thinking", text: "reason", streaming: true, id: "t1" },
    ];
    const withPrep: Item[] = [...base, { kind: "tool_prep", name: "read_file" }];
    expect(transcriptFingerprint(withThink)).not.toBe(transcriptFingerprint(base));
    expect(transcriptFingerprint(withPrep)).not.toBe(transcriptFingerprint(base));
    expect(transcriptFingerprint(withThink)).not.toBe(transcriptFingerprint(withPrep));
  });

  it("transcriptResponseToItems maps history when display is empty", () => {
    const items = transcriptResponseToItems({
      history: [
        { role: "user", content: "(system note)" },
        { role: "user", content: "hello" },
        { role: "assistant", content: "hi" },
      ],
    });
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "msg", msg: { role: "user", text: "hello" } });
    expect(items[1]).toMatchObject({ kind: "msg", msg: { role: "assistant", text: "hi" } });
  });

  it("mergeTranscriptItems keeps extra local cards and appends remote-only ones", () => {
    const local: Item[] = [
      {
        kind: "card",
        card: {
          id: "a",
          goal: "one",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "b",
          goal: "two",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "d",
          goal: "four",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
    ];
    const remote: Item[] = [
      {
        kind: "card",
        card: {
          id: "a",
          goal: "one",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "c",
          goal: "three",
          cwd: null,
          kind: "write_file",
          running: false,
          open: false,
        },
      },
    ];
    // local has more cards -> prefer-local merge path
    const merged = mergeTranscriptItems(local, remote);
    const ids = merged
      .filter((i): i is Extract<Item, { kind: "card" }> => i.kind === "card")
      .map((i) => i.card.id);
    expect(ids).toEqual(["a", "b", "d", "c"]);
  });
});

describe("thinkingToolPrep module", () => {
  it("upsertToolPrep accumulates distinct call ids and clear removes placeholders", () => {
    let items: Item[] = [msg("user", "go")];
    items = upsertToolPrep(items, "Read", { id: "call-1", goal: "a.ts" });
    items = upsertToolPrep(items, "Read", { id: "call-2", goal: "b.ts" });
    const cards = items.filter((i) => i.kind === "card");
    expect(cards).toHaveLength(2);
    expect(clearToolPrepPlaceholders(items).every((i) => i.kind !== "tool_prep")).toBe(true);
    expect(clearToolPrepPlaceholders(items).every((i) => i.kind !== "card")).toBe(true);
  });

  it("finalizeStreamingThinking drops streaming flag but keeps id", () => {
    const live = upsertStreamingThinking([], "think");
    const id = (live[0] as Extract<Item, { kind: "thinking" }>).id;
    const done = finalizeStreamingThinking(live);
    expect((done[0] as Extract<Item, { kind: "thinking" }>).streaming).toBeFalsy();
    expect((done[0] as Extract<Item, { kind: "thinking" }>).id).toBe(id);
  });
});

describe("chatEvents module", () => {
  it("exports the reattach poll interval", () => {
    expect(CHAT_EVENTS_POLL_MS).toBe(1000);
  });

  it("classifies available:false as miss without advancing cursor", () => {
    const replay = { available: false, ok: true, missed: false };
    expect(isChatEventReplayMiss(replay)).toBe(true);
    expect(shouldAdvanceReplayCursor(replay)).toBe(false);
    expect(shouldHydrateTranscriptOnReplayMiss(replay)).toBe(true);
  });

  it("nextAppliedCursor prefers the highest frame or replay cursor", () => {
    expect(nextAppliedCursor(1, [{ cursor: 2 }, { cursor: 4 }], 3)).toBe(4);
    expect(nextAppliedCursor(5, [{ cursor: 2 }], 6)).toBe(6);
  });

  it("maps frames and recognizes terminals", () => {
    expect(chatFrameToStreamEvent({ kind: "done", data: { ok: 1 } })).toEqual({
      kind: "done",
      data: { ok: 1 },
    });
    expect(isTerminalStreamKind("error")).toBe(true);
  });

  it("gates poll and runner arming", () => {
    expect(
      shouldPollChatEvents({
        detachedBusy: true,
        localStreamActive: false,
        userStopped: false,
        sawTerminal: false,
      }),
    ).toBe(true);
    expect(
      shouldArmChatEventsFromRunners({
        runnerBusy: true,
        localStreamActive: false,
        userStopped: false,
        chatEventsPollArmed: false,
      }),
    ).toBe(true);
  });

  it("resets cursor on ring_miss and keeps generation otherwise", () => {
    expect(cursorAfterReplayMiss({ code: "ring_miss" }, 9)).toBe(0);
    expect(ringGenerationAfterReplayMiss({ code: "cursor_gap" }, 2)).toBe(2);
  });
});

describe("leaseExhausted + composerStatus modules", () => {
  it("formats capacity-only lease copy", () => {
    expect(
      formatWorkspaceOpenLeaseExhaustedMessage({
        code: "lease_exhausted",
        max_concurrent: 2,
        active_count: 2,
      }),
    ).toMatch(/2\/2/);
    expect(isWorkspaceOpenLeaseExhausted({ code: "lease_exhausted" })).toBe(true);
  });

  it("composerStatusFromRunner ignores attaching cold builds", () => {
    expect(composerStatusFromRunner("s", { s: "attaching" }, false)).toBe("idle");
    expect(composerStatusFromRunner(null, { s: "running" }, false)).toBeNull();
  });
});

describe("slashCommands + mention listing", () => {
  it("merges custom commands and recognizes built-ins", () => {
    expect(SLASH_COMMANDS.some((s) => s.cmd === "/clear")).toBe(true);
    expect(isBuiltInSlashCommand("/clear")).toBe(true);
    expect(isBuiltInSlashCommand("/custom")).toBe(false);
    const merged = mergeSlashCommands([{ name: "ship", description: "Ship it", scope: "user" }]);
    expect(merged).toContainEqual({ cmd: "/ship", desc: "Ship it (custom)" });
  });

  it("formats mention listing caps", () => {
    expect(formatMentionListingCapMessage({ total: 5000, capped: 1000 })).toMatch(/Showing .+ of .+/);
    expect(formatMentionListingCapMessage({ capped: 2000 })).toMatch(/capped at/i);
    expect(formatMentionListingCapMessage({})).toMatch(/capped/i);
  });
});

describe("tabPaths module", () => {
  it("normalizes separators and nest checks", () => {
    expect(normalizeTabPath("a\\b\\c")).toBe("a/b/c");
    expect(pathIsUnder("repo/src/a.ts", "repo/src")).toBe(true);
    expect(pathIsUnder("repo/other", "repo/src")).toBe(false);
  });

  it("filters deletes and remaps renames including nested paths", () => {
    const tabs = [
      { path: "src/a.ts", isDirty: false },
      { path: "src/nested/b.ts", isDirty: true },
      { path: "keep.ts", isDirty: false },
    ];
    expect(filterTabsAfterDelete(tabs, "src").map((t) => t.path)).toEqual(["keep.ts"]);
    const renamed = remapTabsAfterRename(tabs, "src", "lib");
    expect(renamed.map((t) => t.path)).toEqual(["lib/a.ts", "lib/nested/b.ts", "keep.ts"]);
    expect(remapActiveTabAfterRename("src/nested/b.ts", "src", "lib")).toBe("lib/nested/b.ts");
    expect(remapActiveTabAfterRename("src", "src", "lib")).toBe("lib");
    expect(remapActiveTabAfterRename("chat", "src", "lib")).toBe("chat");
  });
});

describe("streamBubbles module", () => {
  it("finds streaming bubble past decoration and appends text", () => {
    const items: Item[] = [
      msg("user", "go"),
      { kind: "msg", msg: { role: "assistant", text: "hi", streaming: true } },
      { kind: "thinking", text: "reason", streaming: true, id: "t1" },
      {
        kind: "card",
        card: { id: "c1", goal: "read", cwd: null, kind: "read_file", running: true, open: false },
      },
    ];
    expect(findStreamingBubbleIdx(items)).toBe(1);
    const next = appendStreamingTextToItems(items, " there");
    expect((next[1] as Extract<Item, { kind: "msg" }>).msg.text).toBe("hi there");
  });

  it("skips workerStream bubbles when asked and scales typewriter drain", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "assistant", text: "w", streaming: true, workerStream: true } },
    ];
    expect(findStreamingBubbleIdx(items, { excludeWorkerStream: true })).toBe(-1);
    expect(typewriterCharsPerFrame(0, false)).toBe(0);
    expect(typewriterCharsPerFrame(3, false)).toBe(3);
    expect(typewriterCharsPerFrame(40, true)).toBeGreaterThanOrEqual(12);
  });
});

describe("pillStatus + workspaceDisplay + StatusPill chrome", () => {
  it("derivePillStatus prefers investigation and open-turn over idle flaps", () => {
    expect(
      derivePillStatus({
        transcriptStale: true,
        answerChromeIdle: false,
        liveInvestigation: false,
        turnOpen: false,
        status: "idle",
      }),
    ).toBe("switching…");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: true,
        liveInvestigation: false,
        turnOpen: false,
        status: "thinking",
      }),
    ).toBe("idle");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: true,
        turnOpen: true,
        status: "idle",
      }),
    ).toBe("executing");
    expect(
      derivePillStatus({
        transcriptStale: false,
        answerChromeIdle: false,
        liveInvestigation: false,
        turnOpen: true,
        status: "done",
      }),
    ).toBe("thinking");
  });

  it("workspaceLeafName and StatusPill helpers stay stable", () => {
    expect(workspaceLeafName("C:\\Users\\me\\proj", undefined)).toBe("proj");
    expect(workspaceLeafName("C:\\Users\\me\\.pmharness\\home", "C:\\Users\\me\\.pmharness\\home")).toBe("Home");
    expect(statusPillLabel("thinking", "read_file")).toBe("read_file");
    expect(statusPillLabel("idle", "x")).toBe("idle");
    expect(statusPillTextClass("error")).toContain("risk");
    expect(statusPillDotClass("streaming")).toContain("animate-pulse");
  });
});
