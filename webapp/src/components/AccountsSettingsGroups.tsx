import { useEffect, useState } from "react";
import {
  api,
  type AuthPoolPublic,
  type AuthPoolsResponse,
  type BedrockCredentials,
  type BedrockStatus,
  type PlatformAdapter,
} from "../lib/api";
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
/* Extra API keys (credential pools)                                    */
/* ------------------------------------------------------------------ */

/** Providers that take extra API keys here. Sign-in pools fill from Accounts. */
export const POOL_KEY_PROVIDERS = ["openrouter", "openai", "cursor"] as const;

const STRATEGY_TEXT: Record<string, string> = {
  fill_first: "First key until it fails",
  round_robin: "Take turns",
  least_used: "Least used first",
  random: "Random",
};

function strategyText(strategy: string): string {
  return STRATEGY_TEXT[strategy] || strategy;
}

function entryStatusText(status: string | undefined): string {
  if (status === "exhausted") return "Limit reached";
  if (status === "dead") return "Rejected";
  return "OK";
}

export function CredentialPoolsGroup() {
  const [pools, setPools] = useState<AuthPoolsResponse | null>(null);
  const [busy, setBusy] = useState("");
  const [adding, setAdding] = useState(false);
  const [provider, setProvider] = useState<string>(POOL_KEY_PROVIDERS[0]);
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  const refresh = async () => {
    try {
      setPools(await api.getAuthPools());
    } catch (e: unknown) {
      setNote({ ok: false, text: `Extra keys did not load: ${errorText(e, "no answer")}` });
    }
  };

  useEffect(() => { void refresh(); }, []);

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

  const add = () => work("add", async () => {
    const value = key.trim();
    if (!value) return;
    await api.addAuthPoolKey(provider, value, label.trim() || undefined);
    setKey("");
    setLabel("");
    setAdding(false);
    setNote({ ok: true, text: `Key added to ${provider}.` });
    await refresh();
    window.dispatchEvent(new Event("harness-config-changed"));
  });

  const remove = (pool: AuthPoolPublic, entryId: string) => work(pool.provider, async () => {
    await api.removeAuthPoolEntry(pool.provider, entryId);
    await refresh();
  });

  const setStrategy = (pool: AuthPoolPublic, strategy: string) => work(pool.provider, async () => {
    await api.setAuthPoolStrategy(pool.provider, strategy);
    await refresh();
  });

  const strategies = pools?.strategies?.length ? pools.strategies : Object.keys(STRATEGY_TEXT);
  // One credential is the provider row above. A pool adds a choice only with two or more.
  const filled = (pools?.pools || []).filter((p) => (p.entries || []).length > 1);

  return (
    <SettingsGroup
      title="Extra API keys"
      description="Optional. When a key reaches a plan limit (HTTP 429 or 402), the pilot uses the next key of the same provider. The prompt cache can reset then."
      testId="credential-pools"
    >
      {filled.map((pool) => {
        const count = pool.entries.length;
        return (
          <SettingsRow
            key={pool.provider}
            testId="credential-pool-row"
            label={pool.provider}
            description={`${count} credential${count === 1 ? "" : "s"}. Order: ${strategyText(pool.strategy || "fill_first")}.`}
            control={(
              <select
                aria-label={`Key order for ${pool.provider}`}
                value={pool.strategy || "fill_first"}
                disabled={busy === pool.provider}
                onChange={(e) => { void setStrategy(pool, e.target.value); }}
                className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-10 text-muted"
              >
                {strategies.map((s) => <option key={s} value={s}>{strategyText(s)}</option>)}
              </select>
            )}
          >
            <ul className="space-y-0.5">
              {pool.entries.map((entry) => (
                <li key={entry.id} className="flex items-center justify-between gap-2 text-ui-10">
                  <span className="min-w-0 truncate">
                    <span className="text-txt">{entry.label || entry.id}</span>
                    {entry.masked ? <span className="text-faint font-mono ml-1.5">{entry.masked}</span> : null}
                    <span className={`ml-1.5 ${entry.last_status === "exhausted" || entry.last_status === "dead" ? "text-risk" : "text-muted"}`}>
                      {entryStatusText(entry.last_status)}
                    </span>
                  </span>
                  <button
                    type="button"
                    className={SETTINGS_QUIET_BUTTON}
                    disabled={busy === pool.provider}
                    aria-label={`Remove ${entry.label || entry.id} from ${pool.provider}`}
                    onClick={() => { void remove(pool, entry.id); }}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          </SettingsRow>
        );
      })}
      <SettingsRow
        label="Add a key"
        description={filled.length === 0 ? "No provider has an extra key." : "Add one more key to a provider."}
        control={adding ? null : (
          <button type="button" className={SETTINGS_PRIMARY_BUTTON} onClick={() => setAdding(true)}>
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
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                  className="bg-panel border border-edge rounded px-1.5 py-1 text-ui-11 text-txt"
                >
                  {POOL_KEY_PROVIDERS.map((p) => <option key={p} value={p}>{p}</option>)}
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
