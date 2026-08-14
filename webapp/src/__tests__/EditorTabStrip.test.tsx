import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import EditorTabStrip, {
  type OpenEditorTab,
} from "../components/conversation/EditorTabStrip";

vi.mock("../lib/transport", async () => {
  const actual = await vi.importActual<typeof import("../lib/transport")>(
    "../lib/transport",
  );
  return {
    ...actual,
    revealWorkspacePath: vi.fn().mockResolvedValue({ ok: true }),
    toAbsoluteWorkspacePath: vi.fn((root: string, rel: string) => `${root}/${rel}`),
  };
});

afterEach(() => cleanup());

const tabs: OpenEditorTab[] = [
  { path: "src/a.ts", isDirty: true },
  { path: "docs/readme.md", isDirty: false },
];

function renderStrip(overrides: Partial<Parameters<typeof EditorTabStrip>[0]> = {}) {
  const onSelectTab = vi.fn();
  const onCloseTab = vi.fn();
  const onCloseOtherTabs = vi.fn();
  const onCloseAllTabs = vi.fn();
  const onOpenContextMenu = vi.fn();
  const onCloseContextMenu = vi.fn();

  render(
    <EditorTabStrip
      openTabs={tabs}
      activeTab="chat"
      tabContextMenu={null}
      repoRoot="/repo"
      onSelectTab={onSelectTab}
      onCloseTab={onCloseTab}
      onCloseOtherTabs={onCloseOtherTabs}
      onCloseAllTabs={onCloseAllTabs}
      onOpenContextMenu={onOpenContextMenu}
      onCloseContextMenu={onCloseContextMenu}
      {...overrides}
    />,
  );

  return {
    onSelectTab,
    onCloseTab,
    onCloseOtherTabs,
    onCloseAllTabs,
    onOpenContextMenu,
    onCloseContextMenu,
  };
}

describe("EditorTabStrip", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders chat plus open file tabs", () => {
    renderStrip();
    expect(screen.getByText("Chat")).toBeTruthy();
    expect(screen.getByText("a.ts")).toBeTruthy();
    expect(screen.getByText("readme.md")).toBeTruthy();
  });

  it("selects chat or a file tab on click", () => {
    const { onSelectTab } = renderStrip();

    fireEvent.click(screen.getByText("a.ts"));
    expect(onSelectTab).toHaveBeenCalledWith("src/a.ts");

    fireEvent.click(screen.getByText("Chat"));
    expect(onSelectTab).toHaveBeenCalledWith("chat");
  });

  it("fires onCloseTab when the tab close control is clicked", () => {
    const { onCloseTab } = renderStrip();
    const tabLabel = screen.getByText("a.ts");
    const tabRow = tabLabel.closest("div");
    expect(tabRow).toBeTruthy();
    const buttons = tabRow!.querySelectorAll("button");
    expect(buttons.length).toBeGreaterThanOrEqual(2);
    fireEvent.click(buttons[buttons.length - 1]);
    expect(onCloseTab).toHaveBeenCalledWith("src/a.ts");
  });

  it("shows a dirty indicator only on unsaved tabs", () => {
    renderStrip();
    const dirtyDot = screen.getByText("a.ts").parentElement?.querySelector(".bg-warn");
    expect(dirtyDot).toBeTruthy();
    const cleanDot = screen.getByText("readme.md").parentElement?.querySelector(".bg-warn");
    expect(cleanDot).toBeNull();
  });

  it("opens the context menu and routes close actions to callbacks", () => {
    const { onOpenContextMenu, onCloseTab, onCloseOtherTabs, onCloseAllTabs, onCloseContextMenu } =
      renderStrip({
        tabContextMenu: { x: 10, y: 20, path: "src/a.ts" },
      });

    expect(onOpenContextMenu).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("Close"));
    expect(onCloseContextMenu).toHaveBeenCalled();
    expect(onCloseTab).toHaveBeenCalledWith("src/a.ts");

    fireEvent.click(screen.getByText("Close others"));
    expect(onCloseOtherTabs).toHaveBeenCalledWith("src/a.ts");

    fireEvent.click(screen.getByText("Close all"));
    expect(onCloseAllTabs).toHaveBeenCalled();
  });

  it("calls onOpenContextMenu on right-click", () => {
    const { onOpenContextMenu } = renderStrip();
    const tabLabel = screen.getByText("readme.md");
    const tabRow = tabLabel.closest("div");
    expect(tabRow).toBeTruthy();
    fireEvent.contextMenu(tabRow!, { clientX: 42, clientY: 84 });
    expect(onOpenContextMenu).toHaveBeenCalledWith({
      x: 42,
      y: 84,
      path: "docs/readme.md",
    });
  });
});
