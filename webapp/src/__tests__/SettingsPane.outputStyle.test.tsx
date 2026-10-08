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
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
  compactionResidual: "catalog",
  outputStyle: "off",
};

describe("SettingsPane output style opt-in", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    writeSettingsSnapshot(sampleSettings);
    mockSettings.mockResolvedValue(sampleSettings);
    mockUpdate.mockResolvedValue({ ...sampleSettings, outputStyle: "ste" });
  });

  it("is off by default and turns Simplified Technical English on", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(await screen.findByText("Output Style")).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /simplified technical english/i });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);
    await waitFor(() => {
      expect(mockUpdate).toHaveBeenCalledWith({ outputStyle: "ste" });
    });
  });
});
