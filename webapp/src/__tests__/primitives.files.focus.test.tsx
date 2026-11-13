import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import FileEditorPane from "../components/FileEditorPane";
import FileTree from "../components/FileTree";
import { SessionFork } from "../components/SessionFork";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn().mockResolvedValue({ repo: "/repo" }),
    getWorkspaceFiles: vi.fn().mockResolvedValue({ files: ["alpha.ts"] }),
    mkdir: vi.fn().mockResolvedValue({ ok: true }),
    writeFile: vi.fn().mockResolvedValue({ ok: true }),
    renameFile: vi.fn(),
    deleteFile: vi.fn(),
    readFile: vi.fn().mockResolvedValue({ ok: true, content: "a\n" }),
    fileRawUrl: vi.fn((p: string) => `raw:${p}`),
    getReviews: vi.fn().mockResolvedValue([]),
    previewSessionFork: vi.fn().mockResolvedValue({ revision: "r", boundaries: [{ event_id: 1, role: "user", label: "Hi" }] }),
    forkSession: vi.fn(),
  },
}));

vi.mock("@uiw/react-codemirror", () => ({
  default: function MockCodeMirror({ value }: { value?: string }) {
    return <pre data-testid="cm-value">{value || ""}</pre>;
  },
}));

afterEach(() => { cleanup(); });

/** Focus the guard after the popup. Base UI sends a Tab past the last control back inside. */
function tabPastEnd(popup: HTMLElement) {
  const guards = [...document.querySelectorAll<HTMLElement>("[data-base-ui-focus-guard]")]
    .filter((guard) => popup.compareDocumentPosition(guard) & Node.DOCUMENT_POSITION_FOLLOWING);
  const guard = guards[0];
  expect(guard).toBeTruthy();
  guard.focus();
}

function ForkHarness() {
  const [open, setOpen] = useState(false);
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  return <>
    <button type="button" onClick={(event) => { setTrigger(event.currentTarget); setOpen(true); }}>Open fork</button>
    <button type="button">Outside</button>
    <SessionFork session={{ id: "s", title: "Source" }} sessions={[]} onSelect={() => {}} onCreated={() => {}}
      open={open} returnFocus={trigger} onClose={() => setOpen(false)} />
  </>;
}

describe("fork, inspect and files overlays", () => {
  it("SessionFork modal takes focus, keeps Tab inside, closes on Escape and returns focus", async () => {
    render(<ForkHarness />);
    const opener = screen.getByRole("button", { name: "Open fork" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Fork Source" });
    const close = within(dialog).getByRole("button", { name: "Close Fork Source" });
    await waitFor(() => expect(close).toHaveFocus());
    await within(dialog).findByLabelText("Fork after saved message");
    tabPastEnd(dialog);
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    expect(screen.getByRole("button", { name: "Outside", hidden: true })).not.toHaveFocus();
    fireEvent.keyDown(document.activeElement ?? dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("FileTree context menu takes focus, closes on Escape and returns focus", async () => {
    render(<FileTree />);
    const row = await screen.findByText("alpha.ts");
    const refresh = screen.getByTitle("Refresh file tree");
    refresh.focus();
    fireEvent.contextMenu(row, { clientX: 40, clientY: 60 });
    const menu = await screen.findByRole("menu", { name: "File actions" });
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    expect(within(menu).getByRole("menuitem", { name: "Open" })).toBeTruthy();
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(refresh).toHaveFocus());
  });

  it("FileTree name prompt focuses the input, keeps Tab inside, submits on Enter and closes on Escape", async () => {
    render(<FileTree />);
    const row = await screen.findByText("alpha.ts");
    fireEvent.contextMenu(row, { clientX: 40, clientY: 60 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "New Folder…" }));
    const dialog = await screen.findByRole("dialog", { name: "New folder" });
    const input = within(dialog).getByPlaceholderText("folder-name");
    await waitFor(() => expect(input).toHaveFocus());
    tabPastEnd(dialog);
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    fireEvent.keyDown(document.activeElement ?? dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.mkdir).not.toHaveBeenCalled();

    fireEvent.contextMenu(row, { clientX: 40, clientY: 60 });
    fireEvent.click(await screen.findByRole("menuitem", { name: "New Folder…" }));
    const again = await screen.findByPlaceholderText("folder-name");
    await waitFor(() => expect(again).toHaveFocus());
    fireEvent.change(again, { target: { value: "docs" } });
    fireEvent.keyDown(again, { key: "Enter" });
    await waitFor(() => expect(api.mkdir).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("FileEditorPane path menu takes focus, closes on Escape and returns focus", async () => {
    render(<>
      <button type="button">Before editor</button>
      <FileEditorPane path="src/a.ts" onClose={() => {}} onDirtyChange={() => {}} />
    </>);
    await screen.findByTestId("cm-value");
    const before = screen.getByRole("button", { name: "Before editor" });
    before.focus();
    fireEvent.contextMenu(screen.getByTitle("src/a.ts"), { clientX: 20, clientY: 20 });
    const menu = await screen.findByRole("menu", { name: "Path actions" });
    await waitFor(() => expect(menu.contains(document.activeElement)).toBe(true));
    expect(within(menu).getAllByRole("menuitem")).toHaveLength(1);
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(before).toHaveFocus());
  });
});
