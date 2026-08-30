/**
 * Warm-cache session switch effect. Mid-turn reattach lives in chatEventsReattach.
 */

import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { clearSessionTodos } from "../../lib/sessionTodos";
import { clearActivityFoldPrefs, type Item } from "../TranscriptList";
import {
  peekTranscriptCacheEntry,
  resolveSwitchTranscript,
  writeTranscriptCache,
} from "./transcriptCache";
import {
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./transcriptItems";
import {
  clearRecoveredSessionFailNotice,
  emptySessionSwitchState,
  emptyTranscriptAfterRetryDecision,
  runnerBusySwitchDecision,
  sessionStateFailureSwitchDecision,
  shouldPreserveBusyStatus,
  shouldResetBusyChromeOnSwitch,
  shouldRetryEmptyTranscript,
  transcriptRefreshFailureDecision,
} from "./sessionHydrate";
import { resolveComposerDraftOnSwitch } from "./composerDraftCache";
import {
  releaseDroppedComposerAttachmentPreviews,
  resolveComposerAttachmentsOnSwitch,
  type ComposerAttachedImage,
} from "./composerAttachmentCache";
import type { MemoryProposal } from "./streamEventHandler";
import { createChatEventsReattach } from "./chatEventsReattach";
import { cancelStreamPaint, cancelTypewriterWithoutFlush } from "./streamTypewriter";
import { gatherSessionArtifacts } from "./sessionArtifacts";
import { releaseAllTranscriptPreviewBlobs } from "./transcriptImageBlobs";
import {
  foldSwarmLiveJobsAfterReload,
  shouldApplySwarmLiveMerge,
} from "./streamApply";
import {
  hydratePendingJobIdsAfterReload,
  sessionStateShowsAwaitingSwarm,
  SWARM_AWAIT_HINT,
} from "./swarmPoll";
import {
  resetCrossSessionLatchesOnSwitch,
  resetTurnLifecycleOnSessionSwitch,
  resetTurnSettledOnSessionSwitch,
} from "./streamTerminal";
import type { RecoveryContext, TerminalCause, TurnLifecycle } from "../../lib/turnTerminal";

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
  /** Drop a zombie local EventSource after runners go idle (store cursor). */
  abandonStaleLocalStreamRef: MutableRefObject<() => void>;
  /** Keep-alive resume queued on A must not fire into B after switch. */
  resumeQueuedRef: MutableRefObject<boolean>;
  /** Approved-command retry queued on A must not execute into B after switch. */
  approvedCommandRetryRef: MutableRefObject<string | null>;
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
  setAttachedImages: Dispatch<SetStateAction<ComposerAttachedImage[]>>;
  /** Live composer attachments; kept in sync by Conversation for per-session cache. */
  attachedImagesRef: MutableRefObject<ComposerAttachedImage[]>;
  /** Composer chrome that must not bleed across sessions (wiki/memory/notices). */
  setWikiPrepared: Dispatch<SetStateAction<{ pages: any[]; autoIngested: boolean } | null>>;
  setMemoryProposals: Dispatch<SetStateAction<MemoryProposal[]>>;
  setDistillNotice: Dispatch<SetStateAction<string | null>>;
  setUploadError: Dispatch<SetStateAction<string | null>>;
  setWaitHint: Dispatch<SetStateAction<string | null>>;
  /** Rehydrate local pending job tracker after transcript hydrate on switch. */
  setPendingJobIds: Dispatch<SetStateAction<string[]>>;
  /**
   * Re-arm swarm-results poll enablement when switch restores awaiting chrome.
   * Cleared on activeSessionId change in Conversation; must not stay sticky
   * after idle finalize with no pending swarms.
   */
  setBackendPendingSwarms: Dispatch<SetStateAction<boolean>>;
  /** Hide Send until B's runner state is known (no flash onto a running session). */
  setSessionSwitchPending: Dispatch<SetStateAction<boolean>>;
  /** Clear pending setSafeTimeout kicks so A→B cannot executeSend into B. */
  clearSafeTimeouts: () => void;
  setTurnLifecycle: Dispatch<SetStateAction<TurnLifecycle>>;
  setTerminalCause: Dispatch<SetStateAction<TerminalCause | null>>;
  recoveryDispatchingRef: MutableRefObject<boolean>;
  recoveryContextRef: MutableRefObject<RecoveryContext | null>;
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
    abandonStaleLocalStreamRef,
    resumeQueuedRef,
    approvedCommandRetryRef,
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
    setAttachedImages,
    attachedImagesRef,
    setWikiPrepared,
    setMemoryProposals,
    setDistillNotice,
    setUploadError,
    setWaitHint,
    setPendingJobIds,
    setBackendPendingSwarms,
    setSessionSwitchPending,
    clearSafeTimeouts,
    setTurnLifecycle,
    setTerminalCause,
    recoveryDispatchingRef,
    recoveryContextRef,
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

      // Per-session composer attachments: same cache/restore contract as drafts.
      // Keep outgoing blob URLs alive in the cache; only revoke uncached drops.
      const currentAttachments = attachedImagesRef.current;
      const restoredAttachments = resolveComposerAttachmentsOnSwitch({
        prevId,
        nextId: activeSessionId,
        currentAttachments,
      });
      const retainedForPreview = [
        ...(prevId ? currentAttachments : []),
        ...restoredAttachments,
      ];
      releaseDroppedComposerAttachmentPreviews(
        currentAttachments,
        retainedForPreview,
      );
      attachedImagesRef.current = restoredAttachments;
      setAttachedImages(restoredAttachments);
    }
    if (switchedSession) {
      // Owned sent-image blob previews belong to the outgoing session; durable
      // /api/image paths remain on warm-cache rows for reload recovery.
      releaseAllTranscriptPreviewBlobs();
      // Investigation / reasoning fold prefs are session-scoped — stable ids
      // must not reopen folds from the previous conversation.
      clearActivityFoldPrefs();
      // Composer chrome is session-local (match R8 edit/queue clear style).
      setWikiPrepared(null);
      setMemoryProposals([]);
      setDistillNotice(null);
      setUploadError(null);
      setWaitHint(null);
      // Drop already-queued drain/resume/retry kicks before they fire into B.
      clearSafeTimeouts();
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
    if (switchedSession) {
      clearSessionTodos();
      // Stop / resume / approved-retry latched on A must not force idle, skip
      // reattach, or execute into mid-turn B.
      resetCrossSessionLatchesOnSwitch({
        userStoppedRef,
        resumeQueuedRef,
        approvedCommandRetryRef,
      });
      resetTurnLifecycleOnSessionSwitch({
        setTurnLifecycle,
        setTerminalCause,
        recoveryDispatchingRef,
        recoveryContextRef,
      });
    }
    // Reset mid-turn reattach cursor/poll so the next session starts clean.
    clearChatEventsPoll();
    lastAppliedCursorRef.current = 0;
    ringGenerationRef.current = undefined;
    // Drop the typewriter loop without flushing into items (would race the
    // cache hydrate below). Authoritative text comes back via sessionTranscript.
    cancelTypewriterWithoutFlush(
      { typeBufRef, typeRafRef, typeDoneRef },
      cancelStreamPaint,
    );
    // Default idle until getSessionState / runners poll resolve the target.
    // Keep the mouth busy via sessionSwitchPending so running B never flashes Send.
    if (shouldResetBusyChromeOnSwitch(switchedSession)) {
      setSessionSwitchPending(true);
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
      setSessionSwitchPending(false);
      setTurnOpen(false);
      setStatus("idle");
      setCompactingStatus(null);
      return;
    }

    const cacheEntry = peekTranscriptCacheEntry(activeSessionId);
    const cachedItems = cacheEntry?.items;
    const hadCache = cacheEntry !== undefined;
    const seededEmpty = cacheEntry?.seededEmpty === true;
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
      sessionState?: string | null,
      pendingSwarms?: boolean,
    ) => {
      if (cancelled) return;
      if (localStreamActiveRef.current) {
        setSessionSwitchPending(false);
        return;
      }
      if (!activeSessionId) return;
      if (userStoppedRef.current) {
        setSessionSwitchPending(false);
        return;
      }
      const decision = runnerBusySwitchDecision({
        runnerState: runners?.[activeSessionId],
        localStreamActive: false,
        switchedSession: prevId !== activeSessionId,
        sessionState,
        pendingSwarms,
      });
      setSessionSwitchPending(false);
      if (decision.kind === "awaiting") {
        // Pause-point: Still working… with Steer/Stop via awaiting_swarm latch
        // (turnOpen stays false; isAgentLoopOpen covers awaiting_swarm).
        detachedBusyRef.current = runners?.[activeSessionId] === "running";
        setTurnOpen(false);
        setStatus("awaiting_swarm");
        setWaitHint(SWARM_AWAIT_HINT);
        // Switch clear drops backendPendingSwarms; re-arm so swarmResultsPending
        // enables the drain/pilot_resume poller (not only chrome + lucky peek).
        setBackendPendingSwarms(
          sessionStateShowsAwaitingSwarm({
            state: sessionState,
            pendingSwarms,
            userStopped: userStoppedRef.current,
          }),
        );
      } else if (decision.kind === "busy") {
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev) => (shouldPreserveBusyStatus(prev) ? prev : "thinking"));
        // Running without pause-point: do not leave a prior session's true sticky.
        setBackendPendingSwarms(!!pendingSwarms);
      } else if (decision.kind === "idle") {
        // Idle or cold-attaching: never flash turn-thinking on New Session.
        detachedBusyRef.current = false;
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
        setBackendPendingSwarms(false);
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
        applyRunnerBusy(
          res?.runners,
          res?.state,
          !!res?.pending_swarms,
        );
        // Definitive runners map recovered — drop sticky SESSION_* fail banner.
        setEditNotice((prev) => clearRecoveredSessionFailNotice(prev));
      })
      .catch(() => {
        if (cancelled) return;
        // Idle until useRunnersBusyPoll re-arms; never leave A's chrome stuck.
        const failure = sessionStateFailureSwitchDecision();
        detachedBusyRef.current = false;
        setSessionSwitchPending(false);
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
        setEditNotice(failure.notice);
      });

    // Long chats / deferred cold attach can return empty or flake once on boot.
    // Retry a few times before accepting a blank feed (switching away and back
    // was the user workaround — do that automatically). Cache-hit empty gets
    // the same retry budget so a disk/attach flake cannot wipe warm rows.
    const loadTranscriptWithRetry = async (sid: string, gen: number) => {
      let lastErr: unknown = null;
      const maxAttempts = 4;
      for (let attempt = 0; attempt < maxAttempts; attempt++) {
        if (gen !== transcriptLoadGenRef.current) return null;
        if (cachedSessionIdRef.current !== sid) return null;
        try {
          const res = await api.sessionTranscript(sid);
          if (gen !== transcriptLoadGenRef.current) return null;
          const loadedItems = transcriptResponseToItems(res);
          if (shouldRetryEmptyTranscript({
            loadedCount: loadedItems.length,
            attempt,
            maxAttempts,
            cachedCount: hadCache ? (cachedItems?.length ?? 0) : undefined,
            seededEmpty: hadCache ? seededEmpty : undefined,
          })) {
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
        // Non-empty warm cache + empty after retries: keep rows (flake honesty).
        // Seeded empty cache (New Session) must accept blank — not the fail banner.
        if (loadedItems.length === 0 && hadCache) {
          const emptyHit = emptyTranscriptAfterRetryDecision({
            cachedCount: cachedItems?.length ?? 0,
            seededEmpty,
          });
          if (emptyHit.kind === "keep_warm_with_notice") {
            setTranscriptStale(emptyHit.stale);
            setEditNotice(emptyHit.notice);
            return;
          }
        }
        setItems(loadedItems);
        itemsRef.current = loadedItems;
        transcriptFpRef.current = transcriptFingerprint(loadedItems);
        writeTranscriptCache(activeSessionId, loadedItems);
        setTranscriptStale(false);
        // Successful hydrate — drop sticky SESSION_* fail banner from a prior flake.
        setEditNotice((prev) => clearRecoveredSessionFailNotice(prev));
        // Pending ids come from the live snapshot after reload — session
        // job_ids are historical and must not re-arm Still working… on a
        // completed turn. Unresolved swarm_pending cards are fallback only
        // when swarmLive itself fails.

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
          setPendingJobIds(
            hydratePendingJobIdsAfterReload({
              liveJobs: jobs,
              items: loadedItems,
              activeSessionId,
            }),
          );
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
        }).catch(() => {
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
          setPendingJobIds(
            hydratePendingJobIdsAfterReload({
              liveJobs: null,
              items: loadedItems,
              activeSessionId,
            }),
          );
        });

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
          setCompactingStatus,
          setWaitHint,
          setBackendPendingSwarms,
          turnSettledRef,
          abandonStaleLocalStreamRef,
        });
        ensureChatEventsReattachRef.current = () => {
          void startChatEventsReattach();
        };
        void startChatEventsReattach();
      })
      .catch(() => {
        if (loadGen !== transcriptLoadGenRef.current) return;
        if (cachedSessionIdRef.current !== activeSessionId) return;
        // Cache hit: keep warm rows. Cache miss: clear relics but mark stale
        // (+ notice) so we never look like a silent first-run empty session.
        const failure = transcriptRefreshFailureDecision(hadCache);
        if (failure.clearItems) {
          setItems([]);
          itemsRef.current = [];
        }
        setTranscriptStale(failure.stale);
        setEditNotice(failure.notice);
      });

    return () => {
      cancelled = true;
      clearChatEventsPoll();
      ensureChatEventsReattachRef.current = () => {};
    };
    // refs/setters are stable; match prior Conversation effect deps
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps
}
