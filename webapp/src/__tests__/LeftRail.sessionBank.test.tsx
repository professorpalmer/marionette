import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
import { api, type Session } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({ useOperationalDiagnostic: () => null }));
const rows: Session[] = [
  { id: "active", title: "Active conversation", created: 1, active: true, repo: "/workspace" },
  { id: "older", title: "Older conversation", created: 2, repo: "/workspace" },
];
vi.mock("../lib/api", () => ({ api: {
  getWorkspace: vi.fn().mockResolvedValue({ repo: "/workspace", is_git: false, recents: [] }),
  sessions: vi.fn(), sessionsBank: vi.fn(), jobs: vi.fn().mockResolvedValue([]),
} }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("pmharness.leftRail.tab", "sessions");
  clearSWRCache();
  vi.mocked(api.sessions).mockResolvedValue(rows);
  vi.mocked(api.sessionsBank).mockResolvedValue(rows);
});

it("a failed background refetch keeps the session list", async () => {
  const { rerender } = render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole("button", { name: /Older conversation/ });

  vi.mocked(api.sessionsBank).mockRejectedValue(new Error("ECONNREFUSED"));
  rerender(<LeftRail jobsRefresh={1} />);
  await waitFor(() => expect(api.sessionsBank).toHaveBeenCalledTimes(2));

  expect(screen.getByRole("button", { name: /Older conversation/ })).toBeTruthy();
  expect(screen.queryByText("No sessions")).toBeNull();
});

it("a failed first read offers Retry instead of a false empty state", async () => {
  vi.mocked(api.sessionsBank).mockRejectedValue(new Error("ECONNREFUSED"));
  render(<LeftRail jobsRefresh={0} />);

  const retry = await screen.findByRole("button", { name: "Couldn't load sessions. Retry" });
  expect(screen.queryByText("No sessions")).toBeNull();

  vi.mocked(api.sessionsBank).mockResolvedValue(rows);
  fireEvent.click(retry);
  expect(await screen.findByRole("button", { name: /Older conversation/ })).toBeTruthy();
});

it("a background refetch does not spin the Recent header", async () => {
  const { container, rerender } = render(<LeftRail jobsRefresh={0} />);
  await screen.findByRole("button", { name: /Older conversation/ });
  await waitFor(() => expect(container.querySelector(".animate-spin")).toBeNull());

  vi.mocked(api.sessionsBank).mockReturnValue(new Promise(() => {}));
  rerender(<LeftRail jobsRefresh={1} />);
  await waitFor(() => expect(api.sessionsBank).toHaveBeenCalledTimes(2));

  expect(container.querySelector(".animate-spin")).toBeNull();
});
