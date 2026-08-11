/**
 * Self-edit restart resume latch: peek → delay → consume-at-kick.
 *
 * Consuming before the 300ms kick lets clearSafeTimeouts / session switch
 * abandon the timeout after the latch is already gone. Consume only after
 * stillCurrent inside the timeout; re-arm if we leave the owning session
 * between consume and the resume trigger.
 */

export type ResumeLatchSessionState = {
  resume_pending?: boolean;
};

export type ResumeLatchGetSessionState = (opts?: {
  consumeResume?: boolean;
  rearmResume?: boolean;
}) => Promise<ResumeLatchSessionState>;

export type ArmResumeKickOpts = {
  getSessionState: ResumeLatchGetSessionState;
  resume: () => void;
  stillCurrent: () => boolean;
  /** Conversation uses setSafeTimeout so switchedSession can cancel the kick. */
  schedule: (fn: () => void, ms: number) => unknown;
  delayMs?: number;
};

/**
 * Schedule a delayed consume+resume. Caller must have already peeked
 * ``resume_pending === true`` (or use {@link scheduleResumeIfPending}).
 */
export function armResumeKick(opts: ArmResumeKickOpts): void {
  const delayMs = opts.delayMs ?? 300;
  opts.schedule(() => {
    void (async () => {
      if (!opts.stillCurrent()) return;
      let consumed: ResumeLatchSessionState;
      try {
        consumed = await opts.getSessionState({ consumeResume: true });
      } catch {
        return;
      }
      if (!opts.stillCurrent()) {
        // Consumed after leaving the owning session — restore continuity.
        if (consumed?.resume_pending) {
          try {
            await opts.getSessionState({ rearmResume: true });
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
  const res = await opts.getSessionState();
  if (!opts.stillCurrent() || !res?.resume_pending) return;
  armResumeKick(opts);
}
