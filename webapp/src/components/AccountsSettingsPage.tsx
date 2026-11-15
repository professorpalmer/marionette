import { useEffect, useRef, useState } from "react";
import { api, type Config, type ProviderInfo } from "../lib/api";
import type { ProviderConfigValues } from "../lib/providerConfig";
import AccountsList from "./AccountsList";
import ProviderConfigModal from "./ProviderConfigModal";
import { clearCatalogSnapshot } from "./ModelsSettingsPage";
import { BedrockGroup, CredentialPoolsGroup, WorkerPlatformsGroup } from "./AccountsSettingsGroups";
import { SettingsPage } from "./SettingsLayout";

type OAuthFlowOwner = "openai-codex" | "xai-oauth" | "nous" | "claude-cli" | "cursor-cli";

/** The Accounts row that shows each sign-in flow. */
const FLOW_ROW: Record<OAuthFlowOwner, string> = {
  "openai-codex": "openai-codex",
  "xai-oauth": "xai",
  nous: "nous",
  "claude-cli": "claude-code",
  "cursor-cli": "cursor-cli",
};

const CANCELLED = "Sign-in cancelled. Click Sign in to try again.";
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function workspaceArg(): Promise<{ workspace: string } | undefined> {
  try {
    const repo = ((await api.settings()).repo || "").trim();
    return repo ? { workspace: repo } : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The device-code and CLI sign-ins. AccountsList runs Claude (Anthropic) itself;
 * it calls these for the other sign-in rows and shows the flow hint on the row.
 * A failure throws, so the row shows the reason.
 */
function useSignInFlows(onAccountsChanged: () => void) {
  const [owner, setOwner] = useState<OAuthFlowOwner | null>(null);
  const [hint, setHint] = useState("");
  const sessionRef = useRef("");
  const abortRef = useRef(false);
  const cursorSignedIn = useRef<string | null>(null);

  useEffect(() => {
    api.getCursorCliStatus({ refresh: false })
      .then((st) => { cursorSignedIn.current = st?.authenticated ? (st.label || "Cursor account") : null; })
      .catch(() => { /* the row shows the CLI state */ });
  }, []);

  const begin = (next: OAuthFlowOwner) => {
    abortRef.current = false;
    setOwner(next);
    setHint("");
  };
  const finish = () => {
    setOwner(null);
    abortRef.current = false;
  };
  const done = (text: string) => {
    setHint(text);
    onAccountsChanged();
    window.dispatchEvent(new Event("harness-config-changed"));
  };

  /** Wait between polls. Returns false when the user cancelled. */
  const waitTick = async (ms: number) => {
    if (abortRef.current) return false;
    await sleep(ms);
    return !abortRef.current;
  };

  const deviceSignIn = async (provider: "openai-codex" | "xai-oauth" | "nous", fallback: string) => {
    begin(provider);
    try {
      const start = await api.startAuthOAuth(provider);
      if (!start.session_id || !start.user_code) throw new Error(start.error || "Sign-in did not start.");
      sessionRef.current = start.session_id;
      setHint(`Enter code ${start.user_code} at ${start.verification_uri}`);
      try {
        window.open(start.verification_uri_complete || start.verification_uri, "_blank");
      } catch {
        /* the hint shows the address */
      }
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const interval = Math.max(1, start.interval || 5) * 1000;
      while (Date.now() < deadline) {
        if (!(await waitTick(interval))) {
          setHint(CANCELLED);
          return;
        }
        const poll = await api.pollAuthOAuth(start.session_id, provider);
        if (poll.status === "done") {
          sessionRef.current = "";
          done(`Signed in as ${poll.label || fallback}`);
          return;
        }
        if (poll.status === "error") throw new Error(poll.error || "Sign-in failed.");
      }
      throw new Error("Sign-in timed out. Click Sign in to try again.");
    } catch (e: unknown) {
      sessionRef.current = "";
      const msg = e instanceof Error ? e.message : `${provider} sign-in failed.`;
      throw new Error(/device|enabled|access code|chatgpt settings/i.test(msg)
        ? `${msg} Turn on ChatGPT device codes for Codex, then sign in again.`
        : msg);
    } finally {
      finish();
    }
  };

  const claudeCliSignIn = async () => {
    begin("claude-cli");
    try {
      const start = await api.startClaudeCliLogin(await workspaceArg());
      if (!start.ok && start.error) throw new Error(start.error);
      setHint(start.hint || (start.launched
        ? "Finish the Claude login in the window that opened."
        : `Run \`${start.command || "claude auth login"}\` in a terminal.`));
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const interval = Math.max(2, start.poll_interval || 3) * 1000;
      while (Date.now() < deadline) {
        if (!(await waitTick(interval))) {
          setHint(CANCELLED);
          return;
        }
        const st = await api.getClaudeCliStatus({ refresh: true }).catch(() => null);
        if (st?.authenticated) {
          done(`Signed in as ${st.label || "Claude account"}`);
          return;
        }
      }
      throw new Error("Claude login timed out. Click Sign in to try again.");
    } finally {
      finish();
    }
  };

  const cursorCliStatus = async () => {
    // The Models page keeps a catalog snapshot. A new Cursor login can change the list.
    clearCatalogSnapshot();
    const st = await api.getCursorCliStatus({ refresh: true });
    cursorSignedIn.current = st?.authenticated ? (st.label || "Cursor account") : null;
    return st;
  };

  const cursorCliSignIn = async () => {
    begin("cursor-cli");
    const workspace = await workspaceArg();
    try {
      const start = await api.startCursorCliLogin(workspace);
      if (!start.ok && start.error) throw new Error(start.error);
      setHint(start.hint || (start.launched
        ? "Finish the Cursor login in the window that opened."
        : `Run \`${start.command || "agent login"}\` in a terminal.`));
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const interval = Math.max(2, start.poll_interval || 3) * 1000;
      while (Date.now() < deadline) {
        if (!(await waitTick(interval))) {
          setHint(CANCELLED);
          return;
        }
        const st = await cursorCliStatus().catch(() => null);
        if (st?.authenticated) {
          const label = st.label || "Cursor account";
          done(`Signed in as ${label}`);
          // Workspace trust runs after the login. The pilot passes --trust when it is not done.
          void api.trustCursorCliWorkspace(workspace)
            .then((trust) => {
              if (abortRef.current) return;
              if (trust.trusted) setHint(`Signed in as ${label}. Workspace trusted.`);
            })
            .catch(() => { /* the pilot passes --trust */ });
          return;
        }
        if (st && st.installed === false) throw new Error(st.error || "Cursor Agent CLI is not installed.");
      }
      throw new Error("Login timed out. Finish agent login, then sign in again.");
    } finally {
      finish();
    }
  };

  const signIn = async (provider: string): Promise<void> => {
    if (provider === "openai-codex") return deviceSignIn("openai-codex", "chatgpt-codex");
    if (provider === "xai") return deviceSignIn("xai-oauth", "xai-oauth");
    if (provider === "nous") return deviceSignIn("nous", "nous");
    if (provider === "claude-code") return claudeCliSignIn();
    if (provider === "cursor-cli") return cursorCliSignIn();
  };

  const signOut = async (provider: string): Promise<void> => {
    if (provider === "claude-code") {
      setOwner("claude-cli");
      try {
        await api.logoutClaudeCli();
        onAccountsChanged();
      } finally {
        setOwner(null);
      }
      return;
    }
    if (provider === "cursor-cli") {
      setOwner("cursor-cli");
      try {
        await api.logoutCursorCli();
        await cursorCliStatus().catch(() => null);
        onAccountsChanged();
        window.dispatchEvent(new Event("harness-config-changed"));
      } finally {
        setOwner(null);
      }
    }
  };

  const cancel = () => {
    abortRef.current = true;
    const sid = sessionRef.current;
    if (sid && owner) api.cancelAuthOAuth(sid, owner).catch(() => { /* best-effort */ });
    sessionRef.current = "";
    setOwner(null);
    // Cursor can finish the browser login before the poll sees it. Cancel keeps a good login.
    setHint(owner === "cursor-cli" && cursorSignedIn.current ? `Signed in as ${cursorSignedIn.current}` : CANCELLED);
  };

  return {
    flow: owner ? { provider: FLOW_ROW[owner], hint } : null,
    signIn,
    signOut,
    cancel,
  };
}

/**
 * Settings > Accounts: the account list (one row per provider), then the
 * settings that are not one provider row: extra keys, Bedrock, and worker platforms.
 */
export default function AccountsSettingsPage() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [details, setDetails] = useState<ProviderInfo | null>(null);
  const [savingKey, setSavingKey] = useState(false);
  const [bootstrapIssues, setBootstrapIssues] = useState<NonNullable<Config["key_bootstrap_issues"]>>([]);

  const refreshProviders = () => {
    api.providers().then(setProviders).catch(() => { /* the modal needs the list only on click */ });
  };

  useEffect(() => {
    refreshProviders();
    api.config()
      .then((c) => setBootstrapIssues(c.key_bootstrap_issues || []))
      .catch(() => { /* no issues to show */ });
  }, []);

  const flows = useSignInFlows(refreshProviders);

  const saveKey = async (changed: Partial<ProviderConfigValues>) => {
    const name = (changed.name || details?.name || "").trim();
    if (!name) throw new Error("Choose a provider.");
    if (!changed.api_key) throw new Error("Enter an API key.");
    setSavingKey(true);
    try {
      await api.setProviderKey(name, changed.api_key);
    } finally {
      setSavingKey(false);
    }
    // Close only after the key is stored. A failure stays in the modal.
    setDetails(null);
    window.dispatchEvent(new Event("harness-config-changed"));
    refreshProviders();
  };

  return (
    <SettingsPage
      title="Accounts"
      description="Each provider has one row. The row shows the credential the pilot uses. Check sends one request to the provider and shows its answer."
      testId="accounts-page"
    >
      {bootstrapIssues.length > 0 ? (
        <div
          role="alert"
          data-testid="settings-key-bootstrap-issues"
          className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-ui-11 text-txt space-y-1"
        >
          <div className="font-medium">The key store did not finish its save at startup.</div>
          <div className="text-muted">Save the key again if a provider is missing.</div>
          {bootstrapIssues.map((issue, i) => (
            <div key={`${issue.step}-${i}`} className="font-mono text-ui-10 text-faint break-all">
              {issue.step}: {issue.message}
            </div>
          ))}
        </div>
      ) : null}

      <section className="space-y-1.5" data-testid="accounts-group">
        <h3 className="text-ui-11 font-medium uppercase tracking-wide text-faint">Providers</h3>
        <AccountsList
          onExternalSignIn={flows.signIn}
          onExternalSignOut={flows.signOut}
          onCancelFlow={flows.cancel}
          flow={flows.flow}
          onOpenDetails={(name) => {
            const provider = providers.find((p) => p.name === name);
            if (provider) setDetails(provider);
          }}
        />
      </section>

      <CredentialPoolsGroup />
      <BedrockGroup />
      <WorkerPlatformsGroup />

      {details ? (
        <ProviderConfigModal
          open
          provider={details}
          busy={savingKey}
          onClose={() => setDetails(null)}
          onSubmit={saveKey}
        />
      ) : null}
    </SettingsPage>
  );
}

