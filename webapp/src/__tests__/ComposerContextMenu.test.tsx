import { act, createRef } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ComposerContextMenu from "../components/conversation/ComposerContextMenu";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete (window as any).harnessIPC;
  document.body.innerHTML = "";
});

describe("ComposerContextMenu", () => {
  it("renders Hermes-style edit actions for the composer gesture only", async () => {
    let openMenu: ((payload: {
      x: number;
      y: number;
      misspelledWord: string;
      suggestions: string[];
    }) => void) | undefined;
    const contextMenuEdit = vi.fn().mockResolvedValue(undefined);
    const contextMenuSpellcheck = vi.fn().mockResolvedValue(undefined);
    const contextMenuNative = vi.fn().mockResolvedValue(undefined);
    (window as any).harnessIPC = {
      onContextMenuOpen: (callback: typeof openMenu) => {
        openMenu = callback;
        return () => { openMenu = undefined; };
      },
      contextMenuEdit,
      contextMenuSpellcheck,
      contextMenuNative,
    };
    const textarea = document.createElement("textarea");
    textarea.value = "ostencibly";
    textarea.setSelectionRange(0, 10);
    document.body.appendChild(textarea);
    const textareaRef = createRef<HTMLTextAreaElement>();
    (textareaRef as any).current = textarea;

    vi.stubGlobal("innerHeight", 600);
    render(<ComposerContextMenu textareaRef={textareaRef} />);
    await waitFor(() => expect(openMenu).toBeTypeOf("function"));
    act(() => openMenu?.({ x: 20, y: 20, misspelledWord: "elsewhere", suggestions: ["elsewhere"] }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(contextMenuNative).toHaveBeenCalledTimes(1);

    fireEvent.contextMenu(textarea, { clientX: 120, clientY: 350 });
    act(() => openMenu?.({
      x: 121,
      y: 351,
      misspelledWord: "ostencibly",
      suggestions: ["ostensibly"],
    }));

    const menu = await screen.findByRole("menu");
    expect(menu.className).toContain("bg-panel");
    expect(menu.className).not.toContain("backdrop-blur");
    expect(menu.style.top).toBe("351px");
    expect(screen.getByText("Cut")).toBeTruthy();
    expect(screen.getByText("Copy")).toBeTruthy();
    expect(screen.getByText("Paste")).toBeTruthy();
    expect(screen.getByText("Select all")).toBeTruthy();

    fireEvent.click(await screen.findByText("ostensibly"));

    await waitFor(() => expect(contextMenuSpellcheck).toHaveBeenCalledWith({
      kind: "replace",
      word: "ostensibly",
    }));

    fireEvent.contextMenu(textarea, { clientX: 120, clientY: 180 });
    act(() => openMenu?.({ x: 120, y: 180, misspelledWord: "ostencibly", suggestions: ["ostensibly"] }));
    fireEvent.click(await screen.findByText("Add to dictionary"));
    await waitFor(() => expect(contextMenuSpellcheck).toHaveBeenCalledWith({
      kind: "add",
      word: "ostencibly",
    }));

    fireEvent.contextMenu(textarea, { clientX: 120, clientY: 180 });
    act(() => openMenu?.({ x: 120, y: 180, misspelledWord: "", suggestions: [] }));
    expect(contextMenuNative).toHaveBeenCalledTimes(1);
    fireEvent.click(await screen.findByText("Copy"));
    await waitFor(() => expect(contextMenuEdit).toHaveBeenCalledWith("copy"));
  });
});
