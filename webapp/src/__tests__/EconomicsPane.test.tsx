import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import EconomicsPane from "../components/EconomicsPane";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
    compactSession: vi.fn(),
  },
}));

vi.mock("../lib/usePolling", () => ({
  usePolling: (fn: () => unknown) => {
    void fn();
  },
}));

const mockGetUsage = vi.mocked(api.getUsage);

describe("EconomicsPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows spend and list-price value from getUsage", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 8000,
        est_cost_usd: 0.12,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
        cache_savings_usd: 0.04,
        tool_output_savings_usd: 0.02,
      },
      jobs: [],
    });

    render(<EconomicsPane />);

    expect(await screen.findByText("This app run")).toBeInTheDocument();
    expect(screen.getByText("Estimated spend")).toBeInTheDocument();
    expect(screen.getByText("~$0.12")).toBeInTheDocument();
    expect(screen.getByText("Prompt-cache value")).toBeInTheDocument();
    expect(screen.getByText("List-price value (est.)")).toBeInTheDocument();
    expect(screen.queryByText("Session cost")).not.toBeInTheDocument();
  });

  it("refetches on harness-usage-refresh", async () => {
    mockGetUsage
      .mockResolvedValueOnce({
        session: {
          tokens_used: 1000,
          est_cost_usd: 0.05,
          driver: "anthropic:claude-sonnet",
          price_in: 3,
          price_out: 15,
        },
        jobs: [],
      })
      .mockResolvedValue({
        session: {
          tokens_used: 4000,
          est_cost_usd: 0.22,
          driver: "anthropic:claude-sonnet",
          price_in: 3,
          price_out: 15,
          cache_savings_usd: 0.10,
        },
        jobs: [],
      });

    render(<EconomicsPane />);
    expect(await screen.findByText("~$0.05")).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event("harness-usage-refresh"));
    });

    await waitFor(() => {
      expect(screen.getByText("~$0.22")).toBeInTheDocument();
    });
    expect(screen.getByText("List-price value")).toBeInTheDocument();
    expect(mockGetUsage.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
