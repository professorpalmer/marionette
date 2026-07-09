import { renderHook, waitFor, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearSWRCache, useStaleWhileRevalidate, writeSWRCache } from "../lib/useStaleWhileRevalidate";

describe("useStaleWhileRevalidate", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    clearSWRCache();
    sessionStorage.clear();
  });

  it("keeps stale data visible while a new key revalidates", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(["a"])
      .mockImplementationOnce(
        () => new Promise((resolve) => setTimeout(() => resolve(["b"]), 50)),
      );

    const { result, rerender } = renderHook(
      ({ key }) => useStaleWhileRevalidate<string[]>(key, fetcher),
      { initialProps: { key: "one" } },
    );

    await waitFor(() => expect(result.current.data).toEqual(["a"]));
    expect(result.current.isShowingStale).toBe(false);

    rerender({ key: "two" });

    expect(result.current.data).toEqual(["a"]);
    expect(result.current.isValidating).toBe(true);
    expect(result.current.isShowingStale).toBe(true);

    await waitFor(() => expect(result.current.data).toEqual(["b"]));
    expect(result.current.isShowingStale).toBe(false);
    expect(result.current.isValidating).toBe(false);
  });

  it("ignores stale responses when a newer request was started", async () => {
    let resolveFirst: (v: string) => void;
    let resolveSecond: (v: string) => void;
    const first = new Promise<string>((r) => { resolveFirst = r; });
    const second = new Promise<string>((r) => { resolveSecond = r; });

    const fetcher = vi
      .fn()
      .mockResolvedValueOnce("initial")
      .mockReturnValueOnce(first)
      .mockReturnValueOnce(second);

    const { result } = renderHook(() =>
      useStaleWhileRevalidate("key", fetcher),
    );

    await waitFor(() => expect(result.current.data).toBe("initial"));

    await act(async () => {
      const p1 = result.current.revalidate();
      const p2 = result.current.revalidate();
      resolveSecond!("fresh");
      await p2;
      resolveFirst!("stale");
      await p1;
    });

    expect(result.current.data).toBe("fresh");
  });

  it("rehydrates swarm keys from sessionStorage after cache clear", async () => {
    writeSWRCache("swarm:/tmp/proj", { session: { tokens_used: 1, est_cost_usd: 0 }, jobs: [] });
    clearSWRCache();
    // clearSWRCache wipes sessionStorage too -- seed persist directly.
    sessionStorage.setItem(
      "swr.persist.v1:swarm:/tmp/proj",
      JSON.stringify({ session: { tokens_used: 9, est_cost_usd: 0.1 }, jobs: [{ id: "j1" }] }),
    );

    const fetcher = vi.fn().mockImplementation(
      () => new Promise(() => {}), // never resolves -- stay on persisted seed
    );
    const { result } = renderHook(() =>
      useStaleWhileRevalidate("swarm:/tmp/proj", fetcher),
    );

    expect(result.current.data).toEqual({
      session: { tokens_used: 9, est_cost_usd: 0.1 },
      jobs: [{ id: "j1" }],
    });
  });
});
