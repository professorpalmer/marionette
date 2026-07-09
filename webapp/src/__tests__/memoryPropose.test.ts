import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * Client contracts for memory propose Save/Skip and wiki status strip.
 */

describe("memory propose + wiki status client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("memoryProposeAccept/Dismiss POST the proposal id", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("../lib/api");
    await api.memoryProposeAccept("memprop_abc");
    await api.memoryProposeDismiss("memprop_xyz");
    const urls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/api/memory/propose/accept"))).toBe(true);
    expect(urls.some((u) => u.includes("/api/memory/propose/dismiss"))).toBe(true);
    const bodies = fetchMock.mock.calls.map((c) => {
      try {
        return JSON.parse(String((c[1] as RequestInit)?.body || "{}"));
      } catch {
        return {};
      }
    });
    expect(bodies).toEqual(
      expect.arrayContaining([{ id: "memprop_abc" }, { id: "memprop_xyz" }]),
    );
  });

  it("getWikiStatus hits /api/wiki/status and returns counts", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        configured: true,
        status: "ok",
        page_count: 12,
        link_count: 34,
        base_url: "http://127.0.0.1:8000",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { api } = await import("../lib/api");
    const res = await api.getWikiStatus();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/wiki/status");
    expect(res.page_count).toBe(12);
    expect(res.link_count).toBe(34);
  });
});
