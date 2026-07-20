import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPane, {
  clearSettingsSnapshot,
  toSafeSettingsSnapshot,
  writeSettingsSnapshot,
} from "../components/SettingsPane";
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

const SETTINGS_SNAPSHOT_KEY = "pmharness.settings.snapshot";

const sampleSettings: Settings = {
  driver: "cursor",
  reach: "repo",
  budget: 100,
  models: ["anthropic/claude-sonnet"],
  auto_distill: false,
  state_dir: "/tmp/state",
  repo: "/tmp/repo",
  has_api_key: true,
  api_key_masked: "sk-…abcd",
};

describe("SettingsPane cached first paint", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
  });

  afterEach(() => {
    clearSettingsSnapshot();
  });

  it("renders cached settings immediately without a loading flash", async () => {
    writeSettingsSnapshot(sampleSettings);
    localStorage.setItem(
      SETTINGS_SNAPSHOT_KEY,
      JSON.stringify({ settings: sampleSettings, savedAt: Date.now() }),
    );
    mockSettings.mockImplementation(() => new Promise(() => {}));

    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(screen.queryByText("Loading settings...")).toBeNull();
    expect(screen.getByText("Driver (Model)")).toBeInTheDocument();
  });

  it("shows loading gate only when no snapshot exists", async () => {
    clearSettingsSnapshot();
    mockSettings.mockImplementation(() => new Promise(() => {}));

    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(screen.getByText("Loading settings...")).toBeInTheDocument();
  });

  it("retains stale snapshot on transient fetch failure", async () => {
    writeSettingsSnapshot(sampleSettings);
    localStorage.setItem(
      SETTINGS_SNAPSHOT_KEY,
      JSON.stringify({ settings: sampleSettings, savedAt: Date.now() }),
    );
    mockSettings.mockRejectedValue(new Error("offline"));

    render(<SettingsPane onOpenWizard={vi.fn()} section="general" />);

    expect(screen.queryByText("Loading settings...")).toBeNull();

    await waitFor(() => {
      expect(mockSettings).toHaveBeenCalled();
    });

    expect(screen.queryByText("Failed to load settings")).toBeNull();
    expect(screen.getByText("Driver (Model)")).toBeInTheDocument();
  });
});

describe("toSafeSettingsSnapshot", () => {
  it("excludes raw secrets from persisted snapshot fields", () => {
    const raw = {
      ...sampleSettings,
      api_key: "sk-secret-should-not-persist",
      token: "tok-secret",
      credentials: { password: "hunter2" },
    } as Settings & { api_key: string; token: string; credentials: { password: string } };

    const safe = toSafeSettingsSnapshot(raw);
    const serialized = JSON.stringify(safe);

    expect(serialized).not.toContain("sk-secret-should-not-persist");
    expect(serialized).not.toContain("tok-secret");
    expect(serialized).not.toContain("hunter2");
    expect(safe.api_key_masked).toBe("sk-…abcd");
    expect(safe.driver).toBe("cursor");
  });

  it("writeSettingsSnapshot persists only safe fields to localStorage", () => {
    clearSettingsSnapshot();
    const raw = {
      ...sampleSettings,
      api_key: "raw-key-value",
    } as Settings & { api_key: string };

    writeSettingsSnapshot(raw);

    const stored = localStorage.getItem(SETTINGS_SNAPSHOT_KEY);
    expect(stored).toBeTruthy();
    expect(stored!).not.toContain("raw-key-value");
    expect(stored!).toContain("cursor");
  });
});
