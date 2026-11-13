import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ContextRing, contextRingFacts, formatTokenCount } from "../components/conversation/ContextRing";
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
    expect(facts.triggerFraction).toBeCloseTo(0.75);
    expect(facts.tooltip).toContain("Context: 3k of 1.05M tokens (<1%)");
    expect(facts.tooltip).toContain("Auto-compaction starts near 786.4k tokens (75%)");
  });

  it("marks the compaction point unknown when the server does not send it", () => {
    const facts = contextRingFacts(usage({ total: 0 }))!;
    expect(facts.label).toBe("0%");
    expect(facts.triggerFraction).toBeNull();
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

describe("ContextRing", () => {
  it("draws an arc and a compaction tick when both are known", () => {
    render(<ContextRing facts={contextRingFacts(usage({ total: 300_000, compaction_trigger_tokens: 786_432 }))} />);
    expect(screen.getByTestId("context-ring-arc")).toBeInTheDocument();
    expect(screen.getByTestId("context-ring-tick")).toBeInTheDocument();
  });

  it("draws only the track without data", () => {
    render(<ContextRing facts={null} />);
    expect(screen.queryByTestId("context-ring-arc")).toBeNull();
    expect(screen.queryByTestId("context-ring-tick")).toBeNull();
  });
});
