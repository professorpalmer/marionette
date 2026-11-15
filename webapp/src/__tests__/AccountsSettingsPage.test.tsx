import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccountsSettingsPage from "../components/AccountsSettingsPage";
import { bedrockPatch } from "../components/AccountsSettingsGroups";
import { api, type AccountRow } from "../lib/api";

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

const pools = {
  strategies: ["fill_first", "round_robin", "least_used", "random"],
  pools: [
    {
      provider: "openrouter",
      strategy: "fill_first",
      entries: [
        { id: "e1", label: "plan-a", auth_type: "api_key", source: "manual", priority: 0, masked: "sk-or-****1111", last_status: "ok" },
        { id: "e2", label: "plan-b", auth_type: "api_key", source: "manual", priority: 1, masked: "sk-or-****2222", last_status: "exhausted" },
      ],
    },
    { provider: "openai", strategy: "fill_first", entries: [] },
    {
      provider: "cursor",
      strategy: "fill_first",
      entries: [{ id: "env", label: "env:CURSOR_API_KEY", auth_type: "api_key", source: "env", priority: 0, masked: "crsr****0000", last_status: "ok" }],
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
    vi.mocked(api.getAuthPools).mockResolvedValue(pools as never);
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

describe("Extra API keys", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAccounts).mockResolvedValue({ accounts: [] });
    vi.mocked(api.providers).mockResolvedValue([]);
    vi.mocked(api.config).mockResolvedValue({} as never);
    vi.mocked(api.getCursorCliStatus).mockResolvedValue(null as never);
    vi.mocked(api.getAuthPools).mockResolvedValue(pools as never);
    vi.mocked(api.getBedrockStatus).mockResolvedValue({ configured: false, has_key: false } as never);
    vi.mocked(api.getPlatform).mockResolvedValue({ adapters: [] });
  });

  it("lists only pools with two or more keys, one row each, with the entries", async () => {
    render(<AccountsSettingsPage />);
    const rows = await screen.findAllByTestId("credential-pool-row");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("openrouter");
    expect(rows[0]).toHaveTextContent("2 credentials");
    expect(rows[0]).toHaveTextContent("sk-or-****2222");
    expect(rows[0]).toHaveTextContent("Limit reached");
    // A one-key pool is the provider row in the list above.
    expect(screen.queryByText("crsr****0000")).toBeNull();
  });

  it("sets the key order with setAuthPoolStrategy", async () => {
    vi.mocked(api.setAuthPoolStrategy).mockResolvedValue({} as never);
    render(<AccountsSettingsPage />);
    const select = await screen.findByLabelText("Key order for openrouter");
    fireEvent.change(select, { target: { value: "round_robin" } });
    await waitFor(() => expect(api.setAuthPoolStrategy).toHaveBeenCalledWith("openrouter", "round_robin"));
  });

  it("removes one entry with removeAuthPoolEntry", async () => {
    vi.mocked(api.removeAuthPoolEntry).mockResolvedValue({} as never);
    render(<AccountsSettingsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove plan-b from openrouter" }));
    await waitFor(() => expect(api.removeAuthPoolEntry).toHaveBeenCalledWith("openrouter", "e2"));
  });

  it("adds a key with addAuthPoolKey", async () => {
    vi.mocked(api.addAuthPoolKey).mockResolvedValue({} as never);
    render(<AccountsSettingsPage />);
    const group = await screen.findByTestId("credential-pools");
    fireEvent.click(within(group).getByRole("button", { name: "Add key" }));
    fireEvent.change(screen.getByLabelText("Provider for the extra key"), { target: { value: "openai" } });
    fireEvent.change(screen.getByLabelText("Extra API key"), { target: { value: " sk-extra " } });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "plan-c" } });
    fireEvent.click(within(group).getByRole("button", { name: "Save key" }));
    await waitFor(() => expect(api.addAuthPoolKey).toHaveBeenCalledWith("openai", "sk-extra", "plan-c"));
    expect(await within(group).findByText("Key added to openai.")).toBeTruthy();
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
