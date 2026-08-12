import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RightPane from "../components/RightPane";
import RightDock from "../components/RightDock";
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
  visible: true,
  artifacts: [],
  onOpenWizard: vi.fn(),
  onCollapse: vi.fn(),
};

function seedBoardTabOrder(openCards: string[] = ["state", "terminal"]) {
  localStorage.setItem(
    "pmharness.tabOrder",
    JSON.stringify([
      "state", "swarm", "files", "git", "worktrees", "terminal",
      "review", "checkpoints", "browser", "settings",
    ]),
  );
  localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
  localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
  localStorage.setItem("pmharness.board.openCards", JSON.stringify(openCards));
}

function expectCardGridPlacement(label: string, gridColumn: string, gridRow: string) {
  const card = screen.getByRole("region", { name: `${label} panel` });
  expect(card.style.gridColumn).toBe(gridColumn);
  expect(card.style.gridRow).toBe(gridRow);
}

describe("RightPane collapse placement", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    seedBoardTabOrder();
  });

  it("places collapse in the dock action cluster and invokes onCollapse", () => {
    render(<RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />);

    const collapseBtns = screen.getAllByTestId("panel-collapse-btn");
    expect(collapseBtns).toHaveLength(1);

    fireEvent.click(collapseBtns[0]);
    expect(baseProps.onCollapse).toHaveBeenCalledTimes(1);
  });

  it("keeps Add panel items clickable after an inside mousedown", () => {
    const onOpenTab = vi.fn();
    render(<RightDock onOpenTab={onOpenTab} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />);

    fireEvent.click(screen.getByRole("button", { name: "Add panel" }));
    const swarmItem = screen.getByRole("menuitem", { name: "Swarm" });

    fireEvent.mouseDown(swarmItem);
    expect(screen.getByRole("menu", { name: "Add panel" })).toBeInTheDocument();
    fireEvent.click(swarmItem);

    expect(onOpenTab).toHaveBeenCalledWith("swarm");
    expect(screen.queryByRole("menu", { name: "Add panel" })).toBeNull();
  });

  it("renders anchored grid cards that can be reordered and closed independently", () => {
    render(<><RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    const stateCard = screen.getByRole("region", { name: "State panel" });
    expect(stateCard).toHaveClass("right-pane-card");
    expect(stateCard).not.toHaveClass("right-pane-floating-card");
    expect(stateCard.style.position).toBe("");
    expect(stateCard.style.gridColumn).toBe("1 / span 12");
    const board = stateCard.closest(".right-pane-board");
    expect(board).toHaveClass("h-full", "w-full");
    expect(board?.querySelector(".right-pane-board-grid")).toContainElement(stateCard);
    const terminalCard = screen.getByRole("region", { name: "Terminal panel" });
    const stateDragHandle = screen.getByRole("button", { name: "Drag State panel" });

    fireEvent.dragStart(stateDragHandle, {
      dataTransfer: { effectAllowed: "", setData: vi.fn() },
    });
    fireEvent.drop(terminalCard, {
      dataTransfer: { getData: () => "state" },
    });

    expect(JSON.parse(localStorage.getItem("pmharness.board.openCards") || "[]")).toEqual([
      "terminal",
      "state",
    ]);
    expect(screen.getAllByRole("region").map((card) => card.getAttribute("aria-label"))).toEqual([
      "Terminal panel",
      "State panel",
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Close State panel" }));
    expect(screen.queryByRole("region", { name: "State panel" })).toBeNull();
  });

  it("provides independent width resize handles without height resize handles", () => {
    render(<><RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    expect(screen.getByRole("separator", { name: "Resize State panel width" })).toBeInTheDocument();
    expect(screen.getByRole("separator", { name: "Resize Terminal panel width" })).toBeInTheDocument();
    expect(screen.queryByRole("separator", { name: "Resize State panel height" })).toBeNull();
    expect(screen.queryByRole("separator", { name: "Resize Terminal panel height" })).toBeNull();
    expect(screen.getByRole("region", { name: "State panel" })).not.toHaveStyle({ height: "160px" });
    expect(screen.getByRole("region", { name: "Terminal panel" })).not.toHaveStyle({ height: "160px" });
  });

  it("does not render an empty board and asks the shell to close it", () => {
    localStorage.setItem("pmharness.board.openCards", JSON.stringify([]));
    const onEmpty = vi.fn();

    render(<RightPane {...baseProps} onEmpty={onEmpty} />);

    expect(document.querySelector(".right-pane-board")).toBeNull();
    expect(onEmpty).toHaveBeenCalledTimes(1);
  });

  it("keeps the shell open while the requested first card mounts", async () => {
    localStorage.setItem("pmharness.board.openCards", JSON.stringify([]));
    const onEmpty = vi.fn();

    render(<RightPane {...baseProps} onEmpty={onEmpty} initialTab="state" />);

    await waitFor(() => {
      expect(screen.getByRole("region", { name: "State panel" })).toBeInTheDocument();
    });
    expect(onEmpty).not.toHaveBeenCalled();
  });

  it("keeps mounted panes alive when the overlay is hidden", () => {
    const { rerender } = render(<RightPane {...baseProps} />);
    fireEvent.click(screen.getByRole("button", { name: "Close Terminal panel" }));

    rerender(<RightPane {...baseProps} visible={false} />);
    const slot = screen.getByTestId("terminal-pane-slot");
    expect(within(slot).getByTestId("terminal-pane")).toBeTruthy();
    expect(slot.closest("[aria-hidden='true']")).toBeTruthy();
  });
});

describe("RightPane Claude-style card packing", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    seedBoardTabOrder();
  });

  it("stacks the first two cards in column 2 and spans the third card across rows in column 1", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["state", "terminal", "swarm"]),
    );

    render(
      <>
        <RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />
        <RightPane {...baseProps} />
      </>,
    );

    expectCardGridPlacement("State", "7 / span 6", "1");
    expectCardGridPlacement("Terminal", "7 / span 6", "2");
    expectCardGridPlacement("Swarm", "1 / span 6", "1 / span 2");
    expect(screen.queryByTestId("right-pane-toolbar")).toBeNull();
  });

  it("keeps cards 1/2 stacked in column 2 and cards 3/4 stacked in column 1", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["state", "terminal", "swarm", "files"]),
    );

    render(
      <>
        <RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />
        <RightPane {...baseProps} />
      </>,
    );

    expectCardGridPlacement("State", "7 / span 6", "1");
    expectCardGridPlacement("Terminal", "7 / span 6", "2");
    expectCardGridPlacement("Swarm", "1 / span 6", "1");
    expectCardGridPlacement("Files", "1 / span 6", "2");
    expect(screen.queryByTestId("right-pane-toolbar")).toBeNull();
  });

  it.each([
    { count: 1, cards: ["state"] },
    { count: 2, cards: ["state", "terminal"] },
    { count: 3, cards: ["state", "terminal", "swarm"] },
    { count: 4, cards: ["state", "terminal", "swarm", "files"] },
  ])("uses a bounded 12-column grid for $count cards", ({ cards }) => {
    localStorage.setItem("pmharness.board.openCards", JSON.stringify(cards));
    render(<RightPane {...baseProps} />);

    const grid = document.querySelector(".right-pane-board-grid");
    expect(grid).toHaveStyle({
      gridTemplateColumns: "repeat(12, minmax(0, 1fr))",
      gridTemplateRows: "repeat(2, minmax(0, 1fr))",
    });
    expect(screen.getAllByRole("region")).toHaveLength(cards.length);
    expect(screen.queryAllByRole("separator")).toHaveLength(cards.length);
    expect(screen.getAllByRole("region").every(card => !card.getAttribute("style")?.includes("height"))).toBe(true);
  });

  it("widens one card without widening its neighboring stack", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["state", "terminal", "browser"]),
    );
    localStorage.setItem(
      "pmharness.board.cardLayouts.v1",
      JSON.stringify({
        state: { columnSpan: 6, customized: true },
        terminal: { columnSpan: 6, customized: true },
        browser: { columnSpan: 6, customized: true },
      }),
    );

    render(<RightPane {...baseProps} />);

    const browserResizeHandle = screen.getByRole("separator", { name: "Resize Browser panel width" });
    fireEvent.keyDown(browserResizeHandle, { key: "ArrowRight" });

    expect(JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}")).toMatchObject({
      browser: { columnSpan: 7, customized: true },
    });
    expectCardGridPlacement("Browser", "1 / span 7", "1 / span 2");
    expectCardGridPlacement("State", "8 / span 5", "1");
    expectCardGridPlacement("Terminal", "8 / span 5", "2");
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

  it("keeps TerminalPane mounted when its card is closed", () => {
    render(<><RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    expect(screen.getByRole("region", { name: "Terminal panel" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close Terminal panel" }));

    const slot = screen.getByTestId("terminal-pane-slot");
    expect(within(slot).getByTestId("terminal-pane")).toBeTruthy();
    expect(slot.closest("[aria-hidden='true']")).toBeTruthy();
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

  it("keeps SwarmPane mounted when its card is closed", () => {
    render(<><RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    expect(screen.getByRole("region", { name: "Swarm panel" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close Swarm panel" }));

    const slot = screen.getByTestId("swarm-pane-slot");
    expect(slot).toBeInTheDocument();
    expect(slot.closest("[aria-hidden='true']")).toBeTruthy();
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

    expect(screen.getByRole("region", { name: "Review panel" })).toBeInTheDocument();

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

  it("keeps optional panels out of the board until enabled and added", () => {
    const onOpenTab = (tab: string) => {
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: tab }));
    };
    render(<><RightDock onOpenTab={onOpenTab} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    expect(screen.queryByRole("region", { name: "Worktrees panel" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add panel" }));
    expect(screen.getByRole("menu", { name: "Add panel" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Worktrees" }));
    expect(localStorage.getItem("pmharness.rightPane.visibleTabs.v1")).toContain('"worktrees":true');
    fireEvent.click(screen.getByRole("menuitem", { name: "Worktrees" }));
    expect(screen.getByRole("region", { name: "Worktrees panel" })).toBeInTheDocument();
  });

  it("closes the customization menu with Escape and an outside click", () => {
    render(<><RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);
    fireEvent.click(screen.getByRole("button", { name: "Add panel" }));

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Add panel" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Add panel" }));
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menu", { name: "Add panel" })).toBeNull();
  });
});
