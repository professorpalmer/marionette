import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CostBreakdown, {
  compactionAdvicePresentation,
  type CostBreakdownData,
} from "../components/CostBreakdown";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    compactSession: vi.fn(),
  },
}));

const mockCompactSession = vi.mocked(api.compactSession);

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
  compaction_advice: {
    level: "soon",
    reasons: ["hot context above 150000 tokens on a large window"],
    needs_intervention: true,
    warning_reason: "hot context above 150000 tokens on a large window",
  },
  price_in: 3,
  price_out: 15,
};

describe("compactionAdvicePresentation", () => {
  it("maps soon to calm Long session copy", () => {
    const copy = compactionAdvicePresentation("soon");
    expect(copy.label).toBe("Long session");
    expect(copy.message).toMatch(/getting long/i);
    expect(copy.message).not.toMatch(/150000/);
  });

  it("maps now to Needs attention with actionable copy", () => {
    const copy = compactionAdvicePresentation("now");
    expect(copy.label).toBe("Needs attention");
    expect(copy.message).toMatch(/very long/i);
    expect(copy.message).toMatch(/Compact it now/i);
  });
});

describe("CostBreakdown", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCompactSession.mockResolvedValue({ ok: true, before_tokens: 1000, after_tokens: 400 });
  });

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
    expect(screen.getByText("Long session")).toBeInTheDocument();
    expect(
      screen.getByText(/This conversation is getting long/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/150000 tokens/)).not.toBeInTheDocument();
    const badge = screen.getByRole("status");
    expect(badge).toHaveAttribute(
      "title",
      "hot context above 150000 tokens on a large window",
    );
  });

  it("shows soon calm copy and keeps machine reason in the title", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.01,
          compaction_advice: {
            level: "soon",
            needs_intervention: true,
            warning_reason: "hot context above 150000 tokens on a large window",
            reasons: ["hot context above 150000 tokens on a large window"],
          },
        }}
      />,
    );

    expect(screen.getByText("Long session")).toBeInTheDocument();
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
    expect(
      screen.getByText(/Older history can be tidied/),
    ).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "hot context above 150000 tokens on a large window",
    );
  });

  it("shows now actionable copy with Compact now button", async () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.01,
          compaction_advice: {
            level: "now",
            needs_intervention: true,
            warning_reason: "hot context at 80 percent of budget",
            reasons: [],
          },
        }}
      />,
    );

    expect(screen.getByText("Needs attention")).toBeInTheDocument();
    expect(
      screen.getByText(/Compact it now or start a fresh session/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/80 percent of budget/)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveAttribute(
      "title",
      "hot context at 80 percent of budget",
    );

    const button = screen.getByRole("button", { name: "Compact now" });
    fireEvent.click(button);
    await waitFor(() => expect(mockCompactSession).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Compacted" })).toBeInTheDocument(),
    );
  });

  it("renders routing and combined prompt-cache savings when positive", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.70,
          tokens_cached: 50_000,
          cache_savings_usd: 0.02,
          routing_saved_usd: 0.40,
          cache_saved_usd_swarm: 0.05,
        }}
      />,
    );

    expect(screen.getByText("Routing saved")).toBeInTheDocument();
    const routingRow = screen.getByText("Routing saved").closest("div");
    expect(within(routingRow!).getByText("~$0.40")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache saved")).toBeInTheDocument();
    expect(screen.queryByText("Swarm cache saved")).not.toBeInTheDocument();
    const cacheRow = screen.getByText("Prompt-cache saved").closest("div");
    // 0.02 pilot + 0.05 swarm = ~$0.07
    expect(within(cacheRow!).getByText("~$0.07")).toBeInTheDocument();
    expect(screen.getByText("Tokens from cache")).toBeInTheDocument();
  });

  it("omits zero or absent savings rows", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 500,
          est_cost_usd: 0.01,
          routing_saved_usd: 0,
          cache_saved_usd_swarm: 0,
        }}
      />,
    );

    expect(screen.getByText("Estimated spend")).toBeInTheDocument();
    expect(screen.queryByText("Prompt-cache saved")).not.toBeInTheDocument();
    expect(screen.queryByText("Routing saved")).not.toBeInTheDocument();
    expect(screen.queryByText("Swarm cache saved")).not.toBeInTheDocument();
    expect(screen.queryByText("Compact tool outputs saved")).not.toBeInTheDocument();
    expect(
      screen.getByText(/Each task step is routed to the cheapest capable model/),
    ).toBeInTheDocument();
  });
});
