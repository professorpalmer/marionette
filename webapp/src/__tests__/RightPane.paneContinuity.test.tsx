import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import RightPane from "../components/RightPane";

vi.mock("../lib/api", () => ({
  api: {
    getReviews: vi.fn().mockResolvedValue([]),
    swarmLive: vi.fn().mockResolvedValue({ jobs: [] }),
  },
}));

const life = vi.hoisted(() => ({ swarm: { mounts: 0, unmounts: 0 }, state: { mounts: 0, unmounts: 0 } }));

vi.mock("../components/SwarmPane", async () => {
  const { useEffect } = await import("react");
  return {
    default: ({ enabled }: { enabled?: boolean }) => {
      useEffect(() => {
        life.swarm.mounts += 1;
        return () => { life.swarm.unmounts += 1; };
      }, []);
      return <div data-testid="swarm-pane" data-enabled={String(!!enabled)} />;
    },
  };
});
vi.mock("../components/StatePane", async () => {
  const { useEffect } = await import("react");
  return {
    default: ({ networkEnabled }: { networkEnabled?: boolean }) => {
      useEffect(() => {
        life.state.mounts += 1;
        return () => { life.state.unmounts += 1; };
      }, []);
      return <div data-testid="state-pane" data-enabled={String(!!networkEnabled)} />;
    },
  };
});
vi.mock("../components/BrowserPane", () => ({ default: () => <div /> }));
vi.mock("../components/FileTree", () => ({ default: () => <div /> }));
vi.mock("../components/SourceControl", () => ({ default: () => <div /> }));
vi.mock("../components/WorktreesPane", () => ({ default: () => <div /> }));
vi.mock("../components/SettingsShell", () => ({ default: () => <div /> }));
vi.mock("../components/TerminalPane", () => ({ default: () => <div data-testid="terminal-pane" /> }));
vi.mock("../components/CheckpointsPane", () => ({ default: () => <div /> }));
vi.mock("../components/DiffReviewPane", () => ({ default: () => <div /> }));
vi.mock("../components/EconomicsPane", () => ({ default: () => <div /> }));
vi.mock("../components/ErrorBoundary", () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

const baseProps = { visible: true, artifacts: [], onOpenWizard: vi.fn() };

describe("RightPane keeps Jobs and State panes mounted across card close and reopen", () => {
  beforeEach(() => {
    localStorage.clear();
    Element.prototype.scrollIntoView = vi.fn();
    localStorage.setItem("pmharness.board.openCards", JSON.stringify(["state", "swarm"]));
    life.swarm = { mounts: 0, unmounts: 0 };
    life.state = { mounts: 0, unmounts: 0 };
  });

  it.each([
    ["swarm", "Jobs", "swarm-pane", "swarm-pane-slot"],
    ["state", "State", "state-pane", "state-pane-slot"],
  ] as const)("closing and reopening %s never remounts the pane", (tab, label, paneId, parkId) => {
    const { rerender } = render(<RightPane {...baseProps} />);
    const card = screen.getByRole("region", { name: `${label} panel` });
    expect(within(card).getByTestId(paneId).dataset.enabled).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: `Close ${label} panel` }));
    const parked = within(screen.getByTestId(parkId)).getByTestId(paneId);
    expect(parked.dataset.enabled).toBe("false");

    rerender(<RightPane {...baseProps} initialTab={tab} />);
    const reopened = screen.getByRole("region", { name: `${label} panel` });
    expect(within(reopened).getByTestId(paneId).dataset.enabled).toBe("true");
    expect(screen.getAllByTestId(paneId)).toHaveLength(1);
    expect(life[tab].mounts).toBe(1);
    expect(life[tab].unmounts).toBe(0);
  });
});
