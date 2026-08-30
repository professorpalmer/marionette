import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPane, { clearSettingsSnapshot } from "../components/SettingsPane";
import { api } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    settings: vi.fn().mockResolvedValue({
      driver: "cursor",
      reach: "repo",
      budget: 100,
      models: [],
      auto_distill: false,
      state_dir: "/tmp/state",
      repo: "/tmp/repo",
      has_api_key: false,
    }),
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

describe("SettingsPane keys-first providers order", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
    vi.mocked(api.settings).mockResolvedValue({
      driver: "cursor",
      reach: "repo",
      budget: 100,
      models: [],
      auto_distill: false,
      state_dir: "/tmp/state",
      repo: "/tmp/repo",
      has_api_key: false,
    } as never);
  });

  it("shows API keys before optional plan sign-in", () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    const keys = screen.getByText("API keys");
    const signIn = screen.getByText("Optional plan sign-in");
    expect(keys.compareDocumentPosition(signIn) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
