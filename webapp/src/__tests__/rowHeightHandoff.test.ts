import { describe, expect, it } from "vitest";
import type { GroupedItem } from "../components/TranscriptList";
import { createTranscriptRowHeightCache } from "../components/conversation/transcriptRowHeight";

// A row finished in the live tail moves into the virtual list at turn end.
// Its first virtual frame must use the height it already had on screen, not
// an estimate that is corrected a few frames later (a 28px jump in the rig).
describe("row height handoff", () => {
  const answer: GroupedItem = { kind: "msg", msg: { role: "assistant", text: "## Result\n\nAll six probes printed.", id: "m1" } } as GroupedItem;

  it("prefers the height the row was measured at", () => {
    const cache = createTranscriptRowHeightCache();
    const estimate = cache.estimateRowHeight(answer, "m1", 560);
    cache.recordMeasuredHeight(answer, "m1", 560, estimate + 28);
    expect(cache.estimateRowHeight(answer, "m1", 560)).toBe(estimate + 28);
  });

  it("ignores a measurement of shorter content (final text flushed at seal)", () => {
    const cache = createTranscriptRowHeightCache();
    const partial: GroupedItem = { kind: "msg", msg: { role: "assistant", text: "## Result", id: "m1" } } as GroupedItem;
    const estimate = cache.estimateRowHeight(answer, "m1", 560);
    cache.recordMeasuredHeight(partial, "m1", 560, 47);
    expect(cache.estimateRowHeight(answer, "m1", 560)).toBe(estimate);
    expect(cache.wasPaintedLive("m1")).toBe(true);
  });

  it("ignores a measurement taken at another width", () => {
    const cache = createTranscriptRowHeightCache();
    const estimate = cache.estimateRowHeight(answer, "m1", 560);
    cache.recordMeasuredHeight(answer, "m1", 400, 999);
    expect(cache.estimateRowHeight(answer, "m1", 560)).toBe(estimate);
  });
});
