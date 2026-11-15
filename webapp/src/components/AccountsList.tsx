import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AccountAction, type AccountRow, type AccountState } from "../lib/api";

/** Device-code and CLI sign-ins that SettingsPane runs; Claude (Anthropic) runs here. */
export type ExternalSignIn = (provider: string) => Promise<void>;

/** A flow that SettingsPane runs, keyed by the row provider. */
export type AccountFlow = { provider: string; hint: string } | null;

type Props = {
  onExternalSignIn: ExternalSignIn;
  onExternalSignOut: (provider: string) => Promise<void>;
  onCancelFlow: () => void;
  flow: AccountFlow;
  onOpenDetails: (provider: string) => void;
};

/** The one status text of a row. Every page that shows an account uses it. */
export function accountStateText(row: Pick<AccountRow, "state" | "account">): string {
  const state: AccountState = row.state;
  switch (state) {
    case "signed_in":
      return row.account ? `Signed in as ${row.account}` : "Signed in";
    case "api_key":
      return row.account ? `API key set (${row.account})` : "API key set";
    case "expired":
      return "Expired: sign in again";
    case "rejected":
      return "Key rejected: add a new key";
    case "limited":
      return "Rate limited";
    case "off":
      return "Off";
    case "not_set":
      return "Not connected";
    case "not_installed":
      return "CLI not installed";
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}

function stateTone(state: AccountState): string {
  switch (state) {
    case "signed_in":
    case "api_key":
      return "bg-good";
    case "limited":
      return "bg-warn";
    case "expired":
    case "rejected":
      return "bg-risk";
    case "off":
    case "not_set":
    case "not_installed":
      return "bg-faint";
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}

function actionLabel(row: AccountRow): string {
  const action: AccountAction = row.action;
  switch (action) {
    case "sign_in":
    case "cli_login":
      return row.state === "expired" ? "Sign in again" : "Sign in";
    case "add_key":
      return "Add key";
    case "turn_on":
      return "Turn on";
    case "install":
    case "":
      return "";
    default: {
      const exhaustive: never = action;
      return exhaustive;
    }
  }
}

/** The detail line only when it says more than the state text. */
function showDetail(row: AccountRow): boolean {
  if (!row.detail) return false;
  return row.state === "not_installed" || row.state === "limited"
    || (row.state === "signed_in" && row.detail.includes("without renewal"));
}

function checkText(row: AccountRow): string {
  const c = row.check;
  if (!c) return "";
  const when = new Date(c.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (c.ok) return `Checked ${when}: the provider accepted it${c.status ? ` (HTTP ${c.status})` : ""}.`;
  const why = c.message || (c.status ? `HTTP ${c.status}` : "no answer");
  return `Checked ${when}: failed (${c.status ? `HTTP ${c.status}, ` : ""}${why}).`;
}

const BUTTON = "rounded px-2.5 py-0.5 font-medium text-ui-10 border disabled:opacity-30 transition-colors";
const PRIMARY = `${BUTTON} bg-accent/15 hover:bg-accent/25 text-accent border-accent/30`;
const QUIET = "text-muted hover:text-txt text-ui-10 disabled:opacity-30";

/**
 * Settings > Accounts: one row per provider, one state, one primary action.
 * The rows come from /api/accounts, which reads the store the pilot uses.
 */
export default function AccountsList({
  onExternalSignIn,
  onExternalSignOut,
  onCancelFlow,
  flow,
  onOpenDetails,
}: Props) {
  const [rows, setRows] = useState<AccountRow[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState("");
  const [keyFor, setKeyFor] = useState("");
  const [keyInput, setKeyInput] = useState("");
  const [claude, setClaude] = useState<{ sessionId: string; code: string } | null>(null);
  // One result line per row after an action: what happened, in its words.
  const [result, setResult] = useState<Record<string, { ok: boolean; text: string }>>({});
  const mounted = useRef(true);

  const load = useCallback(async () => {
    try {
      const res = await api.getAccounts();
      if (!mounted.current) return;
      setRows(res.accounts);
      setLoadError("");
    } catch (e: unknown) {
      if (mounted.current) setLoadError(e instanceof Error ? e.message : "Accounts did not load.");
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    const onChanged = () => { void load(); };
    window.addEventListener("harness-config-changed", onChanged);
    return () => {
      mounted.current = false;
      window.removeEventListener("harness-config-changed", onChanged);
    };
  }, [load]);

  const putRow = (row: AccountRow | undefined) => {
    if (!row) return;
    setRows((prev) => (prev || []).map((r) => (r.provider === row.provider ? row : r)));
  };

  const announce = (provider: string, ok: boolean, text: string) =>
    setResult((prev) => ({ ...prev, [provider]: { ok, text } }));

  const changed = () => window.dispatchEvent(new Event("harness-config-changed"));

  /**
   * Probe after a sign-in or a saved key: the row shows what the provider said.
   * `done` names what the user did ("Key saved"); empty for a plain Check.
   */
  const probeAndReport = async (provider: string, done: string) => {
    const res = await api.probeAccount(provider);
    putRow(res.row);
    const row = res.row;
    if (!row) {
      announce(provider, false, res.error || "The provider did not answer.");
      return;
    }
    const works = row.state === "signed_in" || row.state === "api_key";
    const said = row.check ? checkText(row) : `${accountStateText(row)}.`;
    const lead = !done ? "" : works ? `${done}. ` : `${done}, but it does not work. `;
    announce(provider, works, `${lead}${said}`);
  };

  const run = async (provider: string, work: () => Promise<void>) => {
    setBusy(provider);
    try {
      await work();
    } catch (e: unknown) {
      announce(provider, false, e instanceof Error ? e.message : "The action failed.");
    } finally {
      if (mounted.current) setBusy("");
    }
  };

  const startClaude = (row: AccountRow) => run(row.provider, async () => {
    const start = await api.startAuthOAuth("anthropic");
    if (!start.session_id || !start.auth_url) throw new Error(start.error || "Sign-in did not start.");
    setClaude({ sessionId: start.session_id, code: "" });
    announce(row.provider, true, "Authorize in the browser, then paste the code here.");
    window.open(start.auth_url, "_blank");
  });

  const completeClaude = (row: AccountRow) => run(row.provider, async () => {
    if (!claude) return;
    const res = await api.completeAuthOAuth(claude.sessionId, claude.code.trim(), "anthropic");
    if (res.status !== "done") throw new Error(res.error || "Sign-in did not finish.");
    setClaude(null);
    putRow(res.row);
    const done = res.row;
    const works = done?.state === "signed_in";
    const said = done?.check ? ` ${checkText(done)}` : "";
    announce(row.provider, works, done
      ? `${works ? "Sign-in finished" : "Sign-in finished, but it does not work"}. ${accountStateText(done)}.${said}`
      : `Signed in as ${res.label || "Claude account"}.`);
    changed();
  });

  const cancelClaude = () => {
    if (claude) api.cancelAuthOAuth(claude.sessionId, "anthropic").catch(() => { /* best-effort */ });
    setClaude(null);
  };

  const signIn = (row: AccountRow) => {
    if (row.provider === "anthropic") return startClaude(row);
    return run(row.provider, async () => {
      await onExternalSignIn(row.provider);
      await probeAndReport(row.provider, "Sign-in finished");
    });
  };

  const saveKey = (row: AccountRow) => run(row.provider, async () => {
    const value = keyInput.trim();
    if (!value) return;
    await api.setProviderKey(row.provider, value);
    setKeyInput("");
    setKeyFor("");
    changed();
    await probeAndReport(row.provider, "Key saved");
  });

  const primary = (row: AccountRow) => {
    switch (row.action) {
      case "sign_in":
      case "cli_login":
        return signIn(row);
      case "add_key":
        setKeyFor(row.provider);
        setKeyInput("");
        return undefined;
      case "turn_on":
        return run(row.provider, async () => {
          await api.setProviderEnabled(row.provider, true);
          changed();
          await probeAndReport(row.provider, "Turned on");
        });
      case "install":
      case "":
        return undefined;
      default: {
        const exhaustive: never = row.action;
        return exhaustive;
      }
    }
  };

  const signOut = (row: AccountRow) => run(row.provider, async () => {
    if (row.methods.includes("cli_login")) {
      await onExternalSignOut(row.provider);
    } else {
      putRow((await api.signOutAccount(row.provider)).row);
    }
    announce(row.provider, true, "Signed out.");
    changed();
  });

  const removeKey = (row: AccountRow) => run(row.provider, async () => {
    await api.clearProviderKey(row.provider);
    announce(row.provider, true, "Key removed. The provider is off until you add a key or turn it on.");
    changed();
  });

  const turnOff = (row: AccountRow) => run(row.provider, async () => {
    await api.setProviderEnabled(row.provider, false);
    announce(row.provider, true, "Turned off. The key stays stored.");
    changed();
  });

  const check = (row: AccountRow) => run(row.provider, () => probeAndReport(row.provider, ""));

  if (loadError) {
    return <p className="text-ui-10 text-risk" role="alert">Accounts did not load: {loadError}</p>;
  }
  if (!rows) {
    return <p className="text-ui-10 text-faint">Loading accounts...</p>;
  }

  // Rows that work or that need attention first; untouched providers after.
  const order = (r: AccountRow) => (r.state === "not_set" || r.state === "not_installed" ? 1 : 0);
  const sorted = [...rows].sort((a, b) => order(a) - order(b));

  return (
    <ul className="space-y-1.5" data-testid="accounts-list">
      {sorted.map((row) => {
        const isBusy = busy === row.provider;
        const works = row.state === "signed_in" || row.state === "api_key" || row.state === "limited";
        const signInRow = row.state === "signed_in" && row.methods.includes("sign_in");
        const cliRow = row.methods.includes("cli_login");
        const label = actionLabel(row);
        const flowHere = flow?.provider === row.provider;
        const otherMethod = row.action === "sign_in" && row.methods.includes("api_key")
          ? "Use an API key"
          : row.action === "add_key" && row.methods.includes("sign_in")
            ? "Sign in instead"
            : "";
        const note = result[row.provider];
        const checkLine = checkText(row);
        return (
          <li
            key={row.provider}
            data-testid="account-row"
            data-provider={row.provider}
            data-state={row.state}
            className="bg-panel2 border border-edge/50 rounded p-2"
          >
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2 min-w-0">
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${stateTone(row.state)}`} aria-hidden />
                <button
                  type="button"
                  data-testid="provider-account-drilldown"
                  data-provider={row.provider}
                  onClick={() => onOpenDetails(row.provider)}
                  className="text-txt font-medium text-ui-11 hover:text-accent text-left shrink-0"
                >
                  {row.name}
                </button>
                <span className="text-muted text-ui-10 truncate" data-testid="account-state">
                  {accountStateText(row)}
                </span>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {label ? (
                  <button type="button" className={PRIMARY} disabled={isBusy || flowHere} onClick={() => { void primary(row); }}>
                    {flowHere ? "Waiting..." : label}
                  </button>
                ) : works ? (
                  <button type="button" className={QUIET} disabled={isBusy} onClick={() => { void check(row); }}>
                    {isBusy ? "Checking..." : "Check"}
                  </button>
                ) : null}
              </div>
            </div>
            {showDetail(row) ? (
              <p className="text-ui-10 text-faint mt-1">{row.detail}</p>
            ) : null}
            {checkLine && !note ? <p className="text-ui-10 text-faint mt-1">{checkLine}</p> : null}
            {note ? (
              <p
                className={`text-ui-10 mt-1 ${note.ok ? "text-muted" : "text-risk"}`}
                role={note.ok ? "status" : "alert"}
                data-testid="account-result"
              >
                {note.text}
              </p>
            ) : null}
            {flowHere && flow?.hint ? (
              <div className="flex items-center gap-2 mt-1">
                <p className="text-ui-10 text-muted font-mono">{flow.hint}</p>
                <button type="button" className={QUIET} onClick={onCancelFlow}>Cancel</button>
              </div>
            ) : null}
            {row.provider === "anthropic" && claude ? (
              <div className="flex items-center gap-2 mt-1.5">
                <input
                  type="text"
                  value={claude.code}
                  onChange={(e) => setClaude({ ...claude, code: e.target.value })}
                  placeholder="Paste the code from the browser (code#state)"
                  aria-label="Claude sign-in code"
                  className="flex-1 bg-panel border border-edge rounded px-2 py-1 text-ui-11 font-mono focus:outline-none focus:border-accent"
                />
                <button
                  type="button"
                  className={PRIMARY}
                  disabled={isBusy || !claude.code.trim()}
                  onClick={() => { void completeClaude(row); }}
                >
                  Finish sign-in
                </button>
                <button type="button" className={QUIET} onClick={cancelClaude}>Cancel</button>
              </div>
            ) : null}
            {keyFor === row.provider ? (
              <div className="flex items-center gap-2 mt-1.5">
                <input
                  type="password"
                  value={keyInput}
                  onChange={(e) => setKeyInput(e.target.value)}
                  placeholder="Paste the API key"
                  aria-label={`${row.name} API key`}
                  className="flex-1 bg-panel border border-edge rounded px-2 py-0.5 text-ui-11 font-mono focus:outline-none focus:border-accent"
                />
                <button type="button" className={PRIMARY} disabled={isBusy || !keyInput.trim()} onClick={() => { void saveKey(row); }}>
                  Save key
                </button>
                <button type="button" className={QUIET} onClick={() => setKeyFor("")}>Cancel</button>
              </div>
            ) : null}
            <div className="flex items-center gap-3 mt-1 empty:hidden">
              {otherMethod ? (
                <button
                  type="button"
                  className={QUIET}
                  disabled={isBusy}
                  onClick={() => {
                    if (otherMethod === "Use an API key") {
                      setKeyFor(row.provider);
                    } else {
                      void signIn(row);
                    }
                  }}
                >
                  {otherMethod}
                </button>
              ) : null}
              {signInRow || (cliRow && row.state === "signed_in") ? (
                <button type="button" className={QUIET} disabled={isBusy} onClick={() => { void signOut(row); }}>
                  Sign out
                </button>
              ) : null}
              {row.state === "api_key" || row.state === "rejected" ? (
                <button type="button" className={QUIET} disabled={isBusy} onClick={() => { void removeKey(row); }}>
                  Remove key
                </button>
              ) : null}
              {works ? (
                <button type="button" className={QUIET} disabled={isBusy} onClick={() => { void turnOff(row); }}>
                  Turn off
                </button>
              ) : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
