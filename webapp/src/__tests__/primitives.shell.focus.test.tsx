import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CommandPalette from "../components/CommandPalette";
import SettingsShell from "../components/SettingsShell";
import OnboardingOverlay from "../components/OnboardingOverlay";
import { api } from "../lib/api";
import { resetOnboardingForTests } from "../state/onboardingStore";

vi.mock("../lib/api", () => ({
  api: {
    sessionsBank: vi.fn(),
    getWorkspaceFiles: vi.fn(),
    searchSessions: vi.fn(),
    providers: vi.fn(),
    getClaudeCliStatus: vi.fn(),
    getLocalModels: vi.fn(),
  },
}));
vi.mock("../components/ModelsSettingsPage", () => ({ default: () => null }));
vi.mock("../components/LocalModelsSettingsPage", () => ({ default: () => null }));
vi.mock("../components/SettingsPane", () => ({ default: () => null }));
vi.mock("../components/PluginsPane", () => ({ default: () => null }));

/** Focus each focus guard in turn. Base UI sends focus back into the popup. */
async function expectTabStaysInside(dialog: HTMLElement) {
  const guards = Array.from(document.querySelectorAll<HTMLElement>("[data-base-ui-focus-guard]"));
  expect(guards.length).toBeGreaterThan(0);
  for (const guard of guards) {
    act(() => guard.focus());
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  }
}

function SettingsHarness() {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open settings</button>
    {open ? <SettingsShell onClose={() => setOpen(false)} onOpenWizard={() => {}} initialPage="models" /> : null}
  </>;
}

function OnboardingHarness() {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open onboarding</button>
    {open ? <OnboardingOverlay onClose={() => setOpen(false)} /> : null}
  </>;
}

describe("shell dialogs focus", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.sessionsBank).mockResolvedValue([]);
    vi.mocked(api.getWorkspaceFiles).mockResolvedValue({ files: [] });
    vi.mocked(api.searchSessions).mockResolvedValue([]);
    vi.mocked(api.providers).mockResolvedValue([]);
    vi.mocked(api.getClaudeCliStatus).mockResolvedValue({ ok: false, installed: false, authenticated: false });
    vi.mocked(api.getLocalModels).mockResolvedValue({ externals: [] });
  });

  it("CommandPalette: focus moves to the input, Tab stays inside, Escape closes, focus returns", async () => {
    render(<>
      <button type="button">Before palette</button>
      <CommandPalette onToggleLeft={() => {}} onToggleRight={() => {}} />
    </>);
    const before = screen.getByRole("button", { name: "Before palette" });
    before.focus();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    const input = screen.getByTestId("command-palette-input");
    await waitFor(() => expect(input).toHaveFocus());
    await expectTabStaysInside(dialog);
    act(() => input.focus());
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(before).toHaveFocus());
  });

  it("CommandPalette: a press outside the panel closes it", async () => {
    render(<CommandPalette onToggleLeft={() => {}} onToggleRight={() => {}} />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    const viewport = dialog.parentElement;
    if (!viewport) throw new Error("palette has no viewport");
    fireEvent.pointerDown(viewport);
    fireEvent.mouseDown(viewport);
    fireEvent.pointerUp(viewport);
    fireEvent.mouseUp(viewport);
    fireEvent.click(viewport);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("SettingsShell: focus moves to Close, Tab stays inside, Escape closes, focus returns", async () => {
    render(<SettingsHarness />);
    const opener = screen.getByRole("button", { name: "Open settings" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Settings" });
    await waitFor(() => expect(within(dialog).getByTitle("Close settings")).toHaveFocus());
    await expectTabStaysInside(dialog);
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("SettingsShell: a press outside the panel does not close it", async () => {
    const onClose = vi.fn();
    render(<SettingsShell onClose={onClose} onOpenWizard={() => {}} initialPage="models" />);
    await screen.findByRole("dialog", { name: "Settings" });
    fireEvent.pointerDown(document.body);
    fireEvent.mouseDown(document.body);
    fireEvent.click(document.body);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("OnboardingOverlay: focus moves in, Tab stays inside, outside press keeps it open, Escape skips", async () => {
    resetOnboardingForTests(true);
    render(<OnboardingHarness />);
    const opener = screen.getByRole("button", { name: "Open onboarding" });
    opener.focus();
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Connect a model" });
    await screen.findByRole("button", { name: /Sign in with a plan/ });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await expectTabStaysInside(dialog);
    fireEvent.pointerDown(document.body);
    fireEvent.mouseDown(document.body);
    fireEvent.click(document.body);
    expect(screen.getByRole("dialog", { name: "Connect a model" })).toBeTruthy();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
