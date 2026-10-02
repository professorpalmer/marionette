import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { USAGE_REFRESH_WINDOW_MS, requestUsageRefresh } from "../lib/usageRefresh";

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
