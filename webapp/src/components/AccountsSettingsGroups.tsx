import { useCallback, useEffect, useState } from "react";
import {
  api,
  type AuthPoolPublic,
  type AuthPoolsResponse,
  type BedrockCredentials,
  type BedrockStatus,
  type PlatformAdapter,
  type PoolEntryCheck,
  type PoolEntryPublic,
} from "../lib/api";
import { ACCOUNTS_CHANGED_EVENT } from "../lib/pilotModelFacts";
import { checkLine } from "./AccountsList";
import { SwitchTrack } from "./ui/Switch";
import {
  SETTINGS_PRIMARY_BUTTON,
  SETTINGS_QUIET_BUTTON,
  SettingsGroup,
  SettingsRow,
} from "./SettingsLayout";

const FIELD = "w-full bg-panel border border-edge rounded px-2 py-1 text-ui-11 text-txt font-mono focus:outline-none focus:border-accent disabled:opacity-50";

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback;
}

function Result({ note }: { note: { ok: boolean; text: string } | null }) {
  if (!note) return null;
  return (
    <p className={`text-ui-10 ${note.ok ? "text-muted" : "text-risk"}`} role={note.ok ? "status" : "alert"}>
      {note.text}
    </p>
  );
}

/* ------------------------------------------------------------------ */
/* Stored keys (credential pools)                                       */
/* ------------------------------------------------------------------ */

const STRATEGY_TEXT: Record<string, string> = {
  fill_first: "First key until it fails",
  round_robin: "Take turns",
  least_used: "Least used first",
  random: "Random",
};

function strategyText(strategy: string): string {
  return STRATEGY_TEXT[strategy] || strategy;
}

/** What one stored credential does now. The server sets `usable` and `first`. */
type EntryState = "in_use" | "ready" | "rejected" | "limited";

function entryState(entry: PoolEntryPublic): EntryState {
  if (entry.first) return "in_use";
  if (entry.usable) return "ready";
  const refused = entry.last_status === "dead" || entry.last_error_code === 401 || entry.last_error_code === 403;
  return refused ? "rejected" : "limited";
}

function entryStateText(state: EntryState): string {
  switch (state) {
    case "in_use":
      return "In use";
    case "ready":
      return "Ready";
    case "rejected":
      return "Rejected";
    case "limited":
      return "Limit reached";
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}

function entryTone(state: EntryState): string {
  switch (state) {
    case "in_use":
      return "bg-good";
    case "ready":
      return "bg-faint";
    case "rejected":
      return "bg-risk";
    case "limited":
      return "bg-warn";
    default: {
      const exhaustive: never = state;
      return exhaustive;
    }
  }
}

/** A name a person knows. Stored labels often name the source of the key. */
function entryName(entry: PoolEntryPublic, provider: string): string {
  const label = entry.label || "";
  if (label === `${provider}-settings`) return "Saved in Settings";
  if (label === "keys.json" || entry.source === "keys.json") return "From the key file";
  if (label.startsWith("env:")) return `From the environment (${label.slice(4)})`;
  return label || entry.id;
}

/** The provider's words from a stored failure. The pool stores the raw response body. */
function providerMessage(raw: string | null | undefined): string {
  const text = (raw || "").trim();
  if (!text.startsWith("{")) return text;
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && "error" in parsed) {
      const error = parsed.error;
      if (typeof error === "string") return error;
      if (error && typeof error === "object" && "message" in error && typeof error.message === "string") {
        return error.message;
      }
    }
  } catch {
    /* A cut body is not JSON: show the text. */
  }
  return text;
}

function failureText(entry: PoolEntryPublic): string {
  if (entry.usable) return "";
  const code = entry.last_error_code ? `HTTP ${entry.last_error_code}` : "";
  return [code, providerMessage(entry.last_error_message)].filter(Boolean).join(": ");
}

function poolSummary(pool: AuthPoolPublic): string {
  const count = pool.entries.length;
  const down = pool.entries.filter((e) => !e.usable).length;
  const total = `${count} credential${count === 1 ? "" : "s"}`;
  const usable = down ? `, ${down} not usable` : "";
  const order = count > 1 ? ` Order: ${strategyText(pool.strategy || "fill_first")}.` : "";
  return `${total}${usable}.${order}`;
}

/** The answer to a Check: the provider's answer, or the reason no request went out. */
type CheckResult =
  | { kind: "checked"; check: PoolEntryCheck }
  | { kind: "not_checked"; text: string };

/**
 * Every credential that Marionette holds, by provider, in the order the pilot
 * tries them. Each one has its state, a live Check, and Remove.
 */
export function CredentialPoolsGroup() {
  const [pools, setPools] = useState<AuthPoolsResponse | null>(null);
  const [busy, setBusy] = useState("");
  const [adding, setAdding] = useState(false);
  const [provider, setProvider] = useState("");
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);
  const [checks, setChecks] = useState<Record<string, CheckResult>>({});
  const [confirming, setConfirming] = useState("");

  const refresh = useCallback(async () => {
    try {
      setPools(await api.getAuthPools());
    } catch (e: unknown) {
      setNote({ ok: false, text: `Stored keys did not load: ${errorText(e, "no answer")}` });
    }
  }, []);

  // A key saved in a provider row changes the pool, so the list loads again.
  useEffect(() => {
    void refresh();
    const onChanged = () => { void refresh(); };
    window.addEventListener("harness-config-changed", onChanged);
    return () => window.removeEventListener("harness-config-changed", onChanged);
  }, [refresh]);

  const changed = () => {
    window.dispatchEvent(new Event(ACCOUNTS_CHANGED_EVENT));
    window.dispatchEvent(new Event("harness-config-changed"));
  };

  const work = async (id: string, fn: () => Promise<void>) => {
    setBusy(id);
    setNote(null);
    try {
      await fn();
    } catch (e: unknown) {
      setNote({ ok: false, text: errorText(e, "The action failed.") });
    } finally {
      setBusy("");
    }
  };

  const keyProviders = pools?.key_providers ?? [];
  const addTo = provider || keyProviders[0]?.provider || "";

  const add = () => work("add", async () => {
    const value = key.trim();
    if (!value || !addTo) return;
    await api.addAuthPoolKey(addTo, value, label.trim() || undefined);
    setKey("");
    setLabel("");
    setAdding(false);
    setNote({ ok: true, text: `Key added to ${addTo}.` });
    changed();
  });

  const remove = (pool: AuthPoolPublic, entry: PoolEntryPublic) => work(entry.id, async () => {
    await api.removeAuthPoolEntry(pool.provider, entry.id);
    setConfirming("");
    setNote({ ok: true, text: `Credential removed from ${pool.name || pool.provider}.` });
    changed();
  });

  const check = (pool: AuthPoolPublic, entry: PoolEntryPublic) => work(entry.id, async () => {
    const res = await api.checkAuthPoolEntry(pool.provider, entry.id);
    const result: CheckResult = res.check
      ? { kind: "checked", check: res.check }
      : { kind: "not_checked", text: res.error || "The provider did not answer." };
    setChecks((prev) => ({ ...prev, [entry.id]: result }));
    changed();
  });

  const setStrategy = (pool: AuthPoolPublic, strategy: string) => work(pool.provider, async () => {
    await api.setAuthPoolStrategy(pool.provider, strategy);
    await refresh();
  });

  const strategies = pools?.strategies?.length ? pools.strategies : Object.keys(STRATEGY_TEXT);
  const filled = (pools?.pools || []).filter((p) => (p.entries || []).length > 0);

  return (
    <SettingsGroup
      title="Stored keys"
      description="Every credential that Marionette holds, one row for each provider. The pilot takes the first usable one. When a provider refuses a key, the pilot takes the next key of that provider."
      testId="credential-pools"
    >
      {filled.map((pool) => {
        const poolName = pool.name || pool.provider;
        return (
          <SettingsRow
            key={pool.provider}
            testId="credential-pool-row"
            label={poolName}
            description={poolSummary(pool)}
            control={pool.entries.length > 1 ? (
              <select
                aria-label={`Key order for ${pool.provider}`}
                value={pool.strategy || "fill_first"}
                disabled={busy === pool.provider}
                onChange={(e) => { void setStrategy(pool, e.target.value); }}
                className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-10 text-muted"
              >
                {strategies.map((s) => <option key={s} value={s}>{strategyText(s)}</option>)}
              </select>
            ) : null}
          >
            <ol className="space-y-1">
              {pool.entries.map((entry, index) => {
                const state = entryState(entry);
                const name = entryName(entry, pool.provider);
                const spoken = `${name}${entry.masked ? ` (${entry.masked})` : ""}`;
                const failure = failureText(entry);
                const result = checks[entry.id];
                const isBusy = busy === entry.id;
                return (
                  <li
                    key={entry.id}
                    data-testid="credential-pool-entry"
                    data-state={state}
                    className="text-ui-10"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex items-center gap-1.5 min-w-0">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${entryTone(state)}`} aria-hidden />
                        <span className="text-faint tabular-nums shrink-0">{index + 1}.</span>
                        <span className="text-txt truncate">{name}</span>
                        {entry.masked ? <span className="text-faint font-mono shrink-0">{entry.masked}</span> : null}
                        <span className={`shrink-0 ${state === "rejected" ? "text-risk" : "text-muted"}`}>
                          {entryStateText(state)}
                        </span>
                        <span className="text-faint shrink-0 tabular-nums">
                          {entry.auth_type === "oauth" ? "Sign-in" : "API key"}
                          {`, ${entry.request_count ?? 0} request${entry.request_count === 1 ? "" : "s"}`}
                        </span>
                      </span>
                      <span className="flex items-center gap-3 shrink-0">
                        <button
                          type="button"
                          className={SETTINGS_QUIET_BUTTON}
                          disabled={isBusy}
                          aria-label={`Check ${spoken} of ${pool.provider}`}
                          onClick={() => { void check(pool, entry); }}
                        >
                          {isBusy && confirming !== entry.id ? "Checking..." : "Check"}
                        </button>
                        <button
                          type="button"
                          className={SETTINGS_QUIET_BUTTON}
                          disabled={isBusy || confirming === entry.id}
                          aria-label={`Remove ${spoken} from ${pool.provider}`}
                          onClick={() => setConfirming(entry.id)}
                        >
                          Remove
                        </button>
                      </span>
                    </div>
                    {failure && !result ? <p className="text-risk mt-0.5 break-words">{failure}</p> : null}
                    {result ? (
                      <p
                        className={`mt-0.5 break-words ${result.kind === "checked" && !result.check.ok ? "text-risk" : "text-muted"}`}
                        role={result.kind === "checked" && !result.check.ok ? "alert" : "status"}
                        data-testid="credential-pool-check"
                      >
                        {result.kind === "checked" ? checkLine(result.check) : result.text}
                      </p>
                    ) : null}
                    {confirming === entry.id ? (
                      <div className="flex flex-wrap items-center gap-3 mt-0.5">
                        <span className="text-muted">
                          Remove this credential from Marionette? It stays valid at the provider.
                        </span>
                        <button
                          type="button"
                          className="text-risk hover:underline text-ui-10 disabled:opacity-30"
                          disabled={isBusy}
                          aria-label={`Confirm: remove ${spoken} from ${pool.provider}`}
                          onClick={() => { void remove(pool, entry); }}
                        >
                          Remove it
                        </button>
                        <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => setConfirming("")}>
                          Cancel
                        </button>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </SettingsRow>
        );
      })}
      <SettingsRow
        label="Add a key"
        description={`${pools && filled.length === 0 ? "No provider has a stored credential. " : ""}One more key goes after the keys that the provider has. To put a key first, save it in the provider row.`}
        control={adding ? null : (
          <button
            type="button"
            className={SETTINGS_PRIMARY_BUTTON}
            disabled={keyProviders.length === 0}
            onClick={() => setAdding(true)}
          >
            Add key
          </button>
        )}
      >
        {adding || note ? (
          <div className="space-y-1.5">
            {adding ? (
              <div className="flex flex-wrap items-center gap-2">
                <select
                  aria-label="Provider for the extra key"
                  value={addTo}
                  onChange={(e) => setProvider(e.target.value)}
                  className="bg-panel border border-edge rounded px-1.5 py-1 text-ui-11 text-txt"
                >
                  {keyProviders.map((p) => <option key={p.provider} value={p.provider}>{p.name}</option>)}
                </select>
                <input
                  type="password"
                  aria-label="Extra API key"
                  placeholder="Paste the API key"
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  className={`${FIELD} flex-1 min-w-40`}
                />
                <input
                  type="text"
                  aria-label="Key name"
                  placeholder="Name (optional)"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  className={`${FIELD} w-36 font-sans`}
                />
                <button
                  type="button"
                  className={SETTINGS_PRIMARY_BUTTON}
                  disabled={busy === "add" || !key.trim()}
                  onClick={() => { void add(); }}
                >
                  {busy === "add" ? "Adding..." : "Save key"}
                </button>
                <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => { setAdding(false); setKey(""); }}>
                  Cancel
                </button>
              </div>
            ) : null}
            <Result note={note} />
          </div>
        ) : null}
      </SettingsRow>
    </SettingsGroup>
  );
}

/* ------------------------------------------------------------------ */
/* AWS Bedrock                                                          */
/* ------------------------------------------------------------------ */

type BedrockDraft = {
  bearer: string;
  accessKey: string;
  secretKey: string;
  sessionToken: string;
  region: string;
  regionAlt: string;
  modelId: string;
};

function draftFrom(status: BedrockStatus | null): BedrockDraft {
  return {
    bearer: "",
    accessKey: "",
    secretKey: "",
    sessionToken: "",
    region: status?.aws_region || "",
    regionAlt: status?.bedrock_region || "",
    modelId: status?.model_id || "",
  };
}

/** The `/api/bedrock` body. Secrets go only when typed. Region and model always go, and empty clears them. */
export function bedrockPatch(draft: BedrockDraft, configured: boolean): BedrockCredentials | null {
  const patch: BedrockCredentials = {};
  if (draft.bearer.trim()) patch.AWS_BEARER_TOKEN_BEDROCK = draft.bearer.trim();
  if (draft.accessKey.trim()) patch.AWS_ACCESS_KEY_ID = draft.accessKey.trim();
  if (draft.secretKey.trim()) patch.AWS_SECRET_ACCESS_KEY = draft.secretKey.trim();
  if (draft.sessionToken.trim()) patch.AWS_SESSION_TOKEN = draft.sessionToken.trim();
  patch.AWS_REGION = draft.region.trim();
  patch.BEDROCK_REGION = draft.regionAlt.trim();
  patch.BEDROCK_MODEL_ID = draft.modelId.trim();
  const hasAuth = !!patch.AWS_BEARER_TOKEN_BEDROCK
    || (!!patch.AWS_ACCESS_KEY_ID && !!patch.AWS_SECRET_ACCESS_KEY)
    || configured;
  if (!hasAuth && !patch.AWS_REGION && !patch.BEDROCK_REGION && !patch.BEDROCK_MODEL_ID) return null;
  return patch;
}

export function BedrockGroup() {
  const [status, setStatus] = useState<BedrockStatus | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<BedrockDraft>(() => draftFrom(null));
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api.getBedrockStatus()
      .then((s) => { setStatus(s); setDraft(draftFrom(s)); })
      .catch((e: unknown) => setNote({ ok: false, text: `Bedrock status did not load: ${errorText(e, "no answer")}` }));
  }, []);

  const set = (field: keyof BedrockDraft) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setDraft((prev) => ({ ...prev, [field]: e.target.value }));

  const run = async (fn: () => Promise<BedrockStatus>, done: string) => {
    setBusy(true);
    setNote(null);
    try {
      const next = await fn();
      setStatus(next);
      setDraft(draftFrom(next));
      setEditing(false);
      setNote({ ok: true, text: done });
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e: unknown) {
      setNote({ ok: false, text: errorText(e, "Bedrock did not save.") });
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    const patch = bedrockPatch(draft, !!status?.configured);
    if (!patch) {
      setNote({ ok: false, text: "Enter a bearer token, or an access key and a secret key." });
      return;
    }
    void run(() => api.setBedrockCredentials(patch), "Bedrock credentials saved.");
  };

  const configured = !!status?.configured;
  const keep = (has: boolean | undefined, name: string) => (has ? `${name} (empty keeps the stored value)` : name);

  return (
    <SettingsGroup
      title="AWS Bedrock"
      description="Workers on the agentic adapter use these credentials for Bedrock models."
      testId="bedrock"
    >
      <SettingsRow
        label="AWS Bedrock"
        description="A bearer token, or an access key with its secret key."
        state={configured ? `Configured (${status?.auth_mode || "credentials"})` : "Not set"}
        control={editing ? null : (
          <>
            {configured ? (
              <button
                type="button"
                className={SETTINGS_QUIET_BUTTON}
                disabled={busy}
                onClick={() => { void run(() => api.clearBedrockCredentials(), "Bedrock credentials removed."); }}
              >
                Disconnect
              </button>
            ) : null}
            <button type="button" className={SETTINGS_PRIMARY_BUTTON} onClick={() => setEditing(true)}>
              {configured ? "Edit" : "Set up"}
            </button>
          </>
        )}
      >
        {editing || note ? (
          <div className="space-y-1.5">
            {editing ? (
              <div className="space-y-1.5">
                <input type="password" aria-label="Bedrock bearer token" placeholder={keep(status?.has_bearer, "AWS_BEARER_TOKEN_BEDROCK")} value={draft.bearer} onChange={set("bearer")} disabled={busy} className={FIELD} />
                <div className="grid grid-cols-2 gap-1.5">
                  <input type="password" aria-label="AWS access key id" placeholder={keep(status?.has_access_key, "AWS_ACCESS_KEY_ID")} value={draft.accessKey} onChange={set("accessKey")} disabled={busy} className={FIELD} />
                  <input type="password" aria-label="AWS secret access key" placeholder={keep(status?.has_access_key, "AWS_SECRET_ACCESS_KEY")} value={draft.secretKey} onChange={set("secretKey")} disabled={busy} className={FIELD} />
                </div>
                <input type="password" aria-label="AWS session token" placeholder={keep(status?.has_session_token, "AWS_SESSION_TOKEN (optional)")} value={draft.sessionToken} onChange={set("sessionToken")} disabled={busy} className={FIELD} />
                <div className="grid grid-cols-3 gap-1.5">
                  <input type="text" aria-label="AWS region" placeholder="AWS_REGION (us-east-1)" value={draft.region} onChange={set("region")} disabled={busy} className={FIELD} />
                  <input type="text" aria-label="Bedrock region" placeholder="BEDROCK_REGION (optional)" value={draft.regionAlt} onChange={set("regionAlt")} disabled={busy} className={FIELD} />
                  <input type="text" aria-label="Default inference profile" placeholder="Inference profile (optional)" value={draft.modelId} onChange={set("modelId")} disabled={busy} className={FIELD} />
                </div>
                <div className="flex items-center gap-2">
                  <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={busy} onClick={save}>
                    {busy ? "Saving..." : "Save"}
                  </button>
                  <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => { setEditing(false); setDraft(draftFrom(status)); }}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : null}
            <Result note={note} />
          </div>
        ) : null}
      </SettingsRow>
    </SettingsGroup>
  );
}

/* ------------------------------------------------------------------ */
/* Worker platforms                                                     */
/* ------------------------------------------------------------------ */

export function WorkerPlatformsGroup() {
  const [adapters, setAdapters] = useState<PlatformAdapter[] | null>(null);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api.getPlatform()
      .then((res) => setAdapters(res.adapters))
      .catch((e: unknown) => setNote({ ok: false, text: `Worker platforms did not load: ${errorText(e, "no answer")}` }));
  }, []);

  const toggle = async (adapter: PlatformAdapter) => {
    setBusy(adapter.name);
    setNote(null);
    try {
      const res = await api.togglePlatform(adapter.name, !adapter.enabled);
      setAdapters(res.adapters);
    } catch (e: unknown) {
      setNote({ ok: false, text: errorText(e, `${adapter.name} did not change.`) });
    } finally {
      setBusy("");
    }
  };

  return (
    <SettingsGroup
      title="Worker platforms"
      description="Optional. Workers run on the built-in provider worker by default. Turn on a platform to send worker runs to that coding-agent CLI."
      testId="worker-platforms"
    >
      {adapters === null && !note ? (
        <SettingsRow label="Loading worker platforms..." />
      ) : null}
      {(adapters || []).map((adapter) => (
        <SettingsRow
          key={adapter.name}
          testId="worker-platform-row"
          label={<span className="font-mono">{adapter.name}</span>}
          description={adapter.note || undefined}
          state={!adapter.available ? "Not installed" : adapter.implement_capable ? "Edits code" : "Analysis only"}
          control={(
            <button
              type="button"
              role="switch"
              aria-checked={adapter.enabled}
              aria-label={`Use ${adapter.name} for workers`}
              aria-disabled={busy === adapter.name || undefined}
              onClick={() => { if (busy !== adapter.name) void toggle(adapter); }}
              className="flex rounded-full aria-disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
            >
              <SwitchTrack on={adapter.enabled} />
            </button>
          )}
        />
      ))}
      {note ? <SettingsRow label={<Result note={note} />} /> : null}
    </SettingsGroup>
  );
}
