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
});
