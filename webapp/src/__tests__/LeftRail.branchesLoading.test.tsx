import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import LeftRail from "../components/LeftRail";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/api", () => ({
  api: {
    getWorkspace: vi.fn().mockResolvedValue({
      repo: "/workspace", branch: "main", is_git: true, head_unborn: false,
      codegraph_status: "ready", recents: [], home: "/home",
    }),
    workspaces: vi.fn(),
    sessions: vi.fn().mockResolvedValue([{ id: "session-1", title: "Current", active: true, repo: "/workspace" }]),
    jobs: vi.fn().mockResolvedValue([]),
  },
}));
vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({ useOperationalDiagnostic: () => null }));

beforeEach(() => {
  localStorage.clear();
  clearSWRCache();
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("does not say No branches while the first branch read is pending", async () => {
  // Rig L0350: after a reload the rail said "No branches" for about 100 ms.
  vi.mocked(api.workspaces).mockImplementation(() => new Promise(() => {}));
  render(<LeftRail jobsRefresh={0} />);
  await waitFor(() => expect(api.workspaces).toHaveBeenCalled());
  expect(screen.getByText("Branches")).toBeInTheDocument();
  expect(screen.queryByText("No branches")).toBeNull();
});

it("says No branches when the read returns none", async () => {
  vi.mocked(api.workspaces).mockResolvedValue([]);
  render(<LeftRail jobsRefresh={0} />);
  expect(await screen.findByText("No branches")).toBeInTheDocument();
});
