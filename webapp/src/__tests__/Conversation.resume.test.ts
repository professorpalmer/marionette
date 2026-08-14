import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  armResumeKick,
  scheduleResumeIfPending,
} from "../components/conversation/sessionResumeLatch";

/**
 * Opening/switching a session must not call api.resume unless the backend
 * reports the explicit resume_pending latch (self-edit restart continuity).
 * A trailing user message alone is not enough.
 *
 * Round-10: peek schedules the kick; consume only inside the timeout after
 * stillCurrent. clearSafeTimeouts / switch must not permanently lose the latch.
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

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => true,
      schedule: setTimeout,
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledTimes(1);
    expect(getSessionState).toHaveBeenCalledWith();
  });

  it("peeks then consumes only inside the delayed kick", async () => {
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
      schedule: setTimeout,
    });
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledTimes(1);
    expect(getSessionState).toHaveBeenNthCalledWith(1);
    await vi.advanceTimersByTimeAsync(300);
    expect(getSessionState).toHaveBeenNthCalledWith(2, { consumeResume: true });
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it("ignores scheduled resume after session switch without consuming (sid/gen fence)", async () => {
    const resume = vi.fn();
    let current = true;
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: true,
    });

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => current,
      schedule: setTimeout,
    });
    current = false; // switch away before 300ms fires
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).not.toHaveBeenCalled();
    // Latch stays armed for the owning session — no consume on abandon.
    expect(getSessionState).toHaveBeenCalledTimes(1);
    expect(getSessionState).not.toHaveBeenCalledWith({ consumeResume: true });
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
      schedule: setTimeout,
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledTimes(1);
    expect(getSessionState).not.toHaveBeenCalledWith({ consumeResume: true });
  });

  it("re-arms latch when consume succeeds but stillCurrent fails before kick", async () => {
    const resume = vi.fn();
    let current = true;
    const getSessionState = vi.fn()
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      })
      .mockImplementationOnce(async () => {
        // Switch away while consume is in flight / immediately after.
        current = false;
        return {
          state: "idle",
          pending_swarms: false,
          resume_pending: true,
        };
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
      schedule: setTimeout,
      sessionId: "sess-a",
    });
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledWith({
      consumeResume: true,
      sessionId: "sess-a",
    });
    expect(getSessionState).toHaveBeenCalledWith({
      rearmResume: true,
      sessionId: "sess-a",
    });
  });

  it("re-schedules after abandon rearm when owner session is active again", async () => {
    const resume = vi.fn();
    let current = true;
    let ownerActive = true;
    const getSessionState = vi.fn()
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      })
      .mockImplementationOnce(async () => {
        current = false;
        return {
          state: "idle",
          pending_swarms: false,
          resume_pending: true,
        };
      })
      // rearm
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      })
      // scheduleResumeIfPending peek after rearm
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      })
      // delayed consume for the rescheduled kick
      .mockResolvedValueOnce({
        state: "idle",
        pending_swarms: false,
        resume_pending: true,
      });

    await scheduleResumeIfPending({
      getSessionState,
      resume,
      stillCurrent: () => current,
      ownerStillActive: () => ownerActive,
      schedule: setTimeout,
      sessionId: "sess-a",
    });
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).toHaveBeenCalledWith({
      rearmResume: true,
      sessionId: "sess-a",
    });
    // Owner is active again — rescheduled kick consumes and resumes.
    current = true;
    await vi.advanceTimersByTimeAsync(300);
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it("clearSafeTimeouts cancel after peek does not consume (owning session keeps latch)", async () => {
    const resume = vi.fn();
    const timeouts = new Set<ReturnType<typeof setTimeout>>();
    const schedule = (fn: () => void, ms: number) => {
      const id = setTimeout(() => {
        timeouts.delete(id);
        fn();
      }, ms);
      timeouts.add(id);
      return id;
    };
    const clearSafeTimeouts = () => {
      timeouts.forEach(clearTimeout);
      timeouts.clear();
    };
    const getSessionState = vi.fn().mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      resume_pending: true,
    });

    armResumeKick({
      getSessionState,
      resume,
      stillCurrent: () => true,
      schedule,
    });
    // Session switch path: cancel pending kicks before consume.
    clearSafeTimeouts();
    await vi.advanceTimersByTimeAsync(500);
    expect(resume).not.toHaveBeenCalled();
    expect(getSessionState).not.toHaveBeenCalled();
  });

  it("triggerResume cancelRef queue clears Looking… / Still working… hints", async () => {
    const {
      clearSwarmAwaitWaitHint,
      PILOT_LOOKING_HINT,
      SWARM_AWAIT_HINT,
      triggerResumeGate,
    } = await import("../components/conversation/swarmPoll");

    expect(
      triggerResumeGate({ userStopped: false, cancelArmed: true }),
    ).toBe("queue_clear_hint");
    // Mirror Conversation triggerResume queue_clear_hint branch.
    let waitHint: string | null = PILOT_LOOKING_HINT;
    const resumeQueued = { current: false };
    const gate = triggerResumeGate({ userStopped: false, cancelArmed: true });
    if (gate === "queue_clear_hint") {
      resumeQueued.current = true;
      waitHint = clearSwarmAwaitWaitHint(waitHint);
    }
    expect(resumeQueued.current).toBe(true);
    expect(waitHint).toBeNull();
    expect(clearSwarmAwaitWaitHint(SWARM_AWAIT_HINT)).toBeNull();
    expect(
      triggerResumeGate({ userStopped: false, cancelArmed: false }),
    ).toBe("execute");
  });
});
