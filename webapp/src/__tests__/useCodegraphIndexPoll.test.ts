import { renderHook, act } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const getCodegraph = vi.hoisted(() => vi.fn());
vi.mock("../lib/api", async (orig) => {
  const actual = await orig<typeof import("../lib/api")>();
  return { ...actual, api: { ...actual.api, getCodegraph } };
});
import { useCodegraphIndexPoll } from "../lib/useCodegraphIndexPoll";

beforeEach(() => { vi.useFakeTimers(); getCodegraph.mockReset(); });
afterEach(() => vi.useRealTimers());
const tick = async (ms: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

it("polls the cheap status while indexing and reloads only when it changes", async () => {
  getCodegraph.mockResolvedValue({ status: "indexing" });
  const onChanged = vi.fn();
  renderHook(() => useCodegraphIndexPoll("indexing", onChanged));
  await tick(0);
  await tick(8000);
  expect(getCodegraph).toHaveBeenCalledTimes(3);
  expect(onChanged).not.toHaveBeenCalled();
  getCodegraph.mockResolvedValue({ status: "ready" });
  await tick(4000);
  expect(onChanged).toHaveBeenCalledTimes(1);
});

it("does not poll once indexing is done", async () => {
  const onChanged = vi.fn();
  renderHook(() => useCodegraphIndexPoll("ready", onChanged));
  await tick(12000);
  expect(getCodegraph).not.toHaveBeenCalled();
});
