import { describe, expect, it, vi, afterEach } from "vitest";
import {
  chatFrameToStreamEvent,
  cursorAfterReplayMiss,
  isChatEventReplayMiss,
  isTerminalStreamKind,
  nextAppliedCursor,
  ringGenerationAfterReplayMiss,
  shouldAdvanceReplayCursor,
  shouldHydrateTranscriptOnReplayMiss,
  shouldArmChatEventsFromRunners,
  shouldPollChatEvents,
  shouldRetryRingAfterReplayMiss,
  createChatEventsReattach,
  mergeTranscriptItems,
  appendActionStartCard,
} from "../components/Conversation";
import { api } from "../lib/api";
import type { Item } from "../components/TranscriptList";

/**
 * Mid-turn chatEvents reattach contracts (cursor + poll gating).
 * Does not mount Conversation — covers pure helpers only.
 */

describe("chatEvents reattach cursor", () => {
  it("advances from frames and replay cursor", () => {
    expect(nextAppliedCursor(0, [{ cursor: 1 }, { cursor: 3 }], 3)).toBe(3);
    expect(nextAppliedCursor(2, [{ cursor: 3 }, { cursor: 4 }], 5)).toBe(5);
    expect(nextAppliedCursor(4, [], 4)).toBe(4);
    expect(nextAppliedCursor(7, [{ cursor: 5 }], 6)).toBe(7);
  });

  it("maps ring frames to live stream events", () => {
    expect(chatFrameToStreamEvent({
      kind: "message_delta",
      data: { text: "hi" },
    })).toEqual({ kind: "message_delta", data: { text: "hi" } });
  });
});

describe("chatEvents replay miss vs empty success", () => {
  it("treats ring_miss and generation_mismatch as miss, not catch-up", () => {
    expect(isChatEventReplayMiss({
      ok: false,
      missed: true,
      code: "ring_miss",
      available: false,
    })).toBe(true);
    expect(isChatEventReplayMiss({
      ok: false,
      missed: true,
      code: "generation_mismatch",
      available: false,
    })).toBe(true);
    expect(shouldAdvanceReplayCursor({
      ok: false,
      missed: true,
      code: "ring_miss",
    })).toBe(false);
  });

  it("treats cursor_gap like other misses (no advance, hydrate, keep gen pin)", () => {
    const gap = {
      ok: false,
      missed: true,
      code: "cursor_gap",
      available: false,
      generation: 4,
    };
    expect(isChatEventReplayMiss(gap)).toBe(true);
    expect(shouldAdvanceReplayCursor(gap)).toBe(false);
    expect(shouldHydrateTranscriptOnReplayMiss(gap)).toBe(true);
    expect(cursorAfterReplayMiss({ code: "cursor_gap" }, 12)).toBe(0);
    // Ring still exists — keep generation pin (unlike ring_miss).
    expect(ringGenerationAfterReplayMiss(gap, 4)).toBe(4);
    // Retained tool/activity tail is still in the ring — retry once.
    expect(shouldRetryRingAfterReplayMiss(gap, {
      alreadyRetried: false,
      prevGeneration: 4,
      nextGeneration: 4,
    })).toBe(true);
    expect(shouldRetryRingAfterReplayMiss(gap, {
      alreadyRetried: true,
      prevGeneration: 4,
      nextGeneration: 4,
    })).toBe(false);
  });

  it("does not retry ring_miss (hydrate-only; never fake catch-up)", () => {
    expect(shouldRetryRingAfterReplayMiss(
      { code: "ring_miss" },
      { alreadyRetried: false },
    )).toBe(false);
    expect(shouldRetryRingAfterReplayMiss(
      { code: "generation_mismatch", generation: 5 },
      { alreadyRetried: false, prevGeneration: 3, nextGeneration: 5 },
    )).toBe(true);
    expect(shouldRetryRingAfterReplayMiss(
      { code: "generation_mismatch", generation: 5 },
      { alreadyRetried: false, prevGeneration: 5, nextGeneration: 5 },
    )).toBe(false);
  });

  it("treats ok:true empty replay as successful catch-up", () => {
    expect(isChatEventReplayMiss({
      ok: true,
      missed: false,
      available: true,
    })).toBe(false);
    expect(shouldAdvanceReplayCursor({
      ok: true,
      missed: false,
    })).toBe(true);
    expect(nextAppliedCursor(2, [], 2)).toBe(2);
  });

  it("refreshes generation pin on mismatch and clears on ring_miss", () => {
    expect(ringGenerationAfterReplayMiss(
      { code: "generation_mismatch", generation: 5 },
      3,
    )).toBe(5);
    expect(ringGenerationAfterReplayMiss(
      { code: "ring_miss", generation: 0 },
      3,
    )).toBeUndefined();
    expect(ringGenerationAfterReplayMiss(
      { code: "other", generation: 9 },
      3,
    )).toBe(3);
  });

  it("hydrates disk transcript on miss and resets cursor", () => {
    expect(shouldHydrateTranscriptOnReplayMiss({
      ok: false,
      missed: true,
    })).toBe(true);
    expect(shouldHydrateTranscriptOnReplayMiss({
      ok: true,
      missed: false,
    })).toBe(false);
    expect(cursorAfterReplayMiss({ code: "ring_miss" }, 12)).toBe(0);
    expect(cursorAfterReplayMiss({ code: "generation_mismatch" }, 7)).toBe(0);
    expect(cursorAfterReplayMiss({ code: "cursor_gap" }, 9)).toBe(0);
    expect(cursorAfterReplayMiss({ code: "other" }, 4)).toBe(4);
    // Miss must not advance cursor as if catch-up succeeded.
    expect(shouldAdvanceReplayCursor({
      ok: false,
      missed: true,
      code: "generation_mismatch",
    })).toBe(false);
  });
});

describe("chatEvents reattach poll gate", () => {
  it("recognizes terminal kinds", () => {
    expect(isTerminalStreamKind("assistant_done")).toBe(true);
    expect(isTerminalStreamKind("done")).toBe(true);
    expect(isTerminalStreamKind("error")).toBe(true);
    expect(isTerminalStreamKind("auto_halt")).toBe(true);
    expect(isTerminalStreamKind("message_delta")).toBe(false);
  });

  it("polls only while detached-busy without local SSE", () => {
    expect(shouldPollChatEvents({
      detachedBusy: true,
      localStreamActive: false,
      userStopped: false,
      sawTerminal: false,
    })).toBe(true);

    expect(shouldPollChatEvents({
      detachedBusy: true,
      localStreamActive: true,
      userStopped: false,
      sawTerminal: false,
    })).toBe(false);

    expect(shouldPollChatEvents({
      detachedBusy: true,
      localStreamActive: false,
      userStopped: true,
      sawTerminal: false,
    })).toBe(false);

    expect(shouldPollChatEvents({
      detachedBusy: true,
      localStreamActive: false,
      userStopped: false,
      sawTerminal: true,
    })).toBe(false);

    expect(shouldPollChatEvents({
      detachedBusy: false,
      localStreamActive: false,
      userStopped: false,
      sawTerminal: false,
    })).toBe(false);
  });

  it("arms chatEvents from runners when a bridge/queue turn starts on an open session", () => {
    expect(shouldArmChatEventsFromRunners({
      runnerBusy: true,
      localStreamActive: false,
      userStopped: false,
      chatEventsPollArmed: false,
    })).toBe(true);

    expect(shouldArmChatEventsFromRunners({
      runnerBusy: true,
      localStreamActive: false,
      userStopped: false,
      chatEventsPollArmed: true,
    })).toBe(false);

    expect(shouldArmChatEventsFromRunners({
      runnerBusy: true,
      localStreamActive: true,
      userStopped: false,
      chatEventsPollArmed: false,
    })).toBe(false);

    expect(shouldArmChatEventsFromRunners({
      runnerBusy: false,
      localStreamActive: false,
      userStopped: false,
      chatEventsPollArmed: false,
    })).toBe(false);
  });
});

describe("detached-busy mid-tool-batch reattach", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("merges durable completed tools with local in-flight cards (no shrink)", () => {
    const local: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "done tool",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: "ok",
        },
      },
      {
        kind: "card",
        card: {
          id: "a2",
          goal: "still running",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const remote: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "done tool",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: "ok from disk",
        },
      },
    ];
    const merged = mergeTranscriptItems(local, remote);
    const cards = merged.filter(
      (i): i is Extract<Item, { kind: "card" }> => i.kind === "card",
    );
    expect(cards.map((c) => c.card.id)).toEqual(["a1", "a2"]);
    expect(cards[0].card.result).toBe("ok from disk");
    expect(cards[1].card.running).toBe(true);
    // Ring replay of the same action_start must stay idempotent.
    const again = appendActionStartCard(merged, {
      id: "a2",
      goal: "still running",
      kind: "run_command",
    });
    expect(
      again.filter((i) => i.kind === "card" && i.card.id === "a2"),
    ).toHaveLength(1);
  });

  it("on cursor_gap: awaits disk hydrate then retries ring for tool tail", async () => {
    const applied: string[] = [];
    let items: Item[] = [{ kind: "msg", msg: { role: "user", text: "go" } }];
    const itemsRef = { current: items };
    const lastAppliedCursorRef = { current: 1 };
    const ringGenerationRef = { current: 2 as number | undefined };
    const detachedBusyRef = { current: true };
    const streamGenRef = { current: 1 };
    const transcriptLoadGenRef = { current: 1 };
    const cachedSessionIdRef = { current: "sess-mid" as string | null };
    const localStreamActiveRef = { current: false };
    const userStoppedRef = { current: false };
    const runnerBusyPollGenRef = { current: 0 };
    const transcriptFpRef = { current: "" };
    const chatEventsPollTimerRef = { current: null as number | null };

    const chatEvents = vi.spyOn(api, "chatEvents")
      .mockResolvedValueOnce({
        ok: false,
        missed: true,
        available: false,
        code: "cursor_gap",
        generation: 2,
        cursor: 9,
        events: [],
        retained: 3,
      } as any)
      .mockResolvedValueOnce({
        ok: true,
        missed: false,
        available: true,
        generation: 2,
        cursor: 9,
        events: [
          {
            cursor: 7,
            kind: "action_start",
            data: { id: "a9", goal: "tail tool", kind: "read_file" },
          },
          {
            cursor: 8,
            kind: "action_start",
            data: { id: "a10", goal: "batch sibling", kind: "read_file" },
          },
        ],
      } as any);

    vi.spyOn(api, "sessionTranscript").mockResolvedValue({
      display: [
        { role: "user", text: "go" },
        {
          type: "card",
          id: "a1",
          goal: "checkpointed",
          kind: "read_file",
          result: "disk",
        },
      ],
    } as any);

    const { pullChatEvents } = createChatEventsReattach({
      cancelled: () => false,
      loadGen: 1,
      transcriptLoadGenRef,
      streamGenRef,
      reattachGen: 1,
      reattachSid: "sess-mid",
      cachedSessionIdRef,
      localStreamActiveRef,
      userStoppedRef,
      lastAppliedCursorRef,
      ringGenerationRef,
      detachedBusyRef,
      runnerBusyPollGenRef,
      itemsRef,
      transcriptFpRef,
      chatEventsPollTimerRef,
      applyStreamEventRef: {
        current: (ev) => {
          applied.push(ev.kind);
          if (ev.kind === "action_start" && ev.data?.id) {
            items = appendActionStartCard(items, ev.data);
            itemsRef.current = items;
          }
        },
      },
      flushTypewriterRef: { current: () => {} },
      maybeRunQueuedResumeRef: { current: () => {} },
      maybeDrainQueueRef: { current: () => {} },
      clearChatEventsPoll: () => {},
      setItems: (next) => {
        items = typeof next === "function" ? next(items) : next;
        itemsRef.current = items;
      },
      setTranscriptStale: () => {},
      setTurnOpen: () => {},
      setStatus: () => {},
    });

    const keepPolling = await pullChatEvents();
    expect(keepPolling).toBe(true);
    expect(chatEvents).toHaveBeenCalledTimes(2);
    // First miss used the stale since; retry after hydrate uses since=0.
    expect(chatEvents.mock.calls[0][0]).toMatchObject({ since: 1, generation: 2 });
    expect(chatEvents.mock.calls[1][0]).toMatchObject({ since: 0, generation: 2 });
    expect(applied).toEqual(["action_start", "action_start"]);
    const cardIds = items
      .filter((i): i is Extract<Item, { kind: "card" }> => i.kind === "card")
      .map((c) => c.card.id);
    expect(cardIds).toContain("a1");
    expect(cardIds).toContain("a9");
    expect(cardIds).toContain("a10");
    // Advances to the replay high-water (not a synthesized mid-gap cursor).
    expect(lastAppliedCursorRef.current).toBe(9);
    expect(ringGenerationRef.current).toBe(2);
    expect(detachedBusyRef.current).toBe(true);
  });

  it("on long ring_miss: hydrates disk once and does not invent ring frames", async () => {
    const applied: string[] = [];
    let items: Item[] = [];
    const itemsRef = { current: items };
    const lastAppliedCursorRef = { current: 40 };
    const ringGenerationRef = { current: 3 as number | undefined };
    const detachedBusyRef = { current: true };

    const chatEvents = vi.spyOn(api, "chatEvents").mockResolvedValue({
      ok: false,
      missed: true,
      available: false,
      code: "ring_miss",
      generation: 0,
      cursor: 0,
      events: [],
      retained: 0,
    } as any);

    vi.spyOn(api, "sessionTranscript").mockResolvedValue({
      display: [
        { role: "user", text: "long turn" },
        {
          type: "card",
          id: "old-1",
          goal: "survived on disk",
          kind: "read_file",
          result: "yes",
        },
      ],
    } as any);

    const { pullChatEvents } = createChatEventsReattach({
      cancelled: () => false,
      loadGen: 1,
      transcriptLoadGenRef: { current: 1 },
      streamGenRef: { current: 7 },
      reattachGen: 7,
      reattachSid: "sess-long",
      cachedSessionIdRef: { current: "sess-long" },
      localStreamActiveRef: { current: false },
      userStoppedRef: { current: false },
      lastAppliedCursorRef,
      ringGenerationRef,
      detachedBusyRef,
      runnerBusyPollGenRef: { current: 0 },
      itemsRef,
      transcriptFpRef: { current: "" },
      chatEventsPollTimerRef: { current: null },
      applyStreamEventRef: { current: (ev) => { applied.push(ev.kind); } },
      flushTypewriterRef: { current: () => {} },
      maybeRunQueuedResumeRef: { current: () => {} },
      maybeDrainQueueRef: { current: () => {} },
      clearChatEventsPoll: () => {},
      setItems: (next) => {
        items = typeof next === "function" ? next(items) : next;
        itemsRef.current = items;
      },
      setTranscriptStale: () => {},
      setTurnOpen: () => {},
      setStatus: () => {},
    });

    const keepPolling = await pullChatEvents();
    expect(keepPolling).toBe(true);
    expect(chatEvents).toHaveBeenCalledTimes(1);
    expect(applied).toEqual([]);
    expect(lastAppliedCursorRef.current).toBe(0);
    expect(ringGenerationRef.current).toBeUndefined();
    expect(detachedBusyRef.current).toBe(true);
    expect(
      items.some((i) => i.kind === "card" && i.card.id === "old-1"),
    ).toBe(true);
  });

  it("on ring_miss equal-card hydrate: keeps a still-pending command approval", async () => {
    const hash = "d".repeat(64);
    let items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      {
        kind: "card",
        card: {
          id: "a1",
          goal: "run",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { adapter: "local" },
        },
      },
      {
        kind: "command_approval",
        id: "call-1",
        command: "ssh prod reboot",
        commandHash: hash,
        sessionId: "sess-appr",
        workspaceRoot: "/repo",
        category: "remote",
        reason: "ssh",
        matched: "ssh",
        status: "pending",
      },
    ];
    const itemsRef = { current: items };

    vi.spyOn(api, "chatEvents").mockResolvedValue({
      ok: false,
      missed: true,
      available: false,
      code: "ring_miss",
      generation: 0,
      cursor: 0,
      events: [],
      retained: 0,
    } as any);

    // Equal card count, no approval on disk — merge must still preserve pending.
    vi.spyOn(api, "sessionTranscript").mockResolvedValue({
      display: [
        { type: "message", role: "user", text: "go" },
        {
          type: "card",
          id: "a1",
          goal: "run",
          kind: "run_command",
          result: { adapter: "local", duration_ms: 4 },
        },
      ],
    } as any);

    const { pullChatEvents } = createChatEventsReattach({
      cancelled: () => false,
      loadGen: 1,
      transcriptLoadGenRef: { current: 1 },
      streamGenRef: { current: 1 },
      reattachGen: 1,
      reattachSid: "sess-appr",
      cachedSessionIdRef: { current: "sess-appr" },
      localStreamActiveRef: { current: false },
      userStoppedRef: { current: false },
      lastAppliedCursorRef: { current: 5 },
      ringGenerationRef: { current: 1 as number | undefined },
      detachedBusyRef: { current: true },
      runnerBusyPollGenRef: { current: 0 },
      itemsRef,
      transcriptFpRef: { current: "" },
      chatEventsPollTimerRef: { current: null },
      applyStreamEventRef: { current: () => {} },
      flushTypewriterRef: { current: () => {} },
      maybeRunQueuedResumeRef: { current: () => {} },
      maybeDrainQueueRef: { current: () => {} },
      clearChatEventsPoll: () => {},
      setItems: (next) => {
        items = typeof next === "function" ? next(items) : next;
        itemsRef.current = items;
      },
      setTranscriptStale: () => {},
      setTurnOpen: () => {},
      setStatus: () => {},
    });

    await pullChatEvents();
    expect(
      items.some(
        (i) => i.kind === "command_approval" && i.commandHash === hash && i.status === "pending",
      ),
    ).toBe(true);
    const card = items.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.result?.duration_ms).toBe(4);
  });
});
