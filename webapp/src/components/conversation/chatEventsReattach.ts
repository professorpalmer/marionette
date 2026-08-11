/**
 * Mid-turn chatEvents reattach for session-switch / bridge.
 * Prefers a live SSE ring watch; falls back to JSON pull + 1Hz poll.
 */

import { api } from "../../lib/api";
import type { Item } from "../TranscriptList";
import {
  CHAT_EVENTS_POLL_MS,
  chatFrameToStreamEvent,
  cursorAfterReplayMiss,
  isChatEventReplayMiss,
  isTerminalStreamKind,
  nextAppliedCursor,
  ringGenerationAfterReplayMiss,
  shouldAdvanceReplayCursor,
  shouldApplyReattachFrame,
  shouldHydrateTranscriptOnReplayMiss,
  shouldPollChatEvents,
  shouldRetryRingAfterReplayMiss,
} from "./chatEvents";
import {
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./transcriptItems";
import { writeTranscriptCache } from "./transcriptCache";
import { preserveOrThinking } from "./runnersBusy";
import { reattachSessionStateFailureDecision } from "./sessionHydrate";

export type ChatEventsReattachDeps = {
  cancelled: () => boolean;
  loadGen: number;
  transcriptLoadGenRef: { current: number };
  streamGenRef: { current: number };
  reattachGen: number;
  reattachSid: string;
  cachedSessionIdRef: { current: string | null };
  localStreamActiveRef: { current: boolean };
  userStoppedRef: { current: boolean };
  lastAppliedCursorRef: { current: number };
  ringGenerationRef: { current: number | undefined };
  detachedBusyRef: { current: boolean };
  runnerBusyPollGenRef: { current: number };
  itemsRef: { current: Item[] };
  transcriptFpRef: { current: string };
  chatEventsPollTimerRef: { current: number | null };
  /** Cancel for live ``?watch=1`` SSE; cleared with poll on session switch/Stop. */
  chatEventsLiveCancelRef: { current: null | (() => void) };
  applyStreamEventRef: { current: (ev: { kind: string; data?: any }) => void };
  flushTypewriterRef: { current: () => void };
  maybeRunQueuedResumeRef: { current: () => void };
  maybeDrainQueueRef: { current: () => void };
  clearChatEventsPoll: () => void;
  setItems: (items: Item[] | ((prev: Item[]) => Item[])) => void;
  setTranscriptStale: (v: boolean) => void;
  setTurnOpen: (v: boolean) => void;
  setStatus: (updater: any) => void;
};

export function createChatEventsReattach(deps: ChatEventsReattachDeps) {
  const {
    cancelled,
    loadGen,
    transcriptLoadGenRef,
    streamGenRef,
    reattachGen,
    reattachSid,
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
    chatEventsLiveCancelRef,
    applyStreamEventRef,
    flushTypewriterRef,
    maybeRunQueuedResumeRef,
    maybeDrainQueueRef,
    clearChatEventsPoll,
    setItems,
    setTranscriptStale,
    setTurnOpen,
    setStatus,
  } = deps;

  const fenceOk = () => shouldApplyReattachFrame({
    streamGen: streamGenRef.current,
    reattachGen,
    cachedSessionId: cachedSessionIdRef.current,
    reattachSid,
  });

  const hydrateDurableTranscript = async (): Promise<void> => {
    // Busy-poll skips disk refresh while chatEvents poll is armed — await a
    // durable baseline before any ring retry so tool/activity tails merge
    // coherently (transcript first, then retained frames).
    const missHydrateGen = ++runnerBusyPollGenRef.current;
    const missSid = reattachSid;
    try {
      const tres = await api.sessionTranscript(missSid);
      if (missHydrateGen !== runnerBusyPollGenRef.current) return;
      if (cancelled()) return;
      if (loadGen !== transcriptLoadGenRef.current) return;
      if (streamGenRef.current !== reattachGen) return;
      if (cachedSessionIdRef.current !== missSid) return;
      if (localStreamActiveRef.current) return;
      const loadedItems = transcriptResponseToItems(tres);
      const next = mergeTranscriptItems(itemsRef.current, loadedItems);
      const fp = transcriptFingerprint(next);
      if (fp === transcriptFpRef.current) return;
      transcriptFpRef.current = fp;
      setItems(next);
      itemsRef.current = next;
      writeTranscriptCache(missSid, next);
      setTranscriptStale(false);
      // Keep detached-busy chrome; do not clear status / poll.
    } catch {
      // Disk hydrate is best-effort; ring retry / poll may still catch up.
    }
  };

  const pullChatEvents = async (missRetried = false): Promise<boolean> => {
    if (cancelled()) return false;
    if (loadGen !== transcriptLoadGenRef.current) return false;
    if (!fenceOk()) return false;
    if (localStreamActiveRef.current || userStoppedRef.current) return false;
    // Live watch owns the turn — do not double-apply via poll.
    if (chatEventsLiveCancelRef.current != null) return false;
    try {
      const replay = await api.chatEvents({
        session: reattachSid,
        since: lastAppliedCursorRef.current,
        ...(ringGenerationRef.current != null
          ? { generation: ringGenerationRef.current }
          : {}),
      });
      if (cancelled()) return false;
      if (loadGen !== transcriptLoadGenRef.current) return false;
      if (!fenceOk()) return false;
      if (localStreamActiveRef.current || userStoppedRef.current) return false;
      if (chatEventsLiveCancelRef.current != null) return false;

      if (isChatEventReplayMiss(replay)) {
        const prevGen = ringGenerationRef.current;
        ringGenerationRef.current = ringGenerationAfterReplayMiss(replay, prevGen);
        // Evicted / wrong-generation frames: do not treat as catch-up.
        lastAppliedCursorRef.current = cursorAfterReplayMiss(
          replay,
          lastAppliedCursorRef.current,
        );
        if (shouldHydrateTranscriptOnReplayMiss(replay)) {
          await hydrateDurableTranscript();
        }
        // cursor_gap / refreshed generation_mismatch: retry once with the
        // recovered cursor/gen so the retained tool/activity tail applies now.
        // ring_miss stays hydrate-only — never synthesize missing frames.
        if (
          shouldRetryRingAfterReplayMiss(replay, {
            alreadyRetried: missRetried,
            prevGeneration: prevGen,
            nextGeneration: ringGenerationRef.current,
          })
        ) {
          return pullChatEvents(true);
        }
        return shouldPollChatEvents({
          detachedBusy: detachedBusyRef.current,
          localStreamActive: localStreamActiveRef.current,
          userStopped: userStoppedRef.current,
          sawTerminal: false,
        });
      }

      if (typeof replay.generation === "number" && replay.generation > 0) {
        ringGenerationRef.current = replay.generation;
      }

      let sawTerminal = false;
      const frames = Array.isArray(replay.events) ? replay.events : [];
      for (const frame of frames) {
        if (!fenceOk()) return false;
        applyStreamEventRef.current(chatFrameToStreamEvent(frame));
        if (isTerminalStreamKind(frame.kind)) sawTerminal = true;
      }
      if (shouldAdvanceReplayCursor(replay)) {
        lastAppliedCursorRef.current = nextAppliedCursor(
          lastAppliedCursorRef.current,
          frames,
          replay.cursor,
        );
      }

      if (sawTerminal) {
        flushTypewriterRef.current();
        detachedBusyRef.current = false;
        clearChatEventsPoll();
        maybeRunQueuedResumeRef.current();
        maybeDrainQueueRef.current();
        return false;
      }
      return shouldPollChatEvents({
        detachedBusy: detachedBusyRef.current,
        localStreamActive: localStreamActiveRef.current,
        userStopped: userStoppedRef.current,
        sawTerminal: false,
      });
    } catch {
      return shouldPollChatEvents({
        detachedBusy: detachedBusyRef.current,
        localStreamActive: localStreamActiveRef.current,
        userStopped: userStoppedRef.current,
        sawTerminal: false,
      });
    }
  };

  const startChatEventsPoll = () => {
    if (cancelled() || localStreamActiveRef.current || userStoppedRef.current) return;
    if (chatEventsLiveCancelRef.current != null) return;
    if (chatEventsPollTimerRef.current != null) return;
    void pullChatEvents().then((keepPolling) => {
      if (!keepPolling || cancelled()) return;
      if (streamGenRef.current !== reattachGen) return;
      if (chatEventsLiveCancelRef.current != null) return;
      if (chatEventsPollTimerRef.current != null) return;
      chatEventsPollTimerRef.current = window.setInterval(() => {
        void pullChatEvents().then((cont) => {
          if (!cont) clearChatEventsPoll();
        });
      }, CHAT_EVENTS_POLL_MS);
    });
  };

  const settleLiveTerminal = () => {
    flushTypewriterRef.current();
    detachedBusyRef.current = false;
    clearChatEventsPoll();
    maybeRunQueuedResumeRef.current();
    maybeDrainQueueRef.current();
  };

  /**
   * Prefer live ``?watch=1`` SSE while the turn is open.
   * Returns true when a live cancel was installed (poll must wait for
   * onError/onDone). Open miss / transport error falls back to 1Hz poll.
   */
  const startLiveChatEventsWatch = (): boolean => {
    if (cancelled() || localStreamActiveRef.current || userStoppedRef.current) {
      return false;
    }
    if (!fenceOk()) return false;
    if (chatEventsLiveCancelRef.current != null) return true;
    // Already on poll fallback — do not open a racing live stream.
    if (chatEventsPollTimerRef.current != null) return false;

    let sawTerminal = false;
    let appliedAny = false;
    let settled = false;
    // Install cancel before stream callbacks can fire (sync onError/onDone).
    let streamCancel: (() => void) | null = null;
    const cancel = () => { streamCancel?.(); };
    chatEventsLiveCancelRef.current = cancel;
    const finishLiveCancel = () => {
      if (chatEventsLiveCancelRef.current === cancel) {
        chatEventsLiveCancelRef.current = null;
      }
    };
    const fallBackToPoll = () => {
      if (settled || cancelled()) return;
      if (!fenceOk()) return;
      if (localStreamActiveRef.current || userStoppedRef.current) return;
      if (sawTerminal) return;
      finishLiveCancel();
      startChatEventsPoll();
    };

    streamCancel = api.chatEventsLive(
      {
        session: reattachSid,
        since: lastAppliedCursorRef.current,
        ...(ringGenerationRef.current != null
          ? { generation: ringGenerationRef.current }
          : {}),
      },
      (ev) => {
        if (cancelled() || settled) return;
        if (!fenceOk()) return;
        if (localStreamActiveRef.current || userStoppedRef.current) return;
        // Framing done is handled by transport onDone (not delivered here).
        const kind = String(ev?.kind || "");
        if (kind === "done") return;
        appliedAny = true;
        if (typeof (ev as { cursor?: number }).cursor === "number") {
          const c = (ev as { cursor: number }).cursor;
          if (c > lastAppliedCursorRef.current) {
            lastAppliedCursorRef.current = c;
          }
        }
        applyStreamEventRef.current(chatFrameToStreamEvent(ev));
        if (isTerminalStreamKind(kind)) sawTerminal = true;
      },
      () => {
        // Wave 3: framing done / body EOF — settle only when we saw a terminal.
        // Early drop without terminal → poll fallback.
        finishLiveCancel();
        if (settled || cancelled()) return;
        if (!fenceOk()) return;
        if (sawTerminal) {
          settled = true;
          settleLiveTerminal();
          return;
        }
        // Open miss / mid-watch disconnect: keep catching up via poll.
        fallBackToPoll();
      },
      () => {
        finishLiveCancel();
        if (settled || cancelled()) return;
        if (sawTerminal) {
          settled = true;
          settleLiveTerminal();
          return;
        }
        fallBackToPoll();
      },
    );
    return true;
  };

  const startChatEventsReattach = async () => {
    if (cancelled() || localStreamActiveRef.current || userStoppedRef.current) return;
    let running = detachedBusyRef.current;
    if (!running) {
      const maxAttempts = 2;
      for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        try {
          const st = await api.getSessionState();
          if (cancelled()) return;
          if (cachedSessionIdRef.current !== reattachSid) return;
          running = st?.runners?.[reattachSid] === "running";
          if (running) {
            detachedBusyRef.current = true;
            setTurnOpen(true);
            setStatus((prev: any) => preserveOrThinking(prev));
          }
          break;
        } catch {
          const decision = reattachSessionStateFailureDecision({
            attempt,
            maxAttempts,
          });
          if (decision === "retry") {
            await new Promise((r) => setTimeout(r, 100 * attempt));
            continue;
          }
          // Optimistic busy + poll/watch: Ready must not lie while a turn runs.
          // useRunnersBusyPoll clears chrome if the target is actually idle.
          running = true;
          detachedBusyRef.current = true;
          setTurnOpen(true);
          setStatus((prev: any) => preserveOrThinking(prev));
        }
      }
    }
    if (!running) return;
    if (streamGenRef.current !== reattachGen) return;

    // Prefer live SSE while the turn is open; poll only if live attach fails.
    if (startLiveChatEventsWatch()) return;
    startChatEventsPoll();
  };

  return { pullChatEvents, startChatEventsReattach, startLiveChatEventsWatch };
}
