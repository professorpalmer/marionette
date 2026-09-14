import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import CostBreakdown from "../components/CostBreakdown";

it("shows plan marginal spend separately from nominal plus cache value", () => {
  render(<CostBreakdown data={{ tokens_used: 100, est_cost_usd: 0,
    cost_source: "plan_estimated", nominal_cost_usd: 2,
    cache_savings_gross_usd: 3, cache_savings_basis: "catalog",
    list_price_complete: false }} />);
  expect(screen.getByText("Included in plan")).toBeInTheDocument();
  expect(screen.getByText("$0 marginal spend")).toBeInTheDocument();
  expect(screen.getByText("At list price (partial)")).toBeInTheDocument();
  expect(screen.getAllByText("~$5.00")).toHaveLength(2);
  expect(screen.getByText("Estimated savings")).toBeInTheDocument();
  expect(screen.getByText("100.0%")).toBeInTheDocument();
});
