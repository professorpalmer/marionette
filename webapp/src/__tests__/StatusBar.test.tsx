import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StatusBar, { deriveFooterRuntimeStatus } from "../components/StatusBar";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
    workspaces: vi.fn(),
    getSessionState: vi.fn(),
    sessions: vi.fn(),
  },
}));

vi.mock("../lib/transport", () => ({
  isDesktop: () => false,
}));

const mockGetUsage = vi.mocked(api.getUsage);
const mockWorkspaces = vi.mocked(api.workspaces);
const mockGetSessionState = vi.mocked(api.getSessionState);
const mockSessions = vi.mocked(api.sessions);

const statusBarProps = {
  config: null,
  leftOpen: true,
  rightOpen: false,
  onToggleLeft: vi.fn(),
  onToggleRight: vi.fn(),
};

describe("deriveFooterRuntimeStatus", () => {
  it("returns ready when idle with no running runner", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: false,
      runners: { "sess-1": "idle" },
    })).toBe("ready");
  });

  it("returns thinking when the active session runner is running", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: false,
      runners: { "sess-1": "running" },
    })).toBe("thinking");
  });

  it("returns thinking when a background session runner is running", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: false,
      runners: { "sess-1": "idle", "sess-2": "running" },
    })).toBe("thinking");
  });

  it("returns thinking when pilot state is thinking", () => {
    expect(deriveFooterRuntimeStatus({
      state: "thinking",
      pending_swarms: false,
    })).toBe("thinking");
  });

  it("returns busy when swarms are pending", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: true,
    })).toBe("busy");
  });
});

describe("StatusBar usage pills", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: {},
    });
    mockSessions.mockResolvedValue([]);
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

  it("labels process-wide spend to distinguish from Swarm pane session spend", async () => {
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
      expect(screen.getByText("process")).toBeInTheDocument();
    });
    expect(screen.getByTitle(/Process-wide token usage/i)).toBeInTheDocument();
    expect(screen.getByTitle(/Swarm pane shows per-repo session spend/i)).toBeInTheDocument();
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

  it("keeps last-good spend when a later poll returns all zeros", async () => {
    mockGetUsage
      .mockResolvedValueOnce({
        session: {
          tokens_used: 1500,
          est_cost_usd: 0.05,
          driver: "anthropic:claude-sonnet",
          price_in: 3,
          price_out: 15,
        },
        jobs: [],
      })
      .mockResolvedValue({
        session: {
          tokens_used: 0,
          est_cost_usd: 0,
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

    // Workspace/project events re-trigger usage fetch (replaces the old
    // jobCount bump that also drove a confusing footer job total).
    window.dispatchEvent(new Event("harness-config-changed"));

    await waitFor(() => {
      expect(mockGetUsage.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    // Cluster must still show the prior non-zero spend.
    expect(screen.getByText("~$0.05")).toBeInTheDocument();
    expect(screen.getByText("1.5k tok")).toBeInTheDocument();
  });
});

const emptyUsageSession = {
  tokens_used: 0,
  est_cost_usd: 0,
  driver: "",
  price_in: 0,
  price_out: 0,
};

describe("StatusBar runtime status", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
    mockGetUsage.mockResolvedValue({ session: emptyUsageSession, jobs: [] });
    mockSessions.mockResolvedValue([{ id: "sess-1", title: "Test", created: 0, active: true }]);
  });

  it("shows ready when the active session runner is idle", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: { "sess-1": "idle" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("ready")).toBeInTheDocument();
    });
  });

  it("shows thinking when the active session runner is running", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: { "sess-1": "running" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("thinking")).toBeInTheDocument();
    });
  });

  it("shows thinking when a background session runner is running", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: { "sess-1": "idle", "sess-2": "running" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("thinking")).toBeInTheDocument();
    });
  });

  it("shows busy when swarms are pending", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "awaiting_swarm",
      pending_swarms: true,
      runners: { "sess-1": "idle" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("busy")).toBeInTheDocument();
    });
  });
});

describe("StatusBar panel toggle shortcuts", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
    mockGetUsage.mockResolvedValue({ session: emptyUsageSession, jobs: [] });
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: {},
    });
    mockSessions.mockResolvedValue([]);
  });

  it("uses Ctrl/Cmd titles for panel toggle buttons", () => {
    render(<StatusBar {...statusBarProps} />);

    expect(screen.getByTitle("Toggle sessions panel (Ctrl/Cmd+B)")).toBeInTheDocument();
    expect(screen.getByTitle("Toggle right panel (Ctrl/Cmd+J)")).toBeInTheDocument();
  });
});
