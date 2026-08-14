/**
 * Self-edit restart resume latch: peek → delay → consume-at-kick.
 *
 * Consuming before the 300ms kick lets clearSafeTimeouts / session switch
 * abandon the timeout after the latch is already gone. Consume only after
 * stillCurrent inside the timeout; re-arm if we leave the owning session
 * between consume and the resume trigger. Latch peek/consume/rearm are
 * session-scoped on the backend — always pass the owning sessionId.
 */

export type ResumeLatchSessionState = {
  resume_pending?: boolean;
};

export type ResumeLatchGetSessionState = (opts?: {
  consumeResume?: boolean;
  rearmResume?: boolean;
  sessionId?: string;
}) => Promise<ResumeLatchSessionState>;

export type ArmResumeKickOpts = {
  getSessionState: ResumeLatchGetSessionState;
  resume: () => void;
  stillCurrent: () => boolean;
  /** Conversation uses setSafeTimeout so switchedSession can cancel the kick. */
  schedule: (fn: () => void, ms: number) => unknown;
  delayMs?: number;
  /** Owning session id for peek/consume/rearm (required for session-scoped latch). */
  sessionId?: string;
  /**
   * Session-only fence used after abandon rearm. Ignores transcript-load gen
   * so a remount that raced the rearm can still scheduleResumeIfPending.
   */
  ownerStillActive?: () => boolean;
};

function bindSessionId(
  getSessionState: ResumeLatchGetSessionState,
  sessionId: string | undefined,
): ResumeLatchGetSessionState {
  if (!sessionId) return getSessionState;
  return (opts) => getSessionState({ ...opts, sessionId });
}

/**
 * Schedule a delayed consume+resume. Caller must have already peeked
 * ``resume_pending === true`` (or use {@link scheduleResumeIfPending}).
 */
export function armResumeKick(opts: ArmResumeKickOpts): void {
  const delayMs = opts.delayMs ?? 300;
  const getSessionState = bindSessionId(opts.getSessionState, opts.sessionId);
  opts.schedule(() => {
    void (async () => {
      if (!opts.stillCurrent()) return;
      let consumed: ResumeLatchSessionState;
      try {
        consumed = await getSessionState({ consumeResume: true });
      } catch {
        return;
      }
      if (!opts.stillCurrent()) {
        // Consumed after leaving the owning session — restore continuity.
        if (consumed?.resume_pending) {
          try {
            await getSessionState({ rearmResume: true });
            // Mount effect may have already peeked before rearm finished.
            // Re-schedule with the session-only fence so the current view can
            // pick the latch up when it owns it again (do not leave it stuck).
            const ownerActive = opts.ownerStillActive ?? opts.stillCurrent;
            if (ownerActive()) {
              await scheduleResumeIfPending({
                ...opts,
                getSessionState,
                stillCurrent: ownerActive,
              });
            }
          } catch {
            /* best-effort; next open may still miss if rearm fails */
          }
        }
        return;
      }
      if (!consumed?.resume_pending) return;
      opts.resume();
    })();
  }, delayMs);
}

/** Peek then arm the delayed consume+kick when the latch is present. */
export async function scheduleResumeIfPending(opts: ArmResumeKickOpts): Promise<void> {
  const getSessionState = bindSessionId(opts.getSessionState, opts.sessionId);
  const res = await getSessionState();
  if (!opts.stillCurrent() || !res?.resume_pending) return;
  armResumeKick({ ...opts, getSessionState });
}
