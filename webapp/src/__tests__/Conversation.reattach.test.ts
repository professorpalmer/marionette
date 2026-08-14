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
  isChatEventsReattachArmed,
  shouldRetryRingAfterReplayMiss,
  shouldApplyReattachFrame,
  createChatEventsReattach,
  mergeTranscriptItems,
  appendActionStartCard,
  applyActionResultCard,
  isDurableTerminalActionResult,
  isUpgradeableActionResult,
} from "../components/Conversation";
import { api } from "../lib/api";
import type { Item } from "../components/TranscriptList";
import { chatEventsPath, sessionEventsPath } from "../lib/transport";
import { nextStoreCursor, shouldApplyStoreEvent } from "../components/conversation/storeEvents";

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
    expect(isTerminalStreamKind("interrupted")).toBe(true);
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

  it("treats live watch cancel as reattach-armed (same as poll timer)", () => {
    expect(isChatEventsReattachArmed({
      pollTimer: null,
      liveCancel: null,
    })).toBe(false);
    expect(isChatEventsReattachArmed({
      pollTimer: 1,
      liveCancel: null,
    })).toBe(true);
    expect(isChatEventsReattachArmed({
      pollTimer: null,
      liveCancel: () => {},
    })).toBe(true);
  });

  it("builds watch=1 chatEvents path for live reattach", () => {
    expect(chatEventsPath({
      session: "sess-a",
      since: 3,
      generation: 2,
      watch: true,
    })).toContain("watch=1");
    expect(chatEventsPath({
      session: "sess-a",
      since: 3,
    })).not.toContain("watch=");
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
          result: { status: "ok" },
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
          result: { message: "ok from disk" },
        },
      },
    ];
    const merged = mergeTranscriptItems(local, remote);
    const cards = merged.filter(
      (i): i is Extract<Item, { kind: "card" }> => i.kind === "card",
    );
    expect(cards.map((c) => c.card.id)).toEqual(["a1", "a2"]);
    expect(cards[0].card.result).toEqual({ message: "ok from disk" });
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

    const readEventsSince = vi.spyOn(api, "readEventsSince")
      .mockResolvedValueOnce({
        ok: true,
        session_id: "sess-mid",
        cursor: 2,
        events: [{
          id: 2,
          kind: "ring_miss",
          data: {
            ok: false,
            missed: true,
            available: false,
            code: "cursor_gap",
            generation: 2,
            cursor: 9,
          },
        }],
      } as any)
      .mockResolvedValueOnce({
        ok: true,
        session_id: "sess-mid",
        cursor: 4,
        events: [
          {
            id: 3,
            kind: "stream",
            data: {
              cursor: 7,
              kind: "action_start",
              data: { id: "a9", goal: "tail tool", kind: "read_file" },
              generation: 2,
            },
          },
          {
            id: 4,
            kind: "stream",
            data: {
              cursor: 8,
              kind: "action_start",
              data: { id: "a10", goal: "batch sibling", kind: "read_file" },
              generation: 2,
            },
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
      chatEventsLiveCancelRef: { current: null },
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
    expect(readEventsSince).toHaveBeenCalledTimes(2);
    expect(readEventsSince.mock.calls[0][0]).toMatchObject({ since: 1, generation: 2 });
    // Retry advances past the ring_miss store id (not ring since=0).
    expect(readEventsSince.mock.calls[1][0]).toMatchObject({ since: 2, generation: 2 });
    expect(applied).toEqual(["action_start", "action_start"]);
    const cardIds = items
      .filter((i): i is Extract<Item, { kind: "card" }> => i.kind === "card")
      .map((c) => c.card.id);
    expect(cardIds).toContain("a1");
    expect(cardIds).toContain("a9");
    expect(cardIds).toContain("a10");
    // Advances to the store high-water cursor.
    expect(lastAppliedCursorRef.current).toBe(4);
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

    const readEventsSince = vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true,
      session_id: "sess-long",
      cursor: 41,
      events: [{
        id: 41,
        kind: "ring_miss",
        data: {
          ok: false,
          missed: true,
          available: false,
          code: "ring_miss",
          generation: 0,
          cursor: 0,
        },
      }],
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
      chatEventsLiveCancelRef: { current: null },
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
    expect(readEventsSince).toHaveBeenCalledTimes(1);
    expect(applied).toEqual([]);
    expect(lastAppliedCursorRef.current).toBe(41);
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

    vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true,
      session_id: "sess-appr",
      cursor: 6,
      events: [{
        id: 6,
        kind: "ring_miss",
        data: {
          ok: false,
          missed: true,
          available: false,
          code: "ring_miss",
          generation: 0,
          cursor: 0,
        },
      }],
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
      chatEventsLiveCancelRef: { current: null },
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

describe("Wave 4 command-job reattach fences", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("treats pending receipts as upgradeable and terminal receipts as durable", () => {
    expect(isUpgradeableActionResult({ status: "pending", job_id: "local-cmd-1" })).toBe(true);
    expect(isUpgradeableActionResult({ status: "registered" })).toBe(true);
    expect(isUpgradeableActionResult({ status: "running" })).toBe(true);
    expect(isDurableTerminalActionResult({ status: "pending", job_id: "local-cmd-1" })).toBe(false);
    expect(isDurableTerminalActionResult({
      status: "completed",
      job_id: "local-cmd-1",
      terminal_receipt: { status: "completed", summary: "exit 0" },
    })).toBe(true);
    expect(isUpgradeableActionResult({
      status: "completed",
      terminal_receipt: { status: "completed" },
    })).toBe(false);
    // Settled status without terminal_receipt is also not upgradeable.
    expect(isUpgradeableActionResult({ status: "completed" })).toBe(false);
    expect(isUpgradeableActionResult({ status: "failed" })).toBe(false);
    expect(isUpgradeableActionResult(null)).toBe(false);
  });

  it("keeps the first durable terminal action_result (duplicate frames)", () => {
    const running: Item[] = [{
      kind: "card",
      card: {
        id: "a-cmd",
        goal: "echo hi",
        cwd: null,
        kind: "run_command",
        running: true,
        open: false,
      },
    }];
    const first = applyActionResultCard(running, {
      id: "a-cmd",
      kind: "run_command",
      status: "completed",
      job_id: "local-cmd-1",
      message: "first wins",
      terminal_receipt: { status: "completed", summary: "exit 0 · first" },
    });
    const dup = applyActionResultCard(first, {
      id: "a-cmd",
      kind: "run_command",
      status: "failed",
      job_id: "local-cmd-1",
      message: "late overwrite",
      error: "nope",
      terminal_receipt: { status: "failed", summary: "late" },
    });
    const card = dup.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.running).toBe(false);
    expect(card.card.result?.message).toBe("first wins");
    expect(card.card.result?.status).toBe("completed");
    expect((card.card.result as any)?.terminal_receipt?.summary).toBe("exit 0 · first");
  });

  it("upgrades a pending background receipt to the later terminal body", () => {
    const pending: Item[] = [{
      kind: "card",
      card: {
        id: "a-bg",
        goal: "echo bg",
        cwd: null,
        kind: "run_command",
        running: false,
        open: false,
        result: {
          status: "pending",
          job_id: "local-cmd-bg",
          message: "registered",
        },
      },
    }];
    const next = applyActionResultCard(pending, {
      id: "a-bg",
      kind: "run_command",
      status: "completed",
      job_id: "local-cmd-bg",
      message: "done",
      terminal_receipt: { status: "completed", summary: "exit 0" },
    });
    const card = next.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(card.card.result?.status).toBe("completed");
    expect(card.card.result?.message).toBe("done");
  });

  it("fences session-switch isolation for reattach frames", () => {
    expect(shouldApplyReattachFrame({
      streamGen: 2,
      reattachGen: 2,
      cachedSessionId: "sess-a",
      reattachSid: "sess-a",
    })).toBe(true);
    expect(shouldApplyReattachFrame({
      streamGen: 3,
      reattachGen: 2,
      cachedSessionId: "sess-a",
      reattachSid: "sess-a",
    })).toBe(false);
    expect(shouldApplyReattachFrame({
      streamGen: 2,
      reattachGen: 2,
      cachedSessionId: "sess-b",
      reattachSid: "sess-a",
    })).toBe(false);
    expect(shouldApplyStoreEvent({
      streamGen: 2,
      subscriptionGen: 2,
      cachedSessionId: "sess-a",
      subscriptionSid: "sess-a",
    })).toBe(true);
    expect(shouldApplyStoreEvent({
      streamGen: 2,
      subscriptionGen: 2,
      cachedSessionId: "sess-b",
      subscriptionSid: "sess-a",
    })).toBe(false);
    expect(nextStoreCursor(0, [{ id: 1 }, { id: 3 }], 3)).toBe(3);
  });

  it("drops late ring frames after a session switch (no cross-session merge)", async () => {
    const applied: Array<{ kind: string; id?: string }> = [];
    const cachedSessionIdRef = { current: "sess-a" as string | null };
    const streamGenRef = { current: 1 };

    vi.spyOn(api, "readEventsSince").mockImplementation(async () => {
      // Session switch lands while the request is in flight.
      cachedSessionIdRef.current = "sess-b";
      streamGenRef.current = 2;
      return {
        ok: true,
        session_id: "sess-a",
        cursor: 4,
        events: [
          {
            id: 3,
            kind: "stream",
            data: {
              cursor: 3,
              kind: "action_result",
              data: {
                id: "a-stale",
                status: "completed",
                job_id: "local-cmd-stale",
                message: "must not apply",
              },
              generation: 1,
            },
          },
          {
            id: 4,
            kind: "stream",
            data: { cursor: 4, kind: "assistant_done", data: {}, generation: 1 },
          },
        ],
      } as any;
    });

    const { pullChatEvents } = createChatEventsReattach({
      cancelled: () => false,
      loadGen: 1,
      transcriptLoadGenRef: { current: 1 },
      streamGenRef,
      reattachGen: 1,
      reattachSid: "sess-a",
      cachedSessionIdRef,
      localStreamActiveRef: { current: false },
      userStoppedRef: { current: false },
      lastAppliedCursorRef: { current: 0 },
      ringGenerationRef: { current: 1 as number | undefined },
      detachedBusyRef: { current: true },
      runnerBusyPollGenRef: { current: 0 },
      itemsRef: { current: [] },
      transcriptFpRef: { current: "" },
      chatEventsPollTimerRef: { current: null },
      chatEventsLiveCancelRef: { current: null },
      applyStreamEventRef: {
        current: (ev) => {
          applied.push({ kind: ev.kind, id: ev.data?.id });
        },
      },
      flushTypewriterRef: { current: () => {} },
      maybeRunQueuedResumeRef: { current: () => {} },
      maybeDrainQueueRef: { current: () => {} },
      clearChatEventsPoll: () => {},
      setItems: () => {},
      setTranscriptStale: () => {},
      setTurnOpen: () => {},
      setStatus: () => {},
    });

    const keep = await pullChatEvents();
    expect(keep).toBe(false);
    expect(applied).toEqual([]);
  });

  it("applies retained action_result frames once after stream loss (idempotent)", async () => {
    let items: Item[] = [{
      kind: "card",
      card: {
        id: "a-cmd",
        goal: "echo hi",
        cwd: null,
        kind: "run_command",
        running: true,
        open: false,
      },
    }];
    const itemsRef = { current: items };
    const frame = {
      cursor: 8,
      kind: "action_result",
      data: {
        id: "a-cmd",
        kind: "run_command",
        status: "completed",
        job_id: "local-cmd-1",
        message: "ok",
        terminal_receipt: { status: "completed", summary: "exit 0" },
      },
    };

    vi.spyOn(api, "readEventsSince")
      .mockResolvedValueOnce({
        ok: true,
        session_id: "sess-dup",
        cursor: 2,
        events: [
          { id: 1, kind: "stream", data: { ...frame, generation: 1 } },
          { id: 2, kind: "stream", data: { ...frame, generation: 1 } },
        ],
      } as any);

    const { pullChatEvents } = createChatEventsReattach({
      cancelled: () => false,
      loadGen: 1,
      transcriptLoadGenRef: { current: 1 },
      streamGenRef: { current: 1 },
      reattachGen: 1,
      reattachSid: "sess-dup",
      cachedSessionIdRef: { current: "sess-dup" },
      localStreamActiveRef: { current: false },
      userStoppedRef: { current: false },
      lastAppliedCursorRef: { current: 0 },
      ringGenerationRef: { current: 1 as number | undefined },
      detachedBusyRef: { current: true },
      runnerBusyPollGenRef: { current: 0 },
      itemsRef,
      transcriptFpRef: { current: "" },
      chatEventsPollTimerRef: { current: null },
      chatEventsLiveCancelRef: { current: null },
      applyStreamEventRef: {
        current: (ev) => {
          if (ev.kind === "action_result") {
            items = applyActionResultCard(items, ev.data || {});
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

    await pullChatEvents();
    const cards = items.filter((i) => i.kind === "card");
    expect(cards).toHaveLength(1);
    expect(cards[0].kind === "card" && cards[0].card.result?.status).toBe("completed");
    expect(cards[0].kind === "card" && cards[0].card.result?.message).toBe("ok");
  });
});

describe("mid-turn store-event cursor reattach", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  function reattachDeps(overrides: Record<string, unknown> = {}) {
    const chatEventsLiveCancelRef = { current: null as null | (() => void) };
    const chatEventsPollTimerRef = { current: null as number | null };
    const detachedBusyRef = { current: true };
    const streamGenRef = { current: 1 };
    const cachedSessionIdRef = { current: "sess-live" as string | null };
    const lastAppliedCursorRef = { current: 0 };
    const applied: string[] = [];
    const base = {
      cancelled: () => false,
      loadGen: 1,
      transcriptLoadGenRef: { current: 1 },
      streamGenRef,
      reattachGen: 1,
      reattachSid: "sess-live",
      cachedSessionIdRef,
      localStreamActiveRef: { current: false },
      userStoppedRef: { current: false },
      lastAppliedCursorRef,
      ringGenerationRef: { current: 1 as number | undefined },
      detachedBusyRef,
      runnerBusyPollGenRef: { current: 0 },
      itemsRef: { current: [] as Item[] },
      transcriptFpRef: { current: "" },
      chatEventsPollTimerRef,
      chatEventsLiveCancelRef,
      applyStreamEventRef: {
        current: (ev: { kind: string }) => { applied.push(ev.kind); },
      },
      flushTypewriterRef: { current: () => {} },
      maybeRunQueuedResumeRef: { current: () => {} },
      maybeDrainQueueRef: { current: () => {} },
      clearChatEventsPoll: () => {
        if (chatEventsPollTimerRef.current != null) {
          window.clearInterval(chatEventsPollTimerRef.current);
          chatEventsPollTimerRef.current = null;
        }
        if (chatEventsLiveCancelRef.current) {
          const c = chatEventsLiveCancelRef.current;
          chatEventsLiveCancelRef.current = null;
          c();
        }
      },
      setItems: () => {},
      setTranscriptStale: () => {},
      setTurnOpen: () => {},
      setStatus: () => {},
      ...overrides,
    };
    return {
      ...base,
      applied,
      chatEventsLiveCancelRef,
      chatEventsPollTimerRef,
      detachedBusyRef,
      streamGenRef,
      cachedSessionIdRef,
      lastAppliedCursorRef,
    };
  }

  it("arms store cursor poll on busy reattach (not live watch)", async () => {
    vi.useFakeTimers();
    const deps = reattachDeps();
    const live = vi.spyOn(api, "chatEventsLive");
    const readEventsSince = vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true,
      session_id: "sess-live",
      cursor: 4,
      events: [{
        id: 4,
        kind: "stream",
        data: { cursor: 4, kind: "message_delta", data: { text: "hi" }, generation: 1 },
      }],
    } as any);
    vi.spyOn(api, "getSessionState").mockResolvedValue({
      runners: { "sess-live": "running" },
    } as any);

    const { startChatEventsReattach } = createChatEventsReattach(deps as any);
    await startChatEventsReattach();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(0);

    expect(live).not.toHaveBeenCalled();
    expect(readEventsSince).toHaveBeenCalled();
    expect(readEventsSince.mock.calls[0][0]).toMatchObject({
      session: "sess-live",
      since: 0,
      generation: 1,
    });
    expect(deps.chatEventsLiveCancelRef.current).toBeNull();
    expect(deps.chatEventsPollTimerRef.current).not.toBeNull();
    expect(deps.applied).toEqual(["message_delta"]);
    expect(deps.lastAppliedCursorRef.current).toBe(4);
  });

  it("drops store stream events after session switch (no cross-session bleed)", async () => {
    const deps = reattachDeps();
    vi.spyOn(api, "readEventsSince").mockImplementation(async () => {
      deps.cachedSessionIdRef.current = "sess-other";
      deps.streamGenRef.current = 2;
      return {
        ok: true,
        session_id: "sess-live",
        cursor: 9,
        events: [{
          id: 9,
          kind: "stream",
          data: {
            cursor: 9,
            kind: "action_result",
            data: { id: "stale", status: "completed" },
            generation: 1,
          },
        }],
      } as any;
    });
    vi.spyOn(api, "getSessionState").mockResolvedValue({
      runners: { "sess-live": "running" },
    } as any);

    const { pullChatEvents } = createChatEventsReattach(deps as any);
    const keep = await pullChatEvents();
    expect(keep).toBe(false);
    expect(deps.applied).toEqual([]);
  });

  it("settles detached-busy on store terminal while keeping poll armed", async () => {
    const deps = reattachDeps();
    vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true,
      session_id: "sess-live",
      cursor: 5,
      events: [{
        id: 5,
        kind: "stream",
        data: { cursor: 5, kind: "assistant_done", data: {}, generation: 1 },
      }],
    } as any);

    const { pullChatEvents } = createChatEventsReattach(deps as any);
    const keep = await pullChatEvents();
    expect(deps.applied).toEqual(["assistant_done"]);
    expect(deps.detachedBusyRef.current).toBe(false);
    expect(keep).toBe(true);
  });

  it("on getSessionState failure optimistic-busy arms store poll", async () => {
    vi.useFakeTimers();
    const turnOpen = vi.fn();
    const setStatus = vi.fn();
    const deps = reattachDeps({
      setTurnOpen: turnOpen,
      setStatus,
    });
    deps.detachedBusyRef.current = false;
    const live = vi.spyOn(api, "chatEventsLive");
    vi.spyOn(api, "readEventsSince").mockResolvedValue({
      ok: true,
      session_id: "sess-live",
      cursor: 0,
      events: [],
    } as any);
    const getSessionState = vi.spyOn(api, "getSessionState").mockRejectedValue(
      new Error("network"),
    );

    const { startChatEventsReattach } = createChatEventsReattach(deps as any);
    const done = startChatEventsReattach();
    // Retry uses setTimeout(100); do not runAllTimers (store poll interval loops).
    await vi.advanceTimersByTimeAsync(250);
    await done;
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(0);

    expect(getSessionState.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(deps.detachedBusyRef.current).toBe(true);
    expect(turnOpen).toHaveBeenCalledWith(true);
    expect(live).not.toHaveBeenCalled();
    expect(deps.chatEventsPollTimerRef.current).not.toBeNull();
  });

  it("sessionEventsPath builds the store cursor URL", () => {
    expect(sessionEventsPath({ session: "s1", since: 3, generation: 2 })).toContain(
      "/api/session/events",
    );
    expect(sessionEventsPath({ session: "s1", since: 3 })).toContain("since=3");
  });
});
