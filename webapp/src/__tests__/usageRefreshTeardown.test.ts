import { afterEach, expect, it, vi } from "vitest";
import { cancelUsageRefresh, requestUsageRefresh, USAGE_REFRESH_WINDOW_MS } from "../lib/usageRefresh";

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
