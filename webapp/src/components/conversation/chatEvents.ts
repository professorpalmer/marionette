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
    || kind === "interrupted"
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

/** True when mid-turn reattach owns the turn via live watch and/or 1Hz poll. */
export function isChatEventsReattachArmed(opts: {
  pollTimer: number | null | undefined;
  liveCancel: (() => void) | null | undefined;
}): boolean {
  return opts.pollTimer != null || opts.liveCancel != null;
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

/**
 * Wave 4 generation + session fence for mid-turn reattach.
 * Late frames from a prior stream generation or a switched session must not
 * merge into the active transcript (no cross-session / stale updates).
 */
export function shouldApplyReattachFrame(opts: {
  streamGen: number;
  reattachGen: number;
  cachedSessionId: string | null | undefined;
  reattachSid: string;
}): boolean {
  if (opts.streamGen !== opts.reattachGen) return false;
  if (!opts.reattachSid) return false;
  return opts.cachedSessionId === opts.reattachSid;
}

/** Prefer live ``?watch=1`` for detached-busy mid-turn reattach. */
export function shouldStartLiveChatEventsWatch(opts: {
  detachedBusy: boolean;
  localStreamActive: boolean;
  userStopped: boolean;
}): boolean {
  return opts.detachedBusy && !opts.localStreamActive && !opts.userStopped;
}

/** Live ``?watch=1`` control frame for a mid-watch ring miss / cursor gap. */
export function isChatEventsLiveRingMissFrame(ev: {
  kind?: string;
  data?: ChatEventReplayMissFields | null;
}): boolean {
  if (ev.kind !== "ring_miss") return false;
  const data = ev.data;
  if (data == null || typeof data !== "object") return false;
  return isChatEventReplayMiss(data);
}

/** Transport open-miss for live watch (JSON 409 from stream_chat_events). */
export function isChatEventsLiveOpenMiss(error: unknown): boolean {
  if (error != null && typeof error === "object") {
    const status = (error as { status?: unknown }).status;
    if (status === 409) return true;
  }
  const message = error instanceof Error
    ? error.message
    : typeof error === "string" ? error : "";
  return /\b409\b/.test(message);
}

export type LiveWatchCloseDecision = "settle" | "reconnect" | "fallback";

/**
 * How a live watch should close.
 * Terminal settles without fallback. Open miss (409) goes straight to store
 * poll. Other error/EOF gets one reconnect, then store poll.
 */
export function liveWatchCloseDecision(opts: {
  sawTerminal: boolean;
  openMiss: boolean;
  reconnectUsed: boolean;
}): LiveWatchCloseDecision {
  if (opts.sawTerminal) return "settle";
  if (opts.openMiss) return "fallback";
  if (!opts.reconnectUsed) return "reconnect";
  return "fallback";
}

/** Advance the live ring cursor only from an accepted newer frame cursor. */
export function ringCursorAfterLiveFrame(
  lastApplied: number,
  frameCursor: number | undefined,
): number {
  if (typeof frameCursor === "number" && frameCursor > lastApplied) {
    return frameCursor;
  }
  return lastApplied;
}

/** Start a stream generation with its fresh server ring. */
export function beginChatStreamGeneration(opts: {
  streamGenRef: { current: number };
  lastAppliedRingCursorRef: { current: number };
  ringGenerationRef: { current: number | undefined };
}): number {
  opts.lastAppliedRingCursorRef.current = 0;
  opts.ringGenerationRef.current = undefined;
  return ++opts.streamGenRef.current;
}

/** Bounded interval for mid-turn chatEvents reattach while detached-busy. */
export const CHAT_EVENTS_POLL_MS = 1000;
