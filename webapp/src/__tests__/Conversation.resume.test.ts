import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Opening/switching a session must not call api.resume unless the backend
 * reports the explicit resume_pending latch (self-edit restart continuity).
 * A trailing user message alone is not enough.
 *
 * Round-9: peek first, consume only when scheduling; fence the 300ms kick on
 * activeSessionId / transcriptLoadGen so a switch cannot fire into B.
 */
describe("Conversation ghost-resume gate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  type SessionState = {
    state: string;
    pending_swarms: boolean;
    resume_pending?: boolean;
  };

  /** Mirror Conversation.tsx activeSessionId resume-schedule contract. */
  async function scheduleResumeIfPending(opts: {
    getSessionState: (o?: { consumeResume?: boolean }) => Promise<SessionState>;
    resume: () => void;
    stillCurrent: () => boolean;
  }) {
    const res = await opts.getSessionState();
    if (!opts.stillCurrent() || !res?.resume_pending) return;
    const consumed = await opts.getSessionState({ consumeResume: true });
    if (!opts.stillCurrent() || !consumed?.resume_pending) return;
    setTimeout(() => {
      if (!opts.stillCurrent()) return;
      opts.resume();
    }, 300);
  }

  it("does not schedule resume when resume_pending is false", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: false,
    });

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => true,
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledTimes(1);
    expect(getSessionState).toHaveBeenCalledWith();
  });

  it("peeks then consumes before scheduling resume", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn()
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      })
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      });

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => true,
    });
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenNthCalledWith(1);
    expect(getSessionState).toHaveBeenNthCalledWith(2, { consumeResume: true });
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it("ignores scheduled resume after session switch (sid/gen fence)", async () => {
    const resume = vi.fn();
    let current = true;
    const getSessionState = vi.fn()
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      })
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      });

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => current,
    });
    current = false; // switch away before 300ms fires
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).not.toHaveBeenCalled();
  });

  it("does not consume when peek sees latch but session already switched", async () => {
    const resume = vi.fn();
    let current = true;
    const getSessionState = vi.fn().mockImplementation(async () => {
      current = false; // switch during peek await
      return {
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      };
    });

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => current,
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledTimes(1);
    expect(getSessionState).not.toHaveBeenCalledWith({ consumeResume: true });
  });
});
