import { render, screen } from "@testing-library/react";
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

const sampleSettings: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 10,
  models: ["anthropic/claude-sonnet"],
  auto_distill: false,
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
  maxPilotSteps: "40",
  pilotToolBudget: "25",
};

describe("SettingsPane pilotToolBudget control", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    writeSettingsSnapshot(sampleSettings);
    mockSettings.mockResolvedValue(sampleSettings);
  });

  it("renders per-turn tool-call cap distinct from max investigation steps", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="safety" />);

    expect(await screen.findByText("Max investigation steps")).toBeInTheDocument();
    expect(screen.getByText("Per-turn tool-call cap")).toBeInTheDocument();
    expect(screen.getByDisplayValue("25")).toBeInTheDocument();
    expect(screen.getByDisplayValue("40")).toBeInTheDocument();
  });
});
