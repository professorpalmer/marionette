import { describe, expect, it } from "vitest";
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
} from "../components/Conversation";

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
