import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import LeftRail from "../components/LeftRail";
import { api, type Session } from "../lib/api";
import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

vi.mock("../lib/usePolling", () => ({ usePolling: vi.fn() }));
vi.mock("../lib/useOperationalDiagnostic", () => ({ useOperationalDiagnostic: () => null }));
const sessions: Session[] = [
  { id: "active", title: "Active conversation", created: 1, active: true, repo: "/workspace" },
  { id: "target", title: "Other conversation", created: 2, repo: "/workspace" },
];
vi.mock("../lib/api", () => ({ api: {
  getWorkspace: vi.fn().mockResolvedValue({ repo: "/workspace", is_git: false, recents: ["/workspace"] }),
  sessions: vi.fn(), sessionsBank: vi.fn(), jobs: vi.fn().mockResolvedValue([]),
  previewSessionFork: vi.fn(), forkSession: vi.fn(), switchSession: vi.fn(),
} }));

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  localStorage.setItem("pmharness.leftRail.tab", "sessions");
  clearSWRCache();
  vi.mocked(api.sessions).mockResolvedValue(sessions);
  vi.mocked(api.sessionsBank).mockResolvedValue(sessions);
});

describe("LeftRail session menu focus", () => {
  it("moves focus into the menu, closes on Escape, and returns focus to the row", async () => {
    render(<LeftRail jobsRefresh={0} onSessionChange={vi.fn()} />);
    const row = await screen.findByRole("button", { name: /Other conversation/ });
    fireEvent.contextMenu(row, { clientX: 30, clientY: 50 });
    const menu = await screen.findByRole("menu", { name: "Other conversation actions" });
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(row).toHaveFocus());
  });

  it("keeps the menu open for the delete confirm and focuses No", async () => {
    render(<LeftRail jobsRefresh={0} onSessionChange={vi.fn()} />);
    const row = await screen.findByRole("button", { name: /Other conversation/ });
    fireEvent.contextMenu(row);
    fireEvent.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(screen.getByRole("menu", { name: "Other conversation actions" })).toBeInTheDocument();
    expect(screen.getByText("Delete?")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("menuitem", { name: "No" })).toHaveFocus());
    fireEvent.click(screen.getByRole("menuitem", { name: "No" }));
    expect(screen.getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
  });
});

describe("LeftRail project menu focus", () => {
  it("moves focus into the menu, closes on Escape, and returns focus into the project row", async () => {
    localStorage.setItem("pmharness.leftRail.tab", "projects");
    render(<LeftRail jobsRefresh={0} onSessionChange={vi.fn()} />);
    const chevron = await screen.findByRole("button", { name: "workspace sessions" });
    fireEvent.contextMenu(chevron, { clientX: 20, clientY: 40 });
    const menu = await screen.findByRole("menu", { name: "Project actions" });
    expect(screen.getByRole("menuitem", { name: "Remove from list" })).toBeInTheDocument();
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(chevron).toHaveFocus());
  });
});
