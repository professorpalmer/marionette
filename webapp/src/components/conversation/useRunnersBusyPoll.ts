/**
 * Poll runners so composer shows Stop/Steer while the active session's
 * backend runner is busy -- even after SSE detach on session switch.
 */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
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

  // Poll runners so composer shows Stop/Steer while the active session's
  // backend runner is busy -- even after SSE detach on session switch.
  usePolling(() => {
    if (!activeSessionId) return;
    if (localStreamActiveRef.current) return;
    if (userStoppedRef.current) {
      // Stop must stick: ignore runners=running while the abandoned generator
      // unwinds; keep chrome idle until the user sends again.
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
      const tick = runnersBusyTickDecision({
        userStopped: userStoppedRef.current,
        localStreamActive: localStreamActiveRef.current,
        runnerBusy: running,
        detachedBusy: detachedBusyRef.current,
        chatEventsPollArmed: chatEventsPollTimerRef.current != null,
        items: itemsRef.current,
      });
      if (running) {
        detachedBusyRef.current = true;
        setTurnOpen(true);
        setStatus((prev) => preserveOrThinking(prev));
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
          if (pollGen !== runnerBusyPollGenRef.current) return;
          if (cachedSessionIdRef.current !== sid) return;
          if (localStreamActiveRef.current) return;
          const loadedItems = transcriptResponseToItems(tres);
          const local = itemsRef.current;
          const next = mergeTranscriptItems(local, loadedItems);
          const fp = transcriptFingerprint(next);
          // Identical payload: keep existing object identities so React does not
          // remount every Investigated/card row (the periodic blink).
          if (fp === transcriptFpRef.current) return;
          transcriptFpRef.current = fp;
          setItems(next);
          itemsRef.current = next;
          writeTranscriptCache(sid, next);
          setTranscriptStale(false);
        }).catch(() => {});
      } else if (detachedBusyRef.current) {
        // Runner went idle after a detached busy view -- finalize + refresh.
        // Do not clear busy chrome while live tool rows are still painted;
        // a lagging runners map was wiping Investigating → idle mid-command.
        if (tick.kind === "hold_live_investigation") {
          return;
        }
        detachedBusyRef.current = false;
        clearChatEventsPoll();
        setTurnOpen(false);
        setStatus("idle");
        setCompactingStatus(null);
        return api.sessionTranscript(sid)
          .then((tres) => {
            if (cachedSessionIdRef.current !== sid) return;
            if (localStreamActiveRef.current) return;
            const loadedItems = transcriptResponseToItems(tres);
            const next = mergeTranscriptItems(itemsRef.current, loadedItems);
            const fp = transcriptFingerprint(next);
            if (fp === transcriptFpRef.current) return;
            transcriptFpRef.current = fp;
            setItems(next);
            itemsRef.current = next;
            writeTranscriptCache(sid, next);
            setTranscriptStale(false);
          })
          .catch(() => {});
      }
    });
  }, 1500, { enabled: !!activeSessionId });
}
