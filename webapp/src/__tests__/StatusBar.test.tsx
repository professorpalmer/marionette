import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StatusBar, {
  deriveFooterRuntimeStatus,
  footerRuntimeStatusLabel,
  sessionGoalForChip,
} from "../components/StatusBar";
import EconomicsPane from "../components/EconomicsPane";
import UpdateBanner, { type UpdateAvailability } from "../components/UpdateBanner";
import { api } from "../lib/api";
import { publishTaskProfile } from "../lib/taskProfileChrome";

vi.mock("../lib/api", () => ({
  api: {
    getEconomics: vi.fn(),
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

const mockGetEconomics = vi.mocked(api.getEconomics);
const mockWorkspaces = vi.mocked(api.workspaces);
const mockGetSessionState = vi.mocked(api.getSessionState);
const mockSessions = vi.mocked(api.sessions);
const mockPauseSessionGoal = vi.mocked(api.pauseSessionGoal);
const mockResumeSessionGoal = vi.mocked(api.resumeSessionGoal);
const mockCompleteSessionGoal = vi.mocked(api.completeSessionGoal);
const mockClearSessionGoal = vi.mocked(api.clearSessionGoal);

beforeEach(() => {
  mockGetEconomics.mockResolvedValue({
    available: true,
    scope: "conversation",
    counterfactual: null,
  });
});

const statusBarProps = {
  config: null,
  update: null,
  leftOpen: true,
  rightOpen: false,
  onToggleLeft: vi.fn(),
  onToggleRight: vi.fn(),
  onOpenEconomics: vi.fn(),
};

function AppUpdateChromeHarness() {
  const [update, setUpdate] = useState<UpdateAvailability | null>(null);
  return (
    <>
      <UpdateBanner onAvailabilityChange={setUpdate} />
      <StatusBar {...statusBarProps} update={update} />
    </>
  );
}

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

  it("humanizes footer labels without flashing raw enums", () => {
    expect(footerRuntimeStatusLabel("ready")).toBe("Idle");
    expect(footerRuntimeStatusLabel("thinking")).toBe("Thinking…");
    expect(footerRuntimeStatusLabel("busy")).toBe("Busy");
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

  it("shows canonical PM session cost and opens that same all-time scope", async () => {
    const onOpenEconomics = vi.fn();
    const selections: unknown[] = [];
    const onSelection = (event: Event) => selections.push((event as CustomEvent).detail);
    window.addEventListener("harness-economics-selection", onSelection);
    mockGetEconomics.mockResolvedValue({
      available: true,
      scope: "conversation",
      counterfactual_source: "job_financial_reports",
      counterfactual_status: "ok",
      counterfactual: {
        actual_cost_usd: 2.312042,
        measured_cost_usd: 2.312042,
        estimated_cost_usd: 0,
        spend_basis: "measured_usage_x_registry_price",
        avoided_usd: 11.34,
      },
    });

    try {
      render(<StatusBar {...statusBarProps} onOpenEconomics={onOpenEconomics} />);

      const pill = await screen.findByRole("button", { name: "$2.31" });
      expect(screen.queryByText("~$0.04")).toBeNull();
      fireEvent.click(pill);
      await waitFor(() => expect(selections).toEqual([{ scope: "conversation", period: "all" }]));
      expect(onOpenEconomics).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener("harness-economics-selection", onSelection);
    }
  });

  it("opens a late-mounted Economics pane at This session and All time", async () => {
    function Harness() {
      const [economicsOpen, setEconomicsOpen] = useState(false);
      return (
        <>
          <StatusBar
            {...statusBarProps}
            onOpenEconomics={() => window.setTimeout(() => setEconomicsOpen(true), 20)}
          />
          {economicsOpen ? <EconomicsPane /> : null}
        </>
      );
    }
    mockGetEconomics.mockImplementation(async (scope = "repo") => ({
      available: true,
      scope,
      counterfactual_source: "job_financial_reports",
      counterfactual_status: "ok",
      counterfactual: {
        actual_cost_usd: scope === "conversation" ? 2.312042 : 115.57,
        measured_cost_usd: scope === "conversation" ? 2.312042 : 115.57,
        estimated_cost_usd: 0,
        spend_basis: "measured_usage_x_registry_price",
        avoided_usd: 11.34,
      },
    }));

    render(<Harness />);
    fireEvent.click(await screen.findByRole("button", { name: "$2.31" }));

    expect(await screen.findByLabelText("Economics ownership")).toHaveValue("conversation");
    expect(screen.getByLabelText("Economics period")).toHaveValue("all");
  });
});

describe("StatusBar runtime status", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
    mockSessions.mockResolvedValue([{ id: "sess-1", title: "Test", created: 0, active: true }]);
  });

  it("shows Idle when the active session runner is idle", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("Idle")).toBeInTheDocument();
      expect(screen.getByText("Idle").closest("[data-runtime-status]"))
        .toHaveAttribute("data-runtime-status", "ready");
    });
  });

  it("shows Thinking… when the active session runner is running", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "running" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("Thinking…")).toBeInTheDocument();
      expect(screen.getByText("Thinking…").closest("[data-runtime-status]"))
        .toHaveAttribute("data-runtime-status", "thinking");
    });
  });

  it("shows Idle when only a background session runner is running", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "idle",
      pending_swarms: false,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle", "sess-2": "running" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("Idle")).toBeInTheDocument();
      expect(screen.getByText("Idle").closest("[data-runtime-status]"))
        .toHaveAttribute("data-runtime-status", "ready");
    });
  });

  it("shows Busy when swarms are pending", async () => {
    mockGetSessionState.mockResolvedValue({
      state: "awaiting_swarm",
      pending_swarms: true,
      active_view_id: "sess-1",
      runners: { "sess-1": "idle" },
    });

    render(<StatusBar {...statusBarProps} />);

    await waitFor(() => {
      expect(screen.getByText("Busy")).toBeInTheDocument();
      expect(screen.getByText("Busy").closest("[data-runtime-status]"))
        .toHaveAttribute("data-runtime-status", "busy");
    });
  });
});


describe("StatusBar panel toggle shortcuts", () => {
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

  it("uses Ctrl/Cmd titles for panel toggle buttons", () => {
    render(<StatusBar {...statusBarProps} />);

    expect(screen.getByTitle("Toggle sessions panel (Ctrl/Cmd+B)")).toBeInTheDocument();
    expect(screen.getByTitle("Toggle floating panels (Ctrl/Cmd+J)")).toBeInTheDocument();
  });
});

describe("StatusBar session GOAL chip", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockWorkspaces.mockResolvedValue([]);
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

describe("StatusBar update progress mirror", () => {
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

  afterEach(() => {
    delete (window as any).harnessIPC;
  });

  it("renders the projected compact update and delegates its click", async () => {
    const applyRequest = vi.fn();
    window.addEventListener("harness-update-apply", applyRequest);
    (window as any).harnessIPC = {
      updates: {
        onProgress: vi.fn(() => () => {}),
      },
    };

    const { rerender } = render(
      <StatusBar
        {...statusBarProps}
        update={{ behind: 2, branch: "main", version: "0.9.245" }}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "update (2)" }));
    expect(applyRequest).toHaveBeenCalledTimes(1);

    rerender(<StatusBar {...statusBarProps} update={null} />);
    expect(screen.queryByRole("button", { name: "update (2)" })).not.toBeInTheDocument();
    window.removeEventListener("harness-update-apply", applyRequest);
  });

  it("shares App-projected availability and banner-owned apply progress", async () => {
    let availableListener: ((payload: any) => void) | null = null;
    const progressListeners: Array<(payload: any) => void> = [];
    const apply = vi.fn(() => new Promise(() => {}));
    (window as any).harnessIPC = {
      updates: {
        check: vi.fn(() => new Promise(() => {})),
        apply,
        onAvailable: vi.fn((listener) => {
          availableListener = listener;
          return () => {};
        }),
        onProgress: vi.fn((listener) => {
          progressListeners.push(listener);
          return () => {};
        }),
      },
    };

    render(<AppUpdateChromeHarness />);
    act(() => availableListener?.({
      available: true,
      downloaded: false,
      behind: 2,
      branch: "main",
      current: "0.9.245",
    }));

    expect(await screen.findByTestId("update-banner")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "update (2)" }));
    await waitFor(() => expect(apply).toHaveBeenCalledTimes(1));

    act(() => progressListeners.forEach((listener) => listener({
      stage: "install",
      message: "Installing update",
      percent: 50,
    })));
    await waitFor(() => expect(screen.getAllByText("Installing update 50%")).toHaveLength(2));
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

describe("StatusBar task profile chip", () => {
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

  it("stays hidden until a task_profile event arrives", async () => {
    render(<StatusBar {...statusBarProps} />);
    await waitFor(() => {
      expect(mockGetSessionState).toHaveBeenCalled();
    });
    expect(screen.queryByTestId("task-profile-chip")).not.toBeInTheDocument();

    act(() => {
      publishTaskProfile({ profile: "micro", source: "heuristic" });
    });

    const chip = await screen.findByTestId("task-profile-chip");
    expect(chip).toHaveTextContent("DEPTH");
    expect(chip).toHaveTextContent("MICRO");
    expect(chip).toHaveAttribute(
      "title",
      expect.stringContaining("skipped wiki and CodeGraph auto-inject"),
    );
  });

  it("clears the chip on session change", async () => {
    render(<StatusBar {...statusBarProps} />);
    act(() => {
      publishTaskProfile({ profile: "standard" });
    });
    expect(await screen.findByTestId("task-profile-chip")).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event("harness-session-changed"));
    });
    await waitFor(() => {
      expect(screen.queryByTestId("task-profile-chip")).not.toBeInTheDocument();
    });
  });
});

describe("StatusBar compact chrome", () => {
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

  it("shortens long model ids", () => {
    render(
      <StatusBar
        {...statusBarProps}
        config={{
          driver: "openrouter:deepseek/deepseek-v4-pro-0813",
          reach: "openrouter",
          budget: 0,
        }}
      />,
    );

    expect(screen.getByText("deepseek-v4-pro-0813")).toBeInTheDocument();
    expect(screen.queryByText("openrouter:deepseek/deepseek-v4-pro-0813")).toBeNull();
  });
});
