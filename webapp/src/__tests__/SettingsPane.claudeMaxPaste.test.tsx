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

function claudeMaxRow(): HTMLElement {
  const title = screen.getByText("Claude Max");
  const row = title.closest(".bg-panel2");
  if (!(row instanceof HTMLElement)) throw new Error("Claude Max row not found");
  return row;
}

describe("Settings Claude Max paste sign-in", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearSettingsSnapshot();
    localStorage.clear();
    vi.mocked(api.settings).mockResolvedValue({
      driver: "cursor", reach: "repo", budget: 100, models: [], auto_distill: false,
      state_dir: "/tmp/state", repo: "/tmp/repo", has_api_key: false,
    } as never);
    vi.mocked(api.startAuthOAuth).mockResolvedValue({
      session_id: "sess-1", auth_url: "https://claude.ai/oauth/authorize?x=1",
    } as never);
    vi.mocked(api.completeAuthOAuth).mockResolvedValue({ status: "done", label: "claude-max" } as never);
    vi.spyOn(window, "open").mockImplementation(() => null);
  });

  it("enables Complete while it waits for the pasted code", async () => {
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    const plans = screen.getByRole("button", { name: /Optional plan sign-in/ });
    if (plans.getAttribute("aria-expanded") === "false") fireEvent.click(plans);

    fireEvent.click(within(claudeMaxRow()).getByRole("button", { name: "Sign in" }));
    const input = await within(claudeMaxRow()).findByPlaceholderText("paste authorization code#state");
    fireEvent.change(input, { target: { value: "abc#state" } });

    const complete = within(claudeMaxRow()).getByRole("button", { name: "Complete" });
    expect(complete).toBeEnabled();
    // Only the Claude Max row waits; the other rows still say Sign in.
    expect(screen.queryByText("Waiting for browser...")).toBeNull();
    expect(screen.queryByText("Waiting for login...")).toBeNull();

    fireEvent.click(complete);
    await waitFor(() => expect(api.completeAuthOAuth).toHaveBeenCalledWith("sess-1", "abc#state", "anthropic"));
    await waitFor(() => expect(within(claudeMaxRow()).queryByPlaceholderText("paste authorization code#state")).toBeNull());
  });

  it("keeps the paste step open after a failed Complete", async () => {
    vi.mocked(api.completeAuthOAuth).mockResolvedValue({ status: "error", error: "bad code" } as never);
    render(<SettingsPane onOpenWizard={vi.fn()} section="providers" />);
    const plans = screen.getByRole("button", { name: /Optional plan sign-in/ });
    if (plans.getAttribute("aria-expanded") === "false") fireEvent.click(plans);

    fireEvent.click(within(claudeMaxRow()).getByRole("button", { name: "Sign in" }));
    const input = await within(claudeMaxRow()).findByPlaceholderText("paste authorization code#state");
    fireEvent.change(input, { target: { value: "wrong#state" } });
    fireEvent.click(within(claudeMaxRow()).getByRole("button", { name: "Complete" }));

    await waitFor(() => expect(within(claudeMaxRow()).getByRole("button", { name: "Complete" })).toBeEnabled());
  });
});
