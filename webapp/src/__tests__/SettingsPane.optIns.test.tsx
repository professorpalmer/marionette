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
    providers: vi.fn().mockResolvedValue([]),
    authPools: vi.fn().mockResolvedValue({ pools: [] }),
    bedrockStatus: vi.fn().mockResolvedValue(null),
    cursorCliStatus: vi.fn().mockResolvedValue(null),
    gitStatus: vi.fn().mockResolvedValue(null),
    platformAdapters: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock("../components/SkillsPane", () => ({ default: () => <div /> }));
vi.mock("../components/MemoryPane", () => ({ default: () => <div /> }));
vi.mock("../components/SchedulesPane", () => ({ default: () => <div /> }));

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
  autoVerify: true,
  browserRealProfile: false,
  compactionResidual: "catalog",
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
});

describe("SettingsPane Safety Chrome login", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    writeSettingsSnapshot(sampleSettings);
    mockSettings.mockResolvedValue(sampleSettings);
    mockUpdate.mockImplementation(async (partial) => ({ ...sampleSettings, ...partial }));
  });

  it("toggles browserRealProfile from Safety", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="safety" />);

    expect(await screen.findByText("Use my Chrome login")).toBeTruthy();
    fireEvent.click(screen.getByText("Use my Chrome login"));
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ browserRealProfile: true });
    });
  });
});
