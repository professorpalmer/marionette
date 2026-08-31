import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import SettingsShell from "../components/SettingsShell";

vi.mock("../components/ModelsSettingsPage", () => ({
  default: () => <div data-testid="models-page" />,
}));

vi.mock("../components/LocalModelsSettingsPage", () => ({
  default: () => <div data-testid="local-models-page" />,
}));

vi.mock("../components/SettingsPane", () => ({
  default: ({ section }: { section: string }) => (
    <div data-testid={`settings-section-${section}`} />
  ),
}));

describe("SettingsShell Local Models nav", () => {
  it("lists Models, General, then Local Models in the sidebar", () => {
    render(<SettingsShell onClose={vi.fn()} onOpenWizard={vi.fn()} />);
    const labels = screen
      .getAllByRole("button")
      .map((el) => el.textContent?.trim() ?? "")
      .filter((label) => label.length > 0);
    const modelsAt = labels.indexOf("Models");
    expect(labels.slice(modelsAt, modelsAt + 3)).toEqual([
      "Models",
      "General",
      "Local Models",
    ]);
  });

  it("opens the Local Models page from the sidebar", () => {
    render(<SettingsShell onClose={vi.fn()} onOpenWizard={vi.fn()} />);
    expect(screen.getByTestId("models-page")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Local Models/i }));
    expect(screen.getByTestId("local-models-page")).toBeTruthy();
    expect(screen.queryByTestId("models-page")).toBeNull();
  });
});
