import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsShell, { focusSettingsPage } from "../components/SettingsShell";

vi.mock("../components/ModelsSettingsPage", () => ({
  default: () => <div data-testid="models-page" />,
}));

vi.mock("../components/SettingsPane", () => ({
  default: ({ section }: { section: string }) => (
    <div data-testid={`settings-section-${section}`} />
  ),
}));

describe("SettingsShell Accounts & Keys focus", () => {
  beforeEach(() => {
    // Clear any latched pending page between tests.
    window.dispatchEvent(new CustomEvent("harness-settings-page", { detail: "models" }));
  });

  it("starts on Accounts & Keys when initialPage is providers", () => {
    render(
      <SettingsShell
        onClose={vi.fn()}
        onOpenWizard={vi.fn()}
        initialPage="providers"
      />,
    );
    expect(screen.getByTestId("settings-section-providers")).toBeTruthy();
    expect(screen.queryByTestId("models-page")).toBeNull();
  });

  it("switches to Accounts & Keys on harness-settings-page / focusSettingsPage", () => {
    render(<SettingsShell onClose={vi.fn()} onOpenWizard={vi.fn()} />);
    expect(screen.getByTestId("models-page")).toBeTruthy();
    act(() => {
      focusSettingsPage("providers");
    });
    expect(screen.getByTestId("settings-section-providers")).toBeTruthy();
  });

  it("latches pending page before mount so Add key works with a closed pane", () => {
    focusSettingsPage("providers");
    render(<SettingsShell onClose={vi.fn()} onOpenWizard={vi.fn()} />);
    expect(screen.getByTestId("settings-section-providers")).toBeTruthy();
  });

  it("sidebar Accounts & Keys nav selects the providers section", () => {
    render(<SettingsShell onClose={vi.fn()} onOpenWizard={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /Accounts & Keys/i }));
    expect(screen.getByTestId("settings-section-providers")).toBeTruthy();
  });
});
