import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPane, { clearSettingsSnapshot } from "../components/SettingsPane";
import { api } from "../lib/api";

vi.mock("../lib/api", () => {
  // Calls the test does not name resolve to null.
  const named: Record<string, unknown> = {
    settings: vi.fn(),
    providers: vi.fn().mockResolvedValue([]),
    authPools: vi.fn().mockResolvedValue({ pools: [] }),
    getAuthPools: vi.fn().mockResolvedValue({ pools: [] }),
    getHooks: vi.fn().mockResolvedValue({ hooks: [], events: [] }),
    getWikiConfig: vi.fn().mockResolvedValue({ api_base: "", has_token: false }),
    archiveStatus: vi.fn().mockResolvedValue({ chats: 0, vault_present: false, backup_dir: "", archive_db: "" }),
    platformAdapters: vi.fn().mockResolvedValue([]),
    startAuthOAuth: vi.fn(),
    completeAuthOAuth: vi.fn(),
    cancelAuthOAuth: vi.fn().mockResolvedValue({ ok: true }),
    setProviderEnabled: vi.fn().mockResolvedValue({ ok: true }),
    getClaudeCliStatus: vi.fn(),
  };
  const api = new Proxy(named, {
    get(target, key: string) {
      if (!(key in target)) target[key] = vi.fn().mockResolvedValue(null);
      return target[key];
    },
  });
  return { api };
});

vi.mock("../components/SkillsPane", () => ({ default: () => <div /> }));
vi.mock("../components/MemoryPane", () => ({ default: () => <div /> }));
vi.mock("../components/SchedulesPane", () => ({ default: () => <div /> }));

const CLAUDE_CODE_OFF = {
  name: "claude-code", display_name: "Claude Code (Max)", env_var: "CLAUDE_CODE_LOGIN",
  base_url: "", has_key: false, masked: "", api_mode: "claude_cli", has_env: false,
  disconnected: true, worker_capability: "pilot_only",
};

function providerRow(name: string): HTMLElement {
  const title = screen.getAllByTestId("provider-account-drilldown").find((el) => el.getAttribute("data-provider") === name);
  const row = title?.closest(".bg-panel2");
  if (!(row instanceof HTMLElement)) throw new Error(`${name} row not found`);
  return row;
}

describe("Settings Claude Code provider row", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
    localStorage.clear();
    vi.mocked(api.settings).mockResolvedValue({
      driver: "cursor", reach: "repo", budget: 100, models: [], auto_distill: false,
      state_dir: "/tmp/state", repo: "/tmp/repo", has_api_key: false,
    } as never);
    vi.mocked(api.getClaudeCliStatus).mockResolvedValue({
      ok: true, installed: true, authenticated: true, label: "me@example.com",
    } as never);
  });

  it("offers a switch, not a paste field, when Claude Code is off", async () => {
    vi.mocked(api.providers).mockResolvedValue([CLAUDE_CODE_OFF] as never);
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    const keys = screen.getByRole("button", { name: /API keys/ });
    if (keys.getAttribute("aria-expanded") === "false") fireEvent.click(keys);

    await waitFor(() => expect(within(providerRow("claude-code")).getByText("disabled - via login")).toBeInTheDocument());
    const row = providerRow("claude-code");
    expect(within(row).queryByPlaceholderText("CLAUDE_CODE_LOGIN...")).toBeNull();

    vi.mocked(api.providers).mockResolvedValue([{ ...CLAUDE_CODE_OFF, disconnected: false, has_key: true }] as never);
    fireEvent.click(within(row).getByRole("switch"));
    await waitFor(() => expect(api.setProviderEnabled).toHaveBeenCalledWith("claude-code", true));
    await waitFor(() => expect(within(providerRow("claude-code")).getByText("connected - via login")).toBeInTheDocument());
  });

  it("says on the Accounts card that a signed-in Claude Code is off", async () => {
    vi.mocked(api.providers).mockResolvedValue([CLAUDE_CODE_OFF] as never);
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    const plans = screen.getByRole("button", { name: /Optional plan sign-in/ });
    if (plans.getAttribute("aria-expanded") === "false") fireEvent.click(plans);

    expect(await screen.findByText("Signed in as me@example.com - off in API keys")).toBeInTheDocument();
  });
});
