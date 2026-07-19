import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RightPane from "../components/RightPane";

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
vi.mock("../components/TerminalPane", () => ({ default: () => <div /> }));
vi.mock("../components/CheckpointsPane", () => ({ default: () => <div /> }));
vi.mock("../components/DiffReviewPane", () => ({ default: () => <div /> }));
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

  it("places collapse in the tab-bar gutter and invokes onCollapse", () => {
    render(<RightPane {...baseProps} />);

    const collapseBtns = screen.getAllByTestId("panel-collapse-btn");
    expect(collapseBtns).toHaveLength(1);

    const tabBar = collapseBtns[0].closest(".border-b");
    expect(tabBar).toBeTruthy();
    const gutter = collapseBtns[0].parentElement;
    expect(gutter?.className).toMatch(/flex-1/);

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
      const gutter = btn.parentElement;
      expect(gutter?.className).toMatch(/flex-1/);
    }

    fireEvent.click(collapseBtns[1]);
    expect(baseProps.onCollapse).toHaveBeenCalledTimes(1);
  });
});
