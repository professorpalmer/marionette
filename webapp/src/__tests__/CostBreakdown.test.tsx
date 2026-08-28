import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CostBreakdown, {
  cacheHitDisplay,
  delegationSavingsCredited,
  formatCacheHitPercent,
  listPriceValueHeading,
  listPriceValueTotal,
  listPriceValueWeakestBasis,
  routingSavingsCredited,
  spendIsEstimated,
  type CostBreakdownData,
} from "../components/CostBreakdown";

const baseData: CostBreakdownData = {
  tokens_used: 12_000,
  est_cost_usd: 0.12,
  cache_savings_usd: 0.04,
  cache_savings_basis: "catalog",
  tool_output_tokens_saved: 736_100,
  tool_output_savings_usd: 0.02,
};

describe("CostBreakdown receipt", () => {
  it("shows one app-run receipt and each savings mechanism once", () => {
    render(<CostBreakdown data={baseData} />);

    expect(screen.getByText("Spend")).toBeTruthy();
    expect(screen.getByText("~$0.12")).toBeTruthy();
    expect(screen.getByText("Without savings")).toBeTruthy();
    expect(screen.getByText("~$0.18")).toBeTruthy();
    expect(screen.getByText("Estimated savings")).toBeTruthy();
    expect(screen.getByText("~$0.06")).toBeTruthy();
    expect(screen.getByText("Less spent")).toBeTruthy();
    expect(screen.getByText("33.3%")).toBeTruthy();
    expect(screen.getByText("Why you saved")).toBeTruthy();
    expect(screen.getByText("Prompt-cache value")).toBeTruthy();
    const compact = screen.getByText("Compact tool outputs").closest("div");
    expect(within(compact as HTMLElement).getByText(/736.1k tok · ~\$0.02/)).toBeTruthy();
    expect(screen.queryByText(/List-price value/)).toBeNull();
  });

  it("keeps context diagnostics and compaction controls out of Economics", () => {
    render(<CostBreakdown data={baseData} />);

    expect(screen.queryByText("Context health")).toBeNull();
    expect(screen.queryByText("Memory layers")).toBeNull();
    expect(screen.queryByText("Offloaded outputs")).toBeNull();
    expect(screen.queryByRole("button", { name: "Compact now" })).toBeNull();
  });

  it("shows exact zero without inventing savings", () => {
    render(<CostBreakdown data={{ tokens_used: 0, est_cost_usd: 0 }} />);

    expect(screen.getByText("Spend").parentElement?.textContent).toContain("~$0.00");
    expect(screen.getByText("Without savings").parentElement?.textContent).toContain("~$0.00");
    expect(screen.getByText("Estimated savings").parentElement?.textContent).toContain("~$0.00");
    expect(screen.getByText("Less spent").parentElement?.textContent).toContain("—");
    expect(screen.queryByText("Why you saved")).toBeNull();
  });

  it("shows model-selection and routing decision values only when supported", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1_000,
          est_cost_usd: 0.70,
          cache_savings_gross_usd: 0.02,
          cache_savings_basis: "catalog",
          delegation_saved_usd: 0.40,
          delegation_savings_basis: "actual_usage",
          routing_saved_usd: 0.02,
          routing_savings_basis: "actual_usage",
        }}
      />,
    );

    expect(screen.getByText("Model selection value")).toBeTruthy();
    expect(screen.getByText("Routing decision value")).toBeTruthy();
    expect(screen.getByText("Prompt-cache value")).toBeTruthy();
  });

  it("refuses an unknown routing basis", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1_000,
          est_cost_usd: 0.10,
          routing_saved_usd: 1.25,
          routing_savings_basis: "unknown",
        }}
      />,
    );

    expect(screen.getByText("unknown basis")).toBeTruthy();
    expect(screen.queryByText("Why you saved")).toBeNull();
  });
});

describe("CostBreakdown accounting helpers", () => {
  it("credits only supported routing and delegation bases", () => {
    expect(routingSavingsCredited("actual_usage", 1.2)).toBe(1.2);
    expect(routingSavingsCredited("estimated", 1.2)).toBe(1.2);
    expect(routingSavingsCredited("unknown", 1.2)).toBe(0);
    expect(delegationSavingsCredited("actual_usage", 1.2)).toBe(1.2);
    expect(delegationSavingsCredited("estimated", 1.2)).toBe(0);
    expect(delegationSavingsCredited("unknown", 1.2)).toBe(0);
  });

  it("keeps additive value mechanisms separate and labels the weakest basis", () => {
    const data: CostBreakdownData = {
      tokens_used: 1_000,
      est_cost_usd: 0.10,
      cache_savings_gross_usd: 2.50,
      cache_savings_basis: "catalog",
      cache_saved_usd_swarm: 0.90,
      swarm_cache_savings_basis: "actual_usage",
      delegation_saved_usd: 0.42,
      delegation_savings_basis: "actual_usage",
      routing_saved_usd: 9,
      routing_savings_basis: "estimated",
      tool_output_savings_usd: 0.76,
    };

    expect(listPriceValueTotal(data)).toBeCloseTo(4.58, 8);
    expect(listPriceValueWeakestBasis(data)).toBe("estimated");
    expect(listPriceValueHeading("estimated")).toBe("List-price value (est.)");
    expect(listPriceValueHeading("partial")).toBe("List-price value (partial)");
  });

  it("does not replace measured-zero delegation with a routing estimate", () => {
    const data: CostBreakdownData = {
      tokens_used: 1_000,
      est_cost_usd: 0.10,
      delegation_saved_usd: 0,
      delegation_savings_basis: "actual_usage",
      routing_saved_usd: 5,
      routing_savings_basis: "estimated",
    };

    expect(listPriceValueTotal(data)).toBe(0);
  });

  it("classifies spend estimates conservatively", () => {
    expect(spendIsEstimated({ cost_source: "estimated", price_source: "default" })).toBe(true);
    expect(spendIsEstimated({ cost_source: "provider", estimated: false })).toBe(false);
    expect(spendIsEstimated({ cost_source: "mixed", estimated: true })).toBe(true);
  });
});

describe("cache hit helpers", () => {
  it("formats valid warm ratios and rejects unknown or invalid values", () => {
    expect(formatCacheHitPercent(0.90)).toBe("90%");
    expect(formatCacheHitPercent(0.995)).toBe("100%");
    expect(formatCacheHitPercent(null)).toBeNull();
    expect(formatCacheHitPercent(0)).toBeNull();
    expect(formatCacheHitPercent(1.01)).toBeNull();
  });

  it("prefers the combined prompt ratio, then a priced lane with reads", () => {
    expect(cacheHitDisplay({
      prompt_cache_hit_ratio: 0.92,
      prompt_cache_read_tokens: 92_000,
      pilot_cache_hit_ratio: 0.70,
      pilot_cache_read_tokens: 7_000,
    }).percent).toBe("92%");

    expect(cacheHitDisplay({
      prompt_cache_hit_ratio: null,
      prompt_cache_read_tokens: 0,
      pilot_cache_hit_ratio: 0.75,
      pilot_cache_read_tokens: 7_500,
    }).percent).toBe("75%");

    expect(cacheHitDisplay({
      prompt_cache_hit_ratio: 0,
      prompt_cache_read_tokens: 0,
      tokens_cached: 0,
    }).percent).toBeNull();
  });
});
