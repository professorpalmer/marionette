import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { _resetActivityForTests, appBusy, setActivity } from "../lib/appActivity";
import { usePolling } from "../lib/usePolling";

afterEach(() => { cleanup(); vi.useRealTimers(); _resetActivityForTests(); });

describe("app activity", () => {
  it("is busy while any source is busy", () => {
    expect(appBusy()).toBe(false);
    setActivity("runners", true);
    setActivity("turn", true);
    setActivity("runners", false);
    expect(appBusy()).toBe(true);
    setActivity("turn", false);
    expect(appBusy()).toBe(false);
  });
});

describe("idle-aware polling", () => {
  it("polls at the idle interval while nothing is running", async () => {
    // Idle, the app made ~56 requests per 30s; MCP status, reviews and the
    // prompt queue each polled every 3-4s for state that only changes
    // while something is running.
    vi.useFakeTimers();
    const poll = vi.fn(async () => {});
    renderHook(() => usePolling(poll, 1000, { idleIntervalMs: 10_000 }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(poll).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(9000));
    expect(poll).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1100));
    expect(poll).toHaveBeenCalledTimes(2);
  });

  it("wakes at once when activity starts and keeps the fast interval while busy", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(async () => {});
    renderHook(() => usePolling(poll, 1000, { idleIntervalMs: 10_000 }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(poll).toHaveBeenCalledTimes(1);
    await act(async () => { setActivity("turn", true); });
    expect(poll).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(3100));
    expect(poll).toHaveBeenCalledTimes(5);
    await act(async () => { setActivity("turn", false); });
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(poll.mock.calls.length).toBeLessThanOrEqual(6);
  });

  it("can slow down when idle without waking on activity", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(async () => {});
    renderHook(() => usePolling(poll, 1000, { idleIntervalMs: 10_000, wakeOnActivity: false }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    await act(async () => { setActivity("turn", true); });
    expect(poll).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(10_100));
    expect(poll).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(1100));
    expect(poll).toHaveBeenCalledTimes(3);
  });

  it("leaves pollers without an idle interval unchanged", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(async () => {});
    renderHook(() => usePolling(poll, 1000));
    await act(() => vi.advanceTimersByTimeAsync(3100));
    expect(poll).toHaveBeenCalledTimes(4);
  });
});
