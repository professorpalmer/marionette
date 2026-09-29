import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const getAdvice = vi.hoisted(() => vi.fn());
vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, getAdvice } };
});

import AdvicePane from "../components/AdvicePane";

const receipt = (status: string) => ({
  request_id: "r1", session_id: "s1", status, created_at: 1, model: "m", question: "q", answer: "",
  error: "", usage: { tokens_in: null, tokens_out: null, cost_usd: null, cost_source: "" },
});

beforeEach(() => { vi.useFakeTimers(); getAdvice.mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

async function flush(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }

it("polls slowly when no consultation is in flight", async () => {
  getAdvice.mockResolvedValue({ session_id: "s1", receipts: [receipt("done")] });
  render(<AdvicePane sessionId="s1" />);
  await flush(0);
  expect(getAdvice).toHaveBeenCalledTimes(1);
  await flush(3000);
  expect(getAdvice).toHaveBeenCalledTimes(1);
  await flush(7000);
  expect(getAdvice).toHaveBeenCalledTimes(2);
});

it("polls every second while a consultation runs", async () => {
  getAdvice.mockResolvedValue({ session_id: "s1", receipts: [receipt("running")] });
  render(<AdvicePane sessionId="s1" />);
  await flush(0);
  await flush(1000);
  await flush(1000);
  expect(getAdvice).toHaveBeenCalledTimes(3);
});
