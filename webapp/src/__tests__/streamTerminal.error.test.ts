/**
 * streamErrorText: the renderer's terminal text for a failed live stream.
 * Regression for the v0.9.95 update-skew incident -- a 403 from the backend
 * auth gate must read as an authentication/backend problem, not the generic
 * "[aborted] Connection closed" chrome, and must never echo raw error payloads
 * (which cross the IPC boundary and could carry sensitive text).
 */
import { describe, expect, it } from "vitest";
import {
  STREAM_ABORT_MESSAGE,
  alreadySettledOnDoneStatus,
  resetCrossSessionLatchesOnSwitch,
  resetTurnLifecycleOnSessionSwitch,
  resetTurnSettledOnSessionSwitch,
  shouldRefreshBusyChrome,
  streamErrorText,
  streamOnDoneDecision,
} from "../components/conversation/streamTerminal";

const SECRET = "deadbeefcafe1234deadbeefcafe1234";

describe("streamErrorText", () => {
  it("renders an authentication error for a structured 403 from the Electron bridge", () => {
    const text = streamErrorText({
      status: 403,
      code: "auth",
      message: "backend rejected the stream (HTTP 403: authentication failed)",
    });
    expect(text).toMatch(/403/);
    expect(text).toMatch(/authentication failed/i);
    expect(text).toMatch(/out of sync|relaunch/i);
    expect(text).not.toBe(STREAM_ABORT_MESSAGE);
  });

  it("parses the web transport's thrown Error('stream /api/chat -> 403')", () => {
    const text = streamErrorText(new Error("stream /api/chat -> 403"));
    expect(text).toMatch(/403/);
    expect(text).toMatch(/authentication failed/i);
  });

  it("renders a generic backend failure for other HTTP statuses", () => {
    const text = streamErrorText({ status: 500, code: "backend_error", message: "boom" });
    expect(text).toMatch(/500/);
    expect(text).not.toMatch(/authentication/i);
  });

  it("renders a backend-unreachable message for connection errors", () => {
    for (const err of [
      { status: null, code: "ECONNREFUSED", message: "backend stream connection failed" },
      new Error("connect ECONNREFUSED 127.0.0.1:8799"),
      "socket hang up",
    ]) {
      const text = streamErrorText(err as any);
      expect(text).toMatch(/not reachable|connection failed/i);
    }
  });

  it("falls back to the abort chrome for unknown errors", () => {
    expect(streamErrorText(undefined)).toBe(STREAM_ABORT_MESSAGE);
    expect(streamErrorText(null)).toBe(STREAM_ABORT_MESSAGE);
    expect(streamErrorText("something odd")).toBe(STREAM_ABORT_MESSAGE);
  });

  it("named model terminals are not connection-lost chrome", () => {
    expect(streamErrorText({ message: "boom" }, "length")).toMatch(/length/i);
    expect(streamErrorText({ message: "boom" }, "length")).not.toMatch(
      /connection|closed before/i,
    );
    expect(streamErrorText({ message: "boom" }, "content_filter")).toMatch(/refused/i);
    expect(streamErrorText({ message: "boom" }, "incomplete")).toMatch(/incomplete/i);
    expect(streamErrorText({ message: "boom" }, "provider_eof")).not.toMatch(
      /connection lost/i,
    );
  });

  it("never echoes raw error payload text (no token/body leakage)", () => {
    for (const err of [
      { status: 403, code: "auth", message: `rejected token ${SECRET}` },
      new Error(`stream /api/chat?token=${SECRET} -> 403`),
      { status: null, code: "ECONNRESET", message: `reset while sending ${SECRET}` },
      `weird failure carrying ${SECRET}`,
    ]) {
      expect(streamErrorText(err as any)).not.toContain(SECRET);
    }
  });
});

describe("Wave 5 stream terminal chrome gates", () => {
  it("treats stream EOF without assistant_done as an honest abort", () => {
    expect(streamOnDoneDecision({ turnSettled: false, userStopped: false }).kind).toBe(
      "abort_error",
    );
    expect(streamOnDoneDecision({ turnSettled: true, userStopped: false }).kind).toBe("done");
  });

  it("does not treat a sealed answer as authority for stream EOF", () => {
    expect(
      streamOnDoneDecision({
        turnSettled: false,
        userStopped: false,
      }).kind,
    ).toBe("abort_error");
  });

  it("blocks busy-chrome refresh after settle or Stop", () => {
    expect(shouldRefreshBusyChrome({ turnSettled: false })).toBe(true);
    expect(shouldRefreshBusyChrome({ turnSettled: true })).toBe(false);
    expect(shouldRefreshBusyChrome({ turnSettled: false, userStopped: true })).toBe(false);
  });

  it("resets turnSettled on session switch", () => {
    const ref = { current: true };
    resetTurnSettledOnSessionSwitch(ref);
    expect(ref.current).toBe(false);
  });

  it("resets turn lifecycle and recovery context on session switch", () => {
    const lifecycle: { current: string } = { current: "settled_incomplete" };
    const cause: { current: string | null } = { current: "provider_eof" };
    const recoveryDispatchingRef = { current: true };
    const recoveryContextRef = {
      current: { sessionId: "sess-a", generation: 2 },
    };
    resetTurnLifecycleOnSessionSwitch({
      setTurnLifecycle: (next) => { lifecycle.current = next; },
      setTerminalCause: (next) => { cause.current = next; },
      recoveryDispatchingRef,
      recoveryContextRef,
    });
    expect(lifecycle.current).toBe("settled_complete");
    expect(cause.current).toBeNull();
    expect(recoveryDispatchingRef.current).toBe(false);
    expect(recoveryContextRef.current).toBeNull();
  });

  it("already-settled onDone never upgrades error/idle to done", () => {
    expect(alreadySettledOnDoneStatus({
      prev: "error",
      liveJobs: false,
      userStopped: false,
    })).toBe("error");
    expect(alreadySettledOnDoneStatus({
      prev: "idle",
      liveJobs: false,
      userStopped: true,
    })).toBe("idle");
  });

  it("clears userStopped / resume / approved-retry latches on session switch", () => {
    const userStoppedRef = { current: true };
    const resumeQueuedRef = { current: true };
    const approvedCommandRetryRef = { current: "ls -la" as string | null };
    resetCrossSessionLatchesOnSwitch({
      userStoppedRef,
      resumeQueuedRef,
      approvedCommandRetryRef,
    });
    expect(userStoppedRef.current).toBe(false);
    expect(resumeQueuedRef.current).toBe(false);
    expect(approvedCommandRetryRef.current).toBeNull();
  });
});
