import { useEffect, useRef } from "react";
import { api, type SessionState } from "./api";

/**
 * One /api/session/state poll shared by every always-mounted reader (footer
 * runtime + GOAL, LeftRail running dots). Each used to run its own 4s poller
 * against the same cheap-but-not-free endpoint.
 *
 * The footer owns which session is asked about (runner statuses in the reply
 * are global, so rail dots do not care). Listeners receive the session id the
 * reply was requested for, so a caller can drop a reply for a stale owner.
 */
type Listener = (state: SessionState, requestedFor: string, seq: number) => void;

export const SESSION_STATE_POLL_MS = 4000;

type RunnerState = NonNullable<SessionState["runners"]>[string];

const listeners = new Set<Listener>();
let runners: Record<string, RunnerState> = {};
let sessionId = "";
let timer: number | undefined;
let inFlight: { promise: Promise<void>; requestedFor: string } | null = null;
let issued = 0;

function schedule(ms: number) {
  window.clearTimeout(timer);
  timer = listeners.size ? window.setTimeout(tick, ms) : undefined;
}

function tick() {
  if (document.hidden) { schedule(SESSION_STATE_POLL_MS); return; }
  void refreshSessionStateFeed().finally(() => schedule(SESSION_STATE_POLL_MS));
}

/**
 * Fetch now. Joins a request already in flight for the same session unless
 * ``fresh`` is set (a caller that just fenced older replies needs a new one).
 */
export function refreshSessionStateFeed(fresh = false): Promise<void> {
  if (!fresh && inFlight && inFlight.requestedFor === sessionId) return inFlight.promise;
  const requestedFor = sessionId;
  const seq = ++issued;
  const promise = Promise.resolve()
    .then(() => api.getSessionState(requestedFor ? { sessionId: requestedFor } : undefined))
    .then((state) => {
      if (!state) return;
      if (state.runners) runners = state.runners;
      for (const listener of [...listeners]) listener(state, requestedFor, seq);
    })
    .catch(() => {})
    .finally(() => { if (inFlight?.promise === promise) inFlight = null; });
  inFlight = { promise, requestedFor };
  return promise;
}

/**
 * A session's runner as of the last reply (at most one poll old), or undefined
 * if never seen. A session switch seeds its busy chrome from this, so a
 * running target does not paint Send while its own state request is in flight.
 */
export function knownRunnerState(id: string): RunnerState | undefined {
  return runners[id];
}

/** Sequence of the newest request issued; replies at or below a fence are stale. */
export function sessionStateFeedSequence(): number {
  return issued;
}

export function setSessionStateFeedSession(id: string) {
  sessionId = id;
}

export function subscribeSessionState(listener: Listener): () => void {
  listeners.add(listener);
  if (listeners.size === 1) schedule(0);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) schedule(0);
  };
}

/** Subscribe with the latest callback; callers need not memoize it. */
export function useSessionStateFeed(listener: Listener) {
  const ref = useRef(listener);
  ref.current = listener;
  useEffect(() => subscribeSessionState((state, requestedFor, seq) => ref.current(state, requestedFor, seq)), []);
}
