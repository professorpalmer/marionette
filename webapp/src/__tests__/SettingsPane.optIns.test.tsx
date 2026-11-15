import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPane, { writeSettingsSnapshot } from "../components/SettingsPane";
import { api, type Settings } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    settings: vi.fn(),
    updateSettings: vi.fn(),
    getUsage: vi.fn().mockResolvedValue(null),
    getWikiConfig: vi.fn().mockResolvedValue({ api_base: "", has_token: false }),
    getHooks: vi.fn().mockResolvedValue({ hooks: [], events: [] }),
    archiveStatus: vi.fn().mockResolvedValue({ chats: 0, vault_present: false, backup_dir: "", archive_db: "" }),
    providers: vi.fn().mockResolvedValue([]),
    authPools: vi.fn().mockResolvedValue({ pools: [] }),
    bedrockStatus: vi.fn().mockResolvedValue(null),
    cursorCliStatus: vi.fn().mockResolvedValue(null),
    gitStatus: vi.fn().mockResolvedValue(null),
    platformAdapters: vi.fn().mockResolvedValue([]),
  },
}));


const mockSettings = vi.mocked(api.settings);
const mockUpdate = vi.mocked(api.updateSettings);

const sampleSettings: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 10,
  models: ["anthropic/claude-sonnet"],
  auto_distill: false,
  reviewEditsBeforeApply: false,
  hash_edit_enabled: false,
  jev_enabled: false,
  jev_ready: false,
  autoVerify: true,
  browserRealProfile: false,
  compactionResidual: "catalog",
  outputStyle: "off",
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
};

describe("SettingsPane Opt-ins section", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    writeSettingsSnapshot(sampleSettings);
    mockSettings.mockResolvedValue(sampleSettings);
    mockUpdate.mockImplementation(async (partial) => ({ ...sampleSettings, ...partial }));
  });

  it("wires Opt-ins into existing Settings and writes existing keys", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(await screen.findByTestId("settings-opt-ins")).toBeTruthy();
    expect(screen.getByText("Opt-ins")).toBeTruthy();

    fireEvent.click(screen.getByTestId("settings-opt-in-auto_distill"));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ auto_distill: true });
    });

    fireEvent.click(screen.getByTestId("settings-opt-in-reviewEditsBeforeApply"));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ reviewEditsBeforeApply: true });
    });
  });

  it("is off by default and turns Simplified Technical English on", async () => {
    mockUpdate.mockResolvedValue({ ...sampleSettings, outputStyle: "ste" });
    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(await screen.findByText("Output Style")).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /simplified technical english/i });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ outputStyle: "ste" });
    });
  });

  it("defaults the control to catalog and can opt into hybrid", async () => {
    mockUpdate.mockResolvedValue({ ...sampleSettings, compactionResidual: "hybrid" });
    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(await screen.findByText("Compact Residual")).toBeInTheDocument();
    expect(screen.getByText("catalog")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /handle catalog; vault retrieve/i }));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ compactionResidual: "hybrid" });
    });
  });
});
