/**
 * Mid-turn chatEvents reattach (pull + light poll) for session-switch / bridge.
 * Conversation supplies refs / API / chrome setters.
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
  shouldHydrateTranscriptOnReplayMiss,
  shouldPollChatEvents,
} from "./chatEvents";
import {
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./transcriptItems";
import { writeTranscriptCache } from "./transcriptCache";
import { preserveOrThinking } from "./runnersBusy";

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

  const pullChatEvents = async (generationMismatchRetried = false): Promise<boolean> => {
    if (cancelled()) return false;
    if (loadGen !== transcriptLoadGenRef.current) return false;
    if (streamGenRef.current !== reattachGen) return false;
    if (cachedSessionIdRef.current !== reattachSid) return false;
    if (localStreamActiveRef.current || userStoppedRef.current) return false;
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
      if (streamGenRef.current !== reattachGen) return false;
      if (cachedSessionIdRef.current !== reattachSid) return false;
      if (localStreamActiveRef.current || userStoppedRef.current) return false;

      if (isChatEventReplayMiss(replay)) {
        const prevGen = ringGenerationRef.current;
        ringGenerationRef.current = ringGenerationAfterReplayMiss(replay, prevGen);
        // Evicted / wrong-generation frames: do not treat as catch-up.
        lastAppliedCursorRef.current = cursorAfterReplayMiss(
          replay,
          lastAppliedCursorRef.current,
        );
        // Busy-poll skips disk refresh while chatEvents poll is armed —
        // hydrate once (per miss) so mid-turn UI does not freeze.
        if (shouldHydrateTranscriptOnReplayMiss(replay)) {
          const missHydrateGen = ++runnerBusyPollGenRef.current;
          const missSid = reattachSid;
          void api.sessionTranscript(missSid).then((tres) => {
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
          }).catch(() => {});
        }
        if (
          replay.code === "generation_mismatch"
          && !generationMismatchRetried
          && ringGenerationRef.current != null
          && ringGenerationRef.current !== prevGen
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
        if (streamGenRef.current !== reattachGen) return false;
        if (cachedSessionIdRef.current !== reattachSid) return false;
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

  const startChatEventsReattach = async () => {
    if (cancelled() || localStreamActiveRef.current || userStoppedRef.current) return;
    let running = detachedBusyRef.current;
    if (!running) {
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
      } catch {
        return;
      }
    }
    if (!running) return;

    const keepPolling = await pullChatEvents();
    if (!keepPolling || cancelled()) return;
    if (streamGenRef.current !== reattachGen) return;
    if (chatEventsPollTimerRef.current != null) return;
    chatEventsPollTimerRef.current = window.setInterval(() => {
      void pullChatEvents().then((cont) => {
        if (!cont) clearChatEventsPoll();
      });
    }, CHAT_EVENTS_POLL_MS);
  };

  return { pullChatEvents, startChatEventsReattach };
}
