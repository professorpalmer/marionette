import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SafetySettingsPage from "../components/SafetySettingsPage";
import { clearSettingsSnapshot, writeSettingsSnapshot } from "../components/settingsSnapshot";
import { api, type Settings } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    settings: vi.fn(),
    updateSettings: vi.fn(),
  },
}));
vi.mock("../components/EncryptedSessions", () => ({ default: () => <p>encrypted sessions group</p> }));
vi.mock("../components/DeviceAccess", () => ({ default: () => <p>device access group</p> }));

const mockSettings = vi.mocked(api.settings);
const mockUpdate = vi.mocked(api.updateSettings);

const sample: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 10,
  models: ["anthropic/claude-sonnet"],
  auto_distill: false,
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
  autoCommandGuard: true,
  browserRealProfile: false,
  commandTimeout: "120",
  maxPilotSteps: "40",
  pilotToolBudget: "25",
  maxOutputTokens: "unlimited",
  autoMaxTokens: "500000",
  workerTokenBudget: "250000",
  swarm_reasoning_effort: "medium",
};

async function mount() {
  await act(async () => { render(<SafetySettingsPage />); });
  await screen.findByTestId("safety-settings");
}

function row(testId: string) {
  return within(screen.getByTestId(testId));
}

beforeEach(() => {
  vi.clearAllMocks();
  clearSettingsSnapshot();
  writeSettingsSnapshot(sample);
  mockSettings.mockResolvedValue(sample);
  mockUpdate.mockImplementation(async (partial) => ({ ...sample, ...partial }));
});

describe("Safety settings page", () => {
  it("renders one row for each setting, with its current value", async () => {
    await mount();
    expect(screen.getByRole("heading", { name: "Safety" })).toBeInTheDocument();
    for (const name of ["Full-auto", "Limits for each message", "Workers", "Browser"]) {
      expect(screen.getByRole("heading", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("switch", { name: "Block dangerous commands" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("switch", { name: "Use my Chrome sign-ins" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("textbox", { name: "Command timeout (seconds)" })).toHaveValue("120");
    expect(screen.getByRole("textbox", { name: "Model steps per message" })).toHaveValue("40");
    expect(screen.getByRole("textbox", { name: "Tool calls per turn" })).toHaveValue("25");
    expect(screen.getByRole("textbox", { name: "Reply length limit" })).toHaveValue("unlimited");
    expect(screen.getByRole("textbox", { name: "Full-auto token limit" })).toHaveValue("500000");
    expect(screen.getByRole("textbox", { name: "Worker token limit" })).toHaveValue("250000");
    expect(screen.getByRole("combobox", { name: "Worker reasoning" })).toHaveValue("medium");
    expect(screen.getByText("encrypted sessions group")).toBeInTheDocument();
    expect(screen.getByText("device access group")).toBeInTheDocument();
  });

  it("writes each switch through updateSettings", async () => {
    await mount();
    fireEvent.click(screen.getByRole("switch", { name: "Block dangerous commands" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith({ autoCommandGuard: false }));
    await waitFor(() => expect(row("safety-autoCommandGuard").getByText("Saved")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("switch", { name: "Use my Chrome sign-ins" })).toBeEnabled());
    fireEvent.click(screen.getByRole("switch", { name: "Use my Chrome sign-ins" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith({ browserRealProfile: true }));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Use my Chrome sign-ins" })).toHaveAttribute("aria-checked", "true"));
  });

  it.each([
    ["Command timeout (seconds)", "commandTimeout", "0"],
    ["Model steps per message", "maxPilotSteps", "unlimited"],
    ["Tool calls per turn", "pilotToolBudget", "50"],
    ["Reply length limit", "maxOutputTokens", "32000"],
    ["Full-auto token limit", "autoMaxTokens", "0"],
    ["Worker token limit", "workerTokenBudget", "400000"],
  ])("writes %s as %s on blur", async (label, key, value) => {
    await mount();
    const field = screen.getByRole("textbox", { name: label });
    fireEvent.change(field, { target: { value: ` ${value} ` } });
    fireEvent.blur(field);
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith({ [key]: value }));
  });

  it("does not write an unchanged or empty field", async () => {
    await mount();
    const field = screen.getByRole("textbox", { name: "Tool calls per turn" });
    fireEvent.blur(field);
    fireEvent.change(field, { target: { value: "  " } });
    fireEvent.blur(field);
    expect(mockUpdate).not.toHaveBeenCalled();
  });

  it("writes worker reasoning from the select", async () => {
    await mount();
    fireEvent.change(screen.getByRole("combobox", { name: "Worker reasoning" }), { target: { value: "high" } });
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith({ swarm_reasoning_effort: "high" }));
  });

  it("shows the backend error under the row that failed", async () => {
    mockUpdate.mockRejectedValueOnce({ error: "Invalid pilotToolBudget" });
    await mount();
    const field = screen.getByRole("textbox", { name: "Tool calls per turn" });
    fireEvent.change(field, { target: { value: "-3" } });
    fireEvent.blur(field);
    expect(await row("safety-pilotToolBudget").findByRole("alert")).toHaveTextContent("Invalid pilotToolBudget");
  });

  it("shows a load line before the first settings arrive", async () => {
    clearSettingsSnapshot();
    mockSettings.mockReturnValue(new Promise(() => {}));
    await mount();
    expect(screen.getByText("Loading settings...")).toBeInTheDocument();
  });
});
