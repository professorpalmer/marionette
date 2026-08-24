import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
    providers: vi.fn(),
    authPools: vi.fn().mockResolvedValue({ pools: [] }),
    getAuthPools: vi.fn().mockResolvedValue({ pools: [] }),
    bedrockStatus: vi.fn().mockResolvedValue(null),
    getBedrockStatus: vi.fn().mockResolvedValue(null),
    cursorCliStatus: vi.fn().mockResolvedValue(null),
    getCursorCliStatus: vi.fn().mockResolvedValue(null),
    gitStatus: vi.fn().mockResolvedValue(null),
    platformAdapters: vi.fn().mockResolvedValue([]),
    setProviderKey: vi.fn().mockResolvedValue({ ok: true, provider: "openrouter", has_key: true, masked: "sk-…" }),
    clearProviderKey: vi.fn().mockResolvedValue({ ok: true }),
    setProviderEnabled: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

vi.mock("../components/SkillsPane", () => ({ default: () => <div /> }));
vi.mock("../components/MemoryPane", () => ({ default: () => <div /> }));
vi.mock("../components/SchedulesPane", () => ({ default: () => <div /> }));

const row = {
  name: "openrouter",
  display_name: "OpenRouter",
  env_var: "OPENROUTER_API_KEY",
  base_url: "https://openrouter.ai/api/v1",
  api_mode: "chat_completions",
  has_key: true,
  masked: "sk-or-••••abcd",
  worker_capability: "full_stack" as const,
  worker_capability_label: "Full stack",
};

describe("SettingsPane Accounts provider config", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
    vi.mocked(api.providers).mockResolvedValue([row]);
  });

  it("opens Add provider with manual=true", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    await waitFor(() => {
      expect(screen.getByTestId("add-provider")).toBeTruthy();
    });
    fireEvent.click(screen.getByTestId("add-provider"));
    const dialog = await screen.findByRole("dialog", { name: "Add provider" });
    expect(dialog.getAttribute("data-manual")).toBe("true");
  });

  it("drills down into an existing provider from Accounts", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    const name = await screen.findByTestId("provider-account-drilldown");
    fireEvent.click(name);
    const dialog = await screen.findByRole("dialog", { name: /Configure OpenRouter/ });
    expect(dialog.getAttribute("data-manual")).toBe("false");
    expect((screen.getByTestId("provider-config-field-name") as HTMLInputElement).value).toBe("openrouter");
  });
});
