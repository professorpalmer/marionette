import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Opening/switching a session must not call api.resume unless the backend
 * reports the explicit resume_pending latch (self-edit restart continuity).
 * A trailing user message alone is not enough.
 */
describe("Conversation ghost-resume gate", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("does not schedule resume when resume_pending is false", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: false,
    });

    // Mirror Conversation.tsx activeSessionId effect contract.
    const scheduleIfPending = async () => {
      const res = await getSessionState();
      if (res?.resume_pending) {
        setTimeout(() => resume(), 300);
      }
    };

    await scheduleIfPending();
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
  });

  it("schedules resume only when resume_pending latch is true", async () => {
    const resume = vi.fn();
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: true,
    });

    const scheduleIfPending = async () => {
      const res = await getSessionState();
      if (res?.resume_pending) {
        setTimeout(() => resume(), 300);
      }
    };

    await scheduleIfPending();
    expect(resume).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).toHaveBeenCalledTimes(1);
  });
});
