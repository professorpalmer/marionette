import { useEffect, useRef, useState } from "react";
import { Plus, Search, X } from "lucide-react";
import {
  api,
  type Settings,
  type Config,
  type PlatformAdapter,
  type GitStatus,
  type ProviderInfo,
  type BedrockStatus,
  type AuthPoolsResponse,
} from "../lib/api";
import {
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { SettingsCollapse } from "./SettingsCollapse";
import ProviderConfigModal from "./ProviderConfigModal";
import AccountsList from "./AccountsList";
import type { ProviderConfigValues } from "../lib/providerConfig";

export type SettingsSection = "general" | "providers" | "notifications" | "plugins";

export {
  toSafeSettingsSnapshot,
  clearSettingsSnapshot,
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";

type OAuthFlowOwner = "openai-codex" | "xai-oauth" | "nous" | "anthropic" | "claude-cli" | "cursor-cli";

/** The Accounts row that shows each sign-in flow. */
const FLOW_ROW: Record<OAuthFlowOwner, string> = {
  "openai-codex": "openai-codex",
  "xai-oauth": "xai",
  nous: "nous",
  anthropic: "anthropic",
  "claude-cli": "claude-code",
  "cursor-cli": "cursor-cli",
};

export default function SettingsPane({ section = "general" }: { onOpenWizard: () => void; section?: SettingsSection }) {
  const show = (s: SettingsSection) => section === s;
  // Settings search/filter: when a query is active we search ACROSS all sections
  // (ignore the current-section gate) and hide individual settings whose label +
  // help text do not contain the query. Empty query = today's behavior exactly.
  const [filter, setFilter] = useState("");
  const q = filter.trim().toLowerCase();
  const matches = (text: string) => !q || text.toLowerCase().includes(q);
  // Section gate: normally only the active section shows; while searching, all
  // sections are eligible so cross-section matches surface.
  const active = (s: SettingsSection) => (q ? true : show(s));
  // Track whether any setting rendered, to show a "no matches" hint.
  let anyShown = false;
  // gate() combines the section visibility with the keyword filter and records
  // whether anything survived so we can render a "no matches" line.
  const gate = (s: SettingsSection, keywords: string) => {
    const needsSettings = s !== "providers";
    const ok = active(s) && matches(keywords) && (!needsSettings || settings !== null);
    if (ok) anyShown = true;
    return ok;
  };
  // Note: search granularity is PER-SETTING already -- every gate() call below
  // carries its own keyword string (label + synonyms) for one logical setting,
  // so typing e.g. "timeout" or "distill" filters to just that control across
  // all sections. No separate per-item wrapper is needed.
  const [settings, setSettings] = useState<Settings | null>(() => readSettingsSnapshot());
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const errorNotice = usePanelNotice(error || null);
  
  // Git Provision states
  const [gitStatus, setGitStatus] = useState<GitStatus | null>(null);
  const [gitConnecting, setGitConnecting] = useState(false);
  const [gitError, setGitError] = useState("");
  const [deviceFlow, setDeviceFlow] = useState<{
    user_code: string;
    verification_uri: string;
    device_code: string;
  } | null>(null);
  const [gitPolling, setGitPolling] = useState(false);
  
  // Platform Adapter states
  const [platformAdapters, setPlatformAdapters] = useState<PlatformAdapter[]>([]);
  const [platformError, setPlatformError] = useState("");

  // Per-provider key management states
  const [providers, setProviders] = useState<ProviderInfo[]>([]);

  const [keyBootstrapIssues, setKeyBootstrapIssues] = useState<NonNullable<Config["key_bootstrap_issues"]>>([]);
  const [provBusy, setProvBusy] = useState<string>("");
  const [providerConfig, setProviderConfig] = useState<
    { manual: true } | { manual: false; provider: ProviderInfo } | null
  >(null);

  // Hermes-style credential pools (multi-key / rotate on plan limit)
  const [authPools, setAuthPools] = useState<AuthPoolsResponse | null>(null);
  const [poolBusy, setPoolBusy] = useState("");
  const [poolProvider, setPoolProvider] = useState("cursor");
  const [poolKeyInput, setPoolKeyInput] = useState("");
  const [poolLabelInput, setPoolLabelInput] = useState("");
  const POOL_FOCUS = ["cursor", "cursor-cli", "openrouter", "anthropic", "openai", "openai-codex", "xai-oauth", "nous"] as const;
  // One sign-in flow at a time. "awaiting_code" is the paste step of the
  // Claude Max flow: Complete must be clickable while it waits.
  const [oauthFlow, setOauthFlow] = useState<{ owner: OAuthFlowOwner; phase: "running" | "awaiting_code" } | null>(null);
  const flowOwner = oauthFlow?.owner ?? null;
  const [oauthHint, setOauthHint] = useState("");
  const [oauthSessionId, setOauthSessionId] = useState("");
  const oauthAbortRef = useRef(false);
  const [cursorCliStatus, setCursorCliStatus] = useState<{
    installed?: boolean;
    authenticated?: boolean;
    label?: string;
    error?: string;
    binary?: string | null;
  } | null>(null);

  // AWS Bedrock BYOK (multi-field; separate from single-key providers)
  const [bedrock, setBedrock] = useState<BedrockStatus | null>(null);
  const [bedrockBusy, setBedrockBusy] = useState(false);
  const [bedrockBearer, setBedrockBearer] = useState("");
  const [bedrockAccessKey, setBedrockAccessKey] = useState("");
  const [bedrockSecretKey, setBedrockSecretKey] = useState("");
  const [bedrockSessionToken, setBedrockSessionToken] = useState("");
  const [bedrockRegion, setBedrockRegion] = useState("");
  const [bedrockRegionAlt, setBedrockRegionAlt] = useState("");
  const [bedrockModelId, setBedrockModelId] = useState("");

  };

  useEffect(() => {
    // Revalidate in background; cached snapshot paints immediately (no blink).
    api.settings()
      .then((next) => {
        setSettings(next);
        writeSettingsSnapshot(next);
        setError("");
      })
      .catch((err) => {
        if (!readSettingsSnapshot()) {
          setError("Failed to load settings");
        }
        console.error(err);
      });
  }, []);

  // Section-scoped loads. API keys summary ("N/M connected") is visible while
  // the accordion is collapsed, so providers must load on section entry — not
  // only onFirstOpen of the collapse (that left a misleading 0/0).
  const searchActive = filter.trim().length > 0;
  useEffect(() => {
    const wantProviders = section === "providers" || searchActive;

    if (wantProviders) {
      loadProvidersList();
      if (typeof api.config === "function") {
        api.config()
          .then((c) => setKeyBootstrapIssues(c.key_bootstrap_issues || []))
          .catch((err) => console.error("Failed to load key bootstrap issues", err));
      }
    }
  }, [section, searchActive]);

  useEffect(() => {
    let timer: any = null;
    if (deviceFlow && gitPolling) {
      timer = setInterval(async () => {
        try {
          const res = await api.pollGitDevice(deviceFlow.device_code);
          if (res.connected) {
            setGitStatus(res);
            setDeviceFlow(null);
            setGitPolling(false);
          } else if (res.status !== "pending") {
            setGitPolling(false);
            if (res.error) {
              setGitError(res.error);
            }
          }
        } catch (err) {
          console.error("Polling error", err);
          setGitPolling(false);
          setGitError("Device authorization failed");
        }
      }, 5000);
    }
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [deviceFlow, gitPolling]);

  const handleConnectGH = async () => {
    setGitConnecting(true);
    setGitError("");
    setDeviceFlow(null);
    try {
      const res = await api.connectGit("gh");
      if ("error" in res && res.error) {
        setGitError(res.error);
      } else {
        setGitStatus(res as GitStatus);
      }
    } catch (err: any) {
      setGitError(err?.message || "Failed to connect via GitHub CLI");
    } finally {
      setGitConnecting(false);
    }
  };

  const handleStartDeviceFlow = async () => {
    setGitConnecting(true);
    setGitError("");
    setDeviceFlow(null);
    try {
      const res = await api.connectGit("device");
      if ("error" in res && res.error) {
        setGitError(res.error);
      } else if (res.device_code) {
        setDeviceFlow({
          user_code: res.user_code || "",
          verification_uri: res.verification_uri || "",
          device_code: res.device_code
        });
        setGitPolling(true);
      }
    } catch (err: any) {
      setGitError(err?.message || "Failed to start device flow");
    } finally {
      setGitConnecting(false);
    }
  };

  const refreshProviders = async () => {
    try {
      setProviders(await api.providers());
    } catch (e) {
      console.error(e);
    }
  };

  const refreshAuthPools = async () => {
    try { setAuthPools(await api.getAuthPools()); } catch (e) { console.error(e); }
  };

  const loadSignInData = () => {
    api.getAuthPools()
      .then(setAuthPools)
      .catch((err) => console.error("Failed to load auth pools", err));
    api.getCursorCliStatus({ refresh: false })
      .then(setCursorCliStatus)
      .catch((err) => console.error("Failed to load Cursor CLI status", err));
  };

  const loadProvidersList = () => {
    api.providers()
      .then((list) => {
        setProviders(list);
      })
      .catch((err) => console.error("Failed to load providers", err));
  };

  const loadAuthPoolsIfNeeded = () => {
    if (authPools !== null) return;
    api.getAuthPools()
      .then(setAuthPools)
      .catch((err) => console.error("Failed to load auth pools", err));
  };

  const loadBedrockData = () => {
    api.getBedrockStatus()
      .then((b) => {
        setBedrock(b);
        setBedrockRegion(b.aws_region || "");
        setBedrockRegionAlt(b.bedrock_region || "");
        setBedrockModelId(b.model_id || "");
      })
      .catch((err) => console.error("Failed to load Bedrock status", err));
  };

  const loadPlatformData = () => {
    api.getPlatform()
      .then((res) => setPlatformAdapters(res.adapters))
      .catch((err) => {
        setPlatformError("platform settings unavailable");
        console.error("Failed to load platform adapters", err);
      });
  };

  const loadGitData = () => {
    api.getGitStatus()
      .then(setGitStatus)
      .catch((err) => console.error("Failed to load Git status", err));
  };

  const PLAN_POOL_PROVIDERS = ["cursor-cli", "openai-codex", "anthropic", "xai-oauth", "nous"] as const;

  const handleAddPoolKey = async () => {
    const key = poolKeyInput.trim();
    if (!key || !poolProvider) return;
    setPoolBusy(poolProvider);
    try {
      await api.addAuthPoolKey(poolProvider, key, poolLabelInput.trim() || undefined);
      setPoolKeyInput("");
      setPoolLabelInput("");
      await refreshAuthPools();
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e) {
      console.error("Failed to add pool key", e);
      setError("Failed to add pool key");
    } finally {
      setPoolBusy("");
    }
  };

  const handleRemovePoolEntry = async (provider: string, entryId: string) => {
    setPoolBusy(provider);
    try {
      await api.removeAuthPoolEntry(provider, entryId);
      await refreshAuthPools();
    } catch (e) {
      console.error("Failed to remove pool entry", e);
    } finally {
      setPoolBusy("");
    }
  };



  const handlePoolStrategy = async (provider: string, strategy: string) => {
    setPoolBusy(provider);
    try {
      await api.setAuthPoolStrategy(provider, strategy);
      await refreshAuthPools();
    } catch (e) {
      console.error("Failed to set pool strategy", e);
    } finally {
      setPoolBusy("");
    }
  };

  const handleDeviceOAuthSignIn = async (provider: "openai-codex" | "xai-oauth" | "nous", labelFallback: string) => {
    oauthAbortRef.current = false;
    setOauthFlow({ owner: provider, phase: "running" });
    setOauthHint("");
    setOauthSessionId("");
    setError("");
    try {
      const start = await api.startAuthOAuth(provider, poolLabelInput.trim() || undefined);
      if (!start.session_id || !start.user_code) {
        throw new Error(start.error || "oauth start failed");
      }
      setOauthSessionId(start.session_id);
      setOauthHint(`Enter code ${start.user_code} at ${start.verification_uri}`);
      try {
        window.open(start.verification_uri_complete || start.verification_uri, "_blank");
      } catch {
        /* ignore */
      }
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const intervalMs = Math.max(1, start.interval || 5) * 1000;
      while (Date.now() < deadline) {
        if (oauthAbortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          return;
        }
        await new Promise((r) => setTimeout(r, intervalMs));
        if (oauthAbortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          return;
        }
        const poll = await api.pollAuthOAuth(start.session_id, provider);
        if (poll.status === "done") {
          setOauthHint(`Signed in as ${poll.label || labelFallback}`);
          setOauthSessionId("");
          await refreshAuthPools();
          await refreshProviders();
          setPoolProvider(provider);
          window.dispatchEvent(new Event("harness-config-changed"));
          return;
        }
        if (poll.status === "error") {
          throw new Error(poll.error || "oauth failed");
        }
      }
      throw new Error("Login timed out — click Sign in to try again.");
    } catch (e: any) {
      console.error(`${provider} OAuth failed`, e);
      const msg = e?.message || e?.error || `${provider} sign-in failed`;
      setError(msg);
      setOauthSessionId("");
      // Keep a retry-friendly hint (device-code toggle is a common first-time miss).
      setOauthHint(
        /device|enabled|access code|chatgpt settings/i.test(msg)
          ? "Enable ChatGPT device / Codex login codes, then Sign in again."
          : "Sign-in failed — fix the issue above, then Sign in again.",
      );
    } finally {
      setOauthFlow(null);
      oauthAbortRef.current = false;
    }
  };

  const handleCancelOAuth = () => {
    oauthAbortRef.current = true;
    const sid = oauthSessionId;
    if (sid) {
      api.cancelAuthOAuth(sid, flowOwner ?? poolProvider).catch(() => { /* best-effort */ });
    }
    setOauthSessionId("");
    setOauthFlow(null);
    // Cursor CLI often finishes browser login before the poll/trust step;
    // Cancel must not overwrite an already-good account with "cancelled".
    if (cursorCliStatus?.authenticated) {
      setOauthHint(`Signed in as ${cursorCliStatus.label || "Cursor account"}`);
    } else {
      setOauthHint("Sign-in cancelled — click Sign in to try again.");
    }
  };

  /** Sign-ins that run here; the Accounts list runs Claude (Anthropic) itself. */
  const accountSignIn = async (provider: string): Promise<void> => {
    if (provider === "openai-codex") return handleCodexSignIn();
    if (provider === "xai") return handleXaiSignIn();
    if (provider === "nous") return handleNousSignIn();
    if (provider === "claude-code") return handleClaudeCliSignIn();
    if (provider === "cursor-cli") return handleCursorCliSignIn();
  };

  const accountSignOut = async (provider: string): Promise<void> => {
    if (provider === "claude-code") return handleClaudeCliLogout();
    if (provider === "cursor-cli") return handleCursorCliLogout();
  };

  const handleCodexSignIn = async () => handleDeviceOAuthSignIn("openai-codex", "chatgpt-codex");
  const handleXaiSignIn = async () => handleDeviceOAuthSignIn("xai-oauth", "xai-oauth");
  const handleNousSignIn = async () => handleDeviceOAuthSignIn("nous", "nous");

  const refreshCursorCliStatus = async (opts?: { refresh?: boolean }) => {
    try {
      // ModelsSettingsPage keeps a localStorage snapshot; clear it when the
      // user refreshes Cursor CLI auth so the next Models visit isn't stuck
      // on a pre-Opus-5 catalog.
      try {
        const { clearCatalogSnapshot } = await import("./ModelsSettingsPage");
        clearCatalogSnapshot();
      } catch {
        /* ignore */
      }
      const st = await api.getCursorCliStatus({ refresh: opts?.refresh !== false });
      setCursorCliStatus(st);
      return st;
    } catch (e: any) {
      setCursorCliStatus({
        installed: false,
        authenticated: false,
        error: e?.message || "status check failed",
      });
      return null;
    }
  };

  const refreshClaudeCliStatus = async (opts?: { refresh?: boolean }) => {
    try {
      return await api.getClaudeCliStatus({ refresh: opts?.refresh !== false });
    } catch {
      return null;
    }
  };

  const handleClaudeCliSignIn = async () => {
    oauthAbortRef.current = false;
    setOauthFlow({ owner: "claude-cli", phase: "running" });
    setOauthHint("");
    setError("");
    const workspace = (settings?.repo || "").trim();
    try {
      const start = await api.startClaudeCliLogin(
        workspace ? { workspace } : undefined,
      );
      if (!start.ok && start.error) {
        throw new Error(start.error);
      }
      setOauthHint(
        start.hint
        || (start.launched
          ? "Complete Claude account login in the opened window, then wait…"
          : `Run \`${start.command || "claude auth login"}\` in a terminal, then wait…`),
      );
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const intervalMs = Math.max(2, start.poll_interval || 3) * 1000;
      while (Date.now() < deadline) {
        if (oauthAbortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          return;
        }
        await new Promise((r) => setTimeout(r, intervalMs));
        if (oauthAbortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          return;
        }
        const st = await refreshClaudeCliStatus();
        if (st?.authenticated) {
          const label = st.label || "Claude account";
          setOauthHint(`Signed in as ${label}`);
          await refreshProviders();
          window.dispatchEvent(new Event("harness-config-changed"));
          return;
        }
      }
      setOauthHint("Claude login timed out. Click Sign in to try again.");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Claude Code login failed");
    } finally {
      setOauthFlow(null);
    }
  };

  const handleClaudeCliLogout = async () => {
    setOauthFlow({ owner: "claude-cli", phase: "running" });
    try {
      await api.logoutClaudeCli();
      await refreshClaudeCliStatus();
      await refreshProviders();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Claude Code logout failed");
    } finally {
      setOauthFlow(null);
    }
  };

  const handleCursorCliSignIn = async () => {
    oauthAbortRef.current = false;
    setOauthFlow({ owner: "cursor-cli", phase: "running" });
    setOauthHint("");
    setError("");
    const workspace = (settings?.repo || "").trim();
    try {
      const start = await api.startCursorCliLogin(
        workspace ? { workspace } : undefined,
      );
      if (!start.ok && start.error) {
        throw new Error(start.error);
      }
      setOauthHint(
        start.hint
        || (start.launched
          ? "Complete Cursor account login in the opened window, then wait…"
          : `Run \`${start.command || "agent login"}\` in a terminal, then wait…`),
      );
      const deadline = Date.now() + (start.expires_in || 900) * 1000;
      const intervalMs = Math.max(2, start.poll_interval || 3) * 1000;
      while (Date.now() < deadline) {
        if (oauthAbortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          return;
        }
        await new Promise((r) => setTimeout(r, intervalMs));
        if (oauthAbortRef.current) {
          setOauthHint("Sign-in cancelled — click Sign in to try again.");
          return;
        }
        const st = await refreshCursorCliStatus();
        if (st?.authenticated) {
          // Browser login is done — clear the spinner immediately. Workspace
          // trust used to `await` a full `agent --print` cold start here, so
          // Sign in hung until Cancel even though status was already good.
          const label = st.label || "Cursor account";
          setOauthHint(`Signed in as ${label}`);
          setPoolProvider("cursor-cli");
          await refreshProviders();
          window.dispatchEvent(new Event("harness-config-changed"));
          void (async () => {
            try {
              const trust = await api.trustCursorCliWorkspace(
                workspace ? { workspace } : undefined,
              );
              if (oauthAbortRef.current) return;
              if (trust.trusted) {
                setOauthHint(
                  trust.workspace
                    ? `Signed in as ${label} · workspace trusted (${trust.workspace})`
                    : `Signed in as ${label} · workspace trusted`,
                );
              } else if (trust.error) {
                setOauthHint(
                  `Signed in as ${label} · workspace trust pending (pilot still passes --trust)`,
                );
              }
            } catch {
              if (!oauthAbortRef.current) {
                setOauthHint(
                  `Signed in as ${label} · workspace trust pending (pilot still passes --trust)`,
                );
              }
            }
          })();
          return;
        }
        if (st && st.installed === false) {
          throw new Error(st.error || "Cursor Agent CLI not installed");
        }
      }
      throw new Error("Login timed out — finish agent login, then Sign in again.");
    } catch (e: any) {
      console.error("Cursor CLI login failed", e);
      setError(e?.message || e?.error || "Cursor CLI sign-in failed");
      setOauthHint("Sign-in failed — install/login via Cursor Agent CLI, then try again.");
    } finally {
      setOauthFlow(null);
      oauthAbortRef.current = false;
    }
  };

  const handleCursorCliLogout = async () => {
    setOauthFlow({ owner: "cursor-cli", phase: "running" });
    try {
      await api.logoutCursorCli();
      setOauthHint("Signed out of Cursor account.");
      await refreshCursorCliStatus();
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e: any) {
      setError(e?.message || "Cursor CLI logout failed");
    } finally {
      setOauthFlow(null);
    }
  };



  const handleSaveBedrock = async () => {
    setBedrockBusy(true);
    try {
      const patch: Record<string, string> = {};
      if (bedrockBearer.trim()) patch.AWS_BEARER_TOKEN_BEDROCK = bedrockBearer.trim();
      if (bedrockAccessKey.trim()) patch.AWS_ACCESS_KEY_ID = bedrockAccessKey.trim();
      if (bedrockSecretKey.trim()) patch.AWS_SECRET_ACCESS_KEY = bedrockSecretKey.trim();
      if (bedrockSessionToken.trim()) patch.AWS_SESSION_TOKEN = bedrockSessionToken.trim();
      // Region / model always send current field values (including empty to clear).
      patch.AWS_REGION = bedrockRegion.trim();
      patch.BEDROCK_REGION = bedrockRegionAlt.trim();
      patch.BEDROCK_MODEL_ID = bedrockModelId.trim();
      const hasAuth =
        !!patch.AWS_BEARER_TOKEN_BEDROCK ||
        (!!patch.AWS_ACCESS_KEY_ID && !!patch.AWS_SECRET_ACCESS_KEY) ||
        !!bedrock?.configured;
      if (!hasAuth && !patch.AWS_REGION && !patch.BEDROCK_REGION && !patch.BEDROCK_MODEL_ID) {
        return;
      }
      const res = await api.setBedrockCredentials(patch);
      setBedrock(res);
      setBedrockBearer("");
      setBedrockAccessKey("");
      setBedrockSecretKey("");
      setBedrockSessionToken("");
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
      setStatus("saved");
      setTimeout(() => setStatus(""), 2000);
    } catch (e) {
      console.error("Failed to save Bedrock credentials", e);
      setError("Failed to save Bedrock credentials");
    } finally {
      setBedrockBusy(false);
    }
  };

  const handleClearBedrock = async () => {
    setBedrockBusy(true);
    try {
      const res = await api.clearBedrockCredentials();
      setBedrock(res);
      setBedrockBearer("");
      setBedrockAccessKey("");
      setBedrockSecretKey("");
      setBedrockSessionToken("");
      setBedrockRegion("");
      setBedrockRegionAlt("");
      setBedrockModelId("");
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e) {
      console.error("Failed to clear Bedrock credentials", e);
    } finally {
      setBedrockBusy(false);
    }
  };

  const handleProviderConfigSubmit = async (changed: Partial<ProviderConfigValues>) => {
    const name = (
      changed.name
      || (providerConfig && !providerConfig.manual ? providerConfig.provider.name : "")
      || ""
    ).trim();
    if (!name) throw new Error("Choose a provider.");
    if (!changed.api_key) throw new Error("Enter an API key.");
    setProvBusy(name);
    try {
      await api.setProviderKey(name, changed.api_key);
    } finally {
      setProvBusy("");
    }
    // Close only once the key is stored; a failure stays in the modal.
    setProviderConfig(null);
    window.dispatchEvent(new Event("harness-config-changed"));
    void refreshProviders();
  };




  const handleDisconnectGit = async () => {
    setGitConnecting(true);
    setGitError("");
    try {
      const res = await api.disconnectGit();
      setGitStatus(res);
      setDeviceFlow(null);
      setGitPolling(false);
    } catch (err: any) {
      setGitError(err?.message || "Failed to disconnect");
    } finally {
      setGitConnecting(false);
    }
  };

  const handleTogglePlatform = async (name: string, enabled: boolean) => {
    try {
      const res = await api.togglePlatform(name, enabled);
      setPlatformAdapters(res.adapters);
    } catch (err) {
      console.error("Failed to toggle platform adapter", err);
    }
  };

  const update = async (partial: Partial<Settings> & { api_key?: string; clear_api_key?: boolean }) => {
    if (!settings) return;
    setSaving(true);
    setStatus("");
    setError("");
    try {
      const updated = await api.updateSettings(partial);
      setSettings(updated);
      writeSettingsSnapshot(updated);
      setStatus("saved");
      // Mirror PilotPicker swap: settings can change driver / reach / keys, so
      // the picker and other listeners must refetch without a full reload.
      window.dispatchEvent(new Event("harness-config-changed"));
      const timer = setTimeout(() => setStatus(""), 2000);
      return () => clearTimeout(timer);
    } catch (err: any) {
      setError(err?.error || "Failed to update settings");
    } finally {
      setSaving(false);
    }
  };

  const canRenderWithoutSettings = section === "providers" || searchActive;
  if (!settings && !canRenderWithoutSettings) {
    return (
      <div className="flex flex-col h-full text-ui-12 p-4 text-faint">
        {errorNotice ? errorNotice : "Loading settings..."}
      </div>
    );
  }

  return (
    <div className="text-ui-12 max-w-3xl">
      {/* Floating save/error toast: fixed to the bottom-right so it overlays
          instead of inserting a block at the top that shoves every setting down
          (the reflow was the annoyance). Auto-dismiss handled by the callers
          that setStatus(""). pointer-events-none so it never blocks controls. */}
      {(status || error) && (
        <div className="fixed bottom-4 right-4 z-50 pointer-events-none flex items-center gap-2
                        px-3 py-1.5 rounded-lg border shadow-lg bg-panel2/95 backdrop-blur
                        border-edge">
          {status && <span className="text-good text-ui-11 font-medium">{status}</span>}
          {errorNotice && <span className="text-risk text-ui-11 font-medium">{errorNotice}</span>}
        </div>
      )}

      {/* Settings search: sticky at the top so it stays reachable while the
          dense settings list scrolls. Uses a FULLY OPAQUE background (bg-panel)
          and a high z-index so scrolled settings pass BEHIND it instead of
          bleeding through the bar. -mx-8 pt-6 -mt-6 offsets the shell's px-8/py-6
          scroll-container padding so the bar spans edge-to-edge and covers the
          gap above it (no sliver of content shows over the top). */}
      <div className="sticky -top-6 z-30 -mx-8 px-8 pt-6 pb-2 mb-2 bg-panel border-b border-edge">
        <div className="relative">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 text-faint" size={13} />
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search settings..."
            className="w-full bg-panel border border-edge rounded text-ui-11 text-txt
                       pl-7 pr-7 py-1.5 outline-none focus:border-accent placeholder:text-faint"
          />
          {filter && (
            <button
              type="button"
              onClick={() => setFilter("")}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 -translate-y-1/2 text-faint hover:text-txt"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      <div className="space-y-4">
        {gate("providers", "accounts sign in api keys connect disconnect subscription oauth claude max anthropic chatgpt codex cursor xai grok nous openrouter openai login") && (<>
        {/* One row per provider, one state, one primary action (harness/accounts.py). */}
        <SettingsCollapse
          id="accounts"
          title="Accounts"
          defaultOpen={true}
          forceOpen={!!q}
          onFirstOpen={() => { loadProvidersList(); loadSignInData(); }}
          className="space-y-2"
        >
          {keyBootstrapIssues.length > 0 && (
            <div
              data-testid="settings-key-bootstrap-issues"
              className="rounded-md border border-warn/40 bg-warn/10 px-2 py-1.5 text-ui-11 text-txt space-y-1"
            >
              <div className="font-medium">Key store did not finish saving on startup.</div>
              <div className="text-muted">
                The app kept running. Re-save keys here if a provider looks missing.
              </div>
              {keyBootstrapIssues.map((issue, i) => (
                <div key={`${issue.step}-${i}`} className="font-mono text-ui-10 text-faint break-all">
                  {issue.step}: {issue.message}
                </div>
              ))}
            </div>
          )}
          <p className="text-ui-10 text-muted leading-normal">
            Each provider has one row. The row shows the credential the pilot uses.
            Check sends one request to the provider and shows its answer.
          </p>
          <AccountsList
            onExternalSignIn={accountSignIn}
            onExternalSignOut={accountSignOut}
            onCancelFlow={handleCancelOAuth}
            flow={flowOwner ? { provider: FLOW_ROW[flowOwner], hint: oauthHint } : null}
            onOpenDetails={(name) => {
              const provider = providers.find((p) => p.name === name);
              if (provider) setProviderConfig({ manual: false, provider });
            }}
          />
          <button
            type="button"
            data-testid="add-provider"
            onClick={() => setProviderConfig({ manual: true })}
            className="flex items-center gap-1 text-accent hover:text-accent/80 text-ui-11 font-medium"
          >
            <Plus size={12} />
            Add provider
          </button>
        </SettingsCollapse>
        {providerConfig ? (
          <ProviderConfigModal
            open
            manual={providerConfig.manual}
            provider={providerConfig.manual ? null : providerConfig.provider}
            providerChoices={providers.map((p) => ({ name: p.name, label: p.display_name || p.name }))}
            busy={!!provBusy}
            onClose={() => setProviderConfig(null)}
            onSubmit={handleProviderConfigSubmit}
          />
        ) : null}

        </>)}
        {gate("providers", "credential pool rotate cursor openrouter anthropic openai api key accounts") && (<>
        <SettingsCollapse
          id="credential-pools"
          title="Credential pools"
          defaultOpen={false}
          forceOpen={!!q}
          onFirstOpen={loadAuthPoolsIfNeeded}
          summary={(() => {
            const pools = authPools?.pools || [];
            const entries = pools.reduce((n, p) => n + (p.entries?.length || 0), 0);
            if (!entries) return "empty";
            return `${entries} key${entries === 1 ? "" : "s"} · ${pools.length} provider${pools.length === 1 ? "" : "s"}`;
          })()}
        >
          <p className="text-ui-10 text-muted leading-normal">
            Optional. Add more API keys for one provider. When a key hits a plan limit
            (HTTP 429 or 402), the pilot uses the next key. The prompt cache can reset then.
            Sign-ins (ChatGPT Codex, Claude, Cursor CLI, xAI, Nous) are in Accounts above.
            When no key works, the turn fails until a cooldown ends or you add a key.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {POOL_FOCUS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPoolProvider(p)}
                className={`px-2 py-0.5 rounded text-ui-10 font-mono border transition-colors ${
                  poolProvider === p
                    ? "bg-accent/15 border-accent/40 text-accent"
                    : "bg-panel2 border-edge text-muted hover:bg-panel"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
          <div className="space-y-1.5 bg-panel2 border border-edge/50 rounded p-2">
            {(PLAN_POOL_PROVIDERS as readonly string[]).includes(poolProvider) ? (
              <>
                <p className="text-ui-10 text-muted">
                  Sign in to this account in Accounts above.
                </p>
                <select
                  value={
                    (authPools?.pools || []).find((x) => x.provider === poolProvider)?.strategy
                    || "fill_first"
                  }
                  onChange={(e) => handlePoolStrategy(poolProvider, e.target.value)}
                  className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-10 text-muted"
                >
                  {(authPools?.strategies || ["fill_first", "round_robin", "least_used", "random"]).map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
                {(() => {
                  const pool = (authPools?.pools || []).find((x) => x.provider === poolProvider);
                  const entries = pool?.entries || [];
                  if (!entries.length) {
                    return (
                      <p className="text-ui-10 text-faint italic">No pooled credentials for {poolProvider} yet.</p>
                    );
                  }
                  return (
                    <ul className="space-y-1 pt-1 border-t border-edge/40">
                      {entries.map((e) => (
                        <li key={e.id} className="flex items-center justify-between gap-2 text-ui-10">
                          <div className="min-w-0">
                            <span className="font-medium text-txt">{e.label || e.id}</span>
                            <span className="text-faint font-mono ml-1.5">{e.masked}</span>
                            <span className={`ml-1.5 uppercase tracking-wider text-ui-8 ${
                              e.last_status === "exhausted" ? "text-risk" : "text-good"
                            }`}>
                              {e.last_status || "ok"}
                            </span>
                          </div>
                          <button
                            type="button"
                            onClick={() => handleRemovePoolEntry(poolProvider, e.id)}
                            className="text-risk/80 hover:text-risk text-ui-10 shrink-0"
                          >
                            remove
                          </button>
                        </li>
                      ))}
                    </ul>
                  );
                })()}
              </>
            ) : (
              <>
            <input
              type="password"
              value={poolKeyInput}
              onChange={(e) => setPoolKeyInput(e.target.value)}
              placeholder={`${poolProvider} API key`}
              className="w-full bg-panel border border-edge rounded px-2 py-1 text-ui-11 font-mono"
            />
            <input
              type="text"
              value={poolLabelInput}
              onChange={(e) => setPoolLabelInput(e.target.value)}
              placeholder="label (optional, e.g. cursor-plan-a)"
              className="w-full bg-panel border border-edge rounded px-2 py-1 text-ui-11"
            />
            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                onClick={handleAddPoolKey}
                disabled={!!poolBusy || !poolKeyInput.trim()}
                className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-0.5 font-medium text-ui-10 disabled:opacity-30"
              >
                {poolBusy === poolProvider ? "Adding..." : "Add to pool"}
              </button>
              <select
                value={
                  (authPools?.pools || []).find((x) => x.provider === poolProvider)?.strategy
                  || "fill_first"
                }
                onChange={(e) => handlePoolStrategy(poolProvider, e.target.value)}
                className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-10 text-muted"
              >
                {(authPools?.strategies || ["fill_first", "round_robin", "least_used", "random"]).map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
            {(() => {
              const pool = (authPools?.pools || []).find((x) => x.provider === poolProvider);
              const entries = pool?.entries || [];
              if (!entries.length) {
                return (
                  <p className="text-ui-10 text-faint italic">No pooled credentials for {poolProvider} yet.</p>
                );
              }
              return (
                <ul className="space-y-1 pt-1 border-t border-edge/40">
                  {entries.map((e) => (
                    <li key={e.id} className="flex items-center justify-between gap-2 text-ui-10">
                      <div className="min-w-0">
                        <span className="font-medium text-txt">{e.label || e.id}</span>
                        <span className="text-faint font-mono ml-1.5">{e.masked}</span>
                        <span className={`ml-1.5 uppercase tracking-wider text-ui-8 ${
                          e.last_status === "exhausted" ? "text-risk" : "text-good"
                        }`}>
                          {e.last_status || "ok"}
                        </span>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleRemovePoolEntry(poolProvider, e.id)}
                        className="text-risk/80 hover:text-risk text-ui-10 shrink-0"
                      >
                        remove
                      </button>
                    </li>
                  ))}
                </ul>
              );
            })()}
              </>
            )}
          </div>
        </SettingsCollapse>
        </>)}
        {gate("providers", "bedrock aws amazon bearer access key region inference profile") && (<>
        {/* AWS Bedrock BYOK -- multi-field credentials for agentic/PM workers */}
        <SettingsCollapse
          id="bedrock"
          title="AWS Bedrock"
          defaultOpen={false}
          forceOpen={!!q}
          onFirstOpen={loadBedrockData}
          summary={
            bedrock?.configured
              ? `configured · ${bedrock.auth_mode || "credentials"}`
              : "not configured"
          }
        >
          <p className="text-ui-10 text-muted leading-normal">
            Preferred: paste an <span className="font-mono text-faint">AWS_BEARER_TOKEN_BEDROCK</span>.
            Or use access key + secret (+ optional session token). Credentials are injected into
            the worker process env for Bedrock-priced models on the agentic backend.
          </p>
          <div className="space-y-1.5 bg-panel2 border border-edge/50 rounded p-2">
            <input
              type="password"
              placeholder={bedrock?.has_bearer ? "Bearer token (leave blank to keep)" : "AWS_BEARER_TOKEN_BEDROCK"}
              value={bedrockBearer}
              onChange={(e) => setBedrockBearer(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <div className="text-ui-9 text-faint uppercase tracking-wider pt-1">or access keys</div>
            <input
              type="password"
              placeholder={bedrock?.has_access_key ? "Access key id (leave blank to keep)" : "AWS_ACCESS_KEY_ID"}
              value={bedrockAccessKey}
              onChange={(e) => setBedrockAccessKey(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <input
              type="password"
              placeholder={bedrock?.has_access_key ? "Secret access key (leave blank to keep)" : "AWS_SECRET_ACCESS_KEY"}
              value={bedrockSecretKey}
              onChange={(e) => setBedrockSecretKey(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <input
              type="password"
              placeholder={bedrock?.has_session_token ? "Session token (leave blank to keep)" : "AWS_SESSION_TOKEN (optional)"}
              value={bedrockSessionToken}
              onChange={(e) => setBedrockSessionToken(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <div className="grid grid-cols-2 gap-1.5 pt-1">
              <input
                type="text"
                placeholder="AWS_REGION (e.g. us-east-1)"
                value={bedrockRegion}
                onChange={(e) => setBedrockRegion(e.target.value)}
                disabled={bedrockBusy}
                className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
              />
              <input
                type="text"
                placeholder="BEDROCK_REGION (optional)"
                value={bedrockRegionAlt}
                onChange={(e) => setBedrockRegionAlt(e.target.value)}
                disabled={bedrockBusy}
                className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
              />
            </div>
            <input
              type="text"
              placeholder="Default inference profile id (optional)"
              value={bedrockModelId}
              onChange={(e) => setBedrockModelId(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-ui-11 focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <div className="flex gap-2 pt-1">
              <button
                onClick={handleSaveBedrock}
                disabled={bedrockBusy}
                className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded px-2.5 py-0.5 font-medium text-ui-10 disabled:opacity-30 transition-colors"
              >
                {bedrockBusy ? "Saving..." : "Save Bedrock"}
              </button>
              {bedrock?.configured ? (
                <button
                  onClick={handleClearBedrock}
                  disabled={bedrockBusy}
                  className="bg-risk/10 hover:bg-risk/20 text-risk border border-risk/30 hover:border-risk/50 rounded px-2.5 py-0.5 font-medium text-ui-10 disabled:opacity-30 transition-colors"
                >
                  Disconnect
                </button>
              ) : null}
            </div>
          </div>
        </SettingsCollapse>

        </>)}
        {gate("providers", "platform adapters control cli claude codex openai cursor") && (<>
        {/* Platform Adapters Control (ADVANCED -- optional) */}
        <SettingsCollapse
          id="external-platforms"
          title="External Worker Platforms"
          defaultOpen={false}
          forceOpen={!!q}
          onFirstOpen={loadPlatformData}
          summary={(() => {
            const on = platformAdapters.filter((a) => a.enabled).length;
            return on > 0 ? `${on} on · advanced` : "advanced / optional";
          })()}
        >
          <p className="text-ui-10 text-muted leading-normal">
            By default, implement/parallel workers run on the built-in provider worker (your configured API key, in an isolated worktree) -- no external CLI needed. These adapters let you instead delegate worker runs to an external coding-agent CLI (Cursor, Claude Code, Codex) when it is installed. Optional.
          </p>

          {platformError ? (
            <p className="text-ui-10 text-muted italic">{platformError}</p>
          ) : platformAdapters.length === 0 ? (
            <p className="text-ui-10 text-muted italic">Loading platform settings...</p>
          ) : (
            <div className="space-y-2">
              <div className="space-y-2 bg-panel rounded border border-edge/40 p-2">
                {platformAdapters.map((adapter) => (
                  <div key={adapter.name} className="flex items-center justify-between gap-2 border-b border-edge/30 last:border-b-0 pb-1.5 last:pb-0 pt-1.5 first:pt-0">
                    <div className="space-y-0.5">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono font-medium text-ui-11 text-txt">{adapter.name}</span>
                        <span className={`px-1 py-0.5 text-ui-8 uppercase font-bold tracking-wider rounded ${
                          adapter.implement_capable 
                            ? "bg-accent/10 text-accent/90 border border-accent/25" 
                            : "bg-panel2 text-muted border border-edge"
                        }`}>
                          {adapter.implement_capable ? "implement" : "analysis"}
                        </span>
                        {!adapter.available && (
                          <span className="px-1 py-0.5 text-ui-8 uppercase font-bold tracking-wider rounded bg-risk/10 text-risk border border-risk/20">
                            not available
                          </span>
                        )}
                      </div>
                      <p className="text-ui-10 text-muted">
                        {adapter.note}
                      </p>
                    </div>
                    <button
                      onClick={() => handleTogglePlatform(adapter.name, !adapter.enabled)}
                      className={`px-2.5 py-1 rounded text-ui-10 uppercase font-bold tracking-wider border transition-colors ${
                        adapter.enabled
                          ? "bg-accent/10 border-accent/30 text-accent hover:bg-accent/20"
                          : "bg-panel2 border-edge text-muted hover:bg-panel"
                      }`}
                    >
                      {adapter.enabled ? "on" : "off"}
                    </button>
                  </div>
                ))}
              </div>

              <p className="text-ui-10 text-muted leading-normal">
                With no external adapter enabled, implement/parallel workers run on the built-in provider worker (default). Enable an adapter above only to delegate to that external CLI instead.
              </p>
            </div>
          )}
        </SettingsCollapse>

        </>)}
        </div>

        </>)}
        {gate("providers", "github wiki repo provisioning git connect device flow") && (<>
        {/* GitHub & Wiki Repo Provisioning */}
        <SettingsCollapse
          id="github-wiki"
          title="GitHub / Wiki Repo"
          defaultOpen={false}
          forceOpen={!!q}
          onFirstOpen={loadGitData}
          summary={gitStatus?.connected ? "connected" : "not connected"}
        >
          {gitError && (
            <div className="text-risk text-ui-10 font-semibold bg-risk/10 border border-risk/30 rounded p-2">
              {gitError}
            </div>
          )}

          {gitStatus?.connected ? (
            <div className="space-y-2 bg-panel rounded border border-edge/40 p-2.5">
              <div className="text-ui-11 leading-relaxed text-muted">
                Connected to GitHub. Wiki repository is provisioned and active.
              </div>
              <div className="flex items-center justify-between gap-2 border-t border-edge/30 pt-2 mt-1">
                <div className="space-y-0.5">
                  <div className="text-ui-10 text-faint uppercase font-bold tracking-wider">Wiki Repository</div>
                  {gitStatus.html_url ? (
                    <a
                      href={gitStatus.html_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-ui-11 text-accent hover:underline break-all"
                    >
                      {gitStatus.wiki_repo}
                    </a>
                  ) : (
                    <span className="font-mono text-ui-11 text-txt">{gitStatus.wiki_repo}</span>
                  )}
                </div>
                <button
                  disabled={gitConnecting}
                  onClick={handleDisconnectGit}
                  className="bg-risk/10 border border-risk/20 hover:bg-risk/20 text-risk text-ui-10 uppercase font-bold tracking-wider px-2.5 py-1 rounded transition disabled:opacity-50"
                >
                  Disconnect
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-2.5">
              <div className="text-ui-10 text-muted leading-relaxed">
                Connect your GitHub account to automatically provision a private "my-portable-llm-wiki" repository as your durable cross-LLM memory.
              </div>

              {gitConnecting && (
                <div className="text-ui-10 text-muted italic flex items-center gap-1.5">
                  <span className="animate-pulse">Provisioning repository...</span>
                </div>
              )}

              {!gitConnecting && !deviceFlow && (
                <div className="flex flex-col gap-2">
                  {gitStatus?.gh_available ? (
                    <button
                      onClick={handleConnectGH}
                      className="w-full bg-accent hover:bg-accent/90 text-accent-txt text-ui-11 font-bold px-3 py-1.5 rounded transition shadow-sm text-center"
                    >
                      Connect with GitHub CLI ({gitStatus.gh_user})
                    </button>
                  ) : (
                    <div className="text-ui-10 text-muted italic bg-panel rounded border border-edge/30 p-2 leading-normal">
                      GitHub CLI (gh) not detected or not authenticated. Install or authenticate to enable one-click connection.
                    </div>
                  )}

                  <button
                    onClick={handleStartDeviceFlow}
                    className="w-full bg-panel hover:bg-panel2 border border-edge text-txt text-ui-11 font-semibold px-3 py-1.5 rounded transition text-center"
                  >
                    Connect via Device Code instead
                  </button>
                </div>
              )}

              {deviceFlow && (
                <div className="bg-panel rounded border border-edge/40 p-2.5 space-y-2">
                  <div className="text-ui-11 font-medium text-txt">
                    Verification Code:
                  </div>
                  <div className="font-mono text-center text-lg tracking-widest font-bold bg-bg border border-edge/60 rounded py-1.5 text-accent select-all">
                    {deviceFlow.user_code}
                  </div>
                  <div className="text-ui-10 text-muted leading-normal">
                    Go to{" "}
                    <a
                      href={deviceFlow.verification_uri}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-accent underline hover:text-accent-hover"
                    >
                      {deviceFlow.verification_uri.replace(/^https?:\/\//, "")}
                    </a>{" "}
                    and enter the code above to authorize.
                  </div>
                  {gitPolling && (
                    <div className="text-ui-10 text-accent/90 italic flex items-center gap-1.5">
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-ping" />
                      Waiting for authorization...
                    </div>
                  )}
                  <button
                    onClick={() => {
                      setDeviceFlow(null);
                      setGitPolling(false);
                    }}
                    className="w-full text-muted hover:text-txt text-ui-10 font-semibold uppercase tracking-wider text-center pt-1"
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          )}
        </SettingsCollapse>

        </>)}
        {q && !anyShown && (
          <p className="text-ui-11 text-muted">No settings match "{filter.trim()}".</p>
        )}
      </div>
    </div>
  );
}
