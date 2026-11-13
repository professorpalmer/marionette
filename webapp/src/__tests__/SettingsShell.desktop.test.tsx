import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { expect, it, vi } from "vitest";
import SettingsShell from "../components/SettingsShell";
import { SessionFork } from "../components/SessionFork";

vi.mock("../lib/api", () => ({ api: {
  previewSessionFork: vi.fn().mockResolvedValue({ revision: "saved", boundaries: [] }),
} }));
vi.mock("../components/ModelsSettingsPage", () => ({ default: function NestedDialogProbe() {
  const [open, setOpen] = useState(false);
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  return <>
    <button onClick={event => { setTrigger(event.currentTarget); setOpen(true); }}>Open nested dialog</button>
    <SessionFork session={{ id: "source", title: "Source" }} sessions={[]} onSelect={() => {}}
      onCreated={() => {}} open={open} returnFocus={trigger} onClose={() => setOpen(false)} />
  </>;
} }));
vi.mock("../components/LocalModelsSettingsPage", () => ({ default: () => null }));
vi.mock("../components/SettingsPane", () => ({ default: () => null }));
vi.mock("../components/PluginsPane", () => ({ default: () => null }));

it.each([360, 640, 1024])("retains Settings side navigation and nested Escape/focus at %ipx", async width => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
  const onClose = vi.fn();
  const offset = vi.spyOn(HTMLElement.prototype, "offsetParent", "get").mockImplementation(function () { return document.body; });
  try {
    render(<SettingsShell onClose={onClose} onOpenWizard={() => {}} initialPage="models" />);
    const settings = screen.getByRole("dialog", { name: "Settings" });
    expect(within(settings).queryByRole("combobox")).toBeNull();
    expect(within(settings).getByRole("button", { name: "Accounts & Keys" })).toBeVisible();
    const trigger = within(settings).getByRole("button", { name: "Open nested dialog" });
    trigger.focus();
    fireEvent.click(trigger);
    const nested = await screen.findByRole("dialog", { name: "Fork Source" });
    const close = within(nested).getByRole("button", { name: "Close Fork Source" });
    await waitFor(() => expect(close).toHaveFocus());
    fireEvent.keyDown(close, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Fork Source" })).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(settings).toBeVisible();
    fireEvent.keyDown(trigger, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
    await act(async () => {});
  } finally { offset.mockRestore(); }
});
