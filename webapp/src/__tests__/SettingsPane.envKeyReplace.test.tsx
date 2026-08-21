import { render, screen, waitFor } from "@testing-library/react";
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
    providers: vi.fn().mockResolvedValue([]),
    authPools: vi.fn().mockResolvedValue({ pools: [] }),
    getAuthPools: vi.fn().mockResolvedValue({ pools: [] }),
    bedrockStatus: vi.fn().mockResolvedValue(null),
    getBedrockStatus: vi.fn().mockResolvedValue(null),
    cursorCliStatus: vi.fn().mockResolvedValue(null),
    getCursorCliStatus: vi.fn().mockResolvedValue(null),
    gitStatus: vi.fn().mockResolvedValue(null),
    platformAdapters: vi.fn().mockResolvedValue([]),
    clearProviderKey: vi.fn().mockResolvedValue({ ok: true }),
    setProviderEnabled: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

vi.mock("../components/SkillsPane", () => ({ default: () => <div /> }));
vi.mock("../components/MemoryPane", () => ({ default: () => <div /> }));
vi.mock("../components/SchedulesPane", () => ({ default: () => <div /> }));

const goRow = {
  name: "opencode-go",
  display_name: "OpenCode Go",
  env_var: "OPENCODE_GO_API_KEY",
  base_url: "https://opencode.ai/zen/go/v1",
  api_mode: "chat_completions",
  worker_capability: "full_stack" as const,
  worker_capability_label: "Full stack",
};

describe("SettingsPane env-imported key replace", () => {
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

  it("shows Disconnect on an env-connected provider", async () => {
    vi.mocked(api.providers).mockResolvedValue([
      { ...goRow, has_key: true, has_env: true, disconnected: false },
    ]);

    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);

    await waitFor(() => {
      expect(screen.getByText("OpenCode Go")).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeTruthy();
    expect(screen.getByRole("switch")).toBeTruthy();
  });

  it("shows a paste field after an env-imported provider is disconnected", async () => {
    vi.mocked(api.providers).mockResolvedValue([
      { ...goRow, has_key: false, has_env: false, disconnected: true },
    ]);

    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText("OPENCODE_GO_API_KEY...")).toBeTruthy();
    });
    expect(screen.getByRole("button", { name: "Connect" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Disconnect" })).toBeNull();
  });
});
