import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ContextRing, contextRingFacts, formatTokenCount, ringArcPath, ringPoint, ringSweep } from "../components/conversation/ContextRing";
import type { ContextUsageResponse } from "../lib/api";

const usage = (over: Partial<ContextUsageResponse>): ContextUsageResponse => ({
  total: 6600,
  limit: 1_048_576,
  categories: [],
  ...over,
});

describe("contextRingFacts", () => {
  it("returns null without a finite total and window", () => {
    expect(contextRingFacts(null)).toBeNull();
    expect(contextRingFacts(usage({ total: Number.NaN }))).toBeNull();
    expect(contextRingFacts(usage({ limit: 0 }))).toBeNull();
  });

  it("says <1% for a small real total and names the compaction point", () => {
    const facts = contextRingFacts(usage({ total: 3000, compaction_trigger_tokens: 786_432 }))!;
    expect(facts.label).toBe("<1%");
    expect(facts.tone).toBe("normal");
    expect(facts.tooltip).toContain("Context: 3k of 1.05M tokens (<1%)");
    expect(facts.tooltip).toContain("Auto-compaction starts near 786.4k tokens (75%)");
  });

  it("marks the compaction point unknown when the server does not send it", () => {
    const facts = contextRingFacts(usage({ total: 0 }))!;
    expect(facts.label).toBe("0%");
    expect(facts.tooltip).toContain("Auto-compaction point: unknown");
  });

  it("warns near the trigger and turns risk at or past it", () => {
    expect(contextRingFacts(usage({ total: 92_000, limit: 200_000, compaction_trigger_tokens: 100_000 }))!.tone).toBe("warn");
    expect(contextRingFacts(usage({ total: 100_000, limit: 200_000, compaction_trigger_tokens: 100_000 }))!.tone).toBe("risk");
    expect(contextRingFacts(usage({
      total: 10_000,
      limit: 200_000,
      compaction_advice: { level: "now", needs_intervention: true },
    }))!.tone).toBe("risk");
  });
});

describe("formatTokenCount", () => {
  it("formats compact counts", () => {
    expect(formatTokenCount(950)).toBe("950");
    expect(formatTokenCount(6600)).toBe("6.6k");
    expect(formatTokenCount(1_000_000)).toBe("1M");
    expect(formatTokenCount(1_050_000)).toBe("1.05M");
  });
});

describe("ring arc math", () => {
  const PERCENTS = [0, 0.01, 0.04, 0.25, 0.5, 0.9, 1];

  it("sweeps the exact fill at 20% and above", () => {
    for (const f of [0.2, 0.25, 0.5, 0.9, 1]) expect(ringSweep(f)).toBeCloseTo(f, 10);
    expect(ringSweep(0)).toBe(0);
    expect(ringSweep(-1)).toBe(0);
    expect(ringSweep(Number.NaN)).toBe(0);
    expect(ringSweep(2)).toBe(1);
  });

  it("keeps low values visible and different, and always increases", () => {
    expect(ringSweep(0.01)).toBeCloseTo(0.044);
    expect(ringSweep(0.04)).toBeCloseTo(0.086);
    expect(ringSweep(0.05)).toBeCloseTo(0.1);
    // At least 1px more arc on the 5px-radius ring between 1% and 4%.
    expect((ringSweep(0.04) - ringSweep(0.01)) * 2 * Math.PI * 5).toBeGreaterThan(1);
    let last = 0;
    for (let i = 1; i <= 1000; i += 1) {
      const next = ringSweep(i / 1000);
      expect(next).toBeGreaterThan(last);
      last = next;
    }
  });

  it("puts 0 at 12 o'clock and turns clockwise", () => {
    expect(ringPoint(0)).toEqual({ x: 7, y: 2 });
    expect(ringPoint(0.25)).toEqual({ x: 12, y: 7 });
    expect(ringPoint(0.5)).toEqual({ x: 7, y: 12 });
    expect(ringPoint(0.75)).toEqual({ x: 2, y: 7 });
  });

  it("draws each arc from 12 o'clock with the clockwise flag", () => {
    expect(ringArcPath(0)).toBeNull();
    expect(ringArcPath(1)).toBeNull();
    expect(ringArcPath(0.25)).toBe("M 7 2 A 5 5 0 0 1 12 7");
    expect(ringArcPath(0.5)).toBe("M 7 2 A 5 5 0 0 1 7 12");
    expect(ringArcPath(0.9)).toMatch(/^M 7 2 A 5 5 0 1 1 /);
    for (const f of PERCENTS.filter((p) => p > 0 && p < 1)) {
      expect(ringArcPath(ringSweep(f))).toMatch(/^M 7 2 A 5 5 0 [01] 1 /);
    }
  });
});

describe("ContextRing", () => {
  const ring = (total: number) => contextRingFacts(usage({ total, limit: 100_000, compaction_trigger_tokens: 80_000 }));

  it("draws a track and an arc path, with no compaction tick", () => {
    render(<ContextRing facts={ring(25_000)} />);
    expect(screen.getByTestId("context-ring-arc").getAttribute("d")).toBe("M 7 2 A 5 5 0 0 1 12 7");
    expect(screen.queryByTestId("context-ring-tick")).toBeNull();
  });

  it("draws a full circle at 100% and no arc at 0%", () => {
    const { unmount } = render(<ContextRing facts={ring(100_000)} />);
    expect(screen.getByTestId("context-ring-arc").tagName.toLowerCase()).toBe("circle");
    unmount();
    render(<ContextRing facts={ring(0)} />);
    expect(screen.queryByTestId("context-ring-arc")).toBeNull();
  });

  it("draws only the track without data", () => {
    render(<ContextRing facts={null} />);
    expect(screen.queryByTestId("context-ring-arc")).toBeNull();
  });
});
