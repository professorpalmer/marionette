import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronRight, ExternalLink, HardDrive, KeyRound, LogIn } from "lucide-react";
import { api, type ProviderInfo } from "../lib/api";
import {
  defaultOnboardingProvider,
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
import { focusSettingsPage } from "./SettingsShell";
import { Modal } from "./ui/overlay";
import { focusComposer } from "../lib/sessionNav";

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
          <div className="text-ui-13 font-semibold text-txt leading-tight">{label}</div>
          <div className="text-ui-11 text-muted mt-0.5 leading-snug">{copy.tagline}</div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {busy || sessionId ? (
            <button
              type="button"
              onClick={onCancel}
              className="text-ui-11 text-muted hover:text-txt border border-edge rounded px-2 py-1"
            >
              Cancel
            </button>
          ) : null}
          <button
            type="button"
            onClick={onSignIn}
            disabled={busy}
            className="inline-flex items-center gap-1 bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-1 text-ui-11 font-medium disabled:opacity-35"
          >
            <LogIn size={12} />
            {busy ? (pkce ? "Waiting for code…" : "Waiting…") : "Sign in"}
          </button>
        </div>
      </div>
      {hint ? (
        <p className="mt-1.5 text-ui-11 text-accent font-mono leading-normal">{hint}</p>
      ) : null}
      {pkce && sessionId ? (
        <div className="mt-2 flex items-center gap-2">
          <input
            type="text"
            value={pasteCode}
            onChange={(e) => onPasteCode(e.target.value)}
            placeholder="paste authorization code#state"
            className="flex-1 bg-bg border border-edge rounded-lg px-2.5 py-1.5 text-ui-12 font-mono text-txt placeholder:text-faint focus:outline-none focus:border-accent"
          />
          <button
            type="button"
            onClick={onComplete}
            disabled={busy || !pasteCode.trim()}
            className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-1 text-ui-11 font-medium disabled:opacity-35"
          >
            Complete
          </button>
        </div>
      ) : null}
      {errorNotice ? (
        <p className="mt-1.5 text-ui-11 text-risk" role="alert">
          {errorNotice}
        </p>
      ) : null}
    </div>
  );
}

/** Plain words for worker_capability ("full stack" meant nothing to a new user). */
export function capabilityLabel(provider: Pick<ProviderInfo, "worker_capability">): string {
  switch (provider.worker_capability) {
    case "full_stack":
      return "Chat and workers";
    case "pilot_only":
      return "Chat only";
    case "platform_worker":
      return "Workers only";
    case undefined:
      return "";
    default: {
      const exhaustive: never = provider.worker_capability;
      return exhaustive;
    }
  }
}

function CapabilityBadge({ provider }: { provider: ProviderInfo }) {
  const label = capabilityLabel(provider);
  if (!label) return null;
  return <span className="text-ui-10 text-faint">{label}</span>;
}

export type KeyProviderRowProps = {
  provider: ProviderInfo;
  /** Shows the "Recommended" tag. */
  recommended?: boolean;
  expanded: boolean;
  onExpand: () => void;
  disabled: boolean;
  onConnected: () => void;
};

export function KeyProviderRow({
  provider,
  recommended = false,
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
        <div className="flex items-center gap-2">
          <span className="text-ui-13 font-semibold text-txt leading-tight">
            {provider.display_name || provider.name}
          </span>
          {recommended ? (
            <span className="text-ui-10 font-semibold text-accent border border-accent/40 rounded px-1.5 py-px">
              Recommended
            </span>
          ) : null}
          <CapabilityBadge provider={provider} />
        </div>
        <div className="text-ui-11 text-muted mt-0.5 leading-snug">{copy.tagline}</div>
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
            <p className="text-ui-12 text-muted leading-relaxed">{copy.blurb}</p>
            {copy.keyUrl ? (
              <button
                type="button"
                onClick={() => openOnboardingKeyUrl(copy.keyUrl!)}
                className="shrink-0 inline-flex items-center gap-1 text-ui-12 text-accent hover:underline pt-0.5"
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
            placeholder={`Paste your ${provider.display_name || provider.name} API key`}
            aria-label={`${provider.display_name || provider.name} API key`}
            {...(recommended ? { [FIRST_CONTROL]: "" } : {})}
            value={keyValue}
            onChange={(e) => setKeyValue(e.target.value)}
            disabled={saving || disabled}
            className="mt-2 w-full bg-bg border border-edge rounded-lg px-3 py-2 text-ui-13 font-mono text-txt placeholder:text-faint focus:outline-none focus:border-accent disabled:opacity-50"
          />
          {errorNotice ? (
            <p className="mt-2 text-ui-12 text-risk" role="alert">
              {errorNotice}
            </p>
          ) : null}
          <div className="mt-2 flex justify-end">
            <button
              type="submit"
              disabled={!canConnect}
              className="inline-flex items-center gap-1.5 bg-accent text-panel font-semibold rounded-lg px-3.5 py-1.5 text-ui-12.5 hover:brightness-110 disabled:opacity-35 disabled:hover:brightness-100 transition"
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


/** What first launch found on this Mac before the user chooses anything. */
export type OnboardingDetection = {
  /** Keys already saved or in the shell environment (a disconnected env key can be turned on). */
  keys: ProviderInfo[];
  /** Claude Code CLI is installed but not signed in (signed in counts as a key). */
  claudeCliInstalled: boolean;
  /** Local model servers found on this Mac or the LAN. */
  localEndpoints: { id: string; label: string }[];
};

const NO_DETECTION: OnboardingDetection = { keys: [], claudeCliInstalled: false, localEndpoints: [] };

/** Pure: providers plus the two optional probes, each of which may fail. */
export function detectionFrom(
  providers: ProviderInfo[],
  claudeCli: { installed?: boolean; authenticated?: boolean } | null,
  local: unknown,
): OnboardingDetection {
  const keys = providers.filter((p) => p.has_key || (p.has_env && p.disconnected));
  const externals = local && typeof local === "object" && Array.isArray((local as { externals?: unknown }).externals)
    ? ((local as { externals: unknown[] }).externals)
    : [];
  const localEndpoints = externals.flatMap((row) => {
    if (!row || typeof row !== "object") return [];
    const endpoint = row as { id?: unknown; name?: unknown; vendor?: unknown; base_url?: unknown };
    const id = typeof endpoint.id === "string" ? endpoint.id : "";
    if (!id) return [];
    const name = typeof endpoint.name === "string" && endpoint.name ? endpoint.name
      : typeof endpoint.vendor === "string" ? endpoint.vendor : "Local server";
    const url = typeof endpoint.base_url === "string" ? endpoint.base_url : "";
    return [{ id, label: url ? `${name} at ${url}` : name }];
  });
  return {
    keys,
    claudeCliInstalled: !!(claudeCli?.installed && !claudeCli.authenticated),
    localEndpoints,
  };
}

function openSettingsPage(page: "local-models" | "providers"): void {
  focusSettingsPage(page);
  window.dispatchEvent(new CustomEvent("harness-focus-tab", { detail: "settings" }));
}

function Disclosure({
  title,
  detail,
  open,
  onToggle,
  children,
}: {
  title: string;
  detail: string;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-edge/70">
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-3 py-2.5 text-left rounded-lg hover:bg-panel2/60 transition"
      >
        <ChevronRight size={14} className={`text-muted shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
        <span className="text-ui-12.5 font-semibold text-txt">{title}</span>
        <span className="text-ui-11 text-faint truncate">{detail}</span>
      </button>
      {open ? <div className="px-2 pb-2 space-y-2">{children}</div> : null}
    </section>
  );
}

/** Marks the control that takes focus when the provider load ends. */
const FIRST_CONTROL = "data-onboarding-first";

function DetectedRow({ title, detail, action, onAction, first = false }: { title: string; detail: string; action: string; onAction: () => void; first?: boolean }) {
  return (
    <div data-testid="detected-row" className="flex items-center justify-between gap-3 rounded-lg border border-good/30 bg-good/5 px-3 py-2.5">
      <div className="min-w-0">
        <div className="text-ui-13 font-semibold text-txt leading-tight">{title}</div>
        <div className="text-ui-11 text-muted mt-0.5 leading-snug">{detail}</div>
      </div>
      <button
        type="button"
        onClick={onAction}
        {...(first ? { [FIRST_CONTROL]: "" } : {})}
        className="shrink-0 bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-1 text-ui-11 font-medium"
      >
        {action}
      </button>
    </div>
  );
}

export default function OnboardingOverlay({ onClose }: OnboardingOverlayProps) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [detection, setDetection] = useState<OnboardingDetection>(NO_DETECTION);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [selectedKey, setSelectedKey] = useState("");
  const [plansOpen, setPlansOpen] = useState(false);
  const [othersOpen, setOthersOpen] = useState(false);
  const [oauthName, setOauthName] = useState("");
  const [oauthBusy, setOauthBusy] = useState(false);
  const [oauthHint, setOauthHint] = useState("");
  const [oauthError, setOauthError] = useState("");
  const [oauthSessionId, setOauthSessionId] = useState("");
  const [oauthPasteCode, setOauthPasteCode] = useState("");
  const abortRef = useRef(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  // The element focused when onboarding opened. The app opens it on first launch
  // with nothing focused; then Escape must land in the composer, not on BODY.
  const [opener] = useState(() => document.activeElement);
  const finalFocus = () => {
    if (opener instanceof HTMLElement && opener !== document.body && opener.isConnected) return true;
    requestAnimationFrame(focusComposer);
    return false;
  };
  const userMovedRef = useRef(false);
  const loadNotice = usePanelNotice(loadError || null);

  const featured = useMemo(() => featuredOnboardingProviders(providers), [providers]);
  const keyRows = useMemo(() => keyOnboardingProviders(providers), [providers]);
  const recommendedName = useMemo(() => defaultOnboardingProvider(providers), [providers]);
  const recommended = keyRows.find((p) => p.name === recommendedName);
  const otherKeyRows = keyRows.filter((p) => p.name !== recommendedName);

  // When the load ends, focus the first useful control. Do not take focus
  // back from a user who pressed a key or the pointer during the load.
  useEffect(() => {
    if (loading) return;
    const frame = requestAnimationFrame(() => {
      if (userMovedRef.current) return;
      bodyRef.current?.querySelector<HTMLElement>(`[${FIRST_CONTROL}]`)?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [loading]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError("");
      try {
        const list = await api.providers();
        if (cancelled) return;
        setProviders(list);
        setSelectedKey(defaultOnboardingProvider(list));
        // Optional probes: a missing CLI or local-model API only hides its row.
        const [claude, local] = await Promise.allSettled([
          Promise.resolve().then(() => api.getClaudeCliStatus()),
          Promise.resolve().then(() => api.getLocalModels()),
        ]);
        if (cancelled) return;
        setDetection(detectionFrom(
          list,
          claude.status === "fulfilled" ? claude.value : null,
          local.status === "fulfilled" ? local.value : null,
        ));
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

  const useDetectedKey = async (provider: ProviderInfo) => {
    if (provider.disconnected) {
      try {
        await api.setProviderEnabled(provider.name, true);
      } catch {
        setLoadError(`Could not turn on ${provider.display_name || provider.name}.`);
        return;
      }
    }
    finishConfigured(onClose);
  };

  const leaveFor = (page: "local-models" | "providers") => {
    finishSkip(onClose);
    openSettingsPage(page);
  };

  const foundNames = [
    ...detection.keys.map((p) => `${p.display_name || p.name} key`),
    ...(detection.claudeCliInstalled ? ["Claude Code CLI"] : []),
    ...detection.localEndpoints.map(() => "Local model server"),
  ];
  const found = foundNames.length > 0;
  const loadStatus = loading
    ? "Looking for keys and local models."
    : loadError
      ? ""
      : found
        ? `Found on this Mac: ${foundNames.join(", ")}.`
        : recommended
          ? `Nothing found on this Mac. ${recommended.display_name || recommended.name} is recommended.`
          : "Nothing found on this Mac.";

  return (
    <Modal
      open
      onClose={() => finishSkip(onClose)}
      labelledBy="onboarding-title"
      dismissOnOutsidePress={false}
      finalFocus={finalFocus}
      testId="onboarding-overlay"
      backdropClassName="fixed inset-0 z-50 [background:radial-gradient(ellipse_at_50%_38%,rgba(224,164,90,0.08)_0%,transparent_52%),#0f1113]"
      viewportClassName="fixed inset-0 z-50 flex items-center justify-center px-4 py-6"
      className="onboarding-card w-full max-w-[34rem] max-h-full flex flex-col rounded-2xl border border-edge bg-panel shadow-[0_24px_80px_rgba(0,0,0,0.45)] outline-none"
    >
      <div
        className="contents"
        onKeyDownCapture={() => { userMovedRef.current = true; }}
        onPointerDownCapture={() => { userMovedRef.current = true; }}
      >
        <header className="shrink-0 text-center px-7 pt-7 pb-5">
          <h1 id="onboarding-title" className="text-[1.45rem] font-semibold tracking-tight text-txt leading-tight">
            Connect a model
          </h1>
          <p className="mt-2 text-ui-13 text-muted leading-relaxed text-balance">
            Marionette runs its pilot and its workers on a model you choose. Pick one way to connect. You can add more later in Settings.
          </p>
        </header>

        <p role="status" data-testid="onboarding-load-status" className="sr-only">{loadStatus}</p>
        <div ref={bodyRef} className="min-h-0 flex-1 overflow-y-auto px-7 pb-5 space-y-5">
          {loading ? (
            <p aria-hidden="true" className="text-center text-muted text-ui-13 py-10">Looking for keys and local models…</p>
          ) : (
            <>
              {loadNotice ? (
                <p className="text-center text-ui-12 text-risk" role="alert">
                  {loadNotice}
                </p>
              ) : null}

              {found ? (
                <section aria-label="Found on this Mac">
                  <h2 className="text-ui-11 font-semibold uppercase tracking-wide text-muted mb-2">Found on this Mac</h2>
                  <div className="space-y-2">
                    {detection.keys.map((p, index) => (
                      <DetectedRow
                        key={p.name}
                        first={index === 0}
                        title={`${p.display_name || p.name} key`}
                        detail={`${p.has_env ? "From your shell environment" : "Saved in Marionette"}. ${capabilityLabel(p) || "Ready"}.`}
                        action={p.disconnected ? "Turn on" : "Use it"}
                        onAction={() => void useDetectedKey(p)}
                      />
                    ))}
                    {detection.claudeCliInstalled ? (
                      <DetectedRow
                        first={detection.keys.length === 0}
                        title="Claude Code CLI"
                        detail="Installed, not signed in. Sign in to chat on your Claude plan. Workers still need an API key."
                        action="Sign in"
                        onAction={() => leaveFor("providers")}
                      />
                    ) : null}
                    {detection.localEndpoints.map((endpoint, index) => (
                      <DetectedRow
                        key={endpoint.id}
                        first={index === 0 && detection.keys.length === 0 && !detection.claudeCliInstalled}
                        title="Local model server"
                        detail={endpoint.label}
                        action="Set up"
                        onAction={() => leaveFor("local-models")}
                      />
                    ))}
                  </div>
                </section>
              ) : null}

              {recommended ? (
                <section aria-label="Recommended">
                  <KeyProviderRow
                    provider={recommended}
                    recommended
                    expanded={selectedKey === recommended.name}
                    onExpand={() => setSelectedKey(recommended.name)}
                    disabled={oauthBusy}
                    onConnected={() => finishConfigured(onClose)}
                  />
                </section>
              ) : null}

              <section
                aria-label="Local model"
                data-testid="local-model-option"
                className="flex items-center justify-between gap-3 rounded-lg border border-edge/70 bg-panel2 px-3 py-2.5"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <HardDrive size={13} className="text-muted" />
                    <span className="text-ui-13 font-semibold text-txt leading-tight">Local model</span>
                    <span className="text-ui-10 text-faint">Chat only</span>
                  </div>
                  <div className="text-ui-11 text-muted mt-0.5 leading-snug">
                    Run a model on this Mac. No key and no cost per token. Slower than hosted models.
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => leaveFor("local-models")}
                  className="shrink-0 border border-edge text-txt hover:bg-panel rounded px-2.5 py-1 text-ui-11 font-medium"
                >
                  Set up
                </button>
              </section>

              <Disclosure
                title="Sign in with a plan"
                detail="ChatGPT, Claude Max, SuperGrok, Nous"
                open={plansOpen}
                onToggle={() => setPlansOpen((v) => !v)}
              >
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
              </Disclosure>

              <Disclosure
                title="Other API keys"
                detail={otherKeyRows.map((p) => p.display_name || p.name).slice(0, 4).join(", ")}
                open={othersOpen}
                onToggle={() => setOthersOpen((v) => !v)}
              >
                {otherKeyRows.length === 0 ? (
                  <p className="text-center text-muted text-ui-12 py-3">
                    No other key providers. You can add a key later in Settings.
                  </p>
                ) : (
                  otherKeyRows.map((p) => (
                    <KeyProviderRow
                      key={p.name}
                      provider={p}
                      expanded={p.name === selectedKey}
                      onExpand={() => setSelectedKey(p.name)}
                      disabled={oauthBusy}
                      onConnected={() => finishConfigured(onClose)}
                    />
                  ))
                )}
              </Disclosure>
            </>
          )}
        </div>

        <footer className="shrink-0 flex items-center justify-between gap-3 border-t border-edge/70 px-7 py-3.5">
          <span className="text-ui-11 text-faint">Esc also closes this.</span>
          <button
            type="button"
            onClick={() => finishSkip(onClose)}
            className="text-ui-12.5 text-muted hover:text-txt transition-colors"
          >
            I&apos;ll choose a provider later
          </button>
        </footer>
      </div>
    </Modal>
  );
}
