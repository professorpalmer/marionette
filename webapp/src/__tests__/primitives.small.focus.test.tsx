import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import PluginInstallModal from "../components/PluginInstallModal";
import ProviderConfigModal from "../components/ProviderConfigModal";
import RegistryWizard from "../components/RegistryWizard";
import SpillPreviewModal, { type SpillPreviewState } from "../components/conversation/SpillPreviewModal";
import SessionToolsPanel, { type SessionToolsView } from "../components/conversation/SessionToolsPanel";

vi.mock("../lib/api", () => ({
  api: {
    providers: vi.fn(async () => []),
    setProviderKey: vi.fn(),
    installPlugin: vi.fn(),
    setPluginEnabled: vi.fn(),
  },
}));

vi.mock("../components/AdvicePane", () => ({
  default: () => <button type="button">Advice body</button>,
}));

vi.mock("../components/SchedulesPane", () => ({
  default: () => <button type="button">Schedules body</button>,
}));

type Surface = (open: boolean, onClose: () => void) => ReactNode;

function Harness({ surface }: { surface: Surface }) {
  const [open, setOpen] = useState(false);
  return <>
    <button type="button" onClick={() => setOpen(true)}>Open</button>
    {surface(open, () => setOpen(false))}
  </>;
}

const spill: SpillPreviewState = { uri: "spill://abc", content: "out", chars: 3, truncated: false };
const advice: SessionToolsView = { kind: "advice", sessionId: "s1", error: "" };

/** Focus each focus guard in turn. Base UI sends focus back into the popup. */
async function expectTabStaysInside(dialog: HTMLElement) {
  const guards = Array.from(document.querySelectorAll<HTMLElement>("[data-base-ui-focus-guard]"));
  expect(guards.length).toBeGreaterThan(0);
  for (const guard of guards) {
    act(() => guard.focus());
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  }
}

const cases: { name: string; label: string; surface: Surface; first: () => HTMLElement }[] = [
  {
    name: "PluginInstallModal",
    label: "Install plugin",
    surface: (open, onClose) => <PluginInstallModal open={open} onClose={onClose} />,
    first: () => screen.getByTestId("plugin-install-source"),
  },
  {
    name: "ProviderConfigModal",
    label: "Add provider",
    surface: (open, onClose) => (
      <ProviderConfigModal open={open} manual provider={null} providerChoices={[{ name: "openai", label: "OpenAI" }]}
        onClose={onClose} onSubmit={vi.fn()} />
    ),
    first: () => screen.getByTestId("provider-config-field-name"),
  },
  {
    name: "SpillPreviewModal",
    label: "Spill preview",
    surface: (open, onClose) => <SpillPreviewModal preview={open ? spill : null} onClose={onClose} />,
    first: () => screen.getByTitle("Close"),
  },
  {
    name: "SessionToolsPanel",
    label: "Advice",
    surface: (open, onClose) => <SessionToolsPanel view={open ? advice : null} onClose={onClose} />,
    first: () => screen.getByRole("button", { name: "Close" }),
  },
];

describe("small dialogs focus", () => {
  for (const c of cases) {
    it(`${c.name}: focus moves in, Tab stays inside, Escape closes, focus returns`, async () => {
      render(<Harness surface={c.surface} />);
      const opener = screen.getByRole("button", { name: "Open" });
      opener.focus();
      fireEvent.click(opener);
      const dialog = await screen.findByRole("dialog", { name: c.label });
      await waitFor(() => expect(c.first()).toHaveFocus());
      await expectTabStaysInside(dialog);
      fireEvent.keyDown(dialog, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(opener).toHaveFocus());
    });
  }

  it("RegistryWizard: focus moves in, Tab stays inside, outside press keeps it open, Escape skips", async () => {
    const onClose = vi.fn();
    render(<RegistryWizard onClose={onClose} />);
    const dialog = await screen.findByTestId("provider-onboarding");
    await screen.findByText(/No Full stack providers/i);
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    await expectTabStaysInside(dialog);
    fireEvent.pointerDown(document.body);
    fireEvent.mouseDown(document.body);
    fireEvent.click(document.body);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
