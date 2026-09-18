import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../App";
const sessionSelection = vi.hoisted(() => ({ current: (_id: string) => {} }));

vi.mock("../lib/api", () => ({ api: {
  config: () => Promise.resolve({}), diagnostics: () => Promise.resolve({}), providers: () => Promise.resolve([{ has_key: true }]),
} }));
vi.mock("../components/LeftRail", () => ({ default: ({ onSessionChange }: { onSessionChange: (id: string) => void }) => {
  sessionSelection.current = onSessionChange;
  return <button>Session item</button>;
} }));
vi.mock("../components/Conversation", () => ({ default: () => <textarea aria-label="Chat editor" /> }));
vi.mock("../components/RightPane", () => ({ default: () => <button>Panel item</button> }));
vi.mock("../components/RightDock", () => ({ default: ({ onOpenTab }: { onOpenTab: (tab: string) => void }) => <button onClick={() => onOpenTab("review")}>Review shortcut</button> }));
vi.mock("../components/StatusBar", () => ({ default: () => null }));
vi.mock("../components/UpdateBanner", () => ({ default: () => null }));
vi.mock("../components/ProviderKeyBanner", () => ({ default: () => null }));
vi.mock("../components/KeyBootstrapBanner", () => ({ default: () => null }));
vi.mock("../components/RegistryWizard", () => ({ default: () => null }));
vi.mock("../components/OnboardingOverlay", () => ({ default: () => null }));
vi.mock("../components/CommandPalette", () => ({ default: () => null }));
vi.mock("../components/SettingsShell", () => ({ focusSettingsPage: vi.fn() }));
function resize(width: number) {
  act(() => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    window.dispatchEvent(new Event("resize"));
  });
}
beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("pmharness.leftW", "248");
  localStorage.setItem("pmharness.rightW", "520");
  localStorage.setItem("pmharness.leftOpen", "1");
  localStorage.setItem("pmharness.rightOpen", "1");
  localStorage.setItem("pmharness.board.openCards", '["review"]');
  resize(1280);
});
afterEach(cleanup);


it.each([360, 640, 1024])("retains the desktop shell and draft at %ipx", async width => {
  await act(async () => { render(<App />); });
  const editor = screen.getByRole("textbox", { name: "Chat editor" });
  fireEvent.change(editor, { target: { value: "unsent draft" } });
  const session = screen.getByRole("button", { name: "Session item" });
  fireEvent.keyDown(window, { key: "b", ctrlKey: true });
  expect(screen.queryByRole("button", { name: "Session item" })).toBeNull();
  fireEvent.keyDown(window, { key: "b", ctrlKey: true });
  expect(screen.getByRole("button", { name: "Session item" })).toBe(session);
  resize(width);
  expect(screen.queryByRole("navigation", { name: "Workspace views" })).toBeNull();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(editor.closest("[inert], [aria-hidden=true]")).toBeNull();
  expect(screen.getByRole("button", { name: "Session item" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Panel item" })).toBeVisible();
  expect(screen.getByRole("separator", { name: "Resize left panel" })).toBeVisible();
  expect(screen.getByRole("separator", { name: "Resize right panel" })).toBeVisible();
  fireEvent.keyDown(window, { key: "j", ctrlKey: true });
  expect(screen.queryByRole("button", { name: "Panel item" })).toBeNull();
  expect(screen.getByRole("button", { name: "Session item" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Review shortcut" }));
  expect(screen.getByRole("button", { name: "Panel item" })).toBeVisible();
  act(() => { sessionSelection.current("selected-session"); });
  expect(screen.getByRole("button", { name: "Session item" })).toBeVisible();
  const focusEditor = () => editor.focus();
  window.addEventListener("harness-focus-input", focusEditor);
  fireEvent.keyDown(window, { key: "l", ctrlKey: true });
  expect(editor).toHaveFocus();
  window.removeEventListener("harness-focus-input", focusEditor);
  resize(1280);
  expect(screen.getByRole("textbox")).toBe(editor);
  expect(editor).toHaveValue("unsent draft");
  expect(localStorage.getItem("pmharness.leftW")).toBe("248");
  expect(localStorage.getItem("pmharness.rightW")).toBe("520");
  const surface = screen.getByTestId("chat-surface");
  const board = screen.getByTestId("right-board");
  expect(screen.queryByTestId("right-board-overlay")).toBeNull();
  expect(surface.contains(board)).toBe(false);
  expect(board.className).not.toMatch(/\babsolute\b/);
});
