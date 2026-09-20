import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
import { api } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

// Same lightweight mock shape as LeftRail.layout.test.tsx: only the endpoints
// the rail touches on mount + session switch.
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
    workspaces: vi.fn().mockResolvedValue([{ name: "main", active: true, dirty: false }]),
    sessions: vi.fn().mockResolvedValue([
      { id: "session-a", title: "Active chat", active: true, repo: "/workspace" },
      { id: "session-b", title: "Other chat", active: false, repo: "/workspace" },
    ]),
    jobs: vi.fn().mockResolvedValue([]),
    createSession: vi.fn().mockResolvedValue({ id: "session-new" }),
    switchSession: vi.fn().mockResolvedValue({ ok: true, repo: "/workspace" }),
    sessionsBank: vi.fn().mockResolvedValue([]),
    interruptSession: vi.fn().mockResolvedValue({}),
  },
}));

vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({
  useOperationalDiagnostic: () => null,
}));

const switchSpy = vi.mocked(api.switchSession);

/**
 * End-to-end simulation of the phantom session switch at the component level:
 * a real LeftRail mount, a real click on a real session row button, and a spy
 * on the network boundary (api.switchSession). Clicking the row you are already
 * in must not fire POST /api/sessions/switch.
 */
describe("LeftRail phantom session switch", () => {
  beforeEach(() => {
    localStorage.clear();
    clearSWRCache();
    switchSpy.mockClear();
  });

  it("does NOT call the backend when clicking the already-active session", async () => {
    render(<LeftRail jobsRefresh={0} />);
    const activeRow = await screen.findByRole("button", { name: /Active chat/ });
    fireEvent.click(activeRow);
    // Give any stray async continuation a chance to fire before asserting.
    await new Promise((r) => setTimeout(r, 25));
    expect(switchSpy).not.toHaveBeenCalled();
  });

  it("still switches when clicking a genuinely different session", async () => {
    render(<LeftRail jobsRefresh={0} />);
    const otherRow = await screen.findByRole("button", { name: /Other chat/ });
    fireEvent.click(otherRow);
    await waitFor(() => expect(switchSpy).toHaveBeenCalledWith("session-b"));
  });
});
