import { useEffect, useMemo, useRef, useState } from "react";
import { ExternalLink, KeyRound, LogIn } from "lucide-react";
import { api, type ProviderInfo } from "../lib/api";
import {
  FEATURED_LABELS,
  featuredOnboardingProviders,
  featuredOAuthKind,
  keyOnboardingProviders,
  onboardingCopy,
  openOnboardingKeyUrl,
  type FeaturedOAuthName,
} from "../lib/onboardingProviders";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { setConfigured, setFlow, setMode, skipFirstRun } from "../state/onboardingStore";

export type OnboardingOverlayProps = {
  onClose: () => void;
};

function finishConfigured(onClose: () => void): void {
  setFlow("success");
  setConfigured(true);
  window.dispatchEvent(new Event("harness-config-changed"));
  onClose();
}

function finishSkip(onClose: () => void): void {
  skipFirstRun();
  onClose();
}

export type FeaturedProviderRowProps = {
  provider: ProviderInfo;
  busy: boolean;
  hint: string;
  error: string;
  sessionId: string;
  pasteCode: string;
  onPasteCode: (value: string) => void;
  onSignIn: () => void;
  onComplete: () => void;
  onCancel: () => void;
};

export function FeaturedProviderRow({
  provider,
  busy,
  hint,
  error,
  sessionId,
  pasteCode,
  onPasteCode,
  onSignIn,
  onComplete,
  onCancel,
}: FeaturedProviderRowProps) {
  const copy = onboardingCopy(provider.name);
  const label =
    FEATURED_LABELS[provider.name as FeaturedOAuthName] ||
    provider.display_name ||
    provider.name;
  const pkce = featuredOAuthKind(provider.name) === "pkce";
  const errorNotice = usePanelNotice(error || null);

  return (
    <div
      data-testid="featured-provider-row"
      data-provider={provider.name}
      className="rounded-lg border border-edge/70 bg-panel2 px-3 py-2.5"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[13px] font-semibold text-txt leading-tight">{label}</div>
          <div className="text-[11px] text-muted mt-0.5 leading-snug">{copy.tagline}</div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {busy || sessionId ? (
            <button
              type="button"
              onClick={onCancel}
              className="text-[11px] text-muted hover:text-txt border border-edge rounded px-2 py-1"
            >
              Cancel
            </button>
          ) : null}
          <button
            type="button"
            onClick={onSignIn}
            disabled={busy}
            className="inline-flex items-center gap-1 bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-1 text-[11px] font-medium disabled:opacity-35"
          >
            <LogIn size={12} />
            {busy ? (pkce ? "Waiting for code…" : "Waiting…") : "Sign in"}
          </button>
        </div>
      </div>
      {hint ? (
        <p className="mt-1.5 text-[11px] text-accent font-mono leading-normal">{hint}</p>
      ) : null}
      {pkce && sessionId ? (
        <div className="mt-2 flex items-center gap-2">
          <input
            type="text"
            value={pasteCode}
            onChange={(e) => onPasteCode(e.target.value)}
            placeholder="paste authorization code#state"
            className="flex-1 bg-bg border border-edge rounded-lg px-2.5 py-1.5 text-[12px] font-mono text-txt placeholder:text-faint focus:outline-none focus:border-accent"
          />
          <button
            type="button"
            onClick={onComplete}
            disabled={busy || !pasteCode.trim()}
            className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-1 text-[11px] font-medium disabled:opacity-35"
          >
            Complete
          </button>
        </div>
      ) : null}
      {errorNotice ? (
        <p className="mt-1.5 text-[11px] text-risk" role="alert">
          {errorNotice}
        </p>
      ) : null}
    </div>
  );
}

export type KeyProviderRowProps = {
  provider: ProviderInfo;
  expanded: boolean;
  onExpand: () => void;
  disabled: boolean;
  onConnected: () => void;
};

export function KeyProviderRow({
  provider,
  expanded,
  onExpand,
  disabled,
  onConnected,
}: KeyProviderRowProps) {
  const [keyValue, setKeyValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const copy = onboardingCopy(provider.name);
  const errorNotice = usePanelNotice(error || null);
  const canConnect = Boolean(keyValue.trim()) && !saving && !disabled;

  const connect = async () => {
    const key = keyValue.trim();
    if (!key) return;
    setSaving(true);
    setError("");
    setMode("apikey");
    setFlow("submitting");
    try {
      const res = await api.setProviderKey(provider.name, key);
      if (!res?.ok) {
        throw new Error("Could not save that key.");
      }
      onConnected();
    } catch (err: unknown) {
      setFlow("error");
      setError(err instanceof Error ? err.message : "Could not save that key.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      data-testid="key-provider-row"
      data-provider={provider.name}
      className={`rounded-lg border px-3 py-2.5 ${
        expanded ? "border-accent bg-accent/10" : "border-edge/70 bg-panel2"
      }`}
    >
      <button
        type="button"
        onClick={onExpand}
        disabled={disabled}
        className="w-full text-left"
      >
        <div className="text-[13px] font-semibold text-txt leading-tight">
          {provider.display_name || provider.name}
        </div>
        <div className="text-[11px] text-muted mt-0.5 leading-snug">{copy.tagline}</div>
      </button>
      {expanded ? (
        <form
          className="mt-2.5"
          onSubmit={(e) => {
            e.preventDefault();
            void connect();
          }}
        >
          <div className="flex items-start justify-between gap-3">
            <p className="text-[12px] text-muted leading-relaxed">{copy.blurb}</p>
            {copy.keyUrl ? (
              <button
                type="button"
                onClick={() => openOnboardingKeyUrl(copy.keyUrl!)}
                className="shrink-0 inline-flex items-center gap-1 text-[12px] text-accent hover:underline pt-0.5"
              >
                Get a key
                <ExternalLink size={11} />
              </button>
            ) : null}
          </div>
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={provider.env_var || "API key"}
            value={keyValue}
            onChange={(e) => setKeyValue(e.target.value)}
            disabled={saving || disabled}
            className="mt-2 w-full bg-bg border border-edge rounded-lg px-3 py-2 text-[13px] font-mono text-txt placeholder:text-faint focus:outline-none focus:border-accent disabled:opacity-50"
          />
          {errorNotice ? (
            <p className="mt-2 text-[12px] text-risk" role="alert">
              {errorNotice}
            </p>
          ) : null}
          <div className="mt-2 flex justify-end">
            <button
              type="submit"
              disabled={!canConnect}
              className="inline-flex items-center gap-1.5 bg-accent text-panel font-semibold rounded-lg px-3.5 py-1.5 text-[12.5px] hover:brightness-110 disabled:opacity-35 disabled:hover:brightness-100 transition"
            >
              <KeyRound size={13} />
              {saving ? "Connecting…" : "Connect"}
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}

export default function OnboardingOverlay({ onClose }: OnboardingOverlayProps) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [selectedKey, setSelectedKey] = useState("");
  const [oauthName, setOauthName] = useState("");
  const [oauthBusy, setOauthBusy] = useState(false);
  const [oauthHint, setOauthHint] = useState("");
  const [oauthError, setOauthError] = useState("");
  const [oauthSessionId, setOauthSessionId] = useState("");
  const [oauthPasteCode, setOauthPasteCode] = useState("");
  const abortRef = useRef(false);
  const loadNotice = usePanelNotice(loadError || null);

  const featured = useMemo(() => featuredOnboardingProviders(providers), [providers]);
  const keyRows = useMemo(() => keyOnboardingProviders(providers), [providers]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") finishSkip(onClose);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError("");
      try {
        const list = await api.providers();
        if (cancelled) return;
        setProviders(list);
        const keys = keyOnboardingProviders(list);
        setSelectedKey(keys[0]?.name || "");
      } catch (err: unknown) {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Failed to load providers.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    return () => {
      abortRef.current = true;
    };
  }, []);

  const cancelOAuth = () => {
    abortRef.current = true;
    const sid = oauthSessionId;
    const name = oauthName;
    if (sid) {
      api.cancelAuthOAuth(sid, name).catch(() => {
        /* best-effort */
      });
    }
    setOauthBusy(false);
    setOauthSessionId("");
    setOauthPasteCode("");
    setOauthHint("Sign-in cancelled — click Sign in to try again.");
    setFlow("idle");
  };

  const signInFeatured = async (provider: ProviderInfo) => {
    abortRef.current = false;
    setOauthName(provider.name);
    setOauthBusy(true);
    setOauthHint("");
    setOauthError("");
    setOauthSessionId("");
    setOauthPasteCode("");
    setMode("oauth");
    setFlow("starting");
    try {
      const start = await api.startAuthOAuth(provider.name);
      if (!start.session_id) {
        throw new Error(start.error || "oauth start failed");
      }
      setOauthSessionId(start.session_id);
      const kind = featuredOAuthKind(provider.name);
      if (kind === "pkce") {
        if (!start.auth_url) {
          throw new Error(start.error || "oauth start failed");
        }
        setFlow("awaiting_user");
        setOauthHint("Browser opened — authorize, then paste the code below (code#state).");
        try {
          openOnboardingKeyUrl(start.auth_url);
        } catch {
          setOauthHint(`Open ${start.auth_url} then paste the code below.`);
        }
        return;
      }
      if (!start.user_code) {
        throw new Error(start.error || "oauth start failed");
      }
      setFlow("awaiting_user");
      setOauthHint(`Enter code ${start.user_code} at ${start.verification_uri || "the login page"}`);
      const verify = start.verification_uri_complete || start.verification_uri;
      if (verify) {
        try {
          openOnboardingKeyUrl(verify);
        } catch {
          /* ignore */
        }
      }
      setFlow("polling");
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const intervalMs = Math.max(1, start.interval || 5) * 1000;
      while (Date.now() < deadline) {
        if (abortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          setFlow("idle");
          return;
        }
        const poll = await api.pollAuthOAuth(start.session_id, provider.name);
        if (poll.status === "done") {
          finishConfigured(onClose);
          return;
        }
        if (poll.status === "error") {
          throw new Error(poll.error || "oauth failed");
        }
        await new Promise((r) => setTimeout(r, intervalMs));
      }
      throw new Error("Login timed out — click Sign in to try again.");
    } catch (err: unknown) {
      setFlow("error");
      setOauthError(err instanceof Error ? err.message : `${provider.name} sign-in failed`);
      setOauthSessionId("");
    } finally {
      setOauthBusy(false);
    }
  };

  const completeAnthropic = async () => {
    const code = oauthPasteCode.trim();
    if (!oauthSessionId || !code) return;
    setOauthBusy(true);
    setOauthError("");
    setFlow("submitting");
    try {
      const res = await api.completeAuthOAuth(oauthSessionId, code, "anthropic");
      if (res.status !== "done") {
        throw new Error(res.error || "oauth complete failed");
      }
      finishConfigured(onClose);
    } catch (err: unknown) {
      setFlow("error");
      setOauthError(err instanceof Error ? err.message : "Claude sign-in failed");
    } finally {
      setOauthBusy(false);
    }
  };

  return (
    <div
      data-testid="onboarding-overlay"
      className="fixed inset-0 z-50 flex items-center justify-center px-4 py-6"
      style={{
        background:
          "radial-gradient(ellipse at 50% 38%, rgba(224,164,90,0.08) 0%, transparent 52%), #0f1113",
      }}
    >
      <div className="onboarding-card w-full max-w-[36rem] max-h-full overflow-y-auto rounded-2xl border border-edge bg-panel px-7 py-8 shadow-[0_24px_80px_rgba(0,0,0,0.45)]">
        <header className="text-center mb-6">
          <h1 className="text-[1.45rem] font-semibold tracking-tight text-txt leading-tight">
            Let&apos;s get you set up with Marionette
          </h1>
          <p className="mt-2 text-[13px] text-muted leading-relaxed">
            Sign in with a plan or paste a Full stack key. One credential runs chat and swarms.
          </p>
        </header>

        {loading ? (
          <p className="text-center text-muted text-[13px] py-10">Loading providers…</p>
        ) : (
          <>
            {loadNotice ? (
              <p className="mb-4 text-center text-[12px] text-risk" role="alert">
                {loadNotice}
              </p>
            ) : null}

            <section aria-label="Featured plan sign-in">
              <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted mb-2">
                Sign in with a plan
              </h2>
              <div className="space-y-2">
                {featured.map((p) => (
                  <FeaturedProviderRow
                    key={p.name}
                    provider={p}
                    busy={oauthBusy && oauthName === p.name}
                    hint={oauthName === p.name ? oauthHint : ""}
                    error={oauthName === p.name ? oauthError : ""}
                    sessionId={oauthName === p.name ? oauthSessionId : ""}
                    pasteCode={oauthName === p.name ? oauthPasteCode : ""}
                    onPasteCode={setOauthPasteCode}
                    onSignIn={() => void signInFeatured(p)}
                    onComplete={() => void completeAnthropic()}
                    onCancel={cancelOAuth}
                  />
                ))}
              </div>
            </section>

            <section className="mt-6" aria-label="API key providers">
              <h2 className="text-[11px] font-semibold uppercase tracking-wide text-muted mb-2">
                Or paste an API key
              </h2>
              {keyRows.length === 0 ? (
                <p className="text-center text-muted text-[13px] py-4">
                  No key providers available. You can add a key later from Settings.
                </p>
              ) : (
                <div className="space-y-2">
                  {keyRows.map((p) => (
                    <KeyProviderRow
                      key={p.name}
                      provider={p}
                      expanded={p.name === selectedKey}
                      onExpand={() => setSelectedKey(p.name)}
                      disabled={oauthBusy}
                      onConnected={() => finishConfigured(onClose)}
                    />
                  ))}
                </div>
              )}
            </section>
          </>
        )}

        <div className="mt-6 text-center">
          <button
            type="button"
            onClick={() => finishSkip(onClose)}
            className="text-[12.5px] text-muted hover:text-txt transition-colors"
          >
            I&apos;ll choose a provider later
          </button>
        </div>
      </div>
    </div>
  );
}
