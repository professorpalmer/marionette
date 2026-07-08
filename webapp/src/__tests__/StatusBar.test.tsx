import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StatusBar from "../components/StatusBar";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
    workspaces: vi.fn(),
  },
}));

vi.mock("../lib/transport", () => ({
  isDesktop: false,
}));

const mockGetUsage = vi.mocked(api.getUsage);
const mockWorkspaces = vi.mocked(api.workspaces);

const statusBarProps = {
  config: null,
  jobCount: 0,
  leftOpen: true,
  rightOpen: false,
  onToggleLeft: vi.fn(),
  onToggleRight: vi.fn(),
};

describe("StatusBar usage pills", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
  });

  it("shows a single saved pill combining cache and compaction dollars", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 8000,
        est_cost_usd: 0.12,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
        tokens_cached: 2000,
        cache_savings_usd: 0.04,
        tool_output_tokens_saved: 500,
        tool_output_savings_usd: 0.02,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("$0.06 saved")).toBeInTheDocument();
    });
  });

  it("folds routing and swarm-cache savings into the green saved chip", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1000,
        est_cost_usd: 0.70,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
        cache_savings_usd: 0.01,
        routing_saved_usd: 0.40,
        cache_saved_usd_swarm: 0.05,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("$0.46 saved")).toBeInTheDocument();
    });
  });

  it("hides the saved pill when there is no cache or compaction savings", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 3000,
        est_cost_usd: 0.08,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("~$0.08")).toBeInTheDocument();
    });
    expect(screen.queryByText(/^\$[\d.]+ saved$/)).not.toBeInTheDocument();
  });

  it("renders the spend pill with formatted estimated cost", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1500,
        est_cost_usd: 0.05,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("~$0.05")).toBeInTheDocument();
    });
  });

  it("shows the boot cost cluster when tokens are zero but swarm dollars exist", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 0,
        est_cost_usd: 0.70,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("~$0.70")).toBeInTheDocument();
    });
    expect(screen.getByText("0 tok")).toBeInTheDocument();
  });

  it("never renders a session-total pill, even when the API sends one", async () => {
    // The lifetime "session ~$X" pill was removed by user request (2026-07-08):
    // the boot-scoped "since last open" figure is the useful one. The backend
    // still reports session_total (CostBreakdown and budgeting logic may use
    // it), but the status bar must ignore it.
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1500,
        est_cost_usd: 0.05,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
      },
      session_total: {
        session_id: "abc123",
        est_cost_usd: 3.174,
        input_tokens: 900000,
        output_tokens: 120000,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("~$0.05")).toBeInTheDocument();
    });
    expect(screen.queryByText("session")).not.toBeInTheDocument();
    expect(screen.queryByText("~$3.17")).not.toBeInTheDocument();
  });
});
