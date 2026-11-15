import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ArchivedChatsPane from "../components/ArchivedChatsPane";
import { api, type Session } from "../lib/api";
import { SESSION_UNARCHIVED_EVENT, SWITCH_SESSION_EVENT } from "../lib/sessionNav";

vi.mock("../lib/api", () => ({ api: { sessionsBank: vi.fn(), archiveSession: vi.fn() } }));

const live: Session = { id: "live", title: "Live chat", created: 3, workspace_root: "/w/site" };
const older: Session = { id: "older", title: "Older idea", created: 1, archived: true, workspace_root: "/w/marionette" };
const newer: Session = { id: "newer", title: "Newer idea", created: 2, archived: true, workspace_root: "/w/marionette" };

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.sessionsBank).mockResolvedValue([live, older, newer]);
  vi.mocked(api.archiveSession).mockResolvedValue({ ok: true });
});
afterEach(cleanup);

it("lists only archived chats, newest first", async () => {
  render(<ArchivedChatsPane onOpenSession={() => {}} />);
  const rows = await screen.findAllByTestId("archived-chat-row");
  expect(rows.map((row) => row.textContent?.includes("Newer idea") ? "newer" : "older")).toEqual(["newer", "older"]);
  expect(screen.queryByText("Live chat")).toBeNull();
  expect(screen.getByRole("heading", { name: "Archived chats" })).toBeInTheDocument();
});

it("says so when no chat is archived", async () => {
  vi.mocked(api.sessionsBank).mockResolvedValue([live]);
  render(<ArchivedChatsPane onOpenSession={() => {}} />);
  expect(await screen.findByTestId("archived-chats-empty")).toHaveTextContent("No archived chats.");
  expect(api.sessionsBank).toHaveBeenCalledWith({ limit: 500 });
});

it("opens a chat through the rail switch and closes Settings", async () => {
  const switched = vi.fn();
  const onOpen = vi.fn();
  window.addEventListener(SWITCH_SESSION_EVENT, switched);
  render(<ArchivedChatsPane onOpenSession={onOpen} />);
  await screen.findAllByTestId("archived-chat-row");
  fireEvent.click(screen.getByRole("button", { name: "Newer idea" }));
  window.removeEventListener(SWITCH_SESSION_EVENT, switched);
  expect((switched.mock.calls[0][0] as CustomEvent<{ sessionId: string }>).detail.sessionId).toBe("newer");
  expect(onOpen).toHaveBeenCalledOnce();
});

it("unarchives a chat, drops its row and tells the rail", async () => {
  const unarchived = vi.fn();
  window.addEventListener(SESSION_UNARCHIVED_EVENT, unarchived);
  render(<ArchivedChatsPane onOpenSession={() => {}} />);
  await screen.findAllByTestId("archived-chat-row");
  await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Unarchive" })[0]); });
  window.removeEventListener(SESSION_UNARCHIVED_EVENT, unarchived);
  expect(api.archiveSession).toHaveBeenCalledWith("newer", false);
  expect(screen.getAllByTestId("archived-chat-row")).toHaveLength(1);
  expect(unarchived).toHaveBeenCalledOnce();
});

it("keeps the row and says why when unarchive fails", async () => {
  vi.mocked(api.archiveSession).mockRejectedValue({ error: "Archive restore failed; session remains archived" });
  render(<ArchivedChatsPane onOpenSession={() => {}} />);
  await screen.findAllByTestId("archived-chat-row");
  await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Unarchive" })[0]); });
  expect(screen.getAllByTestId("archived-chat-row")).toHaveLength(2);
  expect(screen.getByText(/Archive restore failed/)).toBeInTheDocument();
});
