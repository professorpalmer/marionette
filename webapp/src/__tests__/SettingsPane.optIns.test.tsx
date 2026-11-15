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

