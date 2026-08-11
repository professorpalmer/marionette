import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StatusBar, {
  deriveFooterRuntimeStatus,
  sessionGoalForChip,
} from "../components/StatusBar";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getUsage: vi.fn(),
    workspaces: vi.fn(),
    getSessionState: vi.fn(),
    sessions: vi.fn(),
    pauseSessionGoal: vi.fn(),
    resumeSessionGoal: vi.fn(),
    completeSessionGoal: vi.fn(),
    clearSessionGoal: vi.fn(),
    setSessionGoal: vi.fn(),
  },
}));

vi.mock("../lib/transport", () => ({
  isDesktop: () => false,
}));

const mockGetUsage = vi.mocked(api.getUsage);
const mockWorkspaces = vi.mocked(api.workspaces);
const mockGetSessionState = vi.mocked(api.getSessionState);
const mockSessions = vi.mocked(api.sessions);
const mockPauseSessionGoal = vi.mocked(api.pauseSessionGoal);
const mockResumeSessionGoal = vi.mocked(api.resumeSessionGoal);
const mockCompleteSessionGoal = vi.mocked(api.completeSessionGoal);
const mockClearSessionGoal = vi.mocked(api.clearSessionGoal);

const statusBarProps = {
  config: null,
  leftOpen: true,
  rightOpen: false,
  onToggleLeft: vi.fn(),
  onToggleRight: vi.fn(),
};

describe("sessionGoalForChip", () => {
  it("returns null when goal is absent, cleared, or empty", () => {
    expect(sessionGoalForChip(undefined)).toBeNull();
    expect(sessionGoalForChip(null)).toBeNull();
    expect(sessionGoalForChip({ text: "", status: "active" })).toBeNull();
    expect(sessionGoalForChip({ text: "Ship it", status: "cleared" })).toBeNull();
  });

  it("returns a normalized goal when text is present and not cleared", () => {
    expect(sessionGoalForChip({ text: "  Ship it  ", status: "Active" })).toEqual({
      text: "Ship it",
      status: "active",
    });
  });
});

describe("deriveFooterRuntimeStatus", () => {
  it("returns ready when idle with no running runner", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle" },
    })).toBe("ready");
  });

  it("returns thinking when the active session runner is running", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "running" },
    })).toBe("thinking");
  });

  it("returns ready when only a background session runner is running", () => {
    expect(deriveFooterRuntimeStatus({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle", "sess-2": "running" },
    })).toBe("ready");
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

  it("marks the saved chip when swarm cache pricing is partial", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1000,
        est_cost_usd: 0.10,
        driver: "anthropic:claude-sonnet",
        price_in: 3,
        price_out: 15,
        tokens_cached: 20_000,
        cache_saved_usd_swarm: 0.05,
        swarm_cache_savings_basis: "unknown",
        swarm_cache_unpriced_tokens: 8_000,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    const saved = await screen.findByText("~$0.05 saved");
    expect(saved.closest("span")).toHaveAttribute(
      "title",
      expect.stringMatching(/8k tokens unpriced/i),
    );
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
      active_view_id: "sess-1",
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
      active_view_id: "sess-1",
      runners: { "sess-1": "running" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("thinking")).toBeInTheDocument();
    });
  });

  it("shows ready when only a background session runner is running", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle", "sess-2": "running" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("ready")).toBeInTheDocument();
    });
  });

  it("shows busy when swarms are pending", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "awaiting_swarm",
      pending_swarms: true,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("busy")).toBeInTheDocument();
    });
  });
});

describe("StatusBar prompt-cache hit display", () => {
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

  it("shows honest 90%+ prompt-cache hit from warm usage ratios", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1_500_000,
        est_cost_usd: 0.50,
        driver: "xai:grok",
        price_in: 3,
        price_out: 15,
        tokens_cached: 415_700,
        prompt_cache_hit_ratio: 0.968,
        prompt_input_tokens: 429_000,
        prompt_cache_read_tokens: 415_700,
        cache_savings_usd: 0.10,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    const chip = await screen.findByTitle(/cache-read ÷ prompt-input/i);
    expect(chip).toHaveTextContent(/97% prompt cache/i);
  });

  it("does not invent a cache percent when ratio is unknown", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 1_500_000,
        est_cost_usd: 0.50,
        driver: "xai:grok",
        price_in: 3,
        price_out: 15,
        tokens_cached: 415_700,
        prompt_cache_hit_ratio: null,
        pilot_cache_hit_ratio: null,
        swarm_cache_hit_ratio: null,
        cache_savings_usd: 0.10,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText(/\$0\.10 saved/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/%.*cache/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/prompt cache/i)).not.toBeInTheDocument();
  });

  it("does not render a green 0% cache chip when cache reads are zero", async () => {
    mockGetUsage.mockResolvedValue({
      session: {
        tokens_used: 10_000,
        est_cost_usd: 0.05,
        driver: "xai:grok",
        price_in: 3,
        price_out: 15,
        tokens_cached: 0,
        prompt_cache_read_tokens: 0,
        prompt_cache_hit_ratio: 0,
        pilot_cache_hit_ratio: 0,
        swarm_cache_hit_ratio: 0,
        cache_savings_usd: 0,
      },
      jobs: [],
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText(/10k tok/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/0% .*cache/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/prompt cache/i)).not.toBeInTheDocument();
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

describe("StatusBar session GOAL chip", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
    mockGetUsage.mockResolvedValue({ session: emptyUsageSession, jobs: [] });
    mockSessions.mockResolvedValue([]);
    mockPauseSessionGoal.mockResolvedValue({
      ok: true,
      goal: { text: "Ship StatusBar GOAL chip", status: "paused" },
    });
    mockResumeSessionGoal.mockResolvedValue({
      ok: true,
      goal: { text: "Ship StatusBar GOAL chip", status: "active" },
    });
    mockCompleteSessionGoal.mockResolvedValue({
      ok: true,
      goal: { text: "Ship StatusBar GOAL chip", status: "complete" },
    });
    mockClearSessionGoal.mockResolvedValue({
      ok: true,
      goal: { text: "", status: "cleared" },
    });
  });

  it("hides the GOAL chip when session state has no goal", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: {},
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(mockGetSessionState).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("session-goal-chip")).not.toBeInTheDocument();
    expect(screen.queryByText("GOAL")).not.toBeInTheDocument();
  });

  it("hides the GOAL chip when goal status is cleared", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: {},
      goal: { text: "Old objective", status: "cleared" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(mockGetSessionState).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("session-goal-chip")).not.toBeInTheDocument();
  });

  it("shows the GOAL chip when an active goal is present", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: {},
      goal: { text: "Ship StatusBar GOAL chip", status: "active" },
    });

    render(<StatusBar {...statusBarProps} />);

    const chip = await screen.findByTestId("session-goal-chip");
    expect(chip).toHaveTextContent("GOAL");
    expect(chip).toHaveTextContent("Ship StatusBar GOAL chip");
    expect(screen.getByLabelText("Pause session GOAL")).toBeInTheDocument();
    expect(screen.getByLabelText("Complete session GOAL")).toBeInTheDocument();
    expect(screen.getByLabelText("Clear session GOAL")).toBeInTheDocument();
  });

  it("calls pause / complete / clear via api.*SessionGoal", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      runners: {},
      goal: { text: "Ship StatusBar GOAL chip", status: "active" },
    });

    render(<StatusBar {...statusBarProps} />);
    await screen.findByTestId("session-goal-chip");

    fireEvent.click(screen.getByLabelText("Pause session GOAL"));
    await waitFor(() => {
      expect(mockPauseSessionGoal).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText("paused")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Resume session GOAL"));
    await waitFor(() => {
      expect(mockResumeSessionGoal).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByLabelText("Complete session GOAL"));
    await waitFor(() => {
      expect(mockCompleteSessionGoal).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText("done")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Clear session GOAL"));
    await waitFor(() => {
      expect(mockClearSessionGoal).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.queryByTestId("session-goal-chip")).not.toBeInTheDocument();
    });
  });
});

describe("StatusBar runtime stale toast", () => {
  const runtimeNote =
    "Puppetmaster is at 1.20.10 but this Marionette needs 1.21.13 -- offline. Reconnect and update to finish.";

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

  afterEach(() => {
    delete (window as any).harnessIPC;
  });

  it("surfaces runtimeNote once as a toast without showing the update pill", async () => {
    const check = vi
      .fn()
      .mockResolvedValue({ runtimeStale: true, runtimeNote, available: false });
    (window as any).harnessIPC = {
      updates: { check, onAvailable: null, onProgress: vi.fn(() => () => {}) },
    };

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => expect(check).toHaveBeenCalled());
    await waitFor(() => {
      expect(screen.getByText(runtimeNote)).toBeInTheDocument();
    });
    expect(screen.queryByText(/^update/)).not.toBeInTheDocument();
  });

  it("does not toast when runtimeStale is absent", async () => {
    const check = vi.fn().mockResolvedValue({ available: false });
    (window as any).harnessIPC = {
      updates: { check, onAvailable: null, onProgress: vi.fn(() => () => {}) },
    };

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(check).toHaveBeenCalled();
    });
    expect(screen.queryByText(/Puppetmaster is at/i)).not.toBeInTheDocument();
  });
});

describe("StatusBar update progress mirror", () => {
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

  afterEach(() => {
    delete (window as any).harnessIPC;
  });

  it("clears apply chrome when a terminal idle progress event arrives", async () => {
    let progressListener: ((payload: { stage: string; message?: string; percent?: number | null }) => void) | null = null;
    (window as any).harnessIPC = {
      updates: {
        check: vi.fn().mockResolvedValue({ available: false }),
        onAvailable: null,
        onProgress: vi.fn((listener) => {
          progressListener = listener;
          return () => {};
        }),
      },
    };

    render(<StatusBar {...statusBarProps} />);

    act(() => {
      window.dispatchEvent(new Event("harness-update-committing"));
    });
    expect(screen.getByText("Preparing update")).toBeInTheDocument();

    await waitFor(() => expect(progressListener).not.toBeNull());
    act(() => {
      progressListener?.({ stage: "idle" });
    });

    await waitFor(() => {
      expect(screen.queryByText("Preparing update")).not.toBeInTheDocument();
    });
  });
});
