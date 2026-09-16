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

const sampleSettings: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 10,
  models: ["anthropic/claude-sonnet"],
  auto_distill: false,
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
  maxPilotSteps: "40",
  maxOutputTokens: "unlimited",
  pilotToolBudget: "25",
  autoMaxTokens: "500000",
  workerTokenBudget: "250000",
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
    expect(screen.getByText("Reply output cap")).toBeInTheDocument();
    expect(screen.getByText("Per-turn tool-call cap")).toBeInTheDocument();
    expect(screen.getByDisplayValue("25")).toBeInTheDocument();
    expect(screen.getByDisplayValue("40")).toBeInTheDocument();
    expect(screen.getByDisplayValue("unlimited")).toBeInTheDocument();
    expect(screen.getByText(/factory default lets the provider decide/i)).toBeInTheDocument();
  });

  it("lets full-auto and worker token ceilings be unlimited", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="safety" />);

    expect(await screen.findByText("Full-auto token ceiling")).toBeInTheDocument();
    expect(screen.getByText("Worker run token ceiling")).toBeInTheDocument();
    expect(screen.getByDisplayValue("500000")).toBeInTheDocument();
    expect(screen.getByDisplayValue("250000")).toBeInTheDocument();
    expect(screen.getByText(/Use 0 or "unlimited" so tokens do not stop the run/i)).toBeInTheDocument();
    expect(screen.getByText(/Use 0 or "unlimited" for no per-worker/i)).toBeInTheDocument();
    expect(screen.queryByText(/this field is not unlimited/i)).not.toBeInTheDocument();
  });
});
