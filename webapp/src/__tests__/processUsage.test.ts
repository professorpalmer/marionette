import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import {
  _resetProcessUsageForTests,
  activeSessionUsage,
  getProcessUsage,
  refreshProcessUsage,
  subscribeProcessUsage,
  type ProcessUsageSnapshot,
} from "../lib/processUsage";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
  },
}));

const mockGetUsage = vi.mocked(api.getUsage);

function session(estCostUsd: number, tokensUsed = 100) {
  return {
    session: {
      tokens_used: tokensUsed,
      est_cost_usd: estCostUsd,
      driver: "test",
      price_in: 1,
      price_out: 1,
    },
    jobs: [],
    session_total: { session_id: "s", est_cost_usd: estCostUsd, input_tokens: 1, output_tokens: 1 },
  };
}

describe("processUsage store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    _resetProcessUsageForTests();
    mockGetUsage.mockResolvedValue(session(0.55));
  });

  afterEach(() => {
    _resetProcessUsageForTests();
  });

  it("gives two subscribers the same generation and session", async () => {
    const first: ProcessUsageSnapshot[] = [];
    const second: ProcessUsageSnapshot[] = [];
    const offA = subscribeProcessUsage((snap) => first.push(snap));
    const offB = subscribeProcessUsage((snap) => second.push(snap));
    await refreshProcessUsage();
    const latestA = first.at(-1);
    const latestB = second.at(-1);
    expect(latestA?.session?.est_cost_usd).toBe(0.55);
    expect(latestB?.session?.est_cost_usd).toBe(0.55);
    expect(latestA?.generation).toBe(latestB?.generation);
    expect(latestA?.generation).toBeGreaterThan(0);
    offA();
    offB();
  });

  it("does not start a second getUsage while one is in flight", async () => {
    let resolveFirst!: (value: ReturnType<typeof session>) => void;
    mockGetUsage.mockImplementation(
      () => new Promise((resolve) => {
        resolveFirst = resolve;
      }),
    );
    const first = refreshProcessUsage();
    const second = refreshProcessUsage();
    expect(first).toBe(second);
    expect(mockGetUsage).toHaveBeenCalledTimes(1);
    resolveFirst(session(0.12));
    await first;
    expect(getProcessUsage().session?.est_cost_usd).toBe(0.12);
  });

  it("keeps the previous spend snapshot when a zero poll arrives", async () => {
    subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    expect(getProcessUsage().session?.est_cost_usd).toBe(0.55);
    mockGetUsage.mockResolvedValue(session(0, 0));
    await refreshProcessUsage();
    expect(getProcessUsage().session?.est_cost_usd).toBe(0.55);
  });

  it("accepts a zero snapshot after a session-change event", async () => {
    subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    mockGetUsage.mockResolvedValue(session(0, 0));
    window.dispatchEvent(new Event("harness-session-changed"));
    await refreshProcessUsage();
    expect(getProcessUsage().session?.est_cost_usd).toBe(0);
    expect(getProcessUsage().session?.tokens_used).toBe(0);
  });
});

it("does not swallow incomplete zero and accepts successful retry", async () => {
  _resetProcessUsageForTests();
  mockGetUsage.mockResolvedValue(session(2));
  await refreshProcessUsage();
  mockGetUsage.mockResolvedValue({ ...session(0, 0), session: { ...session(0, 0).session, read_status: "unavailable" } });
  await refreshProcessUsage();
  expect(getProcessUsage().readStatus).toBe("unavailable");
  mockGetUsage.mockResolvedValue(session(0, 0));
  await refreshProcessUsage();
  expect(getProcessUsage().readStatus).toBeUndefined();
  expect(getProcessUsage().session?.est_cost_usd).toBe(0);
});

it("accepts provider-attested zero after positive spend", async () => {
  _resetProcessUsageForTests();
  mockGetUsage.mockResolvedValue(session(2));
  await refreshProcessUsage();
  mockGetUsage.mockResolvedValue({ ...session(0, 0), session: { ...session(0, 0).session, cost_source: "provider" } });
  await refreshProcessUsage();
  expect(getProcessUsage().session?.est_cost_usd).toBe(0);
});

it("fences an old-scope response while the new request fails", async () => {
  _resetProcessUsageForTests();
  let resolveOld!: (value: ReturnType<typeof session>) => void;
  mockGetUsage.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
  const off = subscribeProcessUsage(() => {});
  const old = refreshProcessUsage();
  mockGetUsage.mockRejectedValueOnce(new Error("offline"));
  window.dispatchEvent(new Event("harness-project-selected"));
  await refreshProcessUsage();
  resolveOld(session(99));
  await old;
  expect(getProcessUsage().session).toBeNull();
  expect(getProcessUsage().readStatus).toBe("unavailable");
  off();
});

describe("session usage startup retries", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    _resetProcessUsageForTests();
  });

  afterEach(() => {
    _resetProcessUsageForTests();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  function unavailable() {
    const data = session(2);
    return { ...data, session_total: { ...data.session_total, read_status: "unavailable" as const } };
  }

  it("recovers after one second instead of waiting for the idle poll", async () => {
    mockGetUsage.mockResolvedValueOnce(unavailable()).mockResolvedValue(session(2));
    const off = subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    expect(getProcessUsage().status).toBe("loading");
    await vi.advanceTimersByTimeAsync(1000);
    expect(mockGetUsage).toHaveBeenCalledTimes(2);
    expect(getProcessUsage().status).toBe("ready");
    off();
  });

  it("bounds failed startup retries and gives immediate manual retry feedback", async () => {
    mockGetUsage.mockResolvedValue(unavailable());
    const off = subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockGetUsage).toHaveBeenCalledTimes(4);
    expect(getProcessUsage().status).toBe("unavailable");
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockGetUsage).toHaveBeenCalledTimes(4);
    let resolveRetry!: (value: ReturnType<typeof session>) => void;
    mockGetUsage.mockImplementationOnce(() => new Promise(resolve => { resolveRetry = resolve; }));
    const retry = refreshProcessUsage({ manual: true });
    expect(getProcessUsage().status).toBe("loading");
    resolveRetry(session(3));
    await retry;
    expect(getProcessUsage().status).toBe("ready");
    off();
  });

  it("cancels startup retries when the last subscriber leaves", async () => {
    mockGetUsage.mockResolvedValue(unavailable());
    const off = subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    off();
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockGetUsage).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not perform quick retries while the page is hidden", async () => {
    mockGetUsage.mockResolvedValue(unavailable());
    const off = subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockGetUsage).toHaveBeenCalledTimes(1);
    hidden.mockReturnValue(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await refreshProcessUsage();
    expect(mockGetUsage).toHaveBeenCalledTimes(2);
    off();
  });

  it("accepts authoritative session totals despite unavailable process usage", async () => {
    const data = session(2);
    mockGetUsage.mockResolvedValue({ ...data, session: { ...data.session, read_status: "unavailable" } });
    const off = subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    expect(getProcessUsage().status).toBe("ready");
    expect(activeSessionUsage(getProcessUsage())?.est_cost_usd).toBe(2);
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockGetUsage).toHaveBeenCalledTimes(1);
    off();
  });

  it("does not expose partial totals as authoritative session usage", async () => {
    mockGetUsage.mockResolvedValue(unavailable());
    await refreshProcessUsage();
    expect(activeSessionUsage(getProcessUsage())).toBeNull();
  });

  it("treats confirmed no-session and zero totals as ready without fast retries", async () => {
    mockGetUsage.mockResolvedValue({ ...session(0, 0), session_total: null });
    const off = subscribeProcessUsage(() => {});
    await refreshProcessUsage();
    expect(getProcessUsage().status).toBe("ready");
    expect(activeSessionUsage(getProcessUsage())).toBeNull();
    mockGetUsage.mockResolvedValue(session(0, 0));
    window.dispatchEvent(new Event("harness-session-changed"));
    await refreshProcessUsage();
    expect(getProcessUsage().status).toBe("ready");
    await vi.advanceTimersByTimeAsync(3000);
    expect(mockGetUsage).toHaveBeenCalledTimes(2);
    off();
  });

  it("rejects both old A and B responses after an A-B-A switch", async () => {
    const resolves: Array<(value: ReturnType<typeof session>) => void> = [];
    mockGetUsage.mockImplementation(() => new Promise(resolve => { resolves.push(resolve); }));
    const off = subscribeProcessUsage(() => {});
    const oldA = refreshProcessUsage();
    window.dispatchEvent(new Event("harness-session-changed"));
    const oldB = refreshProcessUsage();
    window.dispatchEvent(new Event("harness-session-changed"));
    const newA = refreshProcessUsage();
    resolves[2](session(3));
    await newA;
    resolves[0](session(99));
    resolves[1](session(88));
    await Promise.all([oldA, oldB]);
    expect(activeSessionUsage(getProcessUsage())?.est_cost_usd).toBe(3);
    expect(getProcessUsage().status).toBe("ready");
    off();
  });
});
