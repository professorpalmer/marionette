import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createGitRefreshCoordinator, type GitRefreshLane } from "../lib/gitRefreshCoordinator";
function deferred() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());
function setup() {
  const reads: { lane: GitRefreshLane; path: string; epoch: number; time: number; result: ReturnType<typeof deferred> }[] = [];
  const onError = vi.fn();
  const onBusy = vi.fn();
  const coordinator = createGitRefreshCoordinator({
    run: (lane, context) => {
      const result = deferred();
      reads.push({ lane, ...context, time: performance.now(), result });
      return result.promise;
    }, onError, onBusy,
  });
  const context = coordinator.activate("/a");
  return { coordinator, context, reads, onError, onBusy };
}
it("serializes each lane, coalesces a burst to one trailing read, and awaits that read", async () => {
  const { coordinator: c, context, reads } = setup();
  const first = c.request(context, ["status", "branches"]);
  let finished = false;
  const trailing = Promise.all(Array.from({ length: 20 }, () => c.request(context, ["status", "branches"]))).then(() => { finished = true; });
  expect(reads.map(r => r.lane)).toEqual(["status", "branches"]);
  reads[0].result.resolve();
  await vi.advanceTimersByTimeAsync(0);
  expect(reads.map(r => r.lane)).toEqual(["status", "branches", "status"]);
  expect(finished).toBe(false);
  reads[1].result.resolve();
  await first;
  expect(reads).toHaveLength(4);
  reads[2].result.resolve(); reads[3].result.resolve();
  await trailing;
  expect(finished).toBe(true);
});
it("sustained automatic events make progress at fixed 500ms deadlines", async () => {
  const { coordinator: c, context, reads } = setup();
  for (let t = 0; t < 2000; t += 100) {
    void c.request(context, ["status"], "automatic");
    reads.forEach(r => r.result.resolve());
    await vi.advanceTimersByTimeAsync(100);
  }
  expect(reads.map(r => r.time)).toEqual([0, 500, 1000, 1500, 2000]);
  reads.forEach(r => r.result.resolve());
  await vi.advanceTimersByTimeAsync(0);
});
it("manual refresh promotes a scheduled automatic read without waiting for the floor", async () => {
  const { coordinator: c, context, reads } = setup();
  const first = c.request(context, ["status"], "automatic");
  reads[0].result.resolve(); await first;
  await vi.advanceTimersByTimeAsync(100);
  const scheduled = c.request(context, ["status"], "automatic");
  expect(reads).toHaveLength(1);
  const manual = c.request(context, ["status", "branches"]);
  expect(reads.map(r => r.time)).toEqual([0, 100, 100]);
  reads[1].result.resolve(); reads[2].result.resolve();
  await Promise.all([scheduled, manual]);
  await vi.advanceTimersByTimeAsync(500);
  expect(reads).toHaveLength(3);
});
it("promotion while running starts just one trailing request and rejection releases the lane", async () => {
  const { coordinator: c, context, reads, onError } = setup();
  const first = c.request(context, ["status"], "automatic");
  const auto = c.request(context, ["status"], "automatic");
  const manual = c.request(context, ["status"]);
  reads[0].result.reject(new Error("failed"));
  await first;
  expect(onError).toHaveBeenCalledTimes(1);
  expect(reads.map(r => r.time)).toEqual([0, 0]);
  reads[1].result.resolve();
  await Promise.all([auto, manual]);
});
it("A-B-A invalidates queued work but holds the occupied lane until its request settles", async () => {
  const { coordinator: c, context: a, reads } = setup();
  const first = c.request(a, ["status"]);
  const stale = c.request(a, ["status"]);
  const b = c.activate("/b");
  const skippedB = c.request(b, ["status"]);
  const a2 = c.activate("/a");
  const latest = c.request(a2, ["status"]);
  expect(c.isCurrent(a)).toBe(false);
  await stale; await skippedB;
  expect(reads).toHaveLength(1);
  reads[0].result.resolve(); await first;
  expect(reads.map(r => r.epoch)).toEqual([a.epoch, a2.epoch]);
  reads[1].result.resolve(); await latest;
});
it("deactivation cancels timers and pending work, and stale rejection cannot report errors", async () => {
  const { coordinator: c, context, reads, onError } = setup();
  const first = c.request(context, ["status"], "automatic");
  reads[0].result.resolve(); await first;
  const scheduled = c.request(context, ["status"], "automatic");
  const branch = c.request(context, ["branches"]);
  c.deactivate();
  reads[1].result.reject(new Error("old"));
  await Promise.all([scheduled, branch]);
  await vi.advanceTimersByTimeAsync(1000);
  await c.request(context, ["status"]);
  expect(reads).toHaveLength(2);
  expect(onError).not.toHaveBeenCalled();
});
