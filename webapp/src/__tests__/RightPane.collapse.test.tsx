import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Profiler, useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import RightPane from "../components/RightPane";
import RightDock from "../components/RightDock";
import css from "../index.css?raw";
import { api } from "../lib/api";
import { dispatchProjectSelected } from "../lib/panelTransition";
import { clearSWRCache, readSWRCache } from "../lib/useStaleWhileRevalidate";
import { JobMetadataContext, JobMetadataOwner, useSharedJobMetadata } from "../lib/jobMetadataContext";
import { JobMetadataStore } from "../lib/useJobMetadata";
import { CombinedMetadataFixture } from "./metadataMigration.fixtures";
import { context } from "./jobMetadata.fixtures";
import { resetSettingsOverlay, setSettingsOverlayOpen } from "../lib/settingsOverlay";

vi.mock("../lib/api", () => ({
  api: {
    getReviews: vi.fn().mockResolvedValue([]),
    swarmLive: vi.fn().mockResolvedValue({ jobs: [] }),
  },
}));


vi.mock("../components/StatePane", () => ({ default: () => <div data-testid="state-pane" /> }));
vi.mock("../components/BrowserPane", () => ({ default: () => <div /> }));
vi.mock("../components/FileTree", () => ({ default: () => <div /> }));
vi.mock("../components/SourceControl", () => ({ default: () => <div /> }));
vi.mock("../components/WorktreesPane", () => ({ default: () => <div /> }));
vi.mock("../components/SettingsShell", () => ({
  default: ({ onClose }: { onClose: () => void }) => (
    <div data-testid="settings-shell">
      <button type="button" onClick={onClose}>close-settings</button>
    </div>
  ),
}));
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
vi.mock("../components/EconomicsPane", () => ({
  default: function MockEconomicsPane() {
    const [scope, setScope] = useState("repo");
    return (
      <div data-testid="economics-pane">
        <select
          aria-label="Economics ownership"
          value={scope}
          onChange={(event) => setScope(event.target.value)}
        >
          <option value="conversation">This session</option>
          <option value="repo">This repo</option>
        </select>
      </div>
    );
  },
}));
vi.mock("../components/ErrorBoundary", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const baseProps = {
  visible: true,
  artifacts: [],
  onOpenWizard: vi.fn(),
  onCollapse: vi.fn(),
};

function CaptureMetadataStore({ capture }: { capture: (store: JobMetadataStore) => void }) {
  capture(useSharedJobMetadata().store);
  return null;
}

function OwnedActivity({ fixture, capture, children }: {
  fixture: CombinedMetadataFixture;
  capture: (store: JobMetadataStore) => void;
  children: ReactNode;
}) {
  return <JobMetadataOwner repo={fixture.target.repo} sessionId={fixture.target.session_id}>
    <CaptureMetadataStore capture={capture} />
    {children}
  </JobMetadataOwner>;
}

function expectBoundedActivity(fixture: CombinedMetadataFixture) {
  expect(api.swarmLive).not.toHaveBeenCalled();
  expect(fixture.calls.every(({ method, path }) => method === "GET"
    && ["/api/endpoint", "/api/jobs/metadata/view", "/api/jobs/metadata", "/api/jobs/metadata/local"]
      .includes(new URL(path, "http://fixture").pathname))).toBe(true);
  expect(fixture.maximumActive).toBe(1);
}

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
  const stack = card.closest(".right-pane-card-stack") as HTMLElement | null;
  expect(stack).not.toBeNull();
  expect(stack!.style.gridColumn).toBe(gridColumn);
  expect(card.style.gridRow).toBe(gridRow);
}

describe("RightPane collapse placement", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    resetSettingsOverlay();
    Element.prototype.scrollIntoView = vi.fn();
    seedBoardTabOrder();
  });

  it("paints the floating pill with the left-rail panel glass", () => {
    render(<RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />);
    expect(screen.getByTestId("floating-dock-pill")).toHaveClass("shell-inset-glass");
  });

  it("keeps the Add panel menu on the opaque overlay token, not glass-mixed --shell-panel", () => {
    const menu = css.match(/\.right-pane-add-menu\s*\{[^}]+\}/)?.[0] ?? "";
    expect(menu).toContain("background: var(--shell-overlay)");
    expect(menu).not.toContain("background: var(--shell-panel)");
    expect(css).toMatch(/--shell-overlay:\s*#181a1d/);
    const glassBlock = css.match(/:root\[data-marionette-glass\]\s*\{[^}]+\}/)?.[0] ?? "";
    expect(glassBlock).not.toMatch(/--shell-overlay\s*:/);
  });

  it("places collapse in the dock action cluster and invokes onCollapse", () => {
    render(<RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />);

    const collapseBtns = screen.getAllByTestId("panel-collapse-btn");
    expect(collapseBtns).toHaveLength(1);

    fireEvent.click(collapseBtns[0]);
    expect(baseProps.onCollapse).toHaveBeenCalledTimes(1);
  });

  it("opens Economics from a first-class dock shortcut", () => {
    const onOpenTab = vi.fn();
    render(<RightDock onOpenTab={onOpenTab} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} />);

    fireEvent.click(screen.getByTitle("Economics"));

    expect(onOpenTab).toHaveBeenCalledWith("economics");
  });

  it("keeps dock shortcuts mounted and usable across panel visibility changes", () => {
    const onOpenTab = vi.fn();
    const onExpand = vi.fn();
    const onCollapse = vi.fn();
    const { rerender } = render(
      <RightDock panelsOpen={false} onOpenTab={onOpenTab} onExpand={onExpand} onCollapse={onCollapse} />,
    );
    const dock = screen.getByRole("complementary", { name: "Floating panel shortcuts" });
    fireEvent.click(within(dock).getByRole("button", { name: "Show panels" }));
    expect(onExpand).toHaveBeenCalledOnce();
    fireEvent.click(within(dock).getByTitle("In-app browser"));
    expect(onOpenTab).toHaveBeenLastCalledWith("browser");

    rerender(<RightDock panelsOpen onOpenTab={onOpenTab} onExpand={onExpand} onCollapse={onCollapse} />);
    expect(screen.getByRole("complementary", { name: "Floating panel shortcuts" })).toBe(dock);
    fireEvent.click(within(dock).getByRole("button", { name: "Hide panels" }));
    expect(onCollapse).toHaveBeenCalledOnce();
    fireEvent.click(within(dock).getByTitle("Economics"));
    expect(onOpenTab).toHaveBeenLastCalledWith("economics");
  });

  it("preserves the mounted Economics selection when another panel opens", () => {
    seedBoardTabOrder(["economics"]);
    render(<RightPane {...baseProps} />);

    const ownership = screen.getByLabelText("Economics ownership");
    fireEvent.change(ownership, { target: { value: "conversation" } });
    expect(ownership).toHaveValue("conversation");

    fireEvent(window, new CustomEvent("harness-focus-tab", { detail: "browser" }));

    expect(screen.getByRole("region", { name: "Browser panel" })).toBeInTheDocument();
    expect(screen.getByLabelText("Economics ownership")).toHaveValue("conversation");
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
    expectCardGridPlacement("State", "1", "1");
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

  it("fills a two-card stack with a height split and no inner width handles", () => {
    render(<><RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    expectCardGridPlacement("State", "1", "1");
    expectCardGridPlacement("Terminal", "1", "2");
    expect(screen.getByRole("region", { name: "State panel" }).style.width).toBe("");
    expect(screen.getByRole("region", { name: "Terminal panel" }).style.width).toBe("");
    expect(screen.queryByRole("separator", { name: "Resize tool columns" })).toBeNull();
    expect(screen.getByRole("separator", { name: "Resize stacked panel height" })).toBeInTheDocument();
    const stack = screen.getByRole("region", { name: "State panel" }).closest(".right-pane-card-stack") as HTMLElement;
    expect(stack.style.gridTemplateRows).toBe("minmax(0, 50fr) minmax(0, 50fr)");
  });

  it("does not close the rail while Settings is open on an empty board", () => {
    localStorage.setItem("pmharness.board.openCards", JSON.stringify([]));
    const onEmpty = vi.fn();

    render(<RightPane {...baseProps} onEmpty={onEmpty} initialTab="settings" />);

    expect(screen.getByTestId("settings-shell")).toBeInTheDocument();
    expect(onEmpty).not.toHaveBeenCalled();
  });

  it("keeps Settings open across a remount when the overlay latch is set", () => {
    setSettingsOverlayOpen(true);
    const onEmpty = vi.fn();
    const { unmount } = render(<RightPane {...baseProps} onEmpty={onEmpty} />);
    expect(screen.getByTestId("settings-shell")).toBeInTheDocument();
    unmount();
    render(<RightPane {...baseProps} onEmpty={onEmpty} />);
    expect(screen.getByTestId("settings-shell")).toBeInTheDocument();
    expect(onEmpty).not.toHaveBeenCalled();
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

  it("keeps three cards in one full-width stack instead of auto-opening a column", () => {
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

    expectCardGridPlacement("State", "1", "1");
    expectCardGridPlacement("Terminal", "1", "2");
    expectCardGridPlacement("Swarm", "1", "3");
    expect(screen.queryAllByRole("separator", { name: "Resize stacked panel height" })).toHaveLength(2);
    expect(screen.queryByTestId("right-pane-toolbar")).toBeNull();
  });

  it("keeps four cards in one full-width stack until a column is opened", () => {
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

    expectCardGridPlacement("State", "1", "1");
    expectCardGridPlacement("Terminal", "1", "2");
    expectCardGridPlacement("Swarm", "1", "3");
    expectCardGridPlacement("Files", "1", "4");
    expect(screen.queryAllByRole("separator", { name: "Resize stacked panel height" })).toHaveLength(3);
    expect(screen.queryByTestId("right-pane-toolbar")).toBeNull();
  });

  it.each([
    { count: 1, cards: ["state"] },
    { count: 2, cards: ["state", "terminal"] },
    { count: 3, cards: ["state", "terminal", "swarm"] },
    { count: 4, cards: ["state", "terminal", "swarm", "files"] },
  ])("uses a single flexible track for $count stacked cards", ({ cards }) => {
    localStorage.setItem("pmharness.board.openCards", JSON.stringify(cards));
    render(<RightPane {...baseProps} />);

    const grid = document.querySelector(".right-pane-board-grid");
    expect(grid).toHaveStyle({
      gridTemplateColumns: "minmax(0, 1fr)",
      gridTemplateRows: "minmax(0, 1fr)",
    });
    expect(screen.getAllByRole("region")).toHaveLength(cards.length);
    expect(screen.queryAllByRole("separator", { name: "Resize tool columns" })).toHaveLength(0);
    expect(screen.queryAllByRole("separator", { name: "Resize stacked panel height" })).toHaveLength(
      Math.max(0, cards.length - 1),
    );
    expect(screen.getAllByRole("region").every(card => !card.getAttribute("style")?.includes("height"))).toBe(true);
    expect(screen.getAllByRole("region").every(card => !card.style.width)).toBe(true);
  });

  it("moves the column split and keeps stacked cards flush", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["state", "terminal", "browser"]),
    );
    localStorage.setItem(
      "pmharness.board.columns.v1",
      JSON.stringify([["state", "terminal"], ["browser"]]),
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

    const columnResizeHandle = screen.getAllByRole("separator", { name: "Resize tool columns" })[0];
    fireEvent.keyDown(columnResizeHandle, { key: "ArrowLeft" });

    expect(JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}")).toMatchObject({
      state: { columnSpan: 7, customized: true },
      terminal: { columnSpan: 7, customized: true },
      browser: { columnSpan: 5, customized: true },
    });
    expectCardGridPlacement("State", "2", "1");
    expectCardGridPlacement("Terminal", "2", "2");
    expectCardGridPlacement("Browser", "1", "1");
  });

  it("gives the middle of three columns its own width handle", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["files", "browser", "state"]),
    );
    localStorage.setItem(
      "pmharness.board.columns.v1",
      JSON.stringify([["files"], ["browser"], ["state"]]),
    );
    localStorage.setItem(
      "pmharness.board.cardLayouts.v1",
      JSON.stringify({
        files: { columnSpan: 4, customized: true },
        browser: { columnSpan: 4, customized: true },
        state: { columnSpan: 4, customized: true },
      }),
    );

    render(<RightPane {...baseProps} />);

    expect(screen.getByTestId("column-resize-0")).toBeInTheDocument();
    expect(screen.getByTestId("column-resize-1")).toBeInTheDocument();
    expect(screen.queryByTestId("column-resize-2")).toBeNull();
    expect(screen.getAllByRole("separator", { name: "Resize tool columns" })).toHaveLength(2);
    expectCardGridPlacement("Files", "3", "1");
    expectCardGridPlacement("Browser", "2", "1");
    expectCardGridPlacement("State", "1", "1");
  });

  it("resizes the middle column against its left neighbor only", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["files", "browser", "state"]),
    );
    localStorage.setItem(
      "pmharness.board.columns.v1",
      JSON.stringify([["files"], ["browser"], ["state"]]),
    );
    localStorage.setItem(
      "pmharness.board.cardLayouts.v1",
      JSON.stringify({
        files: { columnSpan: 4, customized: true },
        browser: { columnSpan: 4, customized: true },
        state: { columnSpan: 4, customized: true },
      }),
    );

    render(<RightPane {...baseProps} />);

    fireEvent.keyDown(screen.getByTestId("column-resize-1"), { key: "ArrowLeft" });

    expect(JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}")).toMatchObject({
      files: { columnSpan: 4, customized: true },
      browser: { columnSpan: 5, customized: true },
      state: { columnSpan: 3, customized: true },
    });
    expectCardGridPlacement("Files", "3", "1");
    expectCardGridPlacement("Browser", "2", "1");
    expectCardGridPlacement("State", "1", "1");
  });

  it("shrinks the top stacked card so the bottom card can fill the rest", () => {
    render(<RightPane {...baseProps} />);

    const handle = screen.getByRole("separator", { name: "Resize stacked panel height" });
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    fireEvent.keyDown(handle, { key: "ArrowUp" });

    const stack = screen.getByRole("region", { name: "State panel" }).closest(".right-pane-card-stack") as HTMLElement;
    expect(stack.style.gridTemplateRows).toBe("minmax(0, 40fr) minmax(0, 60fr)");
    expect(JSON.parse(localStorage.getItem("pmharness.board.stackFractions.v2") || "{}")).toMatchObject({
      "state|terminal": [0.4, 0.6],
    });
  });

  it("resizes the third stacked card from the second row handle", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["state", "terminal", "swarm"]),
    );
    render(<RightPane {...baseProps} />);

    const handles = screen.getAllByRole("separator", { name: "Resize stacked panel height" });
    expect(handles).toHaveLength(2);
    fireEvent.keyDown(handles[1], { key: "ArrowUp" });

    const stack = screen.getByRole("region", { name: "State panel" }).closest(".right-pane-card-stack") as HTMLElement;
    expect(stack.style.gridTemplateRows.match(/minmax/g)).toHaveLength(3);
    expect(stack.style.gridTemplateRows).not.toBe("minmax(0, 33fr) minmax(0, 33fr) minmax(0, 33fr)");
    expect(JSON.parse(localStorage.getItem("pmharness.board.stackFractions.v2") || "{}")["state|terminal|swarm"]).toHaveLength(3);
  });

  it("opens a left column when a stacked card is dropped on the new-column zone", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["review", "swarm", "browser"]),
    );
    const onRequestMinWidth = vi.fn();
    render(<RightPane {...baseProps} onRequestMinWidth={onRequestMinWidth} />);

    const dataTransfer = { effectAllowed: "", setData: vi.fn(), getData: vi.fn(() => "browser") };
    fireEvent.dragStart(screen.getByRole("button", { name: "Drag Browser panel" }), { dataTransfer });
    fireEvent.drop(screen.getByRole("region", { name: "Drop to open a column" }), { dataTransfer });

    expectCardGridPlacement("Review", "2", "1");
    expectCardGridPlacement("Swarm", "2", "2");
    expectCardGridPlacement("Browser", "1", "1");
    expect(onRequestMinWidth).toHaveBeenCalledWith(420);
    expect(JSON.parse(localStorage.getItem("pmharness.board.columns.v1") || "[]")).toEqual([
      ["review", "swarm"],
      ["browser"],
    ]);
  });

  it("keeps a drop on another card in the same stack", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["review", "swarm", "browser"]),
    );
    render(<RightPane {...baseProps} />);

    const dataTransfer = { effectAllowed: "", setData: vi.fn(), getData: vi.fn(() => "browser") };
    fireEvent.dragStart(screen.getByRole("button", { name: "Drag Browser panel" }), { dataTransfer });
    fireEvent.drop(screen.getByRole("region", { name: "Review panel" }), { dataTransfer });

    expectCardGridPlacement("Browser", "1", "1");
    expectCardGridPlacement("Review", "1", "2");
    expectCardGridPlacement("Swarm", "1", "3");
    expect(JSON.parse(localStorage.getItem("pmharness.board.columns.v1") || "[]")).toEqual([
      ["browser", "review", "swarm"],
    ]);
  });

  it("keeps stacked column pairs on independent height splits", () => {
    localStorage.setItem(
      "pmharness.board.openCards",
      JSON.stringify(["terminal", "swarm", "state", "review"]),
    );
    localStorage.setItem(
      "pmharness.board.columns.v1",
      JSON.stringify([["terminal", "swarm"], ["state", "review"]]),
    );
    localStorage.setItem(
      "pmharness.board.stackSplits.v1",
      JSON.stringify({ "terminal|swarm": 0.2, "state|review": 0.5 }),
    );

    render(<RightPane {...baseProps} />);

    const handles = screen.getAllByRole("separator", { name: "Resize stacked panel height" });
    expect(handles).toHaveLength(2);
    fireEvent.keyDown(handles[0], { key: "ArrowUp" });

    const terminalStack = screen.getByRole("region", { name: "Terminal panel" }).closest(".right-pane-card-stack") as HTMLElement;
    const stateStack = screen.getByRole("region", { name: "State panel" }).closest(".right-pane-card-stack") as HTMLElement;
    expect(terminalStack.style.gridTemplateRows).toBe("minmax(0, 15fr) minmax(0, 85fr)");
    expect(stateStack.style.gridTemplateRows).toBe("minmax(0, 50fr) minmax(0, 50fr)");
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

    await waitFor(() => {
      expect(screen.getByTestId("reviews-load-error")).toHaveTextContent(
        /Couldn't load pending reviews/i,
      );
    });
    errSpy.mockRestore();
  });
});

describe("RightPane shared activity observation stays warm", () => {
  const REPO = "C:\\Users\\pwall\\Projects\\warm-swarm";

  it("reuses already-observed metadata in pane and dock without a SwarmPane payload cache", async () => {
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    seedBoardTabOrder(["swarm"]);
    dispatchProjectSelected(REPO);
    vi.mocked(api.getReviews).mockResolvedValue([]);
    const fixture = new CombinedMetadataFixture();
    fixture.total = 1;
    fixture.switchTarget(context.session_id, REPO);
    fixture.installIPC();
    const store = new JobMetadataStore();
    try {
      store.setTarget({ ...context, repo: REPO });
      expect(await store.readView()).toBe("applied");
      for (let i = 0; i < 8; i++) expect(await store.advance()).toBe("applied");
      const observed = store.getSnapshot();
      const before = fixture.calls.length;
      const mount = (visible: boolean) => <JobMetadataContext.Provider value={store}>
        <RightDock panelsOpen={visible} onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={vi.fn()} />
        <RightPane {...baseProps} visible={visible} />
      </JobMetadataContext.Provider>;
      const rendered = render(mount(true));
      expect(screen.getAllByTitle("At least 2 active jobs; coverage incomplete")).toHaveLength(2);
      rendered.rerender(mount(false));
      expect(store.getSnapshot()).toBe(observed);
      rendered.rerender(mount(true));
      expect(screen.getAllByTitle("At least 2 active jobs; coverage incomplete")).toHaveLength(2);
      expect(store.getSnapshot()).toBe(observed);
      expect(fixture.calls).toHaveLength(before);
      expect(readSWRCache(`swarm:${REPO}`)).toBeUndefined();
      expectBoundedActivity(fixture);
    } finally {
      cleanup();
      store.dispose();
      Reflect.deleteProperty(window, "harnessIPC");
    }
  });
});

describe("RightPane add-panel menu", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem("pmharness.tabOrder.swarm2nd", "1");
    localStorage.setItem("pmharness.tabOrder.mcpMerged", "1");
  });

  it("opens the Economics panel from harness-focus-tab", async () => {
    render(<RightPane {...baseProps} />);
    expect(screen.queryByRole("region", { name: "Economics panel" })).toBeNull();

    window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "economics" }));

    expect(await screen.findByRole("region", { name: "Economics panel" })).toBeInTheDocument();
    expect(screen.getByTestId("economics-pane")).toBeInTheDocument();
  });

  it("adds a closed panel from the menu without an optional-panels gate", () => {
    const onOpenTab = (tab: string) => {
      window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: tab }));
    };
    render(<><RightDock onOpenTab={onOpenTab} onExpand={vi.fn()} onCollapse={baseProps.onCollapse} /><RightPane {...baseProps} /></>);

    expect(screen.queryByRole("region", { name: "Worktrees panel" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Add panel" }));
    expect(screen.getByRole("menu", { name: "Add panel" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Economics" })).toBeInTheDocument();
    expect(screen.queryByText("Optional panels")).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Worktrees" })).toBeNull();

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


describe("RightPane and RightDock polling ownership", () => {
  let fixture: CombinedMetadataFixture;
  let store: JobMetadataStore;
  const capture = (value: JobMetadataStore) => { store = value; };
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    localStorage.clear();
    sessionStorage.clear();
    clearSWRCache();
    resetSettingsOverlay();
    dispatchProjectSelected("");
    seedBoardTabOrder(["review", "swarm"]);
    localStorage.setItem("marionette.jobScope.v1", "repo");
    vi.mocked(api.getReviews).mockResolvedValue([]);
    fixture = new CombinedMetadataFixture();
    fixture.total = 1;
    fixture.installIPC();
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  });
  afterEach(() => {
    cleanup();
    store?.dispose();
    vi.useRealTimers();
    vi.restoreAllMocks();
    Reflect.deleteProperty(window, "harnessIPC");
  });
  const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
  const dock = <RightDock panelsOpen={false} onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={vi.fn()} />;
  const owned = (children: ReactNode) => <OwnedActivity fixture={fixture} capture={capture}>{children}</OwnedActivity>;

  it("leaves only dock scans while hidden, including refresh and session changes", async () => {
    const rendered = render(owned(<>{dock}<RightPane {...baseProps} visible={false} /></>));
    await tick(20000);
    expect(api.getReviews).toHaveBeenCalledTimes(5);
    const before = fixture.calls.length;
    await tick(2000);
    expect(fixture.calls).toHaveLength(before + 1);
    act(() => {
      fixture.switchTarget("next", context.repo);
      window.dispatchEvent(new CustomEvent("harness-session-changed", { detail: { sessionId: "next" } }));
    });
    rendered.rerender(owned(<>{dock}<RightPane {...baseProps} visible={false} /></>));
    await tick();
    expect(api.getReviews).toHaveBeenCalledTimes(6);
    const refreshed = fixture.calls.length;
    act(() => { window.dispatchEvent(new Event("harness-reviews-refresh")); });
    await tick();
    expect(api.getReviews).toHaveBeenCalledTimes(7);
    expect(fixture.calls).toHaveLength(refreshed);
    expectBoundedActivity(fixture);
  });

  it("observes once at the owner, refreshes visible badges, and retains warm cache on collapse", async () => {
    const view = render(owned(<>{dock}<RightPane {...baseProps} visible={false} /></>));
    await tick(8000);
    expect(store.getSnapshot().observations.length).toBeGreaterThan(0);
    const before = fixture.calls.length;
    view.rerender(owned(<>{dock}<RightPane {...baseProps} /></>));
    await tick();
    expect(fixture.calls).toHaveLength(before);
    expect(screen.getAllByTitle("At least 1 active jobs; coverage incomplete")).toHaveLength(2);
    await tick(4000);
    expect(fixture.calls).toHaveLength(before + 2);
    expect(screen.getAllByTitle("At least 2 active jobs; coverage incomplete")).toHaveLength(2);
    const observations = store.getSnapshot().observations;
    view.rerender(owned(<>{dock}<RightPane {...baseProps} visible={false} /></>));
    expect(store.getSnapshot().observations).toBe(observations);
    await tick(8000);
    expect(fixture.calls).toHaveLength(before + 6);
    view.rerender(owned(<>{dock}<RightPane {...baseProps} /></>));
    expect(screen.getAllByTitle("At least 2 active jobs; coverage incomplete")).toHaveLength(2);
    await tick();
    expect(fixture.calls).toHaveLength(before + 6);
    expect(readSWRCache(`swarm:${context.repo}`)).toBeUndefined();
    expectBoundedActivity(fixture);
  });

  it("refreshes visible review counts immediately on review events", async () => {
    render(owned(<RightPane {...baseProps} />));
    await tick();
    const before = fixture.calls.length;
    vi.mocked(api.getReviews).mockResolvedValue([
      { id: "new", job_id: "new", objective: "new", files: [], created_at: 0 },
    ]);
    act(() => { window.dispatchEvent(new Event("harness-reviews-refresh")); });
    await tick();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(api.getReviews).toHaveBeenCalledTimes(2);
    expect(fixture.calls).toHaveLength(before);
    expectBoundedActivity(fixture);
  });

  it("keeps an in-flight owner response valid after pane collapse without a stale pane write", async () => {
    const view = render(owned(<>{dock}<RightPane {...baseProps} /></>));
    await tick();
    fixture.total = 2;
    fixture.holdNext = true;
    await tick(2000);
    expect(fixture.nextRelease).not.toBeNull();
    const before = fixture.calls.length;
    view.rerender(owned(<>{dock}<RightPane {...baseProps} visible={false} /></>));
    await tick(2000);
    expect(fixture.calls).toHaveLength(before);
    // Collapse ends pane-owned review work, not the dock's still-current metadata
    // owner. Its held result may warm the shared store, never a private SWR payload.
    await act(async () => { fixture.nextRelease?.(); });
    const warm = store.getSnapshot();
    expect(warm.observations).toHaveLength(2);
    expect(within(screen.getByRole("complementary", { name: "Floating panel shortcuts" })).getByTitle("At least 2 active jobs; coverage incomplete")).toBeInTheDocument();
    expect(readSWRCache(`swarm:${context.repo}`)).toBeUndefined();
    view.rerender(owned(<>{dock}<RightPane {...baseProps} /></>));
    expect(store.getSnapshot()).toBe(warm);
    expect(screen.getAllByTitle("At least 2 active jobs; coverage incomplete")).toHaveLength(2);
    await tick();
    expect(fixture.calls).toHaveLength(before);
    await tick(8000);
    expect(fixture.calls).toHaveLength(before + 4);
    expectBoundedActivity(fixture);
  });

  it("ignores an in-flight pane review response after collapse while the dock remains live", async () => {
    let resolveOld: (value: Awaited<ReturnType<typeof api.getReviews>>) => void = () => {};
    vi.mocked(api.getReviews).mockResolvedValueOnce([]).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
    const view = render(owned(<>{dock}<RightPane {...baseProps} /></>));
    await tick();
    view.rerender(owned(<>{dock}<RightPane {...baseProps} visible={false} /></>));
    await tick();
    await act(async () => {
      resolveOld([{ id: "old", job_id: "old", objective: "old", files: [], created_at: 0 }]);
    });
    view.rerender(owned(<>{dock}<RightPane {...baseProps} /></>));
    expect(screen.queryByText("1")).toBeNull();
    await tick();
    expect(api.getReviews).toHaveBeenCalledTimes(3);
    expect(screen.queryByText("1")).toBeNull();
    expectBoundedActivity(fixture);
  });

  it("stops new metadata requests while document-hidden and after owner disposal", async () => {
    const mounted = render(owned(<>{dock}<RightPane {...baseProps} /></>));
    await tick(8000);
    const before = fixture.calls.length;
    vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await tick(10000);
    expect(fixture.calls).toHaveLength(before);
    vi.spyOn(document, "hidden", "get").mockReturnValue(false);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await tick();
    expect(fixture.calls).toHaveLength(before + 1);
    mounted.unmount();
    const stopped = fixture.calls.length;
    await tick(10000);
    expect(fixture.calls).toHaveLength(stopped);
    expectBoundedActivity(fixture);
  });

  for (const surface of ["pane", "dock"]) {
    it(`${surface} keeps a newer review count when an older refresh resolves last`, async () => {
      let resolveOld: (value: Awaited<ReturnType<typeof api.getReviews>>) => void = () => {};
      vi.mocked(api.getReviews).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }));
      render(owned(surface === "pane" ? <RightPane {...baseProps} /> : dock));
      await tick();
      act(() => { window.dispatchEvent(new Event("harness-reviews-refresh")); });
      await tick();
      expect(api.getReviews).toHaveBeenCalledTimes(2);
      await act(async () => {
        resolveOld([{ id: "old", job_id: "old", objective: "old", files: [], created_at: 0 }]);
      });
      expect(screen.queryByText("1")).toBeNull();
      expectBoundedActivity(fixture);
    });
    for (const change of ["session", "repo"]) {
      it(`${surface} rejects late ${change} responses before cache and badge writes`, async () => {
        let rejectReviews: (reason: Error) => void = () => {};
        vi.mocked(api.getReviews).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectReviews = reject; }));
        const content = surface === "pane" ? <RightPane {...baseProps} /> : dock;
        const mounted = render(owned(content));
        await tick();
        fixture.holdNext = true;
        await tick(2000);
        expect(fixture.nextRelease).not.toBeNull();
        const oldEpoch = store.getSnapshot().contextEpoch;
        const switchTarget = (session: string, repo: string) => {
          act(() => {
            fixture.switchTarget(session, repo);
            window.dispatchEvent(change === "repo"
              ? new CustomEvent("harness-project-selected", { detail: repo })
              : new CustomEvent("harness-session-changed", { detail: { sessionId: session } }));
          });
          mounted.rerender(owned(content));
        };
        switchTarget(change === "session" ? "next" : context.session_id, change === "repo" ? "/next" : context.repo);
        await tick();
        switchTarget(context.session_id, context.repo);
        await tick();
        expect(api.getReviews).toHaveBeenCalledTimes(3);
        expect(store.getSnapshot().contextEpoch).toBeGreaterThan(oldEpoch);
        await act(async () => {
          fixture.nextRelease?.();
          rejectReviews(new Error("obsolete failure"));
        });
        expect(screen.queryByTitle(/At least .*active jobs/)).toBeNull();
        expect(screen.queryByTestId("reviews-load-error")).toBeNull();
        expect(store.getSnapshot().observations).toEqual([]);
        expect(store.getSnapshot().local.observations).toEqual([]);
        expect(readSWRCache(`swarm:${context.repo}`)).toBeUndefined();
        expect(readSWRCache("swarm:/next")).toBeUndefined();
        await tick(4000);
        expect(screen.getByTitle(/At least .*active jobs; coverage incomplete/)).toBeInTheDocument();
        expectBoundedActivity(fixture);
      });
    }
  }
});


it("absorbs shell-edge growth in the leftmost column and keeps mounted card state", () => {
  localStorage.clear();
  seedBoardTabOrder(["state", "economics"]);
  localStorage.setItem("pmharness.board.columns.v1", '[["state"],["economics"]]');
  const layouts = '{"state":{"columnSpan":7,"customized":true},"economics":{"columnSpan":5,"customized":true}}';
  localStorage.setItem("pmharness.board.cardLayouts.v1", layouts);
  let notifyResize = () => {};
  const rectSpy = vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 700, 600));
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { notifyResize = callback; }
    observe() {}
    disconnect() {}
  });
  try {
    const view = render(<RightPane {...baseProps} />);
    const card = screen.getByRole("region", { name: "Economics panel" });
    fireEvent.change(screen.getByRole("combobox", { name: "Economics ownership" }), { target: { value: "conversation" } });
    card.scrollTop = 37;
    rectSpy.mockReturnValue(new DOMRect(0, 0, 900, 600));
    act(() => notifyResize());
    const resized = JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}");
    expect(resized.state.columnSpan / 12 * 900).toBeCloseTo(7 / 12 * 700);
    expect(resized.economics.columnSpan / 12 * 900).toBeCloseTo(900 - 7 / 12 * 700);
    view.rerender(<RightPane {...baseProps} visible={false} />);
    view.rerender(<RightPane {...baseProps} visible />);
    expect(screen.getByRole("region", { name: "Economics panel" })).toBe(card);
    expect(card.scrollTop).toBe(37);
    expect(screen.getByRole("combobox", { name: "Economics ownership" })).toHaveValue("conversation");
    rectSpy.mockReturnValue(new DOMRect(0, 0, 700, 600));
    act(() => notifyResize());
    const restored = JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}");
    expect(restored.state.columnSpan).toBeCloseTo(7);
    expect(restored.economics.columnSpan).toBeCloseTo(5);
  } finally {
    rectSpy.mockRestore();
    vi.unstubAllGlobals();
  }
});

it("defers saved card contents until Panels has first been shown", () => {
  localStorage.clear();
  seedBoardTabOrder(["state", "economics"]);
  const view = render(<RightPane {...baseProps} visible={false} />);
  expect(view.container.querySelector('[aria-label="State panel"]')).toBeNull();
  view.rerender(<RightPane {...baseProps} visible />);
  const card = screen.getByRole("region", { name: "State panel" });
  view.rerender(<RightPane {...baseProps} visible={false} />);
  view.rerender(<RightPane {...baseProps} visible />);
  expect(screen.getByRole("region", { name: "State panel" })).toBe(card);
});

it("keeps panel state and focus through measured wide/narrow/wide widths", () => {
  localStorage.clear();
  seedBoardTabOrder(["economics", "browser"]);
  let width = 900;
  let notify = () => {};
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { notify = callback; }
    observe() {}
    disconnect() {}
  });
  const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, width, 400));
  const view = render(<RightPane {...baseProps} />);
  const input = screen.getByRole("combobox", { name: "Economics ownership" });
  fireEvent.change(input, { target: { value: "conversation" } });
  input.focus();
  const card = screen.getByRole("region", { name: "Economics panel" });
  for (const next of [480, 360, 640, 900]) {
    width = next;
    act(() => notify());
    expect(screen.getByRole("region", { name: "Economics panel" })).toBe(card);
    expect(screen.getByRole("combobox", { name: "Economics ownership" })).toBe(input);
    expect(input).toHaveValue("conversation");
    expect(document.activeElement).toBe(input);
  }
  view.unmount();
  rect.mockRestore();
  vi.unstubAllGlobals();
});

it("accepts a reorder drop on the panel body", () => {
  localStorage.clear();
  seedBoardTabOrder(["economics", "browser"]);
  render(<RightPane {...baseProps} />);
  const dataTransfer = { effectAllowed: "", setData: vi.fn(), getData: vi.fn(() => "browser") };
  fireEvent.dragStart(screen.getByRole("button", { name: "Drag Browser panel" }), { dataTransfer });
  fireEvent.drop(screen.getByRole("combobox", { name: "Economics ownership" }), { dataTransfer });
  expectCardGridPlacement("Browser", "1", "1");
  expectCardGridPlacement("Economics", "1", "2");
});


describe("RightPane pointer resize scheduling", () => {
  const captureMethods = ["setPointerCapture", "hasPointerCapture", "releasePointerCapture"];
  const originalCapture = captureMethods.map(name => Object.getOwnPropertyDescriptor(Element.prototype, name));
  beforeEach(() => {
    localStorage.clear();
    resetSettingsOverlay();
    seedBoardTabOrder();
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 1200, 600));
    vi.stubGlobal("PointerEvent", class extends MouseEvent {
      readonly pointerId: number;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerId = init.pointerId ?? 1;
      }
    });
    let captured = false;
    Object.defineProperty(Element.prototype, "setPointerCapture", { configurable: true, value: () => { captured = true; } });
    Object.defineProperty(Element.prototype, "hasPointerCapture", { configurable: true, value: () => captured });
    Object.defineProperty(Element.prototype, "releasePointerCapture", { configurable: true, value: () => { captured = false; } });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    captureMethods.forEach((name, index) => {
      const original = originalCapture[index];
      if (original) Object.defineProperty(Element.prototype, name, original);
      else Reflect.deleteProperty(Element.prototype, name);
    });
  });

  for (const axis of ["column", "row"]) {
    it(`measures ${axis} event bursts`, () => {
      if (axis === "column") localStorage.setItem("pmharness.board.columns.v1", '[["state"],["terminal"]]');
      const frames = new Map<number, FrameRequestCallback>();
      let frameId = 0;
      vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++frameId, callback); return frameId; });
      vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
      const frame = () => act(() => { const batch = [...frames.values()]; frames.clear(); batch.forEach(callback => callback(0)); });
      let commits = 0;
      render(<Profiler id="resize" onRender={() => { commits += 1; }}><RightPane {...baseProps} /></Profiler>);
      const handle = screen.getByRole("separator", { name: axis === "column" ? "Resize tool columns" : "Resize stacked panel height" });
      const writes = vi.spyOn(Storage.prototype, "setItem");
      commits = 0;
      fireEvent.pointerDown(handle, { button: 0, clientX: 600, clientY: 300 });
      // Separate events, ten frames; every coordinate reaches the same clamp.
      for (let burst = 0; burst < 10; burst += 1) {
        for (let index = 0; index < 24; index += 1) fireEvent.pointerMove(handle, { clientX: -1000 - index, clientY: 2000 + index });
        frame();
      }
      const during = { writes: writes.mock.calls.length, commits };
      fireEvent.pointerUp(handle, { clientX: 500, clientY: 420 });
      process.stdout.write(JSON.stringify({ axis, events: 240, frames: 10, during, total: { writes: writes.mock.calls.length, commits } }) + "\n");
      expect(during).toEqual({ writes: 0, commits: 1 });
      expect(writes).toHaveBeenCalledTimes(1);
      expect(commits).toBe(2);
      expect(frames.size).toBe(0);
      if (axis === "column") {
        expect(JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}").state.columnSpan).toBe(7);
      } else {
        expect(JSON.parse(localStorage.getItem("pmharness.board.stackFractions.v2") || "{}")["state|terminal"][0]).toBeCloseTo(0.7);
      }
    });
  }
  for (const axis of ["column", "row"]) {
    for (const completion of ["pointerup", "pointercancel", "lostpointercapture", "unmount"]) {
      it(`${axis} flushes final geometry on ${completion} and leaves no gesture work`, () => {
        if (axis === "column") localStorage.setItem("pmharness.board.columns.v1", '[["state"],["terminal"]]');
        const frames = new Map<number, FrameRequestCallback>();
        let id = 0;
        vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++id, callback); return id; });
        vi.stubGlobal("cancelAnimationFrame", (frame: number) => frames.delete(frame));
        const view = render(<RightPane {...baseProps} />);
        const handle = screen.getByRole("separator", { name: axis === "column" ? "Resize tool columns" : "Resize stacked panel height" });
        const writes = vi.spyOn(Storage.prototype, "setItem");
        fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 600, clientY: 300 });
        fireEvent.pointerMove(handle, { pointerId: 1, clientX: 500, clientY: 420 });
        fireEvent.pointerUp(handle, { pointerId: 2, clientX: 0, clientY: 0 });
        expect(writes).not.toHaveBeenCalled();
        expect(frames.size).toBe(1);
        if (completion === "unmount") view.unmount();
        else {
          if (completion === "lostpointercapture") handle.releasePointerCapture(1);
          fireEvent(handle, new PointerEvent(completion, { pointerId: 1, clientX: 400, clientY: 480 }));
        }
        expect(frames.size).toBe(0);
        expect(writes).toHaveBeenCalledTimes(1);
        expect(document.body.style.cursor).toBe("");
        expect(document.body.style.userSelect).toBe("");
        expect(document.body).not.toHaveClass("is-col-resizing", "is-row-resizing");
        if (axis === "column") {
          const saved = JSON.parse(localStorage.getItem("pmharness.board.cardLayouts.v1") || "{}");
          expect(saved.state.columnSpan).toBe(completion === "pointerup" ? 8 : 7);
          expect(saved.terminal.columnSpan).toBe(completion === "pointerup" ? 4 : 5);
        } else {
          const saved = JSON.parse(localStorage.getItem("pmharness.board.stackFractions.v2") || "{}")["state|terminal"];
          expect(saved[0]).toBeCloseTo(completion === "pointerup" ? 0.8 : 0.7);
          expect(saved[1]).toBeCloseTo(completion === "pointerup" ? 0.2 : 0.3);
        }
        fireEvent.pointerMove(handle, { clientX: 0, clientY: 0 });
        fireEvent(handle, new PointerEvent("lostpointercapture", { pointerId: 1 }));
        expect(frames.size).toBe(0);
        expect(writes).toHaveBeenCalledTimes(1);
      });
    }
  }

});
