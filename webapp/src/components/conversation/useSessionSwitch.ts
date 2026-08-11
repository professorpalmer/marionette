/**
 * Warm-cache session switch effect. Mid-turn reattach lives in chatEventsReattach.
 */

import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { api } from "../../lib/api";
import { clearActivityFoldPrefs, type Item } from "../TranscriptList";
import {
  peekTranscriptCache,
  resolveSwitchTranscript,
  writeTranscriptCache,
} from "./transcriptCache";
import {
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./transcriptItems";
import {
  emptySessionSwitchState,
  runnerBusySwitchDecision,
  sessionStateFailureSwitchDecision,
  shouldPreserveBusyStatus,
  shouldResetBusyChromeOnSwitch,
} from "./sessionHydrate";
import { resolveComposerDraftOnSwitch } from "./composerDraftCache";
import { createChatEventsReattach } from "./chatEventsReattach";
import { cancelTypewriterWithoutFlush } from "./streamTypewriter";
import { gatherSessionArtifacts } from "./sessionArtifacts";
import { releaseAllTranscriptPreviewBlobs } from "./transcriptImageBlobs";
import {
  foldSwarmLiveJobsAfterReload,
  shouldApplySwarmLiveMerge,
} from "./streamApply";
import { resetTurnSettledOnSessionSwitch } from "./streamTerminal";

export type SessionStatus =
  | "idle"
  | "thinking"
  | "executing"
  | "done"
  | "error"
  | "streaming"
  | "awaiting_swarm";

export type UseSessionSwitchDeps = {
  activeSessionId: string | null;
  onArtifacts: (a: { type: string; headline: string }[]) => void;
  clearChatEventsPoll: () => void;
  itemsRef: MutableRefObject<Item[]>;
  transcriptStaleRef: MutableRefObject<boolean>;
  cachedSessionIdRef: MutableRefObject<string | null>;
  transcriptLoadGenRef: MutableRefObject<number>;
  transcriptFpRef: MutableRefObject<string>;
  streamGenRef: MutableRefObject<number>;
  streamSessionIdRef: MutableRefObject<string | null>;
  lastAppliedCursorRef: MutableRefObject<number>;
  ringGenerationRef: MutableRefObject<number | undefined>;
  chatEventsPollTimerRef: MutableRefObject<number | null>;
  chatEventsLiveCancelRef: MutableRefObject<null | (() => void)>;
  applyStreamEventRef: MutableRefObject<(ev: { kind: string; data?: any }) => void>;
  flushTypewriterRef: MutableRefObject<() => void>;
  maybeRunQueuedResumeRef: MutableRefObject<() => void>;
  maybeDrainQueueRef: MutableRefObject<() => void>;
  ensureChatEventsReattachRef: MutableRefObject<() => void>;
  cancelRef: MutableRefObject<null | (() => void)>;
  localStreamActiveRef: MutableRefObject<boolean>;
  detachedBusyRef: MutableRefObject<boolean>;
  userStoppedRef: MutableRefObject<boolean>;
  /** Session-global; must reset on switch so settled A cannot suppress B chrome. */
  turnSettledRef: MutableRefObject<boolean>;
  runnerBusyPollGenRef: MutableRefObject<number>;
  typeRafRef: MutableRefObject<number | null>;
  typeBufRef: MutableRefObject<string>;
  typeDoneRef: MutableRefObject<boolean>;
  setItems: Dispatch<SetStateAction<Item[]>>;
  setTranscriptStale: Dispatch<SetStateAction<boolean>>;
  setTurnOpen: Dispatch<SetStateAction<boolean>>;
  setStatus: Dispatch<SetStateAction<SessionStatus>>;
  setCompactingStatus: Dispatch<SetStateAction<string | null>>;
  setEditingIndex: Dispatch<SetStateAction<number | null>>;
  setCanRevertEdit: Dispatch<SetStateAction<boolean>>;
  setEditNotice: Dispatch<SetStateAction<string | null>>;
  setEditBusy: Dispatch<SetStateAction<boolean>>;
  setInput: Dispatch<SetStateAction<string>>;
  /** Live composer text; kept in sync by Conversation for per-session draft cache. */
  composerInputRef: MutableRefObject<string>;
};

/** Warm-cache switch + chatEvents reattach arming for the active session id. */
export function useSessionSwitch(deps: UseSessionSwitchDeps) {
  const {
    activeSessionId,
    onArtifacts,
    clearChatEventsPoll,
    itemsRef,
    transcriptStaleRef,
    cachedSessionIdRef,
    transcriptLoadGenRef,
    transcriptFpRef,
    streamGenRef,
    streamSessionIdRef,
    lastAppliedCursorRef,
    ringGenerationRef,
    chatEventsPollTimerRef,
    chatEventsLiveCancelRef,
    applyStreamEventRef,
    flushTypewriterRef,
    maybeRunQueuedResumeRef,
    maybeDrainQueueRef,
    ensureChatEventsReattachRef,
    cancelRef,
    localStreamActiveRef,
    detachedBusyRef,
    userStoppedRef,
    turnSettledRef,
    runnerBusyPollGenRef,
    typeRafRef,
    typeBufRef,
    typeDoneRef,
    setItems,
    setTranscriptStale,
    setTurnOpen,
    setStatus,
    setCompactingStatus,
    setEditingIndex,
    setCanRevertEdit,
    setEditNotice,
    setEditBusy,
    setInput,
    composerInputRef,
  } = deps;

  // Warm-cache session switch: save outgoing transcript, hydrate incoming from
  // cache immediately, detach any open EventSource (backend keeps the turn
  // alive -- do NOT interrupt/stop), then refresh from sessionTranscript in the
  // background without blanking a cache hit.
  //
  // Busy chrome: on switchedSession default idle/turnOpen=false until runners
  // resolve for the target. applyRunnerBusy / useRunnersBusyPoll re-arm Stop
  // when the new session is actually running (no cross-session stickiness).
  useEffect(() => {
    const prevId = cachedSessionIdRef.current;
    const switchedSession = Boolean(prevId && prevId !== activeSessionId);
    if (prevId && prevId !== activeSessionId && !transcriptStaleRef.current) {
      // Only cache when the visible rows belong to prevId. Stale bleed (prior
      // session still painted) must not poison the warm cache.
      writeTranscriptCache(prevId, itemsRef.current);
    }

    // Rewind-edit chrome is session-local; never carry Revert/prefill across ids.
    setEditingIndex(null);
    setCanRevertEdit(false);
    setEditNotice(null);
    setEditBusy(false);
    if (prevId !== activeSessionId) {
      // Per-session composer drafts (Cursor-style): cache outgoing, restore incoming.
      // Also covers null↔session so a project-switch flicker does not drop drafts.
      const restored = resolveComposerDraftOnSwitch({
        prevId,
        nextId: activeSessionId,
        currentDraft: composerInputRef.current,
      });
      composerInputRef.current = restored;
      setInput(restored);
    }
    if (switchedSession) {
      // Owned sent-image blob previews belong to the outgoing session; durable
      // /api/image paths remain on warm-cache rows for reload recovery.
      releaseAllTranscriptPreviewBlobs();
      // Investigation / reasoning fold prefs are session-scoped — stable ids
      // must not reopen folds from the previous conversation.
      clearActivityFoldPrefs();
    }

    // Detach SSE only -- closing EventSource is OK; interrupt would kill the turn.
    // Bump streamGen so any late onmessage from the closed stream is ignored.
    // Bump runnerBusyPollGen so an in-flight session-A transcript poll cannot
    // pass the shared shouldApplySwarmLiveMerge generation fence after switch.
    streamGenRef.current += 1;
    runnerBusyPollGenRef.current += 1;
    streamSessionIdRef.current = null;
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    localStreamActiveRef.current = false;
    detachedBusyRef.current = false;
    // Settled session A must not suppress busy-chrome refresh for mid-turn B.
    resetTurnSettledOnSessionSwitch(turnSettledRef);
    // Reset mid-turn reattach cursor/poll so the next session starts clean.
    clearChatEventsPoll();
    lastAppliedCursorRef.current = 0;
    ringGenerationRef.current = undefined;
    // Drop the typewriter loop without flushing into items (would race the
    // cache hydrate below). Authoritative text comes back via sessionTranscript.
    cancelTypewriterWithoutFlush(
      { typeBufRef, typeRafRef, typeDoneRef },
      cancelAnimationFrame,
    );
    // Default idle until getSessionState / runners poll resolve the target.
    if (shouldResetBusyChromeOnSwitch(switchedSession)) {
      setTurnOpen(false);
      setStatus("idle");
      setCompactingStatus(null);
    }

    const loadGen = ++transcriptLoadGenRef.current;
    cachedSessionIdRef.current = activeSessionId;

    if (!activeSessionId) {
      // Project/session list may briefly report no active id while the next
      // root's sessions load. Keep prior transcript dimmed instead of flashing
      // the first-run empty placeholder; clear only when there was nothing.
      const emptySwitch = emptySessionSwitchState(itemsRef.current.length);
      if (emptySwitch.clearItems) {
        setItems([]);
      }
      setTranscriptStale(emptySwitch.stale);
      setTurnOpen(false);
      setStatus("idle");
      setCompactingStatus(null);
      return;
    }

    const cachedItems = peekTranscriptCache(activeSessionId);
    const hadCache = cachedItems !== undefined;
    const resolved = resolveSwitchTranscript({
      nextId: activeSessionId,
      cached: cachedItems,
      priorItems: itemsRef.current,
    });
    // Always apply resolved items so a cache miss blanks prior session rows
    // instead of leaving A's transcript painted under B's id.
    setItems(resolved.items);
    itemsRef.current = resolved.items;
    transcriptFpRef.current = transcriptFingerprint(resolved.items);
    setTranscriptStale(resolved.stale);

    // Immediately reflect runner busy state for the session we switched TO
    // (warm cache + Stop chrome) before the background transcript refresh.
    let cancelled = false;
    const applyRunnerBusy = (
      runners: Record<string, "running" | "idle" | "attaching" | "missing"> | undefined,
    ) => {
      if (cancelled || localStreamActiveRef.current) return;
      if (!activeSessionId) return;
      const decision = runnerBusySwitchDecision({
        runnerState: runners?.[activeSessionId],
        localStreamActive: false,
        switchedSession: prevId !== activeSessionId,
      });
      if (decision.kind === "busy") {
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev) => (shouldPreserveBusyStatus(prev) ? prev : "thinking"));
      } else if (decision.kind === "idle") {
        // Idle or cold-attaching: never flash turn-thinking on New Session.
        detachedBusyRef.current = false;
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
      }
    };

    const loadSessionRunners = async () => {
      let lastErr: unknown = null;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          return await api.getSessionState();
        } catch (err) {
          lastErr = err;
          await new Promise((r) => setTimeout(r, 120 * (attempt + 1)));
        }
      }
      throw lastErr ?? new Error("getSessionState failed");
    };

    loadSessionRunners()
      .then((res) => {
        if (cancelled) return;
        applyRunnerBusy(res?.runners);
      })
      .catch(() => {
        if (cancelled) return;
        // Idle until useRunnersBusyPoll re-arms; never leave A's chrome stuck.
        const failure = sessionStateFailureSwitchDecision();
        detachedBusyRef.current = false;
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
        setEditNotice(failure.notice);
      });

    // Long chats / deferred cold attach can return empty or flake once on boot.
    // Retry a few times before accepting a blank feed (switching away and back
    // was the user workaround — do that automatically).
    const loadTranscriptWithRetry = async (sid: string, gen: number) => {
      let lastErr: unknown = null;
      for (let attempt = 0; attempt < 4; attempt++) {
        if (gen !== transcriptLoadGenRef.current) return null;
        if (cachedSessionIdRef.current !== sid) return null;
        try {
          const res = await api.sessionTranscript(sid);
          if (gen !== transcriptLoadGenRef.current) return null;
          const loadedItems = transcriptResponseToItems(res);
          // Empty on a cache-miss cold boot: brief wait + retry (disk/attach race).
          if (loadedItems.length === 0 && attempt < 3 && !hadCache) {
            await new Promise((r) => setTimeout(r, 200 * (attempt + 1)));
            continue;
          }
          return { res, loadedItems };
        } catch (err) {
          lastErr = err;
          await new Promise((r) => setTimeout(r, 250 * (attempt + 1)));
        }
      }
      if (lastErr) throw lastErr;
      return null;
    };

    loadTranscriptWithRetry(activeSessionId, loadGen)
      .then((loaded) => {
        if (!loaded) return;
        if (loadGen !== transcriptLoadGenRef.current) return;
        if (cachedSessionIdRef.current !== activeSessionId) return;

        const { res, loadedItems } = loaded;
        setItems(loadedItems);
        itemsRef.current = loadedItems;
        transcriptFpRef.current = transcriptFingerprint(loadedItems);
        writeTranscriptCache(activeSessionId, loadedItems);
        setTranscriptStale(false);

        // Nested worker actions survive restart on local jobs; fold onto cards
        // after display hydrate so investigation rows stay complete on reload.
        // Same shouldApplySwarmLiveMerge fence as the busy-poll path in Conversation.
        void api.swarmLive().then((live) => {
          const pollSid = activeSessionId;
          if (!shouldApplySwarmLiveMerge({
            pollGen: loadGen,
            currentGen: transcriptLoadGenRef.current,
            pollSessionId: pollSid,
            cachedSessionId: cachedSessionIdRef.current,
            activeSessionId: cachedSessionIdRef.current,
          })) {
            return;
          }
          const jobs = Array.isArray(live?.jobs) ? live.jobs : [];
          setItems((prev) => {
            if (!shouldApplySwarmLiveMerge({
              pollGen: loadGen,
              currentGen: transcriptLoadGenRef.current,
              pollSessionId: pollSid,
              cachedSessionId: cachedSessionIdRef.current,
              activeSessionId: cachedSessionIdRef.current,
            })) {
              return prev;
            }
            // Empty swarmLive must not orphan-settle tool-prep / non-job cards —
            // that races mid-turn chatEvents reattach. Only fold authoritative
            // actions/terminal job rows; orphan settle is assistant_done/Stop.
            const next = foldSwarmLiveJobsAfterReload(prev, jobs);
            if (next === prev) return prev;
            itemsRef.current = next;
            transcriptFpRef.current = transcriptFingerprint(next);
            writeTranscriptCache(activeSessionId, next);
            return next;
          });
        }).catch(() => {});

        // Gather all artifacts from (a) card entries in res.display + job fetches.
        const artsOrPromise = gatherSessionArtifacts({
          display: res.display,
          jobIds: res.job_ids,
          stillCurrent: () => loadGen === transcriptLoadGenRef.current,
        });
        const emitArts = (unique: { type: string; headline: string }[]) => {
          if (loadGen !== transcriptLoadGenRef.current) return;
          if (unique.length > 0) onArtifacts(unique);
        };
        if (artsOrPromise instanceof Promise) {
          void artsOrPromise.then(emitArts);
        } else {
          emitArts(artsOrPromise);
        }

        // Mid-turn reattach: if the runner is still busy and we have no local
        // EventSource, prefer a live ring watch (same SSE framing as /api/chat),
        // falling back to retained-frame pull + light poll if live attach fails.
        const reattachSid = activeSessionId;
        const reattachGen = streamGenRef.current;
        const { startChatEventsReattach } = createChatEventsReattach({
          cancelled: () => cancelled,
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
        });
        ensureChatEventsReattachRef.current = () => {
          void startChatEventsReattach();
        };
        void startChatEventsReattach();
      })
      .catch(() => {
        if (loadGen !== transcriptLoadGenRef.current) return;
        if (cachedSessionIdRef.current !== activeSessionId) return;
        // Cache hit: keep showing that session's cached rows on refresh failure.
        // Cache miss: clear — never leave another session's relics on screen.
        if (!hadCache) {
          setItems([]);
          itemsRef.current = [];
          setTranscriptStale(false);
        }
      });

    return () => {
      cancelled = true;
      clearChatEventsPoll();
      ensureChatEventsReattachRef.current = () => {};
    };
    // refs/setters are stable; match prior Conversation effect deps
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps
}
