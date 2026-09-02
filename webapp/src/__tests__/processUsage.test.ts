import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import {
  _resetProcessUsageForTests,
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
