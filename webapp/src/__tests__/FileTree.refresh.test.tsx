import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FileTree from "../components/FileTree";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    config: vi.fn(),
    getWorkspaceFiles: vi.fn(),
    mkdir: vi.fn(),
    writeFile: vi.fn(),
    renameFile: vi.fn(),
    deleteFile: vi.fn(),
  },
}));

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(api.config).mockResolvedValue({ repo: "/a" } as never);
  vi.mocked(api.getWorkspaceFiles).mockResolvedValue({ files: ["alpha.ts"] } as never);
  vi.mocked(api.mkdir).mockResolvedValue({ ok: true } as never);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.clearAllMocks(); });

it("a folder created from the tree lists the workspace once, not twice", async () => {
  render(<FileTree />);
  await screen.findByText("alpha.ts");
  vi.mocked(api.getWorkspaceFiles).mockClear();
  fireEvent.contextMenu(screen.getByText("alpha.ts"));
  fireEvent.click(screen.getByText("New Folder…"));
  const input = screen.getByPlaceholderText("folder-name");
  fireEvent.change(input, { target: { value: "docs" } });
  await act(async () => {
    fireEvent.keyDown(input, { key: "Enter" });
    await vi.advanceTimersByTimeAsync(400);
  });
  expect(api.mkdir).toHaveBeenCalledTimes(1);
  expect(api.getWorkspaceFiles).toHaveBeenCalledTimes(1);
});

it("switching repos never paints the old repo's files when the new listing fails", async () => {
  render(<FileTree />);
  await screen.findByText("alpha.ts");
  vi.mocked(api.config).mockResolvedValue({ repo: "/b" } as never);
  vi.mocked(api.getWorkspaceFiles).mockRejectedValue(new Error("listing failed"));
  await act(async () => {
    window.dispatchEvent(new Event("harness-config-changed"));
    await vi.advanceTimersByTimeAsync(400);
  });
  await waitFor(() => expect(screen.queryByText("alpha.ts")).toBeNull());
  expect(screen.getByText("listing failed")).toBeTruthy();
});
