import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import GeneralSettingsPage, { switchOn } from "../components/GeneralSettingsPage";
import { clearSettingsSnapshot, writeSettingsSnapshot } from "../components/settingsSnapshot";
import src from "../components/GeneralSettingsPage.tsx?raw";
import { api, type Settings } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    settings: vi.fn(),
    updateSettings: vi.fn(),
  },
}));

const mockSettings = vi.mocked(api.settings);
const mockUpdate = vi.mocked(api.updateSettings);

const sample: Settings = {
  driver: "cursor",
  reach: "openrouter",
  budget: 3,
  models: [],
  auto_distill: false,
  reviewEditsBeforeApply: false,
  hash_edit_enabled: false,
  jev_enabled: false,
  jev_ready: false,
  autoVerify: true,
  compactionResidual: "catalog",
  outputStyle: "off",
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
};

const switchOf = (key: string) => screen.getByTestId(`general-switch-${key}`);

describe("GeneralSettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
    mockSettings.mockResolvedValue(sample);
    mockUpdate.mockImplementation(async (partial) => ({ ...sample, ...partial }));
  });

  it("renders one row for each setting, on the shared layout", async () => {
    render(<GeneralSettingsPage />);
    expect(await screen.findByRole("heading", { name: "General" })).toBeInTheDocument();
    for (const key of ["outputStyle", "reviewEditsBeforeApply", "autoVerify", "hash_edit_enabled", "auto_distill", "jev_enabled"]) {
      expect(screen.getByTestId(`general-row-${key}`)).toBeInTheDocument();
    }
    expect(screen.getByRole("combobox", { name: "Keep after compact" })).toHaveValue("catalog");
    expect(screen.getByText("/tmp/repo")).toBeInTheDocument();
    expect(screen.getByText("/tmp/state")).toBeInTheDocument();
    expect(src).toMatch(/from "\.\/SettingsLayout"/);
    expect(src).not.toMatch(/SettingsCollapse/);
  });

  it("shows the state that the backend returns", async () => {
    render(<GeneralSettingsPage />);
    await screen.findByTestId("general-row-autoVerify");
    expect(switchOf("autoVerify")).toHaveAttribute("aria-checked", "true");
    expect(switchOf("auto_distill")).toHaveAttribute("aria-checked", "false");
    expect(switchOf("outputStyle")).toHaveAttribute("aria-checked", "false");
  });

  it.each([
    ["outputStyle", { outputStyle: "ste" }],
    ["reviewEditsBeforeApply", { reviewEditsBeforeApply: true }],
    ["autoVerify", { autoVerify: false }],
    ["hash_edit_enabled", { hash_edit_enabled: true }],
    ["auto_distill", { auto_distill: true }],
    ["jev_enabled", { jev_enabled: true }],
  ])("the %s switch saves %o through api.updateSettings", async (key, patch) => {
    render(<GeneralSettingsPage />);
    await screen.findByTestId(`general-row-${key}`);
    fireEvent.click(switchOf(key));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith(patch));
    await waitFor(() => expect(switchOf(key)).toHaveAttribute("aria-checked", key === "autoVerify" ? "false" : "true"));
  });

  it("saves the compaction choice", async () => {
    render(<GeneralSettingsPage />);
    const select = await screen.findByRole("combobox", { name: "Keep after compact" });
    fireEvent.change(select, { target: { value: "hybrid" } });
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith({ compactionResidual: "hybrid" }));
    fireEvent.change(select, { target: { value: "summary" } });
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith({ compactionResidual: "summary" }));
  });

  it("tells the other readers that the config changed", async () => {
    const heard = vi.fn();
    window.addEventListener("harness-config-changed", heard);
    render(<GeneralSettingsPage />);
    await screen.findByTestId("general-row-auto_distill");
    fireEvent.click(switchOf("auto_distill"));
    await waitFor(() => expect(heard).toHaveBeenCalled());
    window.removeEventListener("harness-config-changed", heard);
  });

  it("marks Jev idle when it is on without an OpenRouter key", async () => {
    mockSettings.mockResolvedValue({ ...sample, jev_enabled: true, jev_ready: false });
    render(<GeneralSettingsPage />);
    const row = await screen.findByTestId("general-row-jev_enabled");
    await waitFor(() => expect(within(row).getByText("Idle: no OpenRouter key")).toBeInTheDocument());
  });

  it("shows the save error and keeps the old state", async () => {
    mockUpdate.mockRejectedValue({ error: "Invalid compactionResidual" });
    render(<GeneralSettingsPage />);
    await screen.findByTestId("general-row-auto_distill");
    fireEvent.click(switchOf("auto_distill"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid compactionResidual");
    expect(switchOf("auto_distill")).toHaveAttribute("aria-checked", "false");
  });

  it("paints the cached snapshot at once and keeps it when the read fails", async () => {
    writeSettingsSnapshot({ ...sample, auto_distill: true });
    mockSettings.mockRejectedValue(new Error("offline"));
    render(<GeneralSettingsPage />);
    expect(screen.queryByText("Loading settings...")).toBeNull();
    expect(switchOf("auto_distill")).toHaveAttribute("aria-checked", "true");
    await waitFor(() => expect(mockSettings).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows a load error when there is no snapshot and the read fails", async () => {
    mockSettings.mockRejectedValue(new Error("offline"));
    render(<GeneralSettingsPage />);
    expect(screen.getByText("Loading settings...")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Settings did not load.");
  });

  it("uses the backend defaults when a key is missing", () => {
    const thin: Settings = { driver: "x", reach: "openrouter", budget: 3, models: [], auto_distill: false, state_dir: "", repo: "" };
    expect(switchOn(thin, "autoVerify")).toBe(true);
    expect(switchOn(thin, "hash_edit_enabled")).toBe(false);
    expect(switchOn(thin, "outputStyle")).toBe(false);
  });

  it("has no removed controls", () => {
    for (const text of [src]) {
      expect(text).not.toMatch(/update\(\{ budget/);
      expect(text).not.toMatch(/Token & Cost Usage/);
      expect(text).not.toMatch(/Connect a provider/);
      expect(text).not.toMatch(/gate\("general"/);
      expect(text).not.toMatch(/gate\("notifications"/);
    }
  });
});
