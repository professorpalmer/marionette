import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import RightDock from "../components/RightDock";
import WorkspaceChip from "../components/conversation/WorkspaceChip";
import ComposerContextMenu from "../components/conversation/ComposerContextMenu";
import EditorTabStrip, { type TabContextMenuState } from "../components/conversation/EditorTabStrip";

vi.mock("../lib/api", () => ({
  api: {
    getReviews: vi.fn().mockResolvedValue([]),
    getWorkspace: vi.fn().mockResolvedValue({
      repo: "/work/alpha",
      branch: "main",
      recents: ["/work/beta"],
      home: "/home/me/.pmharness/home",
    }),
    openWorkspace: vi.fn().mockResolvedValue({ ok: true }),
  },
}));
vi.mock("../lib/useOperationalDiagnostic", () => ({ useOperationalDiagnostic: () => null }));

afterEach(() => {
  cleanup();
  delete (window as { harnessIPC?: unknown }).harnessIPC;
});

function expectFocusInside(menu: HTMLElement) {
  expect(menu.contains(document.activeElement)).toBe(true);
}

describe("menu focus: RightDock Add panel", () => {
  it("moves focus into the menu, closes on Escape, and returns focus to the + button", async () => {
    render(<RightDock onOpenTab={vi.fn()} onExpand={vi.fn()} onCollapse={vi.fn()} />);
    const trigger = screen.getByRole("button", { name: "Add panel" });
    trigger.focus();
    fireEvent.click(trigger);
    const menu = await screen.findByRole("menu", { name: "Add panel" });
    await waitFor(() => expectFocusInside(menu));
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Add panel" })).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe("menu focus: WorkspaceChip", () => {
  it("moves focus into the folder menu, closes on Escape, and returns focus to the chip", async () => {
    render(<WorkspaceChip />);
    const trigger = await screen.findByRole("button", { name: /alpha/ });
    trigger.focus();
    fireEvent.click(trigger);
    const menu = await screen.findByRole("menu", { name: "Workspace folders" });
    expect(screen.getByRole("menuitem", { name: /Open Folder/ })).toBeInTheDocument();
    await waitFor(() => expectFocusInside(menu));
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Workspace folders" })).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe("menu focus: ComposerContextMenu", () => {
  it("takes focus on IPC open, closes on Escape, and returns focus to the composer", async () => {
    let openMenu: ((payload: { x: number; y: number; misspelledWord: string; suggestions: string[] }) => void) | undefined;
    (window as { harnessIPC?: unknown }).harnessIPC = {
      onContextMenuOpen: (callback: typeof openMenu) => {
        openMenu = callback;
        return () => { openMenu = undefined; };
      },
      contextMenuNative: vi.fn(),
    };
    function Harness() {
      const ref = useRef<HTMLTextAreaElement>(null);
      return <>
        <textarea aria-label="Composer" defaultValue="hello" ref={ref} />
        <ComposerContextMenu textareaRef={ref} />
      </>;
    }
    render(<Harness />);
    const textarea = screen.getByRole("textbox", { name: "Composer" });
    await waitFor(() => expect(openMenu).toBeTypeOf("function"));
    textarea.focus();
    fireEvent.contextMenu(textarea, { clientX: 40, clientY: 50 });
    act(() => openMenu?.({ x: 40, y: 50, misspelledWord: "", suggestions: [] }));
    const menu = await screen.findByRole("menu", { name: "Composer menu" });
    await waitFor(() => expectFocusInside(menu));
    expect(screen.getByRole("menuitem", { name: /Cut/ })).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Composer menu" })).toBeNull());
    await waitFor(() => expect(textarea).toHaveFocus());
  });
});

describe("menu focus: EditorTabStrip context menu", () => {
  it("takes focus on right click, closes on Escape, and returns focus to the tab", async () => {
    function Harness() {
      const [menu, setMenu] = useState<TabContextMenuState | null>(null);
      return (
        <EditorTabStrip
          openTabs={[{ path: "src/a.ts", isDirty: false }]}
          activeTab="src/a.ts"
          tabContextMenu={menu}
          repoRoot="/repo"
          onSelectTab={vi.fn()}
          onCloseTab={vi.fn()}
          onCloseOtherTabs={vi.fn()}
          onCloseAllTabs={vi.fn()}
          onOpenContextMenu={setMenu}
          onCloseContextMenu={() => setMenu(null)}
        />
      );
    }
    render(<Harness />);
    const tab = screen.getByTitle("src/a.ts");
    tab.focus();
    fireEvent.contextMenu(tab, { clientX: 30, clientY: 12 });
    const menu = await screen.findByRole("menu", { name: "Editor tab menu" });
    await waitFor(() => expectFocusInside(menu));
    fireEvent.keyDown(document.activeElement ?? menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu", { name: "Editor tab menu" })).toBeNull());
    await waitFor(() => expect(tab).toHaveFocus());
  });
});
