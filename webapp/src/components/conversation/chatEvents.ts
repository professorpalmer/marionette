/** Advance last-applied SSE ring cursor after a chatEvents replay batch. */
export function nextAppliedCursor(
  lastApplied: number,
  frames: { cursor: number }[],
  replayCursor?: number,
): number {
  let next = lastApplied;
  for (const frame of frames) {
    if (typeof frame.cursor === "number" && frame.cursor > next) {
      next = frame.cursor;
    }
  }
  if (typeof replayCursor === "number" && replayCursor > next) {
    next = replayCursor;
  }
  return next;
}

/** Terminal SSE kinds that end a turn (stop mid-turn reattach polling). */
export function isTerminalStreamKind(kind: string): boolean {
  return (
    kind === "assistant_done"
    || kind === "done"
    || kind === "error"
    || kind === "auto_halt"
  );
}

/** Whether a detached-busy session should keep polling chatEvents. */
export function shouldPollChatEvents(opts: {
  detachedBusy: boolean;
  localStreamActive: boolean;
  userStopped: boolean;
  sawTerminal: boolean;
}): boolean {
  if (opts.sawTerminal || opts.userStopped || opts.localStreamActive) return false;
  return opts.detachedBusy;
}

/**
 * When a turn starts outside this tab's EventSource (Discord bridge queue,
 * another client, session already open when the runner flips to running),
 * runners-poll must arm chatEvents reattach — transcript disk polls alone
 * stay empty until the turn finishes, which looks like a stuck
 * "Waiting on provider…" until restart hydrates the final message.
 */
export function shouldArmChatEventsFromRunners(opts: {
  runnerBusy: boolean;
  localStreamActive: boolean;
  userStopped: boolean;
  chatEventsPollArmed: boolean;
}): boolean {
  if (!opts.runnerBusy || opts.localStreamActive || opts.userStopped) return false;
  return !opts.chatEventsPollArmed;
}

/** Fields checked when classifying a chatEvents miss vs empty catch-up. */
export type ChatEventReplayMissFields = {
  ok?: boolean;
  missed?: boolean;
  available?: boolean;
  code?: string;
  generation?: number;
};

/** True when GET /api/chat/events reports the ring is unavailable (not catch-up). */
export function isChatEventReplayMiss(replay: ChatEventReplayMissFields): boolean {
  if (replay.missed === true) return true;
  if (replay.ok === false) return true;
  if (replay.available === false) return true;
  return false;
}

/** Whether a replay response should advance lastAppliedCursor. */
export function shouldAdvanceReplayCursor(replay: ChatEventReplayMissFields): boolean {
  return !isChatEventReplayMiss(replay);
}

/** Refresh ring generation pin after a replay miss. */
export function ringGenerationAfterReplayMiss(
  replay: ChatEventReplayMissFields,
  current: number | undefined,
): number | undefined {
  if (
    replay.code === "generation_mismatch"
    && typeof replay.generation === "number"
    && replay.generation > 0
  ) {
    return replay.generation;
  }
  if (replay.code === "ring_miss") {
    return undefined;
  }
  return current;
}

/**
 * On ring miss / generation mismatch / cursor gap, fall back to disk transcript
 * hydrate (busy-poll skips sessionTranscript while chatEvents poll owns the turn).
 */
export function shouldHydrateTranscriptOnReplayMiss(replay: ChatEventReplayMissFields): boolean {
  return isChatEventReplayMiss(replay);
}

/**
 * Cursor after a replay miss. Ring eviction / generation change / cursor gap
 * means our `since` is no longer contiguous — reset so the next poll can
 * catch up (or hydrate from disk).
 *
 * Never invent mid-gap cursors: a miss is not successful catch-up.
 */
export function cursorAfterReplayMiss(
  replay: { code?: string },
  current: number,
): number {
  if (
    replay.code === "ring_miss"
    || replay.code === "generation_mismatch"
    || replay.code === "cursor_gap"
  ) {
    return 0;
  }
  return current;
}

/**
 * After applying miss recovery (cursor reset + optional gen pin + disk hydrate),
 * whether to immediately retry GET /api/chat/events once.
 *
 * - ``cursor_gap``: ring still holds a tool/activity tail — retry with since=0
 *   so retained frames apply without waiting for the poll interval.
 * - ``generation_mismatch``: retry only when the pin refreshed to the live gen.
 * - ``ring_miss``: nothing to replay; hydrate-only (no fake catch-up).
 */
export function shouldRetryRingAfterReplayMiss(
  replay: ChatEventReplayMissFields,
  opts: {
    alreadyRetried: boolean;
    prevGeneration?: number;
    nextGeneration?: number;
  },
): boolean {
  if (opts.alreadyRetried) return false;
  if (replay.code === "cursor_gap") return true;
  if (
    replay.code === "generation_mismatch"
    && opts.nextGeneration != null
    && opts.nextGeneration !== opts.prevGeneration
  ) {
    return true;
  }
  return false;
}

/** Map a retained ring frame to the live stream-event shape. */
export function chatFrameToStreamEvent(frame: {
  kind: string;
  data?: any;
}): { kind: string; data?: any } {
  return { kind: frame.kind, data: frame.data };
}

/** Bounded interval for mid-turn chatEvents reattach while detached-busy. */
export const CHAT_EVENTS_POLL_MS = 1000;
