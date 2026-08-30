import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
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

  it("keeps branch resizing without reserving empty list height", async () => {
    const { container } = render(<LeftRail jobsRefresh={0} />);

    await screen.findByRole("button", { name: /main/ });
    const branchList = container.querySelector<HTMLElement>("[data-slot=left-rail-branches-list]");
    const upperSections = container.querySelector<HTMLElement>("[data-slot=left-rail-upper-sections]");
    expect(screen.getByRole("button", { name: "Jobs" })).toBeInTheDocument();
    expect(screen.getByRole("separator", { name: "Resize branches list" })).toBeInTheDocument();
    expect(branchList?.style.height).toBe("");
    expect(branchList?.style.maxHeight).not.toBe("");
    expect(upperSections?.className.split(" ")).not.toContain("flex-1");
  });
});
