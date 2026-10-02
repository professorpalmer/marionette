import { afterEach, describe, expect, it, vi } from "vitest";

const getSessionState = vi.fn();
vi.mock("../lib/api", () => ({ api: { getSessionState: (...a: unknown[]) => getSessionState(...a) } }));

import {
  SESSION_STATE_POLL_MS,
  refreshSessionStateFeed,
  setSessionStateFeedSession,
  subscribeSessionState,
} from "../lib/sessionStateFeed";

const reply = { state: "idle", pending_swarms: false, active_view_id: "a", runners: { a: "idle" } };

afterEach(() => {
  vi.useRealTimers();
  getSessionState.mockReset();
});

describe("session state feed", () => {
  it("serves every subscriber from one request per tick", async () => {
    vi.useFakeTimers();
    getSessionState.mockResolvedValue(reply);
    setSessionStateFeedSession("a");
    const footer = vi.fn();
    const rail = vi.fn();
    const offFooter = subscribeSessionState(footer);
    const offRail = subscribeSessionState(rail);
    await vi.advanceTimersByTimeAsync(SESSION_STATE_POLL_MS * 3 + 10);
    expect(getSessionState).toHaveBeenCalledTimes(4);
    expect(getSessionState).toHaveBeenCalledWith({ sessionId: "a" });
    expect(footer).toHaveBeenCalledTimes(4);
    expect(rail).toHaveBeenCalledTimes(4);
    offFooter();
    offRail();
    await vi.advanceTimersByTimeAsync(SESSION_STATE_POLL_MS * 3);
    expect(getSessionState).toHaveBeenCalledTimes(4);
  });

  it("does not hand a new session the reply requested for the old one", async () => {
    let release: (v: unknown) => void = () => {};
    getSessionState.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    getSessionState.mockResolvedValue(reply);
    const seen: string[] = [];
    const off = subscribeSessionState((_s, requestedFor) => seen.push(requestedFor));
    setSessionStateFeedSession("old");
    const first = refreshSessionStateFeed();
    setSessionStateFeedSession("new");
    await refreshSessionStateFeed();
    release(reply);
    await first;
    off();
    expect(getSessionState).toHaveBeenLastCalledWith({ sessionId: "new" });
    expect(seen).toContain("new");
  });
});

import { knownRunnerState } from "../lib/sessionStateFeed";

describe("known runner state", () => {
  it("remembers every session's runner from the latest reply", async () => {
    getSessionState.mockResolvedValue({ ...reply, runners: { a: "idle", b: "running" } });
    const off = subscribeSessionState(() => {});
    setSessionStateFeedSession("a");
    await refreshSessionStateFeed(true);
    // A session switch reads this before its own state request returns, so a
    // running target shows Stop at once instead of Stop -> Send -> Stop.
    expect(knownRunnerState("b")).toBe("running");
    expect(knownRunnerState("a")).toBe("idle");
    expect(knownRunnerState("never-seen")).toBeUndefined();
    off();
  });
});
