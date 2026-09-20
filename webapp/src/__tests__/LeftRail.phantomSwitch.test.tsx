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

describe("LeftRail phantom session switch", () => {
  beforeEach(() => {
    localStorage.clear();
    clearSWRCache();
    switchSpy.mockClear();
  });

  it("does NOT call the backend when clicking the already-active session", async () => {
    const onSessionChange = vi.fn();
    render(<LeftRail jobsRefresh={0} onSessionChange={onSessionChange} />);
    const activeRow = await screen.findByRole("button", { name: /Active chat/ });
    onSessionChange.mockClear();
    switchSpy.mockClear();
    fireEvent.click(activeRow);
    await Promise.resolve();
    await Promise.resolve();
    expect(switchSpy).not.toHaveBeenCalled();
    expect(onSessionChange).not.toHaveBeenCalled();
  });

  it("still switches when clicking a genuinely different session", async () => {
    const onSessionChange = vi.fn();
    render(<LeftRail jobsRefresh={0} onSessionChange={onSessionChange} />);
    const otherRow = await screen.findByRole("button", { name: /Other chat/ });
    onSessionChange.mockClear();
    fireEvent.click(otherRow);
    await waitFor(() => expect(switchSpy).toHaveBeenCalledWith("session-b"));
    expect(onSessionChange).toHaveBeenCalledWith("session-b");
  });
});
