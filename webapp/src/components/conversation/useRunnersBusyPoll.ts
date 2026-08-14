/**
 * Busy chrome for the active session.
 *
 * Collapsed onto the unified store-event cursor (``read_events_since`` via
 * ``createChatEventsReattach``). This hook no longer runs a third getSessionState
 * poller — it only ensures the store subscription is armed on session change.
 */

import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { Item } from "../TranscriptList";
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
  chatEventsLiveCancelRef: MutableRefObject<null | (() => void)>;
  ensureChatEventsReattachRef: MutableRefObject<() => void>;
  turnSettledRef: MutableRefObject<boolean>;
  abandonStaleLocalStreamRef: MutableRefObject<() => void>;
  setItems: Dispatch<SetStateAction<Item[]>>;
  setTranscriptStale: Dispatch<SetStateAction<boolean>>;
  setTurnOpen: Dispatch<SetStateAction<boolean>>;
  setStatus: Dispatch<SetStateAction<SessionStatus>>;
  setCompactingStatus: Dispatch<SetStateAction<string | null>>;
  setWaitHint: Dispatch<SetStateAction<string | null>>;
  setBackendPendingSwarms: Dispatch<SetStateAction<boolean>>;
};

/**
 * Ensure the store-event cursor stays subscribed while a session is active.
 * Runners / stream / ring_miss apply lives in createChatEventsReattach.
 */
export function useRunnersBusyPoll(deps: UseRunnersBusyPollDeps) {
  const {
    activeSessionId,
    ensureChatEventsReattachRef,
  } = deps;

  useEffect(() => {
    if (!activeSessionId) return;
    ensureChatEventsReattachRef.current();
  }, [activeSessionId]); // eslint-disable-line react-hooks/exhaustive-deps
}
