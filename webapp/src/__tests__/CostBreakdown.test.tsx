import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import CostBreakdown, { type CostBreakdownData } from "../components/CostBreakdown";

const baseData: CostBreakdownData = {
  tokens_used: 12000,
  est_cost_usd: 0.042,
  tokens_cached: 4000,
  cache_savings_usd: 0.018,
  tool_output_tokens_saved: 900,
  tool_output_savings_usd: 0.006,
  history_compactions: 2,
  history_tokens_saved: 1500,
  spill_count: 1,
  spill_chars: 3200,
  evals_recorded: 5,
  evals_failed: 1,
  memory_layers: { L1: { bytes: 2048 } },
  compaction_advice: { level: "soon", reasons: ["L1 nearing limit"] },
  price_in: 3,
  price_out: 15,
};

describe("CostBreakdown", () => {
  it("renders the session cost fields it is given", () => {
    render(<CostBreakdown data={baseData} />);

    expect(screen.getByText("Session cost")).toBeInTheDocument();
    expect(screen.getByText("Estimated spend")).toBeInTheDocument();
    expect(screen.getByText("~$0.04")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache saved")).toBeInTheDocument();
    const cacheRow = screen.getByText("Prompt-cache saved").closest("div");
    expect(within(cacheRow!).getByText("~$0.02")).toBeInTheDocument();
    expect(screen.getByText("Tokens from cache")).toBeInTheDocument();
    expect(screen.getByText("4k")).toBeInTheDocument();
    expect(screen.getByText("Compact tool outputs saved")).toBeInTheDocument();
    expect(screen.getByText("History compaction")).toBeInTheDocument();
    expect(screen.getByText("Offloaded outputs")).toBeInTheDocument();
    expect(screen.getByText("Checks recorded")).toBeInTheDocument();
    expect(screen.getByText("Memory layers")).toBeInTheDocument();
    expect(screen.getByText("Compaction advice")).toBeInTheDocument();
    expect(screen.getByText(/soon — L1 nearing limit/)).toBeInTheDocument();
  });

  it("omits zero or absent savings rows", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 500,
          est_cost_usd: 0.01,
        }}
      />,
    );

    expect(screen.getByText("Estimated spend")).toBeInTheDocument();
    expect(screen.queryByText("Prompt-cache saved")).not.toBeInTheDocument();
    expect(screen.queryByText("Compact tool outputs saved")).not.toBeInTheDocument();
    expect(
      screen.getByText(/Each task step is routed to the cheapest capable model/),
    ).toBeInTheDocument();
  });
});
