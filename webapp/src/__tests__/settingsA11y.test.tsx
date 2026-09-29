import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ModelsSettingsPage, { clearCatalogSnapshot } from "../components/ModelsSettingsPage";
import { SettingsCollapse } from "../components/SettingsCollapse";
import { api, type ModelCatalogEntry } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    modelCatalog: vi.fn(),
    toggleModel: vi.fn(),
  },
}));

afterEach(() => {
  cleanup();
  clearCatalogSnapshot();
  localStorage.clear();
});

const catalog: ModelCatalogEntry[] = [{
  spec: "anthropic:claude-sonnet",
  model: "claude-sonnet",
  provider: "anthropic",
  provider_display: "Anthropic",
  available: true,
  enabled: true,
}];

it("settings sections report their open state", () => {
  render(<SettingsCollapse id="a11y-test" title="Advanced" defaultOpen={false}><p>body</p></SettingsCollapse>);
  const header = screen.getByRole("button", { name: /Advanced/ });
  expect(header).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(header);
  expect(header).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("body").parentElement).toHaveAttribute("id", header.getAttribute("aria-controls"));
});

it("model toggles are switches that keep focus while saving", async () => {
  vi.mocked(api.modelCatalog).mockResolvedValue({ catalog } as never);
  let finish: (v: unknown) => void = () => {};
  vi.mocked(api.toggleModel).mockReturnValue(new Promise((r) => { finish = r; }) as never);
  render(<ModelsSettingsPage />);
  const toggle = await screen.findByRole("switch", { name: /claude-sonnet/ });
  expect(toggle).toHaveAttribute("aria-checked", "true");
  toggle.focus();
  fireEvent.click(toggle);
  await waitFor(() => expect(toggle).toHaveAttribute("aria-checked", "false"));
  expect(toggle).not.toBeDisabled();
  expect(toggle).toHaveAttribute("aria-disabled", "true");
  expect(toggle).toHaveFocus();
  fireEvent.click(toggle);
  expect(api.toggleModel).toHaveBeenCalledTimes(1);
  finish({ ok: true });
});
