import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
import { api } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getWorkspace: vi.fn().mockResolvedValue({
      repo: "/workspace",
      branch: "main",
      is_git: true,
      head_unborn: false,
      codegraph_status: "ready",
      recents: [],
      home: "/home",
    }),
    workspaces: vi.fn().mockResolvedValue([
      { name: "main", active: true, dirty: false },
    ]),
    sessions: vi.fn().mockResolvedValue([
      { id: "session-1", title: "Current", active: true, repo: "/workspace" },
    ]),
    jobs: vi.fn().mockResolvedValue([]),
    createSession: vi.fn().mockResolvedValue({ id: "session-new" }),
  },
}));

vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({
  useOperationalDiagnostic: () => null,
}));

describe("LeftRail branch layout", () => {
  beforeEach(() => {
    localStorage.clear();
    clearSWRCache();
  });

  it("gives the branches list a fixed height so the resize handle can move", async () => {
    const { container } = render(<LeftRail jobsRefresh={0} />);

    await waitFor(() => expect(api.workspaces).toHaveBeenCalled());
    await screen.findByRole("button", { name: "main", exact: true }, { timeout: 5000 });
    const branchList = container.querySelector<HTMLElement>("[data-slot=left-rail-branches-list]");
    const upperSections = container.querySelector<HTMLElement>("[data-slot=left-rail-upper-sections]");
    expect(screen.getByRole("separator", { name: "Resize branches list" })).toBeInTheDocument();
    expect(branchList?.style.height).not.toBe("");
    expect(branchList?.style.maxHeight).toBe("");
    expect(upperSections?.className.split(" ")).not.toContain("flex-1");
  });

  it("lists sessions, not jobs: the Jobs pane owns jobs", async () => {
    const { container } = render(<LeftRail jobsRefresh={0} />);
    await screen.findByRole("button", { name: "main", exact: true }, { timeout: 5000 });
    expect(container.querySelector("[data-slot=left-rail-jobs]")).toBeNull();
    expect(screen.queryByRole("button", { name: "Jobs" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry updates" })).toBeNull();
    expect(screen.queryByText(/Job metadata is available/)).toBeNull();
  });

  it("offers a plus on each project row to open a session in that dir", async () => {
    render(<LeftRail jobsRefresh={0} />);
    await waitFor(() => expect(api.getWorkspace).toHaveBeenCalled());
    const plusButtons = await screen.findAllByRole("button", { name: /New session in / });
    expect(plusButtons.length).toBeGreaterThanOrEqual(1);
    const workspacePlus = plusButtons.find((btn) => btn.getAttribute("aria-label")?.includes("workspace"));
    expect(workspacePlus).toBeTruthy();
    workspacePlus?.click();
    await waitFor(() => expect(api.createSession).toHaveBeenCalled());
  });
});
