import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RightPane from "../components/RightPane";
import { api } from "../lib/api";
import { dispatchProjectSelected } from "../lib/panelTransition";
import { usePolling } from "../lib/usePolling";
import { clearSWRCache, readSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getReviews: vi.fn().mockResolvedValue([]),
    swarmLive: vi.fn().mockResolvedValue({ jobs: [] }),
  },
}));

vi.mock("../lib/usePolling", () => ({
  usePolling: vi.fn(),
}));

vi.mock("../components/StatePane", () => ({ default: () => <div data-testid="state-pane" /> }));
vi.mock("../components/BrowserPane", () => ({ default: () => <div /> }));
vi.mock("../components/FileTree", () => ({ default: () => <div /> }));
vi.mock("../components/SourceControl", () => ({ default: () => <div /> }));
vi.mock("../components/WorktreesPane", () => ({ default: () => <div /> }));
vi.mock("../components/SettingsShell", () => ({ default: () => <div /> }));
vi.mock("../components/TerminalPane", () => ({
  default: () => <div data-testid="terminal-pane" />,
}));
vi.mock("../components/CheckpointsPane", () => ({ default: () => <div /> }));
vi.mock("../components/DiffReviewPane", () => ({
  default: ({ loadError }: { loadError?: string | null }) =>
    loadError
      ? <div data-testid="reviews-load-error">{loadError}</div>
      : <div data-testid="diff-review-pane" />,
}));
vi.mock("../components/SwarmPane", () => ({ default: () => <div /> }));
vi.mock("../components/ErrorBoundary", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const baseProps = {
  artifacts: [],
  onOpenWizard: vi.fn(),
  onCollapse: vi.fn(),
};

describe("RightPane collapse placement", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(
      "pmharness.tabOrder",
      JSON.stringify([
        "state", "swarm", "files", "git", "worktrees", "terminal",
        "review", "checkpoints", "browser", "settings",
      ]),
    );
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
  });

  it("places collapse in the fixed right-side action cluster and invokes onCollapse", () => {
    render(<RightPane {...baseProps} />);

    const collapseBtns = screen.getAllByTestId("panel-collapse-btn");
    expect(collapseBtns).toHaveLength(1);

    const tabBar = collapseBtns[0].closest(".border-b");
    expect(tabBar).toBeTruthy();
    const actions = screen.getByTestId("right-pane-actions");
    expect(actions).toContainElement(collapseBtns[0]);
    expect(actions.className).toMatch(/\bshrink-0\b/);
    expect(actions.className).not.toMatch(/\bflex-1\b/);

    const splitControls = tabBar!.querySelector(".border-l.border-edge");
    expect(splitControls).toBeTruthy();
    expect(within(splitControls as HTMLElement).queryByTestId("panel-collapse-btn")).toBeNull();

    fireEvent.click(collapseBtns[0]);
    expect(baseProps.onCollapse).toHaveBeenCalledTimes(1);
  });

  it("adds an equivalent collapse affordance on the secondary split tab bar", () => {
    localStorage.setItem(
      "pmharness.splitState",
      JSON.stringify({
        isSplit: true,
        primaryTab: "state",
        secondaryTab: "terminal",
        direction: "horizontal",
        percent: 50,
      }),
    );

    render(<RightPane {...baseProps} />);

    const collapseBtns = screen.getAllByTestId("panel-collapse-btn");
    expect(collapseBtns).toHaveLength(2);

    for (const btn of collapseBtns) {
      expect(btn).toHaveAttribute("aria-label", "Close side panel");
      expect(btn).toHaveAttribute("title", "Close side panel (Ctrl/Cmd+J)");
      const actions = btn.parentElement;
      expect(actions).toHaveAttribute("data-testid", "right-pane-actions");
      expect(actions?.className).toMatch(/\bshrink-0\b/);
      expect(actions?.className).not.toMatch(/\bflex-1\b/);
    }

    fireEvent.click(collapseBtns[1]);
    expect(baseProps.onCollapse).toHaveBeenCalledTimes(1);
  });
});

describe("RightPane keeps TerminalPane mounted across tab switches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(
      "pmharness.tabOrder",
      JSON.stringify([
        "state", "swarm", "files", "git", "worktrees", "terminal",
        "review", "checkpoints", "browser", "settings",
      ]),
    );
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
    localStorage.setItem(
      "pmharness.splitState",
      JSON.stringify({
        isSplit: false,
        primaryTab: "terminal",
        secondaryTab: "files",
        direction: "horizontal",
        percent: 50,
      }),
    );
  });

  it("CSS-hides TerminalPane instead of unmounting when leaving the tab", () => {
    render(<RightPane {...baseProps} />);

    const slot = screen.getByTestId("terminal-pane-slot");
    expect(within(slot).getByTestId("terminal-pane")).toBeTruthy();
    expect(slot.className).toMatch(/\bh-full\b/);
    expect(slot.className).not.toMatch(/\bhidden\b/);

    fireEvent.click(screen.getByTitle("Files"));

    const stillMounted = screen.getByTestId("terminal-pane-slot");
    expect(within(stillMounted).getByTestId("terminal-pane")).toBeTruthy();
    expect(stillMounted.className).toMatch(/\bhidden\b/);
    expect(stillMounted).toHaveAttribute("aria-hidden", "true");
  });
});

describe("RightPane keeps SwarmPane mounted across tab switches", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(
      "pmharness.tabOrder",
      JSON.stringify([
        "state", "swarm", "files", "git", "worktrees", "terminal",
        "review", "checkpoints", "browser", "settings",
      ]),
    );
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
    localStorage.setItem(
      "pmharness.splitState",
      JSON.stringify({
        isSplit: false,
        primaryTab: "swarm",
        secondaryTab: "files",
        direction: "horizontal",
        percent: 50,
      }),
    );
  });

  it("CSS-hides SwarmPane instead of unmounting when leaving the tab", () => {
    render(<RightPane {...baseProps} />);

    const slot = screen.getByTestId("swarm-pane-slot");
    expect(slot.className).toMatch(/\bh-full\b/);
    expect(slot.className).not.toMatch(/\bhidden\b/);

    fireEvent.click(screen.getByTitle("Files"));

    const stillMounted = screen.getByTestId("swarm-pane-slot");
    expect(stillMounted.className).toMatch(/\bhidden\b/);
    expect(stillMounted).toHaveAttribute("aria-hidden", "true");
  });
});

describe("RightPane reviews-load failure honesty", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(
      "pmharness.tabOrder",
      JSON.stringify([
        "state", "swarm", "files", "git", "worktrees", "terminal",
        "review", "checkpoints", "browser", "settings",
      ]),
    );
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
    // Review is optional by default — enable so DiffReviewPane mounts.
    localStorage.setItem(
      "pmharness.rightPane.visibleTabs.v1",
      JSON.stringify({ worktrees: false, review: true, checkpoints: false }),
    );
    localStorage.setItem(
      "pmharness.splitState",
      JSON.stringify({
        isSplit: false,
        primaryTab: "review",
        secondaryTab: "files",
        direction: "horizontal",
        percent: 50,
      }),
    );
  });

  it("surfaces loadError on DiffReviewPane when getReviews fails", async () => {
    vi.mocked(api.getReviews).mockRejectedValue(new Error("network"));
    vi.mocked(api.swarmLive).mockResolvedValue({ jobs: [] } as never);
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    render(<RightPane {...baseProps} />);

    // Ensure Review is the active primary tab (visibility + click).
    expect(screen.getByRole("button", { name: "Review" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));

    const pollCalls = vi.mocked(usePolling).mock.calls;
    expect(pollCalls.length).toBeGreaterThanOrEqual(1);
    const fetchReviews = pollCalls[0][0];
    await fetchReviews();

    await waitFor(() => {
      expect(screen.getByTestId("reviews-load-error")).toHaveTextContent(
        /Couldn't load pending reviews/i,
      );
    });
    errSpy.mockRestore();
  });
});

describe("RightPane swarm activity poll seeds SWR cache", () => {
  const REPO = "C:\\Users\\pwall\\Projects\\warm-swarm";

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem(
      "pmharness.tabOrder",
      JSON.stringify([
        "state", "swarm", "files", "git", "worktrees", "terminal",
        "review", "checkpoints", "browser", "settings",
      ]),
    );
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
    dispatchProjectSelected(REPO);
    vi.mocked(api.getReviews).mockResolvedValue([]);
  });

  it("writes swarmLive payload to the SwarmPane cache key", async () => {
    const payload = {
      session: { tokens_used: 0, est_cost_usd: 0 },
      jobs: [{ id: "job-1", goal: "Warm me", status: "running" }],
    };
    vi.mocked(api.swarmLive).mockResolvedValue(payload as never);

    render(<RightPane {...baseProps} />);

    // usePolling(fetchReviews) then usePolling(fetchSwarmActivity) each render —
    // the last registration is the swarm activity poller.
    const pollCalls = vi.mocked(usePolling).mock.calls;
    expect(pollCalls.length).toBeGreaterThanOrEqual(2);
    const fetchSwarmActivity = pollCalls[pollCalls.length - 1][0];
    await fetchSwarmActivity();

    expect(api.swarmLive).toHaveBeenCalledWith(REPO);
    expect(readSWRCache(`swarm:${REPO}`)).toEqual(payload);
  });
});

describe("RightPane optional tab customization", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
  });

  it("keeps advanced tabs out of the strip but exposes them in the menu", () => {
    render(<RightPane {...baseProps} />);

    expect(screen.queryByRole("button", { name: "Worktrees" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(screen.queryByRole("button", { name: "History" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Customize tabs" }));
    expect(screen.getByRole("menu", { name: "Customize tabs" })).toBeInTheDocument();
    expect(screen.getByRole("menuitemcheckbox", { name: "Worktrees" })).toHaveAttribute(
      "aria-checked",
      "false",
    );

    fireEvent.click(screen.getByRole("checkbox", { name: "Worktrees" }));
    expect(screen.getByRole("button", { name: "Worktrees" })).toBeInTheDocument();
    expect(localStorage.getItem("pmharness.rightPane.visibleTabs.v1")).toContain('"worktrees":true');
  });

  it("closes the customization menu with Escape and an outside click", () => {
    render(<RightPane {...baseProps} />);
    fireEvent.click(screen.getByRole("button", { name: "Customize tabs" }));

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Customize tabs" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Customize tabs" }));
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu", { name: "Customize tabs" })).toBeNull();
  });
});
