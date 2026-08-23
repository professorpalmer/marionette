/**
 * Unified store-event subscription for session-switch / reattach / busy.
 * One cursor via ``api.readEventsSince`` — not a third poller beside runners.
 */

import { api } from "../../lib/api";
import type { Item } from "../TranscriptList";
import {
  cursorAfterReplayMiss,
  isChatEventsReattachArmed,
  isTerminalStreamKind,
  ringGenerationAfterReplayMiss,
  shouldHydrateTranscriptOnReplayMiss,
  shouldPollChatEvents,
  shouldRetryRingAfterReplayMiss,
} from "./chatEvents";
import {
  STORE_EVENTS_POLL_MS,
  isStoreRingMissEvent,
  nextStoreCursor,
  shouldApplyStoreEvent,
  storeBatchSawTerminal,
  storeRingMissFields,
  storeStreamToStreamEvent,
} from "./storeEvents";
import {
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./transcriptItems";
import { writeTranscriptCache } from "./transcriptCache";
import {
  preserveOrThinking,
  runnersBusyTickDecision,
  staleLocalStreamTickDecision,
  userStoppedBusyChrome,
  RUNNERS_IDLE_CONFIRM_POLLS,
} from "./runnersBusy";
import { reattachSessionStateFailureDecision } from "./sessionHydrate";
import { shouldApplySwarmLiveMerge } from "./streamApply";
import {
  sessionStateShowsAwaitingSwarm,
  SWARM_AWAIT_HINT,
} from "./swarmPoll";
import type { SessionStatus } from "./useSessionSwitch";

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
  /** Unified store cursor (read_events_since), not the SSE ring cursor. */
  lastAppliedCursorRef: { current: number };
  ringGenerationRef: { current: number | undefined };
  detachedBusyRef: { current: boolean };
  runnerBusyPollGenRef: { current: number };
  itemsRef: { current: Item[] };
  transcriptFpRef: { current: string };
  chatEventsPollTimerRef: { current: number | null };
  /** Legacy live-watch cancel slot; store cursor owns the turn (kept for armed()). */
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
  setCompactingStatus?: (v: string | null) => void;
  setWaitHint?: (v: string | null) => void;
  setBackendPendingSwarms?: (v: boolean | ((prev: boolean) => boolean)) => void;
  turnSettledRef?: { current: boolean };
  abandonStaleLocalStreamRef?: { current: () => void };
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
    setCompactingStatus,
    setWaitHint,
    setBackendPendingSwarms,
    turnSettledRef,
    abandonStaleLocalStreamRef,
  } = deps;

  let consecutiveIdlePolls = 0;
  let staleStreamIdlePolls = 0;
  let sawRunnerBusyThisStream = false;

  const fenceOk = () => shouldApplyStoreEvent({
    streamGen: streamGenRef.current,
    subscriptionGen: reattachGen,
    cachedSessionId: cachedSessionIdRef.current,
    subscriptionSid: reattachSid,
  });

  const chatEventsReattachArmed = () => isChatEventsReattachArmed({
    pollTimer: chatEventsPollTimerRef.current,
    liveCancel: chatEventsLiveCancelRef.current,
  });

  const hydrateDurableTranscript = async (): Promise<void> => {
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
    } catch {
      // Disk hydrate is best-effort.
    }
  };

  const refreshTranscriptFromDisk = async (sid: string): Promise<void> => {
    const pollGen = ++runnerBusyPollGenRef.current;
    try {
      const tres = await api.sessionTranscript(sid);
      if (!shouldApplySwarmLiveMerge({
        pollGen,
        currentGen: runnerBusyPollGenRef.current,
        pollSessionId: sid,
        cachedSessionId: cachedSessionIdRef.current,
        activeSessionId: cachedSessionIdRef.current,
      })) {
        return;
      }
      if (localStreamActiveRef.current) return;
      const loadedItems = transcriptResponseToItems(tres);
      let applied = false;
      setItems((prev) => {
        if (!shouldApplySwarmLiveMerge({
          pollGen,
          currentGen: runnerBusyPollGenRef.current,
          pollSessionId: sid,
          cachedSessionId: cachedSessionIdRef.current,
          activeSessionId: cachedSessionIdRef.current,
        })) {
          return prev;
        }
        if (localStreamActiveRef.current) return prev;
        const next = mergeTranscriptItems(prev, loadedItems);
        const fp = transcriptFingerprint(next);
        if (fp === transcriptFpRef.current) return prev;
        transcriptFpRef.current = fp;
        itemsRef.current = next;
        writeTranscriptCache(sid, next);
        applied = true;
        return next;
      });
      if (applied) setTranscriptStale(false);
    } catch {
      // best-effort
    }
  };

  const applyRunnersEvent = async (data: {
    state?: string | null;
    pending_swarms?: boolean;
    runners?: Record<string, string>;
  }): Promise<boolean> => {
    if (!fenceOk()) return false;
    if (userStoppedRef.current) {
      consecutiveIdlePolls = 0;
      staleStreamIdlePolls = 0;
      sawRunnerBusyThisStream = false;
      detachedBusyRef.current = false;
      clearChatEventsPoll();
      setBackendPendingSwarms?.(false);
      setStatus((prev: SessionStatus) => userStoppedBusyChrome(prev));
      return false;
    }

    const sid = reattachSid;
    const runners = data?.runners || {};
    const running = runners[sid] === "running";
    const awaitingSwarm = sessionStateShowsAwaitingSwarm({
      state: data?.state,
      pendingSwarms: !!data?.pending_swarms,
      userStopped: userStoppedRef.current,
    });

    if (localStreamActiveRef.current) {
      if (running || awaitingSwarm) {
        sawRunnerBusyThisStream = true;
        staleStreamIdlePolls = 0;
        return true;
      }
      const nextIdlePolls = staleStreamIdlePolls + 1;
      const staleTick = staleLocalStreamTickDecision({
        localStreamActive: true,
        userStopped: userStoppedRef.current,
        runnerBusy: running,
        awaitingSwarm,
        turnSettled: turnSettledRef?.current ?? false,
        sawRunnerBusyThisStream,
        consecutiveIdlePolls: nextIdlePolls,
      });
      if (staleTick.kind === "hold_unconfirmed") {
        staleStreamIdlePolls = nextIdlePolls;
        return true;
      }
      if (staleTick.kind === "abandon") {
        staleStreamIdlePolls = 0;
        sawRunnerBusyThisStream = false;
        abandonStaleLocalStreamRef?.current();
      }
      return true;
    }

    if (running || awaitingSwarm) {
      consecutiveIdlePolls = 0;
      if (awaitingSwarm) {
        detachedBusyRef.current = running;
        setTurnOpen(false);
        setStatus("awaiting_swarm");
        setWaitHint?.(SWARM_AWAIT_HINT);
        setBackendPendingSwarms?.(true);
      } else {
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev: SessionStatus) => preserveOrThinking(prev));
      }
      if (!running) {
        return true;
      }
      const tick = runnersBusyTickDecision({
        userStopped: userStoppedRef.current,
        localStreamActive: localStreamActiveRef.current,
        runnerBusy: true,
        detachedBusy: true,
        chatEventsPollArmed: chatEventsReattachArmed(),
        items: itemsRef.current,
        consecutiveIdlePolls: 0,
      });
      if (tick.kind === "arm_reattach") {
        return true;
      }
      if (tick.kind === "skip_disk_while_reattach") return true;
      await refreshTranscriptFromDisk(sid);
      return true;
    }

    if (detachedBusyRef.current) {
      consecutiveIdlePolls += 1;
      const tick = runnersBusyTickDecision({
        userStopped: userStoppedRef.current,
        localStreamActive: localStreamActiveRef.current,
        runnerBusy: false,
        detachedBusy: true,
        chatEventsPollArmed: chatEventsReattachArmed(),
        items: itemsRef.current,
        consecutiveIdlePolls,
        idleConfirmPolls: RUNNERS_IDLE_CONFIRM_POLLS,
      });
      if (
        tick.kind === "hold_live_investigation"
        || tick.kind === "hold_idle_unconfirmed"
      ) {
        return true;
      }
      consecutiveIdlePolls = 0;
      detachedBusyRef.current = false;
      // Do not clearChatEventsPoll here — store cursor stays armed for the session.
      setTurnOpen(false);
      setStatus("idle");
      setCompactingStatus?.(null);
      setBackendPendingSwarms?.(false);
      await refreshTranscriptFromDisk(sid);
      return true;
    }

    return true;
  };

  const handleRingMiss = async (ev: {
    kind: string;
    data?: any;
  }, missRetried: boolean): Promise<"retry" | "continue" | "stop"> => {
    const replay = storeRingMissFields(ev as any);
    const prevGen = ringGenerationRef.current;
    ringGenerationRef.current = ringGenerationAfterReplayMiss(replay, prevGen);
    void cursorAfterReplayMiss(replay, 0);
    if (shouldHydrateTranscriptOnReplayMiss(replay)) {
      await hydrateDurableTranscript();
    }
    if (
      shouldRetryRingAfterReplayMiss(replay, {
        alreadyRetried: missRetried,
        prevGeneration: prevGen,
        nextGeneration: ringGenerationRef.current,
      })
    ) {
      return "retry";
    }
    return "continue";
  };

  const pullChatEvents = async (missRetried = false): Promise<boolean> => {
    if (cancelled()) return false;
    if (loadGen !== transcriptLoadGenRef.current) return false;
    if (!fenceOk()) return false;
    if (userStoppedRef.current) return false;
    try {
      const batch = await api.readEventsSince({
        session: reattachSid,
        since: lastAppliedCursorRef.current,
        ...(ringGenerationRef.current != null
          ? { generation: ringGenerationRef.current }
          : {}),
      });
      if (cancelled()) return false;
      if (loadGen !== transcriptLoadGenRef.current) return false;
      if (!fenceOk()) return false;
      if (userStoppedRef.current) return false;

      const events = Array.isArray(batch.events) ? batch.events : [];
      let sawTerminal = false;
      let wantRetry = false;

      for (const ev of events) {
        if (!fenceOk()) return false;
        if (ev.kind === "ring_miss" && isStoreRingMissEvent(ev)) {
          const decision = await handleRingMiss(ev, missRetried);
          if (decision === "retry") wantRetry = true;
          continue;
        }
        if (ev.kind === "runners") {
          const keep = await applyRunnersEvent(ev.data || {});
          if (!keep) {
            lastAppliedCursorRef.current = nextStoreCursor(
              lastAppliedCursorRef.current,
              events,
              batch.cursor,
            );
            return false;
          }
          continue;
        }
        if (ev.kind === "stream") {
          if (localStreamActiveRef.current) continue;
          const frame = ev.data || {};
          if (typeof frame.generation === "number" && frame.generation > 0) {
            ringGenerationRef.current = frame.generation;
          }
          applyStreamEventRef.current(storeStreamToStreamEvent(frame));
          if (isTerminalStreamKind(String(frame.kind || ""))) sawTerminal = true;
        }
      }

      lastAppliedCursorRef.current = nextStoreCursor(
        lastAppliedCursorRef.current,
        events,
        batch.cursor,
      );

      if (wantRetry && !missRetried) {
        return pullChatEvents(true);
      }

      if (sawTerminal || storeBatchSawTerminal(events)) {
        flushTypewriterRef.current();
        detachedBusyRef.current = false;
        maybeRunQueuedResumeRef.current();
        maybeDrainQueueRef.current();
        return true;
      }

      return shouldPollChatEvents({
        detachedBusy: true,
        localStreamActive: false,
        userStopped: userStoppedRef.current,
        sawTerminal: false,
      });
    } catch {
      return !userStoppedRef.current;
    }
  };

  const startChatEventsPoll = () => {
    if (cancelled() || userStoppedRef.current) return;
    if (chatEventsPollTimerRef.current != null) return;
    void pullChatEvents().then((keepPolling) => {
      if (!keepPolling || cancelled()) return;
      if (streamGenRef.current !== reattachGen) return;
      if (chatEventsPollTimerRef.current != null) return;
      chatEventsPollTimerRef.current = window.setInterval(() => {
        void pullChatEvents().then((cont) => {
          if (!cont) clearChatEventsPoll();
        });
      }, STORE_EVENTS_POLL_MS);
    });
  };

  /** Live watch collapsed — store cursor owns reattach. */
  const startLiveChatEventsWatch = (): boolean => false;

  const startChatEventsReattach = async () => {
    if (cancelled() || userStoppedRef.current) return;
    if (streamGenRef.current !== reattachGen) return;
    if (!detachedBusyRef.current && !localStreamActiveRef.current) {
      const maxAttempts = 2;
      for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        try {
          const st = await api.getSessionState();
          if (cancelled()) return;
          if (cachedSessionIdRef.current !== reattachSid) return;
          const running = st?.runners?.[reattachSid] === "running";
          const awaiting = sessionStateShowsAwaitingSwarm({
            state: st?.state,
            pendingSwarms: !!st?.pending_swarms,
            userStopped: userStoppedRef.current,
          });
          if (running || awaiting) {
            if (awaiting) {
              detachedBusyRef.current = running;
              setTurnOpen(false);
              setStatus("awaiting_swarm");
              setWaitHint?.(SWARM_AWAIT_HINT);
              setBackendPendingSwarms?.(true);
            } else {
              detachedBusyRef.current = true;
              setTurnOpen(true);
              setStatus((prev: any) => preserveOrThinking(prev));
            }
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
          // Session state unknown — arm store poll without inventing a turn.
          break;
        }
      }
    }
    if (streamGenRef.current !== reattachGen) return;
    startChatEventsPoll();
  };

  return { pullChatEvents, startChatEventsReattach, startLiveChatEventsWatch };
}
