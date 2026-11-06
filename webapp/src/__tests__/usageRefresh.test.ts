import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { USAGE_REFRESH_WINDOW_MS, cancelUsageRefresh, requestUsageRefresh } from "../lib/usageRefresh";

describe("usage refresh requests", () => {
  let fired = 0;
  const onRefresh = () => { fired += 1; };
  beforeEach(() => {
    vi.useFakeTimers();
    fired = 0;
    window.addEventListener("harness-usage-refresh", onRefresh);
  });
  afterEach(() => {
    window.removeEventListener("harness-usage-refresh", onRefresh);
    vi.useRealTimers();
  });

  it("collapses a burst into one event", () => {
    // A session switch replays every past tool result; each one asked for a
    // refresh, and the Economics pane reloaded on every event (~16 a switch).
    for (let i = 0; i < 16; i++) requestUsageRefresh();
    expect(fired).toBe(0);
    vi.advanceTimersByTime(USAGE_REFRESH_WINDOW_MS);
    expect(fired).toBe(1);
  });

  it("fires again for a request after the window", () => {
    requestUsageRefresh();
    vi.advanceTimersByTime(USAGE_REFRESH_WINDOW_MS);
    requestUsageRefresh();
    vi.advanceTimersByTime(USAGE_REFRESH_WINDOW_MS);
    expect(fired).toBe(2);
  });
});

describe("usage refresh teardown", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    cancelUsageRefresh();
  });

  // A pending refresh fired after its test environment was torn down and threw
  // "window is not defined", failing CI with every test green.
  it("a pending refresh fires on the window it was scheduled on", () => {
    vi.useFakeTimers();
    const seen = vi.fn();
    window.addEventListener("harness-usage-refresh", seen);
    requestUsageRefresh();
    const scheduledOn = window;
    vi.stubGlobal("window", undefined);
    expect(() => vi.advanceTimersByTime(USAGE_REFRESH_WINDOW_MS)).not.toThrow();
    vi.unstubAllGlobals();
    expect(seen).toHaveBeenCalledTimes(1);
    scheduledOn.removeEventListener("harness-usage-refresh", seen);
  });

  it("cancel drops a pending refresh so the next request is not swallowed", () => {
    vi.useFakeTimers();
    const seen = vi.fn();
    window.addEventListener("harness-usage-refresh", seen);
    requestUsageRefresh();
    cancelUsageRefresh();
    vi.advanceTimersByTime(USAGE_REFRESH_WINDOW_MS);
    expect(seen).not.toHaveBeenCalled();
    requestUsageRefresh();
    vi.advanceTimersByTime(USAGE_REFRESH_WINDOW_MS);
    expect(seen).toHaveBeenCalledTimes(1);
    window.removeEventListener("harness-usage-refresh", seen);
  });
});
