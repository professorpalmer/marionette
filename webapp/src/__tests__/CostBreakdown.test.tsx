import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CostBreakdown, {
  compactionAdvicePresentation,
  listPriceValueTotal,
  spendIsEstimated,
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
    mockCompactSession.mockResolvedValue({
      ok: true,
      compacted: true,
      before_tokens: 1000,
      after_tokens: 400,
    });
  });

  it("renders the session cost fields it is given", () => {
    render(<CostBreakdown data={baseData} />);

    expect(screen.getByText("Session cost")).toBeInTheDocument();
    expect(screen.getByText("Estimated spend")).toBeInTheDocument();
    expect(screen.getByText("~$0.04")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache value")).toBeInTheDocument();
    const cacheRow = screen.getByText("Prompt-cache value").closest("div");
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

  it("reconciles the visible additive value total using gross cache value", () => {
    const data: CostBreakdownData = {
      ...baseData,
      cache_savings_usd: 0.1,
      cache_savings_gross_usd: 1.14,
      cache_saved_usd_swarm: 0.22,
      delegation_saved_usd: 2.46,
      delegation_savings_basis: "actual_usage",
      routing_saved_usd: 0.02,
      tool_output_savings_usd: 0.76,
    };
    expect(listPriceValueTotal(data)).toBeCloseTo(4.58, 8);
    render(<CostBreakdown data={data} />);
    const totalRow = screen.getByText("Total value saved").closest("div");
    expect(within(totalRow!).getByText("~$4.58")).toBeInTheDocument();
    expect(totalRow).toHaveAttribute(
      "title",
      expect.stringMatching(/not a cash refund/i),
    );
  });

  it("does not replace measured zero delegation value with a routing estimate", () => {
    const data: CostBreakdownData = {
      ...baseData,
      cache_savings_usd: 0,
      tool_output_savings_usd: 0,
      delegation_saved_usd: 0,
      delegation_savings_basis: "actual_usage",
      routing_saved_usd: 1.25,
      routing_savings_basis: "estimated",
    };

    expect(listPriceValueTotal(data)).toBe(0);
    render(<CostBreakdown data={data} />);
    expect(screen.queryByText("Model selection value")).not.toBeInTheDocument();
    expect(screen.getByText("Routing decision value")).toBeInTheDocument();
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

  const nowAdviceData: CostBreakdownData = {
    tokens_used: 1000,
    est_cost_usd: 0.01,
    compaction_advice: {
      level: "now",
      needs_intervention: true,
      warning_reason: "hot context at 80 percent of budget",
      reasons: [],
    },
  };

  it("shows Compacted and refreshes usage only after a true reduction", async () => {
    const refreshes: Event[] = [];
    const onRefresh = (e: Event) => refreshes.push(e);
    window.addEventListener("harness-usage-refresh", onRefresh);
    try {
      render(<CostBreakdown data={nowAdviceData} />);
      fireEvent.click(screen.getByRole("button", { name: "Compact now" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Compacted" })).toBeInTheDocument(),
      );
      expect(refreshes).toHaveLength(1);
    } finally {
      window.removeEventListener("harness-usage-refresh", onRefresh);
    }
  });

  it("shows Retry compact when the backend reports a no-op", async () => {
    mockCompactSession.mockResolvedValue({
      ok: false,
      compacted: false,
      before_tokens: 1000,
      after_tokens: 1000,
      error: "no compaction occurred",
    });
    const refreshes: Event[] = [];
    const onRefresh = (e: Event) => refreshes.push(e);
    window.addEventListener("harness-usage-refresh", onRefresh);
    try {
      render(<CostBreakdown data={nowAdviceData} />);
      fireEvent.click(screen.getByRole("button", { name: "Compact now" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Retry compact" })).toBeInTheDocument(),
      );
      expect(screen.queryByRole("button", { name: "Compacted" })).not.toBeInTheDocument();
      expect(refreshes).toHaveLength(0);
    } finally {
      window.removeEventListener("harness-usage-refresh", onRefresh);
    }
  });

  it("shows calm Already compact copy for no_compactable_history", async () => {
    mockCompactSession.mockRejectedValue(
      Object.assign(new Error("Recent turn is already compact"), {
        ok: false,
        compacted: false,
        reason: "no_compactable_history",
        status: 409,
      }),
    );
    render(<CostBreakdown data={nowAdviceData} />);
    fireEvent.click(screen.getByRole("button", { name: "Compact now" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Already compact" })).toBeInTheDocument(),
    );
    expect(screen.getByText("Recent turn is already compact.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry compact" })).not.toBeInTheDocument();
  });

  it("shows Retry compact when the request itself fails", async () => {
    mockCompactSession.mockRejectedValue(new Error("/api/session/compact -> 409"));
    render(<CostBreakdown data={nowAdviceData} />);
    fireEvent.click(screen.getByRole("button", { name: "Compact now" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry compact" })).toBeInTheDocument(),
    );
  });

  it("falls back to token delta for legacy responses without a compacted flag", async () => {
    mockCompactSession.mockResolvedValue({ ok: true, before_tokens: 1000, after_tokens: 1000 });
    render(<CostBreakdown data={nowAdviceData} />);
    fireEvent.click(screen.getByRole("button", { name: "Compact now" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry compact" })).toBeInTheDocument(),
    );
  });

  it("renders model selection and routing decision rows when both differ", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.70,
          tokens_cached: 50_000,
          cache_savings_usd: 0.02,
          cache_savings_gross_usd: 0.02,
          delegation_saved_usd: 0.40,
          delegation_savings_basis: "actual_usage",
          routing_saved_usd: 0.02,
          routing_savings_basis: "actual_usage",
          cache_saved_usd_swarm: 0.05,
        }}
      />,
    );

    expect(screen.getByText("Model selection value")).toBeInTheDocument();
    const modelRow = screen.getByText("Model selection value").closest("div");
    expect(within(modelRow!).getByText("~$0.40")).toBeInTheDocument();
    expect(screen.getByText("Routing decision value")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache value")).toBeInTheDocument();
    expect(screen.queryByText("Routing value")).not.toBeInTheDocument();
    expect(screen.queryByText("Swarm cache saved")).not.toBeInTheDocument();
    expect(screen.queryByText(/\(capped\)/)).not.toBeInTheDocument();
    const cacheRow = screen.getByText("Prompt-cache value").closest("div");
    // 0.02 pilot gross + 0.05 swarm = ~$0.07
    expect(within(cacheRow!).getByText("~$0.07")).toBeInTheDocument();
    expect(screen.getByText("Tokens from cache")).toBeInTheDocument();
    expect(screen.getByText(/model selection value vs frontier-equivalent list price/)).toBeInTheDocument();
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
    expect(screen.queryByText("Prompt-cache value")).not.toBeInTheDocument();
    expect(screen.queryByText("Model selection value")).not.toBeInTheDocument();
    expect(screen.queryByText("Routing value")).not.toBeInTheDocument();
    expect(screen.queryByText("Swarm cache saved")).not.toBeInTheDocument();
    expect(screen.queryByText("Compact tool outputs saved")).not.toBeInTheDocument();
    expect(
      screen.getByText(/Each task step is routed to the cheapest capable model/),
    ).toBeInTheDocument();
  });

  it("marks default-rate spend as estimated and labels it", () => {
    expect(spendIsEstimated({ cost_source: "estimated", price_source: "default" })).toBe(true);
    expect(spendIsEstimated({ cost_source: "provider", estimated: false })).toBe(false);
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.05,
          cost_source: "estimated",
          price_source: "default",
          estimated: true,
        }}
      />,
    );
    expect(screen.getByText("Estimated spend (default rates)")).toBeInTheDocument();
    expect(screen.getByText("~$0.05")).toBeInTheDocument();
  });

  it("prefers uncapped gross cache value over reconciled/capped", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.10,
          cost_source: "provider",
          estimated: false,
          tokens_cached: 50_000,
          cache_savings_usd: 0.05,
          cache_savings_gross_usd: 0.90,
          cache_savings_basis: "capped",
        }}
      />,
    );
    expect(screen.getByText("Billed spend")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache value")).toBeInTheDocument();
    expect(screen.queryByText(/\(capped\)/)).not.toBeInTheDocument();
    const cacheRow = screen.getByText("Prompt-cache value").closest("div");
    expect(within(cacheRow!).getByText("~$0.90")).toBeInTheDocument();
  });

  it("never renders capped label for prompt-cache value", () => {
    render(
      <CostBreakdown
        data={{
          tokens_used: 1000,
          est_cost_usd: 0.10,
          cost_source: "provider",
          estimated: false,
          tokens_cached: 50_000,
          cache_savings_usd: 0.05,
          cache_savings_basis: "capped",
        }}
      />,
    );
    expect(screen.queryByText(/\(capped\)/)).not.toBeInTheDocument();
    expect(screen.getByText("Prompt-cache value")).toBeInTheDocument();
  });
});
