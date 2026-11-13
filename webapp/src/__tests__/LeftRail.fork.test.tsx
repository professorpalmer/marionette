import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
import { api, type Session } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({ useOperationalDiagnostic: () => null }));
const sources: Session[] = [
  { id: "active", title: "Active conversation", created: 1, active: true, repo: "/workspace" },
  { id: "target", title: "Other conversation", created: 2, repo: "/workspace" },
];
vi.mock("../lib/api", () => ({ api: {
  getWorkspace: vi.fn().mockResolvedValue({ repo: "/workspace", is_git: false, recents: [] }),
  sessions: vi.fn(), sessionsBank: vi.fn(), jobs: vi.fn().mockResolvedValue([]),
  previewSessionFork: vi.fn(), forkSession: vi.fn(), switchSession: vi.fn(),
} }));
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("pmharness.leftRail.tab", "sessions");
  clearSWRCache();
  vi.mocked(api.sessions).mockResolvedValue(sources);
  vi.mocked(api.sessionsBank).mockResolvedValue(sources);
  vi.mocked(api.previewSessionFork).mockResolvedValue({ revision: "source-revision", boundaries: [
    { event_id: 1, role: "user", label: "Earlier" }, { event_id: 2, role: "assistant", label: "Later" },
  ] });
});

it("forks the actual context target and retains its selection/error/identity across dismissal", async () => {
  vi.mocked(api.forkSession).mockRejectedValue(new Error("Source changed. Refresh boundaries."));
  const onSessionChange = vi.fn();
  render(<LeftRail jobsRefresh={0} onSessionChange={onSessionChange} />);
  const target = await screen.findByRole("button", { name: /Other conversation/ });
  expect(screen.queryByRole("menuitem", { name: "Fork session" })).toBeNull();
  fireEvent.contextMenu(target, { clientX: 30, clientY: 50 });
  fireEvent.click(await screen.findByRole("menuitem", { name: "Fork session" }));
  const boundary = await screen.findByLabelText("Fork after saved message");
  expect(api.previewSessionFork).toHaveBeenCalledWith("target");
  expect(screen.getByRole("dialog", { name: "Fork Other conversation" })).toBeVisible();
  fireEvent.change(boundary, { target: { value: "1" } });
  fireEvent.click(screen.getByRole("button", { name: "Create fork" }));
  await screen.findByRole("alert");
  expect(api.forkSession).toHaveBeenCalledWith({ session_id: "target", event_id: 1,
    revision: "source-revision", request_id: expect.any(String) });
  fireEvent.click(screen.getByRole("button", { name: "Close Fork Other conversation" }));
  await waitFor(() => expect(target).toHaveFocus());
  fireEvent.contextMenu(target);
  fireEvent.click(await screen.findByRole("menuitem", { name: "Fork session" }));
  expect(screen.getByLabelText("Fork after saved message")).toHaveValue("1");
  expect(screen.getByRole("alert")).toHaveTextContent("Source changed");
  fireEvent.click(screen.getByRole("button", { name: "Create fork" }));
  await waitFor(() => expect(api.forkSession).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.forkSession).mock.calls[0]).toEqual(vi.mocked(api.forkSession).mock.calls[1]);
  expect(api.previewSessionFork).toHaveBeenCalledTimes(1);
  expect(api.switchSession).not.toHaveBeenCalled();
});
