/**
 * Poll runners so composer shows Stop/Steer while the active session's
 * backend runner is busy -- even after SSE detach on session switch.
 */

import { useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { api } from "../../lib/api";
import { usePolling } from "../../lib/usePolling";
import type { Item } from "../TranscriptList";
import {
  mergeTranscriptItems,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "./transcriptItems";
import { writeTranscriptCache } from "./transcriptCache";
import {
  preserveOrThinking,
  runnersBusyTickDecision,
  userStoppedBusyChrome,
} from "./runnersBusy";
import { shouldApplySwarmLiveMerge } from "./streamApply";
import type { SessionStatus } from "./useSessionSwitch";

export type UseRunnersBusyPollDeps = {
  activeSessionId: string | null;
  clearChatEventsPoll: () => void;
  itemsRef: MutableRefObject<Item[]>;
  cachedSessionIdRef: MutableRefObject<string | null>;
  transcriptFpRef: MutableRefObject<string>;
  localStreamActiveRef: MutableRefObject<boolean>;
  detachedBusyRef: MutableRefObject<boolean>;
  userStoppedRef: MutableRefObject<boolean>;
  runnerBusyPollGenRef: MutableRefObject<number>;
  chatEventsPollTimerRef: MutableRefObject<number | null>;
  ensureChatEventsReattachRef: MutableRefObject<() => void>;
  setItems: Dispatch<SetStateAction<Item[]>>;
  setTranscriptStale: Dispatch<SetStateAction<boolean>>;
  setTurnOpen: Dispatch<SetStateAction<boolean>>;
  setStatus: Dispatch<SetStateAction<SessionStatus>>;
  setCompactingStatus: Dispatch<SetStateAction<string | null>>;
};

export function useRunnersBusyPoll(deps: UseRunnersBusyPollDeps) {
  const {
    activeSessionId,
    clearChatEventsPoll,
    itemsRef,
    cachedSessionIdRef,
    transcriptFpRef,
    localStreamActiveRef,
    detachedBusyRef,
    userStoppedRef,
    runnerBusyPollGenRef,
    chatEventsPollTimerRef,
    ensureChatEventsReattachRef,
    setItems,
    setTranscriptStale,
    setTurnOpen,
    setStatus,
    setCompactingStatus,
  } = deps;

  // Consecutive idle sightings while detachedBusy; reset whenever runners busy.
  const consecutiveIdlePollsRef = useRef(0);

  // Poll runners so composer shows Stop/Steer while the active session's
  // backend runner is busy -- even after SSE detach on session switch.
  usePolling(() => {
    if (!activeSessionId) return;
    if (localStreamActiveRef.current) return;
    if (userStoppedRef.current) {
      // Stop must stick: ignore runners=running while the abandoned generator
      // unwinds; keep chrome idle until the user sends again.
      consecutiveIdlePollsRef.current = 0;
      detachedBusyRef.current = false;
      clearChatEventsPoll();
      setStatus((prev) => userStoppedBusyChrome(prev));
      return;
    }
    const sid = activeSessionId;
    return api.getSessionState().then((res) => {
      if (cachedSessionIdRef.current !== sid || localStreamActiveRef.current) return;
      if (userStoppedRef.current) return;
      const runners = res?.runners || {};
      const running = runners[sid] === "running";
      if (running) {
        consecutiveIdlePollsRef.current = 0;
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev) => preserveOrThinking(prev));
        const tick = runnersBusyTickDecision({
          userStopped: userStoppedRef.current,
          localStreamActive: localStreamActiveRef.current,
          runnerBusy: true,
          detachedBusy: true,
          chatEventsPollArmed: chatEventsPollTimerRef.current != null,
          items: itemsRef.current,
          consecutiveIdlePolls: 0,
        });
        // Queue/bridge turns start without this tab's EventSource. Arm the
        // chatEvents ring poll so tokens paint live (not only after restart).
        if (tick.kind === "arm_reattach") {
          ensureChatEventsReattachRef.current();
          return;
        }
        // While chatEvents reattach poll owns mid-turn UI, skip disk replace
        // that would wipe in-flight deltas not yet persisted.
        if (tick.kind === "skip_disk_while_reattach") return;
        // Slice C: while detached-but-busy, refresh transcript so eventual
        // dump lands without blanking thinking chrome.
        const pollGen = ++runnerBusyPollGenRef.current;
        return api.sessionTranscript(sid).then((tres) => {
          // Live active id is the cached fence (useSessionSwitch keeps them
          // aligned); do not trust a render-scoped activeSessionId closure.
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
            // Re-fence inside the updater: a session switch between the await
            // and React applying this update must not mutate session B.
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
            // Identical payload: keep existing object identities so React does
            // not remount every Investigated/card row (the periodic blink).
            if (fp === transcriptFpRef.current) return prev;
            transcriptFpRef.current = fp;
            itemsRef.current = next;
            writeTranscriptCache(sid, next);
            applied = true;
            return next;
          });
          if (applied) setTranscriptStale(false);
        }).catch(() => {});
      } else if (detachedBusyRef.current) {
        // Runner went idle after a detached busy view -- finalize + refresh.
        // Require consecutive idle polls so a single false not-running blip
        // cannot clear Stop; live surfaces still hold immediately.
        consecutiveIdlePollsRef.current += 1;
        const tick = runnersBusyTickDecision({
          userStopped: userStoppedRef.current,
          localStreamActive: localStreamActiveRef.current,
          runnerBusy: false,
          detachedBusy: true,
          chatEventsPollArmed: chatEventsPollTimerRef.current != null,
          items: itemsRef.current,
          consecutiveIdlePolls: consecutiveIdlePollsRef.current,
        });
        if (
          tick.kind === "hold_live_investigation"
          || tick.kind === "hold_idle_unconfirmed"
        ) {
          return;
        }
        consecutiveIdlePollsRef.current = 0;
        detachedBusyRef.current = false;
        clearChatEventsPoll();
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
        const pollGen = ++runnerBusyPollGenRef.current;
        return api.sessionTranscript(sid)
          .then((tres) => {
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
          })
          .catch(() => {});
      }
    });
  }, 1500, { enabled: !!activeSessionId });
}
