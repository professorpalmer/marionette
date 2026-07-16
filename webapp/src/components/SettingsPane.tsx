import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight, ChevronDown, Plus, Trash2, ExternalLink, Search, X } from "lucide-react";
import {
  api,
  type Settings,
  type UsageData,
  type PlatformAdapter,
  type GitStatus,
  type ProviderInfo,
  type BedrockStatus,
  type AuthPoolsResponse,
} from "../lib/api";
import SkillsPane from "./SkillsPane";
import MemoryPane from "./MemoryPane";

export type SettingsSection = "general" | "safety" | "providers" | "notifications" | "advanced";

const SETTINGS_SECTION_OPEN_KEY = "pmharness.settings.sectionOpen";

function loadSettingsSectionOpen(id: string, defaultOpen: boolean): boolean {
  try {
    const raw = localStorage.getItem(SETTINGS_SECTION_OPEN_KEY);
    if (!raw) return defaultOpen;
    const map = JSON.parse(raw) as Record<string, boolean>;
    if (typeof map[id] === "boolean") return map[id];
  } catch {
    /* ignore */
  }
  return defaultOpen;
}

function persistSettingsSectionOpen(id: string, open: boolean) {
  try {
    const raw = localStorage.getItem(SETTINGS_SECTION_OPEN_KEY);
    const map = (raw ? JSON.parse(raw) : {}) as Record<string, boolean>;
    map[id] = open;
    localStorage.setItem(SETTINGS_SECTION_OPEN_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

/** Collapsible settings block — chevron title + optional summary; persists open state. */
function SettingsCollapse({
  id,
  title,
  summary,
  defaultOpen = true,
  forceOpen = false,
  className = "space-y-2 border-t border-edge pt-3",
  onFirstOpen,
  children,
}: {
  id: string;
  title: string;
  summary?: string;
  defaultOpen?: boolean;
  forceOpen?: boolean;
  className?: string;
  onFirstOpen?: () => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(() => loadSettingsSectionOpen(id, defaultOpen));
  const shown = forceOpen || open;
  const firstOpenCalled = useRef(false);

  useEffect(() => {
    if (shown && onFirstOpen && !firstOpenCalled.current) {
      firstOpenCalled.current = true;
      onFirstOpen();
    }
  }, [shown, onFirstOpen]);

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => {
          setOpen((v) => {
            const next = !v;
            persistSettingsSectionOpen(id, next);
            return next;
          });
        }}
        className="w-full flex items-center justify-between gap-2 text-left focus:outline-none group"
      >
        <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1.5 min-w-0 group-hover:text-txt transition">
          {shown ? <ChevronDown size={12} className="shrink-0" /> : <ChevronRight size={12} className="shrink-0" />}
          <span className="truncate">{title}</span>
          {summary ? (
            <span className="normal-case tracking-normal font-normal text-faint/80 shrink-0">
              · {summary}
            </span>
          ) : null}
        </span>
      </button>
      {shown ? <div className="space-y-2">{children}</div> : null}
    </div>
  );
}

export default function SettingsPane({ onOpenWizard, section = "general" }: { onOpenWizard: () => void; section?: SettingsSection }) {
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
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [wikiCfg, setWikiCfg] = useState<{ api_base: string; has_token: boolean } | null>(null);
  const [wikiBase, setWikiBase] = useState("");
  const [wikiToken, setWikiToken] = useState("");
  const [wikiSaving, setWikiSaving] = useState(false);
  
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
  const [providersLoaded, setProvidersLoaded] = useState(false);
  const [provKeyInput, setProvKeyInput] = useState<Record<string, string>>({});
  const [provBusy, setProvBusy] = useState<string>("");

  // Hermes-style credential pools (multi-key / rotate on plan limit)
  const [authPools, setAuthPools] = useState<AuthPoolsResponse | null>(null);
  const [poolBusy, setPoolBusy] = useState("");
  const [poolProvider, setPoolProvider] = useState("cursor");
  const [poolKeyInput, setPoolKeyInput] = useState("");
  const [poolLabelInput, setPoolLabelInput] = useState("");
  const POOL_FOCUS = ["cursor", "cursor-cli", "openrouter", "anthropic", "openai", "openai-codex", "xai-oauth", "nous"] as const;
  const [oauthBusy, setOauthBusy] = useState(false);
  const [oauthHint, setOauthHint] = useState("");
  const [oauthSessionId, setOauthSessionId] = useState("");
  const [oauthPasteCode, setOauthPasteCode] = useState("");
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

  // Feature states
  const [hooks, setHooks] = useState<any[]>([]);
  const [allowedEvents, setAllowedEvents] = useState<string[]>([]);

  // Expand/collapse states
  const [hooksOpen, setHooksOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);

  // Form states for hooks
  const [newHookEvent, setNewHookEvent] = useState("");
  const [newHookCommand, setNewHookCommand] = useState("");
  const [hookError, setHookError] = useState("");
  const [hookStatus, setHookStatus] = useState("");

  const loadHooks = async () => {
    try {
      const data = await api.getHooks();
      setHooks(data.hooks || []);
      setAllowedEvents(data.events || []);
      if (data.events && data.events.length > 0 && !newHookEvent) {
        setNewHookEvent(data.events[0]);
      }
    } catch (err) {
      console.error("Failed to load hooks", err);
    }
  };

  const [notify, setNotify] = useState(() => {
    const val = localStorage.getItem("pmharness.notify");
    return val !== null ? val === "true" : true;
  });
  const [sound, setSound] = useState(() => {
    const val = localStorage.getItem("pmharness.sound");
    return val !== null ? val === "true" : false;
  });
  const [queueMessages, setQueueMessages] = useState(() => {
    const val = localStorage.getItem("pmharness.queueMessages");
    return val !== null ? val === "true" : true;
  });

  const toggleNotify = () => {
    const newVal = !notify;
    setNotify(newVal);
    localStorage.setItem("pmharness.notify", String(newVal));
  };
  const toggleSound = () => {
    const newVal = !sound;
    setSound(newVal);
    localStorage.setItem("pmharness.sound", String(newVal));
  };
  const toggleQueue = () => {
    const newVal = !queueMessages;
    setQueueMessages(newVal);
    localStorage.setItem("pmharness.queueMessages", String(newVal));
  };

  // Live UI (Vite HMR): the backend always runs from the source checkout, so this
  // toggle only governs whether the React UI is served from a Vite dev server
  // (instant hot-reload) instead of the prebuilt dist/. Desktop-only (needs
  // Electron to swap the renderer source and restart the backend).
  const _selfDevIpc = (typeof window !== "undefined" && (window as any).harnessIPC?.selfDev) || null;
  const _restartIpc = (typeof window !== "undefined" && (window as any).harnessIPC?.restart) || null;
  const [selfDev, setSelfDev] = useState<{ enabled: boolean; viable: boolean } | null>(null);
  const [selfDevBusy, setSelfDevBusy] = useState(false);
  const [restarting, setRestarting] = useState(false);

  useEffect(() => {
    if (!_selfDevIpc) return;
    _selfDevIpc.get().then((s: any) => setSelfDev(s)).catch(() => {});
  }, []);

  const toggleSelfDev = async () => {
    if (!_selfDevIpc || !selfDev) return;
    setSelfDevBusy(true);
    try {
      const res = await _selfDevIpc.set(!selfDev.enabled);
      const next = await _selfDevIpc.get();
      setSelfDev(next);
      // A runtime change only takes effect on the next backend start, so offer
      // an immediate restart to apply it now.
      if (res && _restartIpc) {
        setRestarting(true);
        try { await _restartIpc(); } finally { setRestarting(false); }
      }
    } finally {
      setSelfDevBusy(false);
    }
  };

  const restartBackend = async () => {
    if (!_restartIpc) return;
    setRestarting(true);
    try { await _restartIpc(); } finally { setRestarting(false); }
  };

  useEffect(() => {
    // Always need core settings to leave the Loading gate.
    api.settings()
      .then(setSettings)
      .catch((err) => {
        setError("Failed to load settings");
        console.error(err);
      });
  }, []);

  // Section-scoped loads. API keys summary ("N/M connected") is visible while
  // the accordion is collapsed, so providers must load on section entry — not
  // only onFirstOpen of the collapse (that left a misleading 0/0).
  const searchActive = filter.trim().length > 0;
  useEffect(() => {
    const wantNotify = section === "notifications" || searchActive;
    const wantAdvanced = section === "advanced" || searchActive;
    const wantProviders = section === "providers" || searchActive;

    if (wantNotify) {
      api.getUsage()
        .then(setUsage)
        .catch((err) => console.error("Failed to load usage statistics", err));
    }

    if (wantAdvanced) {
      api.getWikiConfig()
        .then((w) => { setWikiCfg(w); setWikiBase(w.api_base || ""); })
        .catch(() => {});
      loadHooks();
    }

    if (wantProviders) {
      loadProvidersList();
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
      setProvidersLoaded(true);
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
        setProvidersLoaded(true);
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
  const poolEntriesFor = (provider: string) =>
    (authPools?.pools || []).find((x) => x.provider === provider)?.entries || [];
  const planAccountStatusLine = (provider: string) => {
    const entries = poolEntriesFor(provider);
    if (entries.length) {
      const e = entries[0];
      return `Signed in as ${e.label || e.masked || provider}`;
    }
    return "Not signed in";
  };

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

  /** Sign out every pool credential for a plan OAuth provider (Codex / Claude / xAI / Nous). */
  const handlePlanPoolSignOut = async (provider: string) => {
    const entries = poolEntriesFor(provider);
    if (!entries.length) return;
    setPoolBusy(provider);
    setOauthHint("");
    setError("");
    try {
      for (const e of entries) {
        await api.removeAuthPoolEntry(provider, e.id);
      }
      // OAuth login also mirrors into keys.json / process env — clear that too
      // so Sign out matches Cursor CLI (status flips to Not signed in).
      try {
        await api.clearProviderKey(provider);
      } catch {
        /* pool-only providers may not have a keys.json row */
      }
      if (provider === "xai-oauth") {
        try {
          await api.clearProviderKey("xai");
        } catch {
          /* ignore */
        }
      }
      if (provider === "anthropic") {
        setOauthSessionId("");
        setOauthPasteCode("");
      }
      await refreshAuthPools();
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
      setOauthHint("Signed out");
    } catch (e) {
      console.error("Failed to sign out plan account", e);
      setError("Failed to sign out");
    } finally {
      setPoolBusy("");
    }
  };

  const refreshPlanPoolStatus = async () => {
    try {
      await refreshAuthPools();
      await refreshProviders();
    } catch (e) {
      console.error("Failed to refresh plan account status", e);
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
    setOauthBusy(true);
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
      setOauthBusy(false);
      oauthAbortRef.current = false;
    }
  };

  const handleCancelOAuth = () => {
    oauthAbortRef.current = true;
    const sid = oauthSessionId;
    if (sid) {
      api.cancelAuthOAuth(sid, poolProvider).catch(() => { /* best-effort */ });
    }
    setOauthSessionId("");
    setOauthPasteCode("");
    setOauthBusy(false);
    setOauthHint("Sign-in cancelled — click Sign in to try again.");
  };

  const handleCodexSignIn = async () => handleDeviceOAuthSignIn("openai-codex", "chatgpt-codex");
  const handleXaiSignIn = async () => handleDeviceOAuthSignIn("xai-oauth", "xai-oauth");
  const handleNousSignIn = async () => handleDeviceOAuthSignIn("nous", "nous");

  const refreshCursorCliStatus = async (opts?: { refresh?: boolean }) => {
    try {
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

  const handleCursorCliSignIn = async () => {
    oauthAbortRef.current = false;
    setOauthBusy(true);
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
          // Headless Agent CLI requires workspace trust; bundle it into Sign in.
          let trustNote = "";
          try {
            const trust = await api.trustCursorCliWorkspace(
              workspace ? { workspace } : undefined,
            );
            if (trust.trusted) {
              trustNote = trust.workspace
                ? ` · workspace trusted (${trust.workspace})`
                : " · workspace trusted";
            } else if (trust.error) {
              trustNote = " · workspace trust pending (pilot still passes --trust)";
            }
          } catch {
            trustNote = " · workspace trust pending (pilot still passes --trust)";
          }
          setOauthHint(`Signed in as ${st.label || "Cursor account"}${trustNote}`);
          await refreshProviders();
          setPoolProvider("cursor-cli");
          window.dispatchEvent(new Event("harness-config-changed"));
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
      setOauthBusy(false);
      oauthAbortRef.current = false;
    }
  };

  const handleCursorCliLogout = async () => {
    setOauthBusy(true);
    try {
      await api.logoutCursorCli();
      setOauthHint("Signed out of Cursor account.");
      await refreshCursorCliStatus();
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e: any) {
      setError(e?.message || "Cursor CLI logout failed");
    } finally {
      setOauthBusy(false);
    }
  };

  const handleAnthropicSignIn = async () => {
    oauthAbortRef.current = false;
    setOauthBusy(true);
    setOauthHint("");
    setOauthPasteCode("");
    setOauthSessionId("");
    setError("");
    try {
      const start = await api.startAuthOAuth("anthropic", poolLabelInput.trim() || undefined) as any;
      if (!start.session_id || !start.auth_url) {
        throw new Error(start.error || "oauth start failed");
      }
      setOauthSessionId(start.session_id);
      setOauthHint("Browser opened — authorize, then paste the code below (code#state).");
      try {
        window.open(start.auth_url, "_blank");
      } catch {
        setOauthHint(`Open ${start.auth_url} then paste the code below.`);
      }
      // Stay busy until paste-complete or Cancel (parity with device flows).
    } catch (e: any) {
      console.error("Anthropic OAuth start failed", e);
      setError(e?.message || e?.error || "Claude sign-in failed to start");
      setOauthHint("Sign-in failed — click Sign in (Claude Max) to try again.");
      setOauthBusy(false);
    }
  };

  const handleAnthropicComplete = async () => {
    const code = oauthPasteCode.trim();
    if (!oauthSessionId || !code) return;
    setOauthBusy(true);
    setError("");
    try {
      const res = await api.completeAuthOAuth(oauthSessionId, code, "anthropic");
      if (res.status !== "done") {
        throw new Error(res.error || "oauth complete failed");
      }
      setOauthHint(`Signed in as ${res.label || "claude-max"}`);
      setOauthPasteCode("");
      setOauthSessionId("");
      await refreshAuthPools();
      await refreshProviders();
      setPoolProvider("anthropic");
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e: any) {
      console.error("Anthropic OAuth complete failed", e);
      setError(e?.message || e?.error || "Claude sign-in failed");
    } finally {
      setOauthBusy(false);
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

  const handleSetProviderKey = async (name: string) => {
    const val = (provKeyInput[name] || "").trim();
    if (!val) return;
    setProvBusy(name);
    try {
      await api.setProviderKey(name, val);
      setProvKeyInput((p) => ({ ...p, [name]: "" }));
      await refreshProviders();
      // Picker model list may now include this provider's live catalog.
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e) {
      console.error("Failed to set provider key", e);
    } finally {
      setProvBusy("");
    }
  };

  const handleToggleProvider = async (name: string, enabled: boolean) => {
    setProvBusy(name);
    try {
      await api.setProviderEnabled(name, enabled);
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e) {
      console.error("Failed to toggle provider", e);
    } finally {
      setProvBusy("");
    }
  };

  const handleClearProviderKey = async (name: string) => {
    setProvBusy(name);
    try {
      await api.clearProviderKey(name);
      await refreshProviders();
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (e) {
      console.error("Failed to disconnect provider", e);
    } finally {
      setProvBusy("");
    }
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
      <div className="flex flex-col h-full text-[12px] p-4 text-faint">
        {error ? error : "Loading settings..."}
      </div>
    );
  }

  return (
    <div className="text-[12px] max-w-3xl">
      {/* Floating save/error toast: fixed to the bottom-right so it overlays
          instead of inserting a block at the top that shoves every setting down
          (the reflow was the annoyance). Auto-dismiss handled by the callers
          that setStatus(""). pointer-events-none so it never blocks controls. */}
      {(status || error) && (
        <div className="fixed bottom-4 right-4 z-50 pointer-events-none flex items-center gap-2
                        px-3 py-1.5 rounded-lg border shadow-lg bg-panel2/95 backdrop-blur
                        animate-in fade-in slide-in-from-bottom-2 duration-150
                        border-edge">
          {status && <span className="text-good text-[11px] font-medium">{status}</span>}
          {error && <span className="text-risk text-[11px] font-medium">{error}</span>}
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
            className="w-full bg-panel border border-edge rounded text-[11px] text-txt
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
        {gate("general", "provider model setup wizard api keys routing") && settings && (<>
        {/* Wizard Button */}
        <div className="space-y-1.5 border-b border-edge/65 pb-3">
          <button
            onClick={onOpenWizard}
            className="w-full bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded py-2 font-bold transition-colors text-[11px]"
          >
            Open Provider & Model Setup
          </button>
          <p className="text-[10px] text-muted">
            Configure API keys, probe models, select conversational pilots, and adjust routing scores.
          </p>
        </div>

        </>)}
        {gate("general", "driver model select") && settings && (<>
        {/* Driver Select */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Driver (Model)
          </label>
          <select
            value={settings.driver}
            onChange={(e) => update({ driver: e.target.value })}
            disabled={saving}
            className="w-full bg-panel2 border border-edge rounded px-2.5 py-1.5 text-txt focus:outline-none focus:border-accent disabled:opacity-50"
          >
            {settings.models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <p className="text-[10px] text-muted">
            The pilot model driver. Changes take effect live on the chat session.
          </p>
        </div>

        </>)}
        {gate("general", "budget steps per run") && settings && (<>
        {/* Budget Stepper / Number */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Budget (Steps)
          </label>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min="1"
              max="50"
              value={settings.budget}
              onChange={(e) => {
                const val = parseInt(e.target.value);
                if (!isNaN(val)) {
                  update({ budget: val });
                }
              }}
              disabled={saving}
              className="w-20 bg-panel2 border border-edge rounded px-2.5 py-1 text-txt focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <span className="text-[10px] text-muted">steps per run (1-50)</span>
          </div>
          <p className="text-[10px] text-muted">
            Maximum Orchestration steps/budget allocated per task execution.
          </p>
        </div>

        </>)}
        {gate("general", "auto-distill distillation toggle") && settings && (<>
        {/* Auto Distill Toggle */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Auto-Distill
          </label>
          <button
            onClick={() => update({ auto_distill: !settings.auto_distill })}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              settings.auto_distill
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Propose skills/rules after task</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {settings.auto_distill ? "on" : "off"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            When enabled, PM proposes pending skill/rule candidates automatically on task completion.
          </p>
        </div>

        </>)}
        {gate("general", "hash edit hash-anchored experimental") && settings && (<>
        {/* Hash-anchored edits (experimental) */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Hash-Anchored Edits
          </label>
          <button
            onClick={() => update({ hash_edit_enabled: !(settings.hash_edit_enabled ?? false) })}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              (settings.hash_edit_enabled ?? false)
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Hash-anchored edits (experimental)</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {(settings.hash_edit_enabled ?? false) ? "on" : "off"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            When on, the agent may apply edits anchored by content hashes instead of line numbers.
          </p>
        </div>

        </>)}
        {gate("general", "review edits diff review toggle") && settings && (<>
        {/* Diff Review Toggle */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Review Edits
          </label>
          <button
            onClick={() => update({ reviewEditsBeforeApply: !settings.reviewEditsBeforeApply })}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              settings.reviewEditsBeforeApply
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Review edits before applying</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {settings.reviewEditsBeforeApply ? "on" : "off"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            When on, agent edits are held for your per-hunk approval instead of auto-applying.
          </p>
        </div>

        </>)}
        {gate("general", "auto-verify edits typecheck syntax check self-correct diagnostics") && settings && (<>
        {/* Auto-Verify Edits Toggle */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Auto-Verify Edits
          </label>
          <button
            onClick={() => update({ autoVerify: !(settings.autoVerify ?? true) })}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              (settings.autoVerify ?? true)
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Check edits and self-correct</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {(settings.autoVerify ?? true) ? "on" : "off"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            After the agent edits files, run a fast project check (typecheck / syntax on the
            changed files) and let it self-correct in the same turn before handing back.
          </p>
        </div>

        </>)}
        {gate("safety", "full-auto safety command guard timeout max investigation steps") && settings && (<>
        {/* Full-Auto Safety: command guard + timeout */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Full-Auto Safety
          </label>
          <button
            onClick={() => update({ autoCommandGuard: !(settings.autoCommandGuard ?? true) })}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              (settings.autoCommandGuard ?? true)
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Guard dangerous commands in full-auto</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {(settings.autoCommandGuard ?? true) ? "on" : "off"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            In unattended (full-auto) mode, irreversible/remote/escalating shell commands
            (rm -rf, ssh, curl pipe-to-shell, force-push, sudo, disk writes) are blocked
            and reported instead of running. Interactive co-working is unaffected.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-[11px] text-muted shrink-0">Command timeout (s)</label>
            <input
              type="text"
              defaultValue={settings.commandTimeout || "120"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.commandTimeout || "120")) update({ commandTimeout: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-[11px] text-txt disabled:opacity-50"
              placeholder="120"
            />
          </div>
          <p className="text-[10px] text-muted">
            Per-command shell timeout. Use 0 or "off" for unbounded (needed for long SSH
            sessions or builds). Unbounded plus full-auto is why the guard above matters.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-[11px] text-muted shrink-0">Max investigation steps</label>
            <input
              type="text"
              defaultValue={settings.maxPilotSteps || "40"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.maxPilotSteps || "40")) update({ maxPilotSteps: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-[11px] text-txt disabled:opacity-50"
              placeholder="40"
            />
          </div>
          <p className="text-[10px] text-muted">
            Per-message ceiling on pilot investigation/tool-call steps. Use 0 or "unlimited"
            for true autopilot (loop until done, the budget governor halts, or you stop it).
            Applies on the next turn -- no restart needed.
          </p>
        </div>

        </>)}
        {gate("providers", "sign in subscription oauth chatgpt codex claude max cursor xai grok nous plan account login") && (<>
        <SettingsCollapse
          id="sign-in"
          title="Sign in"
          defaultOpen={true}
          forceOpen={!!q}
          onFirstOpen={loadSignInData}
          className="space-y-2"
          summary={(() => {
            let n = 0;
            if (poolEntriesFor("openai-codex").length) n++;
            if (poolEntriesFor("anthropic").length) n++;
            if (cursorCliStatus?.authenticated) n++;
            if (poolEntriesFor("xai-oauth").length) n++;
            if (poolEntriesFor("nous").length) n++;
            return n > 0 ? `${n} signed in` : "plan accounts";
          })()}
        >
          <p className="text-[10px] text-muted leading-normal">
            Log in with the subscription you already pay for. No API key required.
          </p>
          <div className="space-y-1.5">
            <div className="bg-panel2 border border-edge/50 rounded p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt font-medium text-[11px]">ChatGPT Codex</span>
                <span className="text-faint text-[10px] font-mono truncate">
                  {planAccountStatusLine("openai-codex")}
                </span>
              </div>
              <div className="flex items-center gap-2 flex-wrap mt-1.5">
                <button
                  type="button"
                  onClick={handleCodexSignIn}
                  disabled={oauthBusy || poolBusy === "openai-codex"}
                  className="bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
                >
                  {oauthBusy ? "Waiting for browser..." : "Sign in"}
                </button>
                {poolEntriesFor("openai-codex").length ? (
                  <button
                    type="button"
                    onClick={() => handlePlanPoolSignOut("openai-codex")}
                    disabled={oauthBusy || poolBusy === "openai-codex"}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px] disabled:opacity-30"
                  >
                    Sign out
                  </button>
                ) : null}
                {oauthBusy ? (
                  <button
                    type="button"
                    onClick={handleCancelOAuth}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                  >
                    Cancel
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => { refreshPlanPoolStatus(); }}
                  className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                >
                  Refresh status
                </button>
              </div>
            </div>

            <div className="bg-panel2 border border-edge/50 rounded p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt font-medium text-[11px]">Claude Max</span>
                <span className="text-faint text-[10px] font-mono truncate">
                  {planAccountStatusLine("anthropic")}
                </span>
              </div>
              <p className="text-[10px] text-muted mt-1 leading-normal">
                Claude Pro/Max subscription via claude.ai. Enterprise org keys use API Keys below (or Bedrock) — not this Sign in.
              </p>
              <div className="flex items-center gap-2 flex-wrap mt-1.5">
                <button
                  type="button"
                  onClick={handleAnthropicSignIn}
                  disabled={oauthBusy || poolBusy === "anthropic"}
                  className="bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
                >
                  {oauthBusy ? "Waiting for code..." : "Sign in"}
                </button>
                {poolEntriesFor("anthropic").length ? (
                  <button
                    type="button"
                    onClick={() => handlePlanPoolSignOut("anthropic")}
                    disabled={oauthBusy || poolBusy === "anthropic"}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px] disabled:opacity-30"
                  >
                    Sign out
                  </button>
                ) : null}
                {oauthBusy || oauthSessionId ? (
                  <button
                    type="button"
                    onClick={handleCancelOAuth}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                  >
                    Cancel
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => { refreshPlanPoolStatus(); }}
                  className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                >
                  Refresh status
                </button>
              </div>
              {oauthSessionId ? (
                <div className="flex items-center gap-2 mt-1.5">
                  <input
                    type="text"
                    value={oauthPasteCode}
                    onChange={(e) => setOauthPasteCode(e.target.value)}
                    placeholder="paste authorization code#state"
                    className="flex-1 bg-panel border border-edge rounded px-2 py-1 text-[11px] font-mono"
                  />
                  <button
                    type="button"
                    onClick={handleAnthropicComplete}
                    disabled={oauthBusy || !oauthPasteCode.trim()}
                    className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
                  >
                    Complete
                  </button>
                </div>
              ) : null}
            </div>

            <div className="bg-panel2 border border-edge/50 rounded p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt font-medium text-[11px]">Cursor CLI (plan)</span>
                <span className="text-faint text-[10px] font-mono truncate">
                  {cursorCliStatus?.installed === false
                    ? (cursorCliStatus.error || "agent binary not found")
                    : cursorCliStatus?.authenticated
                      ? `Signed in as ${cursorCliStatus.label || "Cursor account"}`
                      : (cursorCliStatus?.error || "Not signed in")}
                </span>
              </div>
              <p className="text-[10px] text-muted mt-1 leading-normal">
                Burns Cursor plan credits via the local Agent CLI. Requires the `agent` binary on PATH.
              </p>
              <div className="flex items-center gap-2 flex-wrap mt-1.5">
                <button
                  type="button"
                  onClick={handleCursorCliSignIn}
                  disabled={oauthBusy}
                  className="bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
                >
                  {oauthBusy ? "Waiting for login..." : "Sign in"}
                </button>
                {cursorCliStatus?.authenticated ? (
                  <button
                    type="button"
                    onClick={handleCursorCliLogout}
                    disabled={oauthBusy}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px] disabled:opacity-30"
                  >
                    Sign out
                  </button>
                ) : null}
                {oauthBusy ? (
                  <button
                    type="button"
                    onClick={handleCancelOAuth}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                  >
                    Cancel
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => { refreshCursorCliStatus(); }}
                  className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                >
                  Refresh status
                </button>
              </div>
            </div>

            <div className="bg-panel2 border border-edge/50 rounded p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt font-medium text-[11px]">xAI SuperGrok</span>
                <span className="text-faint text-[10px] font-mono truncate">
                  {planAccountStatusLine("xai-oauth")}
                </span>
              </div>
              <div className="flex items-center gap-2 flex-wrap mt-1.5">
                <button
                  type="button"
                  onClick={handleXaiSignIn}
                  disabled={oauthBusy || poolBusy === "xai-oauth"}
                  className="bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
                >
                  {oauthBusy ? "Waiting for browser..." : "Sign in"}
                </button>
                {poolEntriesFor("xai-oauth").length ? (
                  <button
                    type="button"
                    onClick={() => handlePlanPoolSignOut("xai-oauth")}
                    disabled={oauthBusy || poolBusy === "xai-oauth"}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px] disabled:opacity-30"
                  >
                    Sign out
                  </button>
                ) : null}
                {oauthBusy ? (
                  <button
                    type="button"
                    onClick={handleCancelOAuth}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                  >
                    Cancel
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => { refreshPlanPoolStatus(); }}
                  className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                >
                  Refresh status
                </button>
              </div>
            </div>

            <div className="bg-panel2 border border-edge/50 rounded p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt font-medium text-[11px]">Nous</span>
                <span className="text-faint text-[10px] font-mono truncate">
                  {planAccountStatusLine("nous")}
                </span>
              </div>
              <div className="flex items-center gap-2 flex-wrap mt-1.5">
                <button
                  type="button"
                  onClick={handleNousSignIn}
                  disabled={oauthBusy || poolBusy === "nous"}
                  className="bg-good/10 hover:bg-good/20 text-good border border-good/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
                >
                  {oauthBusy ? "Waiting for browser..." : "Sign in"}
                </button>
                {poolEntriesFor("nous").length ? (
                  <button
                    type="button"
                    onClick={() => handlePlanPoolSignOut("nous")}
                    disabled={oauthBusy || poolBusy === "nous"}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px] disabled:opacity-30"
                  >
                    Sign out
                  </button>
                ) : null}
                {oauthBusy ? (
                  <button
                    type="button"
                    onClick={handleCancelOAuth}
                    className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                  >
                    Cancel
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() => { refreshPlanPoolStatus(); }}
                  className="text-muted hover:text-txt border border-edge rounded px-2 py-0.5 text-[10px]"
                >
                  Refresh status
                </button>
              </div>
            </div>
          </div>
          {oauthHint ? (
            <p className="text-[10px] text-accent font-mono leading-normal">{oauthHint}</p>
          ) : null}
        </SettingsCollapse>
        </>)}
        {gate("providers", "providers api keys connect disconnect per-provider key management") && (<>
        {/* Per-provider key management: connect/disconnect each provider independently */}
        <SettingsCollapse
          id="api-keys"
          title="API keys"
          defaultOpen={false}
          forceOpen={!!q}
          onFirstOpen={loadProvidersList}
          className="space-y-2"
          summary={(() => {
            if (!providersLoaded) return "…";
            const list = providers.filter((p) => p.name !== "bedrock");
            const n = list.filter((p) => p.has_key && !p.disconnected).length;
            return `${n}/${list.length} connected`;
          })()}
        >
          <div className="text-[10px] text-muted">
            Connect or disconnect each provider independently. Keys imported from your environment get an on/off toggle -- flip one off to stop using it without losing the key, for easy swapping (e.g. work vs. personal).
          </div>
          <div className="space-y-1.5">
            {providers.filter((p) => p.name !== "bedrock").map((p) => {
              // A provider can carry a key from the environment (e.g. a
              // shell-exported OPENROUTER_API_KEY) rather than one stored in the
              // app. Env-backed providers get an on/off toggle instead of a
              // destructive Disconnect: flipping it off scrubs the key from the
              // running process (so no worker/router uses it) but preserves it
              // for a one-click re-enable -- painless swapping between, say, a
              // work key and a personal one.
              const envBacked = !!p.has_env;
              const enabled = !p.disconnected;
              const connected = p.has_key;
              const busy = provBusy === p.name;
              return (
              <div key={p.name} className="bg-panel2 border border-edge/50 rounded p-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${connected ? "bg-good" : "bg-faint"}`} />
                    <span className="text-txt font-medium text-[11px]">{p.display_name || p.name}</span>
                    <span
                      title={envBacked ? `Key imported from your environment (${p.env_var || "env var"})` : undefined}
                      className="text-faint text-[10px] font-mono truncate"
                    >
                      {envBacked
                        ? `${enabled ? "connected" : "disabled"} - via env`
                        : p.has_key
                          ? "connected - via key"
                          : "not connected"}
                    </span>
                  </div>
                  {envBacked ? (
                    <button
                      role="switch"
                      aria-checked={enabled}
                      title={enabled ? "Enabled -- click to turn off (key is kept for easy re-enable)" : "Disabled -- click to turn on"}
                      onClick={() => handleToggleProvider(p.name, !enabled)}
                      disabled={busy}
                      className={`relative shrink-0 w-9 h-5 rounded-full border transition-colors disabled:opacity-40 ${
                        enabled ? "bg-good/30 border-good/50" : "bg-panel border-edge"
                      }`}
                    >
                      <span
                        className={`absolute top-[1px] w-[15px] h-[15px] rounded-full transition-all ${
                          enabled ? "left-[18px] bg-good" : "left-[2px] bg-faint"
                        }`}
                      />
                    </button>
                  ) : p.has_key ? (
                    <button
                      onClick={() => handleClearProviderKey(p.name)}
                      disabled={busy}
                      className="bg-risk/10 hover:bg-risk/20 text-risk border border-risk/30 hover:border-risk/50 rounded px-2 py-0.5 font-medium text-[10px] disabled:opacity-30 transition-colors shrink-0"
                    >
                      Disconnect
                    </button>
                  ) : null}
                </div>
                {!connected && !envBacked && (
                  <div className="flex gap-2 mt-1.5">
                    <input
                      type="password"
                      placeholder={`${p.env_var || "API key"}...`}
                      value={provKeyInput[p.name] || ""}
                      onChange={(e) => setProvKeyInput((prev) => ({ ...prev, [p.name]: e.target.value }))}
                      disabled={busy}
                      className="flex-1 bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
                    />
                    <button
                      onClick={() => handleSetProviderKey(p.name)}
                      disabled={busy || !(provKeyInput[p.name] || "").trim()}
                      className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30 transition-colors shrink-0"
                    >
                      Connect
                    </button>
                  </div>
                )}
              </div>
              );
            })}
          </div>
        </SettingsCollapse>

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
          <p className="text-[10px] text-muted leading-normal">
            Add multiple API keys for the same provider. On plan-limit / 429 / 402 the pilot
            rotates to the next healthy entry (prompt cache may reset on rotate).
            Plan accounts (ChatGPT Codex, Claude Max, Cursor CLI, xAI, Nous) come from
            Sign in above — pools are for multi-key rotate only. When every entry is
            exhausted, the turn fails until a cooldown expires or you add another key.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {POOL_FOCUS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPoolProvider(p)}
                className={`px-2 py-0.5 rounded text-[10px] font-mono border transition-colors ${
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
                <p className="text-[10px] font-mono text-faint">
                  {poolProvider === "cursor-cli"
                    ? (cursorCliStatus?.installed === false
                      ? (cursorCliStatus.error || "agent binary not found")
                      : cursorCliStatus?.authenticated
                        ? `Signed in as ${cursorCliStatus.label || "Cursor account"}`
                        : (cursorCliStatus?.error || "Not signed in"))
                    : planAccountStatusLine(poolProvider)}
                </p>
                <p className="text-[10px] text-muted">
                  <span className="text-accent">Sign in above</span> to connect your plan account.
                </p>
                <select
                  value={
                    (authPools?.pools || []).find((x) => x.provider === poolProvider)?.strategy
                    || "fill_first"
                  }
                  onChange={(e) => handlePoolStrategy(poolProvider, e.target.value)}
                  className="bg-panel border border-edge rounded px-1.5 py-0.5 text-[10px] text-muted"
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
                      <p className="text-[10px] text-faint italic">No pooled credentials for {poolProvider} yet.</p>
                    );
                  }
                  return (
                    <ul className="space-y-1 pt-1 border-t border-edge/40">
                      {entries.map((e) => (
                        <li key={e.id} className="flex items-center justify-between gap-2 text-[10px]">
                          <div className="min-w-0">
                            <span className="font-medium text-txt">{e.label || e.id}</span>
                            <span className="text-faint font-mono ml-1.5">{e.masked}</span>
                            <span className={`ml-1.5 uppercase tracking-wider text-[8px] ${
                              e.last_status === "exhausted" ? "text-risk" : "text-good"
                            }`}>
                              {e.last_status || "ok"}
                            </span>
                          </div>
                          <button
                            type="button"
                            onClick={() => handleRemovePoolEntry(poolProvider, e.id)}
                            className="text-risk/80 hover:text-risk text-[10px] shrink-0"
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
              className="w-full bg-panel border border-edge rounded px-2 py-1 text-[11px] font-mono"
            />
            <input
              type="text"
              value={poolLabelInput}
              onChange={(e) => setPoolLabelInput(e.target.value)}
              placeholder="label (optional, e.g. cursor-plan-a)"
              className="w-full bg-panel border border-edge rounded px-2 py-1 text-[11px]"
            />
            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                onClick={handleAddPoolKey}
                disabled={!!poolBusy || !poolKeyInput.trim()}
                className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30"
              >
                {poolBusy === poolProvider ? "Adding..." : "Add to pool"}
              </button>
              <select
                value={
                  (authPools?.pools || []).find((x) => x.provider === poolProvider)?.strategy
                  || "fill_first"
                }
                onChange={(e) => handlePoolStrategy(poolProvider, e.target.value)}
                className="bg-panel border border-edge rounded px-1.5 py-0.5 text-[10px] text-muted"
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
                  <p className="text-[10px] text-faint italic">No pooled credentials for {poolProvider} yet.</p>
                );
              }
              return (
                <ul className="space-y-1 pt-1 border-t border-edge/40">
                  {entries.map((e) => (
                    <li key={e.id} className="flex items-center justify-between gap-2 text-[10px]">
                      <div className="min-w-0">
                        <span className="font-medium text-txt">{e.label || e.id}</span>
                        <span className="text-faint font-mono ml-1.5">{e.masked}</span>
                        <span className={`ml-1.5 uppercase tracking-wider text-[8px] ${
                          e.last_status === "exhausted" ? "text-risk" : "text-good"
                        }`}>
                          {e.last_status || "ok"}
                        </span>
                      </div>
                      <button
                        type="button"
                        onClick={() => handleRemovePoolEntry(poolProvider, e.id)}
                        className="text-risk/80 hover:text-risk text-[10px] shrink-0"
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
          <p className="text-[10px] text-muted leading-normal">
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
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <div className="text-[9px] text-faint uppercase tracking-wider pt-1">or access keys</div>
            <input
              type="password"
              placeholder={bedrock?.has_access_key ? "Access key id (leave blank to keep)" : "AWS_ACCESS_KEY_ID"}
              value={bedrockAccessKey}
              onChange={(e) => setBedrockAccessKey(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <input
              type="password"
              placeholder={bedrock?.has_access_key ? "Secret access key (leave blank to keep)" : "AWS_SECRET_ACCESS_KEY"}
              value={bedrockSecretKey}
              onChange={(e) => setBedrockSecretKey(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <input
              type="password"
              placeholder={bedrock?.has_session_token ? "Session token (leave blank to keep)" : "AWS_SESSION_TOKEN (optional)"}
              value={bedrockSessionToken}
              onChange={(e) => setBedrockSessionToken(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <div className="grid grid-cols-2 gap-1.5 pt-1">
              <input
                type="text"
                placeholder="AWS_REGION (e.g. us-east-1)"
                value={bedrockRegion}
                onChange={(e) => setBedrockRegion(e.target.value)}
                disabled={bedrockBusy}
                className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
              />
              <input
                type="text"
                placeholder="BEDROCK_REGION (optional)"
                value={bedrockRegionAlt}
                onChange={(e) => setBedrockRegionAlt(e.target.value)}
                disabled={bedrockBusy}
                className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
              />
            </div>
            <input
              type="text"
              placeholder="Default inference profile id (optional)"
              value={bedrockModelId}
              onChange={(e) => setBedrockModelId(e.target.value)}
              disabled={bedrockBusy}
              className="w-full bg-panel border border-edge rounded px-2 py-0.5 text-txt text-[11px] focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <div className="flex gap-2 pt-1">
              <button
                onClick={handleSaveBedrock}
                disabled={bedrockBusy}
                className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30 transition-colors"
              >
                {bedrockBusy ? "Saving..." : "Save Bedrock"}
              </button>
              {bedrock?.configured ? (
                <button
                  onClick={handleClearBedrock}
                  disabled={bedrockBusy}
                  className="bg-risk/10 hover:bg-risk/20 text-risk border border-risk/30 hover:border-risk/50 rounded px-2.5 py-0.5 font-medium text-[10px] disabled:opacity-30 transition-colors"
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
          <p className="text-[10px] text-muted leading-normal">
            By default, implement/parallel workers run on the built-in provider worker (your configured API key, in an isolated worktree) -- no external CLI needed. These adapters let you instead delegate worker runs to an external coding-agent CLI (Cursor, Claude Code, Codex) when it is installed. Optional.
          </p>

          {platformError ? (
            <p className="text-[10px] text-muted italic">{platformError}</p>
          ) : platformAdapters.length === 0 ? (
            <p className="text-[10px] text-muted italic">Loading platform settings...</p>
          ) : (
            <div className="space-y-2">
              <div className="space-y-2 bg-panel rounded border border-edge/40 p-2">
                {platformAdapters.map((adapter) => (
                  <div key={adapter.name} className="flex items-center justify-between gap-2 border-b border-edge/30 last:border-b-0 pb-1.5 last:pb-0 pt-1.5 first:pt-0">
                    <div className="space-y-0.5">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono font-medium text-[11px] text-txt">{adapter.name}</span>
                        <span className={`px-1 py-0.5 text-[8px] uppercase font-bold tracking-wider rounded ${
                          adapter.implement_capable 
                            ? "bg-accent/10 text-accent/90 border border-accent/25" 
                            : "bg-panel2 text-muted border border-edge"
                        }`}>
                          {adapter.implement_capable ? "implement" : "analysis"}
                        </span>
                        {!adapter.available && (
                          <span className="px-1 py-0.5 text-[8px] uppercase font-bold tracking-wider rounded bg-risk/10 text-risk border border-risk/20">
                            not available
                          </span>
                        )}
                      </div>
                      <p className="text-[10px] text-muted">
                        {adapter.note}
                      </p>
                    </div>
                    <button
                      onClick={() => handleTogglePlatform(adapter.name, !adapter.enabled)}
                      className={`px-2.5 py-1 rounded text-[10px] uppercase font-bold tracking-wider border transition-colors ${
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

              <p className="text-[10px] text-muted leading-normal">
                With no external adapter enabled, implement/parallel workers run on the built-in provider worker (default). Enable an adapter above only to delegate to that external CLI instead.
              </p>
            </div>
          )}
        </SettingsCollapse>

        </>)}
        {gate("notifications", "observability queue notifications sound desktop messages") && (<>
        {/* Observability & Queue Prefs */}
        <div className="space-y-3 border-t border-edge pt-3">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Observability & Queue
          </label>
          
          <div className="space-y-2">
            {/* Desktop Notifications Toggle */}
            <button
              onClick={toggleNotify}
              className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
                notify
                  ? "bg-accent/10 border-accent/30 text-accent"
                  : "bg-panel2 border-edge text-muted"
              }`}
            >
              <span className="font-medium text-[11px]">Desktop notifications</span>
              <span className="text-[10px] uppercase font-bold tracking-wider">
                {notify ? "on" : "off"}
              </span>
            </button>
            
            {/* Completion Sound Toggle */}
            <button
              onClick={toggleSound}
              className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
                sound
                  ? "bg-accent/10 border-accent/30 text-accent"
                  : "bg-panel2 border-edge text-muted"
              }`}
            >
              <span className="font-medium text-[11px]">Completion sound</span>
              <span className="text-[10px] uppercase font-bold tracking-wider">
                {sound ? "on" : "off"}
              </span>
            </button>

            {/* Queue Messages Toggle */}
            <button
              onClick={toggleQueue}
              className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
                queueMessages
                  ? "bg-accent/10 border-accent/30 text-accent"
                  : "bg-panel2 border-edge text-muted"
              }`}
            >
              <span className="font-medium text-[11px]">Queue concurrent messages</span>
              <span className="text-[10px] uppercase font-bold tracking-wider">
                {queueMessages ? "on" : "off"}
              </span>
            </button>
          </div>
        </div>

        </>)}
        {gate("advanced", "live ui vite hmr hot reload self dev restart") && _selfDevIpc && (<>
        {/* Live UI Section (Vite HMR). The backend always runs from source. */}
        <div className="border-t border-edge pt-3 space-y-2">
          <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Live UI (Vite HMR)
          </span>
          <p className="text-[10px] text-muted">
            Marionette always runs its backend from the source checkout, so backend
            edits (harness/**) go live on the next restart and the conversation resumes
            across it. Turn this on to also serve the React UI from a Vite dev server,
            so edits to webapp/src hot-reload instantly instead of needing a rebuild.
          </p>
          <button
            onClick={toggleSelfDev}
            disabled={selfDevBusy || restarting || !(selfDev && selfDev.viable)}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              selfDev && selfDev.enabled
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Serve UI from Vite dev server (HMR)</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {selfDev && selfDev.enabled ? "on" : "off"}
            </span>
          </button>
          {selfDev && !selfDev.viable && (
            <p className="text-[10px] text-warn">
              Vite dev server not available (needs webapp/node_modules + webapp/src).
              The UI is served from the prebuilt dist/ until node deps are installed.
            </p>
          )}
          <button
            onClick={restartBackend}
            disabled={restarting || selfDevBusy}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded border border-edge bg-panel2 text-[11px] text-muted hover:text-txt hover:border-accent/30 transition disabled:opacity-50"
          >
            {restarting ? "Restarting backend..." : "Restart backend (apply self-edits)"}
          </button>
        </div>
        </>)}
        {gate("advanced", "lifecycle hooks events command") && (<>
        {/* Lifecycle Hooks Section */}
        <div className="border-t border-edge pt-3 space-y-2">
          <button
            onClick={() => setHooksOpen(!hooksOpen)}
            className="w-full flex items-center justify-between text-left focus:outline-none"
          >
            <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-good inline-block"></span> Lifecycle Hooks
            </span>
            <span className="text-muted">
              {hooksOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </span>
          </button>

          {hooksOpen && (
            <div className="space-y-3 bg-panel2/40 border border-edge/50 rounded p-2.5 mt-1">
              {hookError && <div className="text-risk text-[10px] font-medium">{hookError}</div>}
              {hookStatus && <div className="text-good text-[10px] font-medium">{hookStatus}</div>}

              {/* Hooks List */}
              <div className="space-y-2 max-h-40 overflow-y-auto pr-1">
                {hooks.length === 0 ? (
                  <div className="text-muted text-[10px]">No configured lifecycle hooks.</div>
                ) : (
                  hooks.map((hk) => (
                    <div key={hk.id} className="flex flex-col p-1.5 bg-panel2/65 border border-edge/30 rounded text-[11px]">
                      <div className="flex items-center justify-between">
                        <span className="bg-edge text-muted text-[9px] px-1.5 py-0.5 rounded font-mono font-semibold uppercase tracking-wider">
                          {hk.event}
                        </span>
                        
                        <div className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            checked={hk.enabled}
                            onChange={async () => {
                              try {
                                setHookError("");
                                const updated = await api.updateHook(hk.id, { enabled: !hk.enabled });
                                setHooks(hooks.map(h => h.id === hk.id ? updated : h));
                              } catch (err: any) {
                                setHookError(err?.error || "Failed to update hook");
                              }
                            }}
                            className="rounded border-edge text-accent focus:ring-accent bg-panel2"
                            title="Enable / Disable hook"
                          />
                          
                          <button
                            onClick={async () => {
                              try {
                                setHookError("");
                                const res = await api.removeHook(hk.id);
                                if (res.ok) {
                                  setHooks(hooks.filter(h => h.id !== hk.id));
                                } else {
                                  setHookError((res as any).error || "Failed to remove hook");
                                }
                              } catch (err: any) {
                                setHookError(err?.error || "Failed to remove hook");
                              }
                            }}
                            className="text-muted hover:text-risk transition-colors p-0.5"
                            title="Remove hook"
                          >
                            <Trash2 size={11} />
                          </button>
                        </div>
                      </div>
                      <div className="text-txt font-mono text-[10px] bg-panel/70 p-1.5 rounded border border-edge/20 mt-1 select-all break-all" title={hk.command}>
                        {hk.command.length > 50 ? hk.command.slice(0, 50) + "..." : hk.command}
                      </div>
                    </div>
                  ))
                )}
              </div>

              {/* Add Hook Form */}
              <div className="border-t border-edge/30 pt-2.5 mt-2 space-y-1.5">
                <div className="text-[10px] uppercase tracking-wider text-faint font-semibold">
                  Add Lifecycle Hook
                </div>
                <div className="space-y-1.5">
                  <select
                    value={newHookEvent}
                    onChange={(e) => setNewHookEvent(e.target.value)}
                    className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt text-[11px] focus:outline-none focus:border-accent"
                  >
                    {allowedEvents.map((evt) => (
                      <option key={evt} value={evt}>
                        {evt}
                      </option>
                    ))}
                  </select>
                  
                  <input
                    type="text"
                    placeholder="Shell command (e.g., echo 'start')"
                    value={newHookCommand}
                    onChange={(e) => setNewHookCommand(e.target.value)}
                    className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-[11px] focus:outline-none focus:border-accent font-mono"
                  />
                  
                  <button
                    onClick={async () => {
                      if (!newHookCommand.trim()) {
                        setHookError("Command is required");
                        return;
                      }
                      try {
                        setHookError("");
                        setHookStatus("Adding hook...");
                        const added = await api.addHook(newHookEvent, newHookCommand.trim());
                        setHooks([...hooks, added]);
                        setHookStatus("Hook added");
                        setNewHookCommand("");
                        setTimeout(() => setHookStatus(""), 2500);
                      } catch (err: any) {
                        setHookError(err?.error || "Failed to add hook");
                        setHookStatus("");
                      }
                    }}
                    className="w-full bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded py-1 font-semibold text-[11px] transition-colors flex items-center justify-center gap-1"
                  >
                    <Plus size={11} /> Add Hook
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>

        </>)}
        {gate("advanced", "agent memory durable facts preferences") && (<>
        {/* Agent Memory Section */}
        <div className="border-t border-edge pt-3 space-y-2">
          <button
            onClick={() => setMemoryOpen(!memoryOpen)}
            className="w-full flex items-center justify-between text-left focus:outline-none"
          >
            <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Agent Memory
            </span>
            <span className="text-muted">
              {memoryOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </span>
          </button>

          {memoryOpen && (
            <div className="space-y-3 bg-panel2/40 border border-edge/50 rounded p-2.5 mt-1">
              <MemoryPane embedded />
            </div>
          )}
        </div>

        </>)}
        {gate("advanced", "skills rules learned") && (<>
        {/* Skills & Rules Section */}
        <div className="border-t border-edge pt-3 space-y-2">
          <button
            onClick={() => setSkillsOpen(!skillsOpen)}
            className="w-full flex items-center justify-between text-left focus:outline-none"
          >
            <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Skills & Rules
            </span>
            <span className="text-muted">
              {skillsOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </span>
          </button>

          {skillsOpen && (
            <div className="space-y-3 bg-panel2/40 border border-edge/50 rounded p-2.5 mt-1">
              <SkillsPane embedded />
            </div>
          )}
        </div>

        </>)}
        {gate("general", "usage cost token dashboard spend statistics") && (<>
        {/* Usage / Cost Dashboard Section */}
        <div className="border-t border-edge pt-3 space-y-2.5">
          <div className="flex items-center justify-between">
            <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
              Token & Cost Usage
            </label>
            <button
              onClick={() => {
                api.getUsage()
                  .then(setUsage)
                  .catch((err) => console.error("Failed to refresh usage", err));
              }}
              className="text-[9px] uppercase font-bold tracking-wider text-accent hover:underline bg-transparent border-0 p-0"
            >
              Refresh
            </button>
          </div>

          {usage ? (
            <div className="space-y-2.5 bg-panel2 border border-edge/50 rounded p-2.5">
              <div className="space-y-1">
                <div className="flex items-center justify-between text-[11px]">
                  <span className="text-faint">Session Tokens:</span>
                  <span className="text-txt font-mono font-medium">{usage.session.tokens_used.toLocaleString()}</span>
                </div>
                <div className="flex items-center justify-between text-[11px]">
                  <span className="text-faint">Session Cost (estimated):</span>
                  <span className="text-good font-mono font-medium">${usage.session.est_cost_usd.toFixed(6)}</span>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-1 text-[11px] border-t border-edge/30 pt-1 mt-1">
                  <span className="text-faint">Active Driver:</span>
                  <span className="text-txt font-mono font-medium truncate max-w-full" title={usage.session.driver}>{usage.session.driver}</span>
                </div>
                <div className="flex items-center justify-between text-[11px]">
                  <span className="text-faint">Price in/out (per Mtok):</span>
                  <span className="text-muted font-mono font-medium">${usage.session.price_in}/${usage.session.price_out}</span>
                </div>
              </div>

              {usage.jobs && usage.jobs.length > 0 && (
                <div className="space-y-1 border-t border-edge/40 pt-1.5 mt-1.5">
                  <div className="text-[9px] uppercase tracking-wider text-faint font-semibold mb-1">
                    PM Job Costs (estimated)
                  </div>
                  <div className="max-h-24 overflow-y-auto space-y-1 pr-1">
                    {usage.jobs.map((job: any) => (
                      <div key={job.job_id} className="flex items-center justify-between gap-x-1.5 text-[10px] font-mono">
                        <span className="text-muted truncate flex-1 min-w-0" title={job.job_id}>{job.job_id}</span>
                        <span className="text-faint text-[9px] flex-shrink-0">{job.tokens.toLocaleString()} tok</span>
                        <span className="text-txt font-medium flex-shrink-0">${job.est_cost_usd.toFixed(6)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <p className="text-[10px] text-muted">Loading usage statistics...</p>
          )}
          <p className="text-[9px] text-muted font-mono">
            All costs are estimated locally based on catalog rates. No live billing APIs are called.
          </p>
        </div>

        </>)}
        {gate("general", "system info version read-only") && settings && (<>
        {/* Read-Only Info */}
        <div className="border-t border-edge pt-3 space-y-2.5">
          <div className="uppercase tracking-wider text-[10px] text-faint font-semibold">
            System Info
          </div>

          <div className="grid grid-cols-3 gap-1">
            <span className="text-faint">Reach:</span>
            <span className="col-span-2 text-muted font-mono select-all break-all bg-panel2 px-1 py-0.5 rounded border border-edge/30 inline-block w-fit">
              {settings.reach}
            </span>
          </div>

          {settings.wiki_auto !== undefined && (
            <div className="grid grid-cols-3 gap-1">
              <span className="text-faint">Wiki Auto:</span>
              <span className="col-span-2 text-muted font-mono inline-block w-fit">
                {settings.wiki_auto ? "yes" : "no"}
              </span>
            </div>
          )}

          <div className="space-y-0.5">
            <div className="text-faint">State Directory:</div>
            <div className="text-muted font-mono select-all break-all bg-panel2 p-1.5 rounded border border-edge/30 text-[11px]">
              {settings.state_dir || "Temporary (per-session)"}
            </div>
          </div>

          <div className="space-y-0.5">
            <div className="text-faint">Repository:</div>
            <div className="text-muted font-mono select-all break-all bg-panel2 p-1.5 rounded border border-edge/30 text-[11px]">
              {settings.repo || "None"}
            </div>
          </div>
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
            <div className="text-risk text-[10px] font-semibold bg-risk/10 border border-risk/30 rounded p-2">
              {gitError}
            </div>
          )}

          {gitStatus?.connected ? (
            <div className="space-y-2 bg-panel rounded border border-edge/40 p-2.5">
              <div className="text-[11px] leading-relaxed text-muted">
                Connected to GitHub. Wiki repository is provisioned and active.
              </div>
              <div className="flex items-center justify-between gap-2 border-t border-edge/30 pt-2 mt-1">
                <div className="space-y-0.5">
                  <div className="text-[10px] text-faint uppercase font-bold tracking-wider">Wiki Repository</div>
                  {gitStatus.html_url ? (
                    <a
                      href={gitStatus.html_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-[11px] text-accent hover:underline break-all"
                    >
                      {gitStatus.wiki_repo}
                    </a>
                  ) : (
                    <span className="font-mono text-[11px] text-txt">{gitStatus.wiki_repo}</span>
                  )}
                </div>
                <button
                  disabled={gitConnecting}
                  onClick={handleDisconnectGit}
                  className="bg-risk/10 border border-risk/20 hover:bg-risk/20 text-risk text-[10px] uppercase font-bold tracking-wider px-2.5 py-1 rounded transition disabled:opacity-50"
                >
                  Disconnect
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-2.5">
              <div className="text-[10px] text-muted leading-relaxed">
                Connect your GitHub account to automatically provision a private "my-portable-llm-wiki" repository as your durable cross-LLM memory.
              </div>

              {gitConnecting && (
                <div className="text-[10px] text-muted italic flex items-center gap-1.5">
                  <span className="animate-pulse">Provisioning repository...</span>
                </div>
              )}

              {!gitConnecting && !deviceFlow && (
                <div className="flex flex-col gap-2">
                  {gitStatus?.gh_available ? (
                    <button
                      onClick={handleConnectGH}
                      className="w-full bg-accent hover:bg-accent/90 text-accent-txt text-[11px] font-bold px-3 py-1.5 rounded transition shadow-sm text-center"
                    >
                      Connect with GitHub CLI ({gitStatus.gh_user})
                    </button>
                  ) : (
                    <div className="text-[10px] text-muted italic bg-panel rounded border border-edge/30 p-2 leading-normal">
                      GitHub CLI (gh) not detected or not authenticated. Install or authenticate to enable one-click connection.
                    </div>
                  )}

                  <button
                    onClick={handleStartDeviceFlow}
                    className="w-full bg-panel hover:bg-panel2 border border-edge text-txt text-[11px] font-semibold px-3 py-1.5 rounded transition text-center"
                  >
                    Connect via Device Code instead
                  </button>
                </div>
              )}

              {deviceFlow && (
                <div className="bg-panel rounded border border-edge/40 p-2.5 space-y-2">
                  <div className="text-[11px] font-medium text-txt">
                    Verification Code:
                  </div>
                  <div className="font-mono text-center text-lg tracking-widest font-bold bg-bg border border-edge/60 rounded py-1.5 text-accent select-all">
                    {deviceFlow.user_code}
                  </div>
                  <div className="text-[10px] text-muted leading-normal">
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
                    <div className="text-[10px] text-accent/90 italic flex items-center gap-1.5">
                      <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent animate-ping" />
                      Waiting for authorization...
                    </div>
                  )}
                  <button
                    onClick={() => {
                      setDeviceFlow(null);
                      setGitPolling(false);
                    }}
                    className="w-full text-muted hover:text-txt text-[10px] font-semibold uppercase tracking-wider text-center pt-1"
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          )}
        </SettingsCollapse>

        </>)}
        {gate("advanced", "wiki graph portable-llm-wiki api base token") && (<>
        {/* WIKI GRAPH (portable-llm-wiki gated owner surface) */}
        <div className="border-t border-edge pt-3 space-y-2">
          <div className="uppercase tracking-wider text-[10px] text-faint font-semibold">
            Wiki Graph
          </div>
          <div className="text-[10px] text-muted leading-relaxed">
            Prefer <span className="text-accent">State → Wiki → Connect portablellm.wiki</span>
            {" "}(pop-out signup auto-links). Manual paste still works: personal LLM URL
            or https://api.portablellm.wiki/t/your-tenant.
            {wikiCfg ? <span className={wikiCfg.has_token ? " text-good" : " text-faint"}> {wikiCfg.has_token ? "Token set." : "No token."}</span> : null}
          </div>
          <input
            type="text"
            value={wikiBase}
            onChange={(e) => setWikiBase(e.target.value)}
            placeholder="Personal LLM URL (or leave blank and use Connect button)"
            className="w-full bg-bg border border-edge rounded px-2 py-1 text-[11px] font-mono text-txt focus:outline-none focus:border-accent"
          />
          <input
            type="password"
            value={wikiToken}
            onChange={(e) => setWikiToken(e.target.value)}
            placeholder={wikiCfg?.has_token ? "Owner token (leave blank to keep)" : "Owner token (optional if URL includes ?t=)"}
            className="w-full bg-bg border border-edge rounded px-2 py-1 text-[11px] font-mono text-txt focus:outline-none focus:border-accent"
          />
          <button
            disabled={wikiSaving}
            onClick={async () => {
              setWikiSaving(true);
              try {
                const res = await api.setWikiConfig(wikiBase, wikiToken || undefined);
                setWikiCfg(res); setWikiToken("");
                window.dispatchEvent(new Event("harness-config-changed"));
              } catch { /* ignore */ }
              finally { setWikiSaving(false); }
            }}
            className="bg-accent/15 hover:bg-accent/25 text-accent text-[11px] font-semibold px-2 py-1 rounded transition disabled:opacity-50"
          >
            {wikiSaving ? "Saving..." : "Save Wiki Config"}
          </button>
          {(wikiCfg?.api_base || wikiCfg?.has_token) ? (
            <button
              disabled={wikiSaving}
              onClick={async () => {
                setWikiSaving(true);
                try {
                  const res = await api.disconnectWiki();
                  setWikiCfg(res);
                  setWikiBase("");
                  setWikiToken("");
                  window.dispatchEvent(new Event("harness-config-changed"));
                } catch { /* ignore */ }
                finally { setWikiSaving(false); }
              }}
              className="ml-2 bg-edge hover:bg-risk/20 text-muted hover:text-risk text-[11px] font-semibold px-2 py-1 rounded transition disabled:opacity-50 border border-edge2"
            >
              Disconnect Wiki
            </button>
          ) : null}
        </div>

        {/* portable-llm-wiki explainer / learn-more link */}
        <div className="border-t border-edge pt-3 mt-1 text-center">
          <a
            href="https://portablellm.wiki"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[10px] text-faint hover:text-accent transition-colors"
          >
            New here? Learn what portable-llm-wiki is at portablellm.wiki
            <ExternalLink size={10} />
          </a>
        </div>
        </>)}
        {q && !anyShown && (
          <p className="text-[11px] text-muted">No settings match "{filter.trim()}".</p>
        )}
      </div>
    </div>
  );
}
