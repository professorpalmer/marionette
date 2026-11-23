import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccountsSettingsPage from "../components/AccountsSettingsPage";
import { bedrockPatch } from "../components/AccountsSettingsGroups";
import { api, type AccountRow, type AuthPoolsResponse } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getAccounts: vi.fn(),
    probeAccount: vi.fn(),
    signOutAccount: vi.fn(),
    providers: vi.fn(),
    config: vi.fn(),
    settings: vi.fn(),
    setProviderKey: vi.fn(),
    clearProviderKey: vi.fn(),
    setProviderEnabled: vi.fn(),
    startAuthOAuth: vi.fn(),
    pollAuthOAuth: vi.fn(),
    cancelAuthOAuth: vi.fn(),
    getCursorCliStatus: vi.fn(),
    getAuthPools: vi.fn(),
    addAuthPoolKey: vi.fn(),
    removeAuthPoolEntry: vi.fn(),
    checkAuthPoolEntry: vi.fn(),
    setAuthPoolStrategy: vi.fn(),
    getBedrockStatus: vi.fn(),
    setBedrockCredentials: vi.fn(),
    clearBedrockCredentials: vi.fn(),
    getPlatform: vi.fn(),
    togglePlatform: vi.fn(),
  },
}));

const accounts: AccountRow[] = [
  {
    provider: "openrouter", name: "OpenRouter", state: "api_key", account: "....abcd",
    detail: "API key set.", action: "", methods: ["api_key"], check: null,
  },
  {
    provider: "openai-codex", name: "ChatGPT Codex", state: "not_set", account: "",
    detail: "Not connected.", action: "sign_in", methods: ["sign_in"], check: null,
  },
];

const openrouterInfo = {
  name: "openrouter",
  display_name: "OpenRouter",
  env_var: "OPENROUTER_API_KEY",
  base_url: "https://openrouter.ai/api/v1",
  api_mode: "chat_completions",
  has_key: true,
  masked: "sk-or-****abcd",
};

const pools: AuthPoolsResponse = {
  strategies: ["fill_first", "round_robin", "least_used", "random"],
  key_providers: [
    { provider: "openai", name: "OpenAI" },
    { provider: "opencode-go", name: "OpenCode Go" },
    { provider: "openrouter", name: "OpenRouter" },
  ],
  pools: [
    {
      provider: "openrouter",
      name: "OpenRouter",
      strategy: "fill_first",
      entries: [
        { id: "e1", label: "plan-a", auth_type: "api_key", source: "manual", priority: 0, masked: "sk-or-****1111", last_status: "ok", request_count: 7, usable: true, first: true },
        { id: "e2", label: "plan-b", auth_type: "api_key", source: "manual", priority: 1, masked: "sk-or-****2222", last_status: "exhausted", last_error_code: 429, usable: false, first: false },
      ],
    },
    { provider: "openai", name: "OpenAI", strategy: "fill_first", entries: [] },
    {
      provider: "cursor",
      name: "cursor",
      strategy: "fill_first",
      entries: [{ id: "env", label: "env:CURSOR_API_KEY", auth_type: "api_key", source: "env", priority: 0, masked: "crsr****0000", last_status: "ok", usable: true, first: true }],
    },
    {
      // The live case of 2026-11-22: the oldest key has no plan, two new keys wait behind it.
      provider: "opencode-go",
      name: "OpenCode Go",
      strategy: "fill_first",
      entries: [
        { id: "new", label: "opencode-go-settings", auth_type: "api_key", source: "manual", priority: -1, masked: "sk-n…DQ-7", last_status: "ok", request_count: 0, usable: true, first: true },
        {
          id: "old", label: "keys.json", auth_type: "api_key", source: "keys.json", priority: 0, masked: "sk-o…KeKe",
          last_status: "exhausted", last_error_code: 403, request_count: 95, usable: false, first: false,
          last_error_message: '{"error":{"type":"permission_error","message":"An active OpenCode Go subscription is required to use OpenCode Go models."}}',
        },
      ],
    },
  ],
};

const adapters = [
  { name: "cursor", enabled: false, implement_capable: true, available: true, note: "Cursor Agent CLI." },
  { name: "codex", enabled: true, implement_capable: true, available: false, note: "Codex CLI." },
];

function rowOf(provider: string): HTMLElement {
  const row = screen.getAllByTestId("account-row").find((r) => r.getAttribute("data-provider") === provider);
  if (!row) throw new Error(`no row ${provider}`);
  return row;
}

describe("AccountsSettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAccounts).mockResolvedValue({ accounts });
    vi.mocked(api.providers).mockResolvedValue([openrouterInfo] as never);
    vi.mocked(api.config).mockResolvedValue({} as never);
    vi.mocked(api.settings).mockResolvedValue({ repo: "/tmp/repo" } as never);
    vi.mocked(api.getCursorCliStatus).mockResolvedValue({ installed: true, authenticated: false } as never);
    vi.mocked(api.getAuthPools).mockResolvedValue(pools);
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: false, has_key: false } as never);
    vi.mocked(api.getPlatform).mockResolvedValue({ adapters });
    vi.mocked(api.probeAccount).mockResolvedValue({ row: accounts[1] } as never);
  });

  it("renders the provider rows and the three settings groups, and nothing else", async () => {
    render(<AccountsSettingsPage />);
    expect(await screen.findByRole("heading", { name: "Accounts" })).toBeTruthy();
    await waitFor(() => expect(screen.getAllByTestId("account-row")).toHaveLength(2));
    expect(screen.getByTestId("credential-pools")).toBeTruthy();
    expect(screen.getByTestId("bedrock")).toBeTruthy();
    await waitFor(() => expect(screen.getAllByTestId("worker-platform-row")).toHaveLength(2));
    // Removed: the duplicate "Add provider" path and the unused GitHub wiki repo block.
    expect(screen.queryByTestId("add-provider")).toBeNull();
    expect(screen.queryByText(/GitHub/)).toBeNull();
    // No accordion on this page.
    expect(document.querySelector("[aria-expanded]")).toBeNull();
  });

  it("shows startup key-store issues", async () => {
    vi.mocked(api.config).mockResolvedValue({
      key_bootstrap_issues: [{ step: "keys.json", message: "permission denied" }],
    } as never);
    render(<AccountsSettingsPage />);
    const alert = await screen.findByTestId("settings-key-bootstrap-issues");
    expect(alert).toHaveTextContent("keys.json: permission denied");
  });

  it("opens the provider details and saves a key through setProviderKey", async () => {
    vi.mocked(api.setProviderKey).mockResolvedValue({ ok: true } as never);
    render(<AccountsSettingsPage />);
    await waitFor(() => expect(api.providers).toHaveBeenCalled());
    const name = await within(await waitFor(() => rowOf("openrouter"))).findByTestId("provider-account-drilldown");
    fireEvent.click(name);
    await screen.findByRole("dialog", { name: /Configure OpenRouter/ });
    fireEvent.change(screen.getByTestId("provider-config-field-api_key"), { target: { value: "sk-new" } });
    fireEvent.click(screen.getByTestId("provider-config-submit"));
    await waitFor(() => expect(api.setProviderKey).toHaveBeenCalledWith("openrouter", "sk-new"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("keeps the details modal open when the key is rejected", async () => {
    vi.mocked(api.setProviderKey).mockRejectedValueOnce(new Error("Provider rejected the key"));
    render(<AccountsSettingsPage />);
    await waitFor(() => expect(api.providers).toHaveBeenCalled());
    fireEvent.click(within(await waitFor(() => rowOf("openrouter"))).getByTestId("provider-account-drilldown"));
    await screen.findByRole("dialog", { name: /Configure OpenRouter/ });
    fireEvent.change(screen.getByTestId("provider-config-field-api_key"), { target: { value: "sk-bad" } });
    fireEvent.click(screen.getByTestId("provider-config-submit"));
    expect(await screen.findByText("Provider rejected the key")).toBeTruthy();
    expect(screen.getByRole("dialog", { name: /Configure OpenRouter/ })).toBeTruthy();
  });

  it("runs the Codex device sign-in and shows the code on the row", async () => {
    vi.mocked(api.startAuthOAuth).mockResolvedValue({
      session_id: "s1", user_code: "ABCD-1234", verification_uri: "https://auth.example/device",
      interval: 1, expires_in: 60,
    } as never);
    vi.mocked(api.pollAuthOAuth).mockResolvedValue({ status: "done", label: "me@example.com" } as never);
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<AccountsSettingsPage />);
    await waitFor(() => rowOf("openai-codex"));
    fireEvent.click(within(rowOf("openai-codex")).getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(within(rowOf("openai-codex")).getByText(/Enter code ABCD-1234/)).toBeTruthy());
    expect(api.startAuthOAuth).toHaveBeenCalledWith("openai-codex");
    await waitFor(() => expect(api.pollAuthOAuth).toHaveBeenCalledWith("s1", "openai-codex"), { timeout: 3000 });
    await waitFor(() => expect(api.probeAccount).toHaveBeenCalledWith("openai-codex"));
    open.mockRestore();
  });

  it("shows a failed sign-in reason on the row", async () => {
    vi.mocked(api.startAuthOAuth).mockRejectedValue(new Error("device codes are not enabled"));
    render(<AccountsSettingsPage />);
    await waitFor(() => rowOf("openai-codex"));
    fireEvent.click(within(rowOf("openai-codex")).getByRole("button", { name: "Sign in" }));
    const result = await within(rowOf("openai-codex")).findByTestId("account-result");
    expect(result).toHaveTextContent("device codes are not enabled");
    expect(result).toHaveTextContent("Turn on ChatGPT device codes");
  });
});

describe("Stored keys", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAccounts).mockResolvedValue({ accounts: [] });
    vi.mocked(api.providers).mockResolvedValue([]);
    vi.mocked(api.config).mockResolvedValue({} as never);
    vi.mocked(api.getCursorCliStatus).mockResolvedValue(null as never);
    vi.mocked(api.getAuthPools).mockResolvedValue(pools);
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: false, has_key: false } as never);
    vi.mocked(api.getPlatform).mockResolvedValue({ adapters: [] });
  });

  const poolRow = async (name: string): Promise<HTMLElement> => {
    const rows = await screen.findAllByTestId("credential-pool-row");
    const row = rows.find((r) => r.textContent?.startsWith(name));
    if (!row) throw new Error(`no pool row ${name}`);
    return row;
  };

  it("lists each pool that holds a credential, with the count and each entry", async () => {
    render(<AccountsSettingsPage />);
    const rows = await screen.findAllByTestId("credential-pool-row");
    // openai holds nothing. A pool with one key is in the list too.
    expect(rows).toHaveLength(3);
    const openrouter = await poolRow("OpenRouter");
    expect(openrouter).toHaveTextContent("2 credentials, 1 not usable. Order: First key until it fails.");
    expect(openrouter).toHaveTextContent("sk-or-****2222");
    expect(openrouter).toHaveTextContent("Limit reached");
    const cursor = await poolRow("cursor");
    expect(cursor).toHaveTextContent("1 credential.");
    expect(cursor).toHaveTextContent("From the environment (CURSOR_API_KEY)");
    // One key has no order to set.
    expect(within(cursor).queryByRole("combobox")).toBeNull();
  });

  it("shows the order, the state of each key, and the provider's reason for a rejected key", async () => {
    render(<AccountsSettingsPage />);
    const entries = within(await poolRow("OpenCode Go")).getAllByTestId("credential-pool-entry");
    expect(entries.map((e) => e.getAttribute("data-state"))).toEqual(["in_use", "rejected"]);
    expect(entries[0]).toHaveTextContent("1.Saved in Settingssk-n…DQ-7In useAPI key, 0 requests");
    expect(entries[1]).toHaveTextContent("2.From the key filesk-o…KeKeRejectedAPI key, 95 requests");
    expect(entries[1]).toHaveTextContent(
      "HTTP 403: An active OpenCode Go subscription is required to use OpenCode Go models.",
    );
  });

  it("sets the key order with setAuthPoolStrategy", async () => {
    vi.mocked(api.setAuthPoolStrategy).mockResolvedValue({} as never);
    render(<AccountsSettingsPage />);
    const select = await screen.findByLabelText("Key order for openrouter");
    fireEvent.change(select, { target: { value: "round_robin" } });
    await waitFor(() => expect(api.setAuthPoolStrategy).toHaveBeenCalledWith("openrouter", "round_robin"));
  });

  it("removes one entry only after the confirm", async () => {
    vi.mocked(api.removeAuthPoolEntry).mockResolvedValue({} as never);
    render(<AccountsSettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove plan-b (sk-or-****2222) from openrouter" }));
    expect(api.removeAuthPoolEntry).not.toHaveBeenCalled();
    expect(screen.getByText(/It stays valid at the provider/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm: remove plan-b (sk-or-****2222) from openrouter" }));
    await waitFor(() => expect(api.removeAuthPoolEntry).toHaveBeenCalledWith("openrouter", "e2"));
    // The list loads again after the removal.
    await waitFor(() => expect(api.getAuthPools).toHaveBeenCalledTimes(2));
  });

  it("does not remove when the confirm is cancelled", async () => {
    render(<AccountsSettingsPage />);
    const row = await poolRow("OpenRouter");
    fireEvent.click(within(row).getByRole("button", { name: "Remove plan-a (sk-or-****1111) from openrouter" }));
    fireEvent.click(within(row).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText(/It stays valid at the provider/)).toBeNull();
    expect(api.removeAuthPoolEntry).not.toHaveBeenCalled();
  });

  it("checks one key and shows the provider's answer on that key", async () => {
    vi.mocked(api.checkAuthPoolEntry).mockResolvedValue({
      provider: "opencode-go", strategy: "fill_first", entries: [],
      check: { entry_id: "old", ok: false, status: 403, message: "An active OpenCode Go subscription is required.", at: 1795400000 },
    });
    render(<AccountsSettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Check From the key file (sk-o…KeKe) of opencode-go" }));
    await waitFor(() => expect(api.checkAuthPoolEntry).toHaveBeenCalledWith("opencode-go", "old"));
    const line = await screen.findByTestId("credential-pool-check");
    expect(line).toHaveTextContent("failed (HTTP 403, An active OpenCode Go subscription is required.)");
    expect(line.getAttribute("role")).toBe("alert");
  });

  it("says so when a provider has no check", async () => {
    vi.mocked(api.checkAuthPoolEntry).mockResolvedValue({
      provider: "cursor", strategy: "fill_first", entries: [], error: "No check for this provider.",
    });
    render(<AccountsSettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: /^Check From the environment/ }));
    expect(await screen.findByTestId("credential-pool-check")).toHaveTextContent("No check for this provider.");
  });

  it("loads the list again when a provider row saves a key", async () => {
    render(<AccountsSettingsPage />);
    await screen.findAllByTestId("credential-pool-row");
    expect(api.getAuthPools).toHaveBeenCalledTimes(1);
    window.dispatchEvent(new Event("harness-config-changed"));
    await waitFor(() => expect(api.getAuthPools).toHaveBeenCalledTimes(2));
  });

  it("adds a key to a provider that the server names, with addAuthPoolKey", async () => {
    vi.mocked(api.addAuthPoolKey).mockResolvedValue({} as never);
    render(<AccountsSettingsPage />);
    const group = await screen.findByTestId("credential-pools");
    await screen.findAllByTestId("credential-pool-row");
    fireEvent.click(within(group).getByRole("button", { name: "Add key" }));
    const select = screen.getByLabelText("Provider for the extra key");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["OpenAI", "OpenCode Go", "OpenRouter"]);
    fireEvent.change(select, { target: { value: "opencode-go" } });
    fireEvent.change(screen.getByLabelText("Extra API key"), { target: { value: " sk-extra " } });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "plan-c" } });
    fireEvent.click(within(group).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(api.addAuthPoolKey).toHaveBeenCalledWith("opencode-go", "sk-extra", "plan-c"));
    expect(await within(group).findByText("Key added to opencode-go.")).toBeTruthy();
  });
});

describe("AWS Bedrock", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAccounts).mockResolvedValue({ accounts: [] });
    vi.mocked(api.providers).mockResolvedValue([]);
    vi.mocked(api.config).mockResolvedValue({} as never);
    vi.mocked(api.getCursorCliStatus).mockResolvedValue(null as never);
    vi.mocked(api.getAuthPools).mockResolvedValue({ pools: [] } as never);
    vi.mocked(api.getPlatform).mockResolvedValue({ adapters: [] });
  });

  it("saves a bearer token and region with setBedrockCredentials", async () => {
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: false, has_key: false } as never);
    vi.mocked(api.setBedrockCredentials).mockResolvedValue({ configured: true, has_key: true, auth_mode: "bearer" } as never);
    render(<AccountsSettingsPage />);
    const group = await screen.findByTestId("bedrock");
    await within(group).findByText("Not set");
    fireEvent.click(within(group).getByRole("button", { name: "Set up" }));
    fireEvent.change(screen.getByLabelText("Bedrock bearer token"), { target: { value: "tok" } });
    fireEvent.change(screen.getByLabelText("AWS region"), { target: { value: "us-east-1" } });
    fireEvent.click(within(group).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(api.setBedrockCredentials).toHaveBeenCalledWith({
      AWS_BEARER_TOKEN_BEDROCK: "tok",
      AWS_REGION: "us-east-1",
      BEDROCK_REGION: "",
      BEDROCK_MODEL_ID: "",
    }));
    expect(await within(group).findByText("Configured (bearer)")).toBeTruthy();
  });

  it("does not send an empty form", async () => {
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: false, has_key: false } as never);
    render(<AccountsSettingsPage />);
    const group = await screen.findByTestId("bedrock");
    fireEvent.click(await within(group).findByRole("button", { name: "Set up" }));
    fireEvent.click(within(group).getByRole("button", { name: "Save" }));
    expect(await within(group).findByRole("alert")).toHaveTextContent("Enter a bearer token");
    expect(api.setBedrockCredentials).not.toHaveBeenCalled();
  });

  it("disconnects with clearBedrockCredentials", async () => {
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: true, has_key: true, auth_mode: "keys" } as never);
    vi.mocked(api.clearBedrockCredentials).mockResolvedValue({ configured: false, has_key: false } as never);
    render(<AccountsSettingsPage />);
    const group = await screen.findByTestId("bedrock");
    await within(group).findByText("Configured (keys)");
    fireEvent.click(within(group).getByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(api.clearBedrockCredentials).toHaveBeenCalledTimes(1));
    expect(await within(group).findByText("Not set")).toBeTruthy();
  });

  it("bedrockPatch keeps stored secrets when their fields are empty", () => {
    const draft = { bearer: "", accessKey: "", secretKey: "", sessionToken: "", region: "eu-west-1", regionAlt: "", modelId: "" };
    expect(bedrockPatch(draft, true)).toEqual({ AWS_REGION: "eu-west-1", BEDROCK_REGION: "", BEDROCK_MODEL_ID: "" });
    expect(bedrockPatch({ ...draft, region: "" }, false)).toBeNull();
    expect(bedrockPatch({ ...draft, region: "", accessKey: "a", secretKey: "b" }, false)).toEqual({
      AWS_ACCESS_KEY_ID: "a", AWS_SECRET_ACCESS_KEY: "b", AWS_REGION: "", BEDROCK_REGION: "", BEDROCK_MODEL_ID: "",
    });
  });
});

describe("Worker platforms", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAccounts).mockResolvedValue({ accounts: [] });
    vi.mocked(api.providers).mockResolvedValue([]);
    vi.mocked(api.config).mockResolvedValue({} as never);
    vi.mocked(api.getCursorCliStatus).mockResolvedValue(null as never);
    vi.mocked(api.getAuthPools).mockResolvedValue({ pools: [] } as never);
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: false, has_key: false } as never);
    vi.mocked(api.getPlatform).mockResolvedValue({ adapters });
  });

  it("shows one switch per adapter and toggles with togglePlatform", async () => {
    vi.mocked(api.togglePlatform).mockResolvedValue({ adapters: [{ ...adapters[0], enabled: true }, adapters[1]] });
    render(<AccountsSettingsPage />);
    const cursor = await screen.findByRole("switch", { name: "Use cursor for workers" });
    expect(cursor).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "Use codex for workers" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getAllByTestId("worker-platform-row")[1]).toHaveTextContent("Not installed");
    fireEvent.click(cursor);
    await waitFor(() => expect(api.togglePlatform).toHaveBeenCalledWith("cursor", true));
    await waitFor(() => expect(screen.getByRole("switch", { name: "Use cursor for workers" })).toHaveAttribute("aria-checked", "true"));
  });
});
