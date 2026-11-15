import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AccountsList, { accountStateText } from "../components/AccountsList";
import { accountsErrorLink } from "../components/TranscriptList";
import { api, type AccountRow } from "../lib/api";

vi.mock("../lib/api", () => ({
  api: {
    getAccounts: vi.fn(),
    probeAccount: vi.fn(),
    signOutAccount: vi.fn(),
    startAuthOAuth: vi.fn(),
    completeAuthOAuth: vi.fn(),
    cancelAuthOAuth: vi.fn().mockResolvedValue({ ok: true }),
    setProviderKey: vi.fn().mockResolvedValue({ ok: true }),
    clearProviderKey: vi.fn().mockResolvedValue({ ok: true }),
    setProviderEnabled: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

const claude = (over: Partial<AccountRow> = {}): AccountRow => ({
  provider: "anthropic",
  name: "Claude (Anthropic)",
  state: "expired",
  account: "....9QAA",
  detail: "Sign-in expired. Sign in again.",
  action: "sign_in",
  methods: ["sign_in", "api_key"],
  check: null,
  ...over,
});

const openrouter = (over: Partial<AccountRow> = {}): AccountRow => ({
  provider: "openrouter",
  name: "OpenRouter",
  state: "not_set",
  account: "",
  detail: "Not connected.",
  action: "add_key",
  methods: ["api_key"],
  check: null,
  ...over,
});

function renderList(rows: AccountRow[]) {
  vi.mocked(api.getAccounts).mockResolvedValue({ accounts: rows });
  return render(
    <AccountsList
      onExternalSignIn={vi.fn().mockResolvedValue(undefined)}
      onExternalSignOut={vi.fn().mockResolvedValue(undefined)}
      onCancelFlow={vi.fn()}
      flow={null}
      onOpenDetails={vi.fn()}
    />,
  );
}

async function rowFor(provider: string): Promise<HTMLElement> {
  const rows = await screen.findAllByTestId("account-row");
  const row = rows.find((r) => r.getAttribute("data-provider") === provider);
  if (!row) throw new Error(`no row for ${provider}`);
  return row;
}

describe("AccountsList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "open").mockReturnValue(null);
  });

  it("gives each state one text", () => {
    expect(accountStateText({ state: "signed_in", account: "cary@example.com" })).toBe("Signed in as cary@example.com");
    expect(accountStateText({ state: "expired", account: "" })).toBe("Expired: sign in again");
    expect(accountStateText({ state: "api_key", account: "....abcd" })).toBe("API key set (....abcd)");
    expect(accountStateText({ state: "off", account: "" })).toBe("Off");
  });

  it("shows an expired Claude sign-in with one action: Sign in again", async () => {
    renderList([claude()]);
    const row = await rowFor("anthropic");
    expect(within(row).getByTestId("account-state")).toHaveTextContent("Expired: sign in again");
    expect(within(row).getByRole("button", { name: "Sign in again" })).toBeEnabled();
    expect(within(row).queryByText(/connected/i)).toBeNull();
    expect(within(row).queryByText(/env:/)).toBeNull();
  });

  it("runs the Claude sign-in and shows the account and the live check", async () => {
    vi.mocked(api.startAuthOAuth).mockResolvedValue({ session_id: "s1", provider: "anthropic", auth_url: "https://claude.ai/x" });
    const signedIn = claude({
      state: "signed_in",
      account: "cary@example.com",
      action: "",
      check: { ok: true, status: 200, message: "", at: 1_763_200_000 },
    });
    vi.mocked(api.completeAuthOAuth).mockImplementation(async () => {
      // The backend store holds the new sign-in before the reply.
      vi.mocked(api.getAccounts).mockResolvedValue({ accounts: [signedIn] });
      return { status: "done", label: "cary@example.com", row: signedIn };
    });
    renderList([claude()]);
    const row = await rowFor("anthropic");
    fireEvent.click(within(row).getByRole("button", { name: "Sign in again" }));
    const code = await within(row).findByLabelText("Claude sign-in code");
    const finish = within(row).getByRole("button", { name: "Finish sign-in" });
    expect(finish).toBeDisabled();
    fireEvent.change(code, { target: { value: "abc#state" } });
    expect(finish).toBeEnabled();
    fireEvent.click(finish);

    const result = await within(row).findByTestId("account-result");
    expect(result).toHaveTextContent("Sign-in finished. Signed in as cary@example.com.");
    expect(result).toHaveTextContent("the provider accepted it (HTTP 200)");
    expect(within(row).getByTestId("account-state")).toHaveTextContent("Signed in as cary@example.com");
    expect(api.completeAuthOAuth).toHaveBeenCalledWith("s1", "abc#state", "anthropic");
  });

  it("keeps the code field open and shows the exact failure", async () => {
    vi.mocked(api.startAuthOAuth).mockResolvedValue({ session_id: "s1", provider: "anthropic", auth_url: "https://claude.ai/x" });
    vi.mocked(api.completeAuthOAuth).mockResolvedValue({ status: "error", error: "OAuth state mismatch" });
    renderList([claude()]);
    const row = await rowFor("anthropic");
    fireEvent.click(within(row).getByRole("button", { name: "Sign in again" }));
    fireEvent.change(await within(row).findByLabelText("Claude sign-in code"), { target: { value: "bad" } });
    fireEvent.click(within(row).getByRole("button", { name: "Finish sign-in" }));
    expect(await within(row).findByRole("alert")).toHaveTextContent("OAuth state mismatch");
    expect(within(row).getByLabelText("Claude sign-in code")).toBeInTheDocument();
  });

  it("checks a saved key and shows what the provider said", async () => {
    vi.mocked(api.probeAccount).mockResolvedValue({
      row: openrouter({
        state: "rejected",
        account: "....dead",
        action: "add_key",
        check: { ok: false, status: 401, message: "User not found.", at: 1_763_200_000 },
      }),
    });
    renderList([openrouter()]);
    const row = await rowFor("openrouter");
    fireEvent.click(within(row).getByRole("button", { name: "Add key" }));
    fireEvent.change(within(row).getByLabelText("OpenRouter API key"), { target: { value: "sk-or-v1-dead" } });
    fireEvent.click(within(row).getByRole("button", { name: "Save key" }));
    const alert = await within(row).findByRole("alert");
    expect(alert).toHaveTextContent("Key saved, but it does not work.");
    expect(alert).toHaveTextContent("HTTP 401, User not found.");
    await waitFor(() => expect(row.getAttribute("data-state")).toBe("rejected"));
  });

  it("puts rows that work or need attention before untouched rows", async () => {
    renderList([openrouter(), claude()]);
    const rows = await screen.findAllByTestId("account-row");
    expect(rows.map((r) => r.getAttribute("data-provider"))).toEqual(["anthropic", "openrouter"]);
  });
});

describe("accountsErrorLink", () => {
  it("links a pilot error that names Settings > Accounts", () => {
    expect(accountsErrorLink("[error] pilot: Claude (Anthropic) sign-in expired. Sign in again in Settings > Accounts.")).toBe(true);
    expect(accountsErrorLink("[error] pilot: the provider stream went silent.")).toBe(false);
    expect(accountsErrorLink("See Settings > Accounts")).toBe(false);
  });
});
