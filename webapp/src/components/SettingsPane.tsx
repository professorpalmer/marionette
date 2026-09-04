import { useEffect, useRef, useState } from "react";
import { ChevronRight, ChevronDown, Plus, Trash2, ExternalLink, Search, X } from "lucide-react";
import {
  api,
  type Settings,
  type Config,
  type UsageData,
  type PlatformAdapter,
  type GitStatus,
  type ProviderInfo,
  type BedrockStatus,
  type AuthPoolsResponse,
} from "../lib/api";
import SkillsPane from "./SkillsPane";
import MemoryPane from "./MemoryPane";
import SchedulesPane from "./SchedulesPane";
import { takePendingExpandMemory } from "../lib/memoryDeepLink";
import {
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { SettingsCollapse } from "./SettingsCollapse";
import WindowGlassSettings from "./WindowGlassSettings";
import ProviderConfigModal from "./ProviderConfigModal";
import SettingsOptIns from "./SettingsOptIns";
import type { ProviderConfigValues } from "../lib/providerConfig";
import { REASONING_LEVELS } from "../lib/reasoningSupport";

export type SettingsSection = "general" | "safety" | "providers" | "notifications" | "plugins" | "advanced";

export {
  toSafeSettingsSnapshot,
  clearSettingsSnapshot,
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";

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
  const [settings, setSettings] = useState<Settings | null>(() => readSettingsSnapshot());
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const errorNotice = usePanelNotice(error || null);
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

  const [keyBootstrapIssues, setKeyBootstrapIssues] = useState<NonNullable<Config["key_bootstrap_issues"]>>([]);
  const [provKeyInput, setProvKeyInput] = useState<Record<string, string>>({});
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
  const [schedulesOpen, setSchedulesOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  // Cmd-K / /memory may fire harness-expand-memory before this mounts — consume latch.
  const [memoryOpen, setMemoryOpen] = useState(() => takePendingExpandMemory());
  const [archiveStatus, setArchiveStatus] = useState<{
    chats: number;
    backup_dir: string;
    vault_present: boolean;
  } | null>(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveNotice, setArchiveNotice] = useState("");

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
      // Vite on/off is read at process start, so relaunch the app to apply it.
      if (res && _restartIpc) {
        setRestarting(true);
        try { await _restartIpc(); } finally { setRestarting(false); }
      }
    } finally {
      setSelfDevBusy(false);
    }
  };

  const relaunchMarionette = async () => {
    if (!_restartIpc) return;
    setRestarting(true);
    try { await _restartIpc(); } finally { setRestarting(false); }
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
    api.archiveStatus()
      .then((st) => {
        setArchiveStatus({
          vault_present: !!st.vault_present,
          chats: Number(st.chats) || 0,
          backup_dir: String(st.backup_dir || ""),
        });
      })
      .catch(() => {});
  }, []);

  // Deep-link: Cmd-K Open Memory / /memory expand Agent Memory (mirror harness-expand-mcp).
  useEffect(() => {
    const onExpandMemory = () => {
      takePendingExpandMemory(); // drop latch — live listener already applied it
      setMemoryOpen(true);
      // Land on Advanced if Settings is open on another page.
      window.dispatchEvent(new CustomEvent("harness-settings-page", { detail: "advanced" }));
    };
    window.addEventListener("harness-expand-memory", onExpandMemory);
    return () => window.removeEventListener("harness-expand-memory", onExpandMemory);
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
    // Cursor CLI often finishes browser login before the poll/trust step;
    // Cancel must not overwrite an already-good account with "cancelled".
    if (cursorCliStatus?.authenticated) {
      setOauthHint(`Signed in as ${cursorCliStatus.label || "Cursor account"}`);
    } else {
      setOauthHint("Sign-in cancelled — click Sign in to try again.");
    }
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

  const handleProviderConfigSubmit = async (changed: Partial<ProviderConfigValues>) => {
    const name = (
      changed.name
      || (providerConfig && !providerConfig.manual ? providerConfig.provider.name : "")
      || ""
    ).trim();
    if (changed.api_key && name) {
      setProvBusy(name);
      try {
        await api.setProviderKey(name, changed.api_key);
        setProvKeyInput((p) => ({ ...p, [name]: "" }));
        await refreshProviders();
        window.dispatchEvent(new Event("harness-config-changed"));
      } catch (e) {
        console.error("Failed to set provider key", e);
      } finally {
        setProvBusy("");
      }
    }
    setProviderConfig(null);
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
      <div className="flex flex-col h-full text-[12px] p-4 text-faint">
        {errorNotice ? errorNotice : "Loading settings..."}
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
          {errorNotice && <span className="text-risk text-[11px] font-medium">{errorNotice}</span>}
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
            Connect a provider
          </button>
          <p className="text-[10px] text-muted">
            Pick a provider, paste a key, start chatting. One Full stack key runs chat and swarms.
          </p>
        </div>

        </>)}
        {gate("general", "transparent background glass vibrancy acrylic mica frost window") && (
          <WindowGlassSettings />
        )}
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
        {gate("general", "opt-ins optin auto-distill distillation toggle hash edit hash-anchored experimental review edits diff review toggle auto-verify edits typecheck syntax check self-correct diagnostics") && settings && (
          <SettingsOptIns settings={settings} onUpdate={(partial) => { void update(partial); }} saving={saving} />
        )}
        {gate("general", "compaction residual hybrid summary catalog vault compact handle index") && settings && (<>
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Compact Residual
          </label>
          <button
            onClick={() => {
              const cur = settings.compactionResidual || "catalog";
              const next = cur === "catalog"
                ? "hybrid"
                : cur === "hybrid"
                  ? "summary"
                  : "catalog";
              update({ compactionResidual: next });
            }}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              settings.compactionResidual && settings.compactionResidual !== "catalog"
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">
              {settings.compactionResidual === "hybrid"
                ? "Pin handle index after compact"
                : settings.compactionResidual === "summary"
                  ? "LLM snapshot after compact"
                  : "Handle catalog; vault retrieve"}
            </span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {settings.compactionResidual === "hybrid"
                ? "hybrid"
                : settings.compactionResidual === "summary"
                  ? "summary"
                  : "catalog"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            Default is catalog: keep files, decisions, and the last-wins
            story after compact, then retrieve matching slices later.
            Hybrid adds a paid LLM paragraph on top. Summary is the paid
            paragraph alone.
          </p>
        </div>

        </>)}
        {gate("safety", "browser chrome cookies real profile login") && settings && (
        <div className="space-y-1.5">
          <button
            onClick={() => update({ browserRealProfile: !(settings.browserRealProfile ?? false) })}
            disabled={saving}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              (settings.browserRealProfile ?? false)
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-[11px]">Use my Chrome login</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {(settings.browserRealProfile ?? false) ? "on" : "off"}
            </span>
          </button>
          <p className="text-[10px] text-muted">
            Copies cookies into a Marionette-owned profile so the agent browser is already
            signed in. Off by default. Closing Chrome may be required on Windows if copy
            fails.
          </p>
        </div>
        )}
        {gate("safety", "full-auto safety command guard timeout max investigation steps per-turn tool-call cap iteration budget guard") && settings && (<>
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
            sessions or builds). Even when off, a 15m safety ceiling still applies unless
            HARNESS_COMMAND_HARD_CEILING is set to off — hung shells otherwise pin the turn
            until Stop. Unbounded plus full-auto is why the guard above matters.
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
            Send-loop step ceiling per user message (model rounds through the tool loop).
            Use 0 or "unlimited" for unbounded autopilot until the pilot finishes, the budget
            governor halts, or you stop it. Distinct from Budget (Steps) and Per-turn tool-call
            cap below. Applies on the next turn — no restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-[11px] text-muted shrink-0">Per-turn tool-call cap</label>
            <input
              type="text"
              defaultValue={settings.pilotToolBudget || "25"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.pilotToolBudget || "25")) update({ pilotToolBudget: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-[11px] text-txt disabled:opacity-50"
              placeholder="25"
            />
          </div>
          <p className="text-[10px] text-muted">
            Iteration-budget guard: hard cap on native tool calls within one pilot turn.
            Loop breaker, swarm gate, and delegate gate stay active when set to 0 or "unlimited"
            (only this cap is disabled). Applies on the next turn — no restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-[11px] text-muted shrink-0">Worker run token ceiling</label>
            <input
              type="text"
              defaultValue={settings.workerTokenBudget || "250000"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.workerTokenBudget || "250000")) update({ workerTokenBudget: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-[11px] text-txt disabled:opacity-50"
              placeholder="250000"
            />
          </div>
          <p className="text-[10px] text-muted">
            Default token ceiling for a single native worker run when no ambient AutoBudget
            is governing the tree (default 250k — 40–50k starves analysis workers).
            Swarm/implement payloads stamp the same value as token_budget. Applies on
            the next worker spawn -- no restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-[11px] text-muted shrink-0">Worker reasoning</label>
            <select
              value={settings.swarm_reasoning_effort || "medium"}
              onChange={(e) => {
                const next = e.target.value as Settings["swarm_reasoning_effort"];
                if (next && next !== settings.swarm_reasoning_effort) {
                  update({ swarm_reasoning_effort: next });
                }
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-[11px] text-txt disabled:opacity-50"
            >
              {REASONING_LEVELS.map((level) => (
                <option key={level.value} value={level.value}>
                  {level.label}
                </option>
              ))}
            </select>
          </div>
          <p className="text-[10px] text-muted">
            Blanket reasoning for swarm, implement, and parallel workers. Factory
            default is medium (the StrongOrc floor). The composer picker is the
            chat pilot only. Omit the tool argument to use this setting; ask the
            pilot to pass reasoning_effort to pin one run.
          </p>
        </div>

        </>)}
        {gate("providers", "providers api keys connect disconnect per-provider key management") && (<>
        {/* Per-provider key management: connect/disconnect each provider independently */}
        <SettingsCollapse
          id="api-keys"
          title="API keys"
          defaultOpen={true}
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
          {keyBootstrapIssues.length > 0 && (
            <div
              data-testid="settings-key-bootstrap-issues"
              className="rounded-md border border-warn/40 bg-warn/10 px-2 py-1.5 text-[11px] text-txt space-y-1"
            >
              <div className="font-medium">Key store did not finish saving on startup.</div>
              <div className="text-muted">
                The app kept running. Re-save keys here if a provider looks missing.
              </div>
              {keyBootstrapIssues.map((issue, i) => (
                <div key={`${issue.step}-${i}`} className="font-mono text-[10px] text-faint break-all">
                  {issue.step}: {issue.message}
                </div>
              ))}
            </div>
          )}
          <div className="text-[10px] text-muted">
            One Full stack key (OpenRouter, Anthropic, OpenAI, Gemini, …) runs the chat
            pilot and agentic swarm/implement workers. No other platform install.
            Env-imported keys get an on/off toggle (keeps the key) and Disconnect
            (forgets it so you can paste a replacement).
          </div>
          <div className="space-y-1.5">
            {providers.filter((p) => p.name !== "bedrock").map((p) => {
              // Env-backed providers keep a toggle so disable does not destroy
              // the imported key. Disconnect is also available so a replacement
              // can be pasted — the toggle alone never reveals the key field.
              const envBacked = !!p.has_env;
              const enabled = !p.disconnected;
              const connected = p.has_key;
              const busy = provBusy === p.name;
              return (
              <div key={p.name} className="bg-panel2 border border-edge/50 rounded p-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${connected ? "bg-good" : "bg-faint"}`} />
                    <button
                      type="button"
                      data-testid="provider-account-drilldown"
                      data-provider={p.name}
                      onClick={() => setProviderConfig({ manual: false, provider: p })}
                      className="text-txt font-medium text-[11px] hover:text-accent text-left"
                    >
                      {p.display_name || p.name}
                    </button>
                    {p.worker_capability_label ? (
                      <span
                        title={p.worker_capability_hint || undefined}
                        className={`text-[10px] shrink-0 ${
                          p.worker_capability === "full_stack"
                            ? "text-good/80"
                            : p.worker_capability === "platform_worker"
                              ? "text-accent/80"
                              : "text-warn/90"
                        }`}
                      >
                        {p.worker_capability_label}
                      </span>
                    ) : null}
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
                  <div className="flex items-center gap-1.5 shrink-0">
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
                    ) : null}
                    {connected ? (
                      <button
                        onClick={() => handleClearProviderKey(p.name)}
                        disabled={busy}
                        className="bg-risk/10 hover:bg-risk/20 text-risk border border-risk/30 hover:border-risk/50 rounded px-2 py-0.5 font-medium text-[10px] disabled:opacity-30 transition-colors shrink-0"
                      >
                        Disconnect
                      </button>
                    ) : null}
                  </div>
                </div>
                {!connected && (
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
          <button
            type="button"
            data-testid="add-provider"
            onClick={() => setProviderConfig({ manual: true })}
            className="flex items-center gap-1 text-accent hover:text-accent/80 text-[11px] font-medium"
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
            busy={!!provBusy}
            onClose={() => setProviderConfig(null)}
            onSubmit={handleProviderConfigSubmit}
          />
        ) : null}

        </>)}

        {gate("providers", "sign in subscription oauth chatgpt codex claude max cursor xai grok nous plan account login") && (<>
        <SettingsCollapse
          id="sign-in"
          title="Optional plan sign-in"
          defaultOpen={false}
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
            Optional. A Full stack API key below is enough for chat and swarms — no
            Cursor, Claude, or Codex CLI install. Plan logins that are Full stack
            (Codex, Claude Max, OpenCode Go, Nous) also drive workers. Cursor CLI is Pilot only.
          </p>
          <div className="space-y-1.5">
            <div className="bg-panel2 border border-edge/50 rounded p-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-txt font-medium text-[11px]">
                  ChatGPT Codex{" "}
                  <span className="text-good/80 font-normal" title="Powers chat pilot and agentic swarm/implement workers.">Full stack</span>
                </span>
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
                <span className="text-txt font-medium text-[11px]">
                  Claude Max{" "}
                  <span className="text-good/80 font-normal" title="Subscription auth stamps Anthropic credentials used by agentic workers.">Full stack</span>
                </span>
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
                <span className="text-txt font-medium text-[11px]">
                  Cursor CLI (plan){" "}
                  <span className="text-warn/90 font-normal" title="Agent login powers the chat pilot only. Swarm/implement workers need a Full stack provider or a Cursor API key in Credential pools.">Pilot only</span>
                </span>
                <span className="text-faint text-[10px] font-mono truncate">
                  {cursorCliStatus?.installed === false
                    ? (cursorCliStatus.error || "agent binary not found")
                    : cursorCliStatus?.authenticated
                      ? `Signed in as ${cursorCliStatus.label || "Cursor account"}`
                      : (cursorCliStatus?.error || "Not signed in")}
                </span>
              </div>
              <p className="text-[10px] text-muted mt-1 leading-normal">
                Optional. Burns Cursor plan credits via the local Agent CLI when the
                `agent` binary is on PATH. Sign in here, or set CURSOR_API_KEY — the
                Agent CLI accepts either. Not required if you already have a Full stack
                chat key (OpenRouter, Anthropic, …).
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
                <span className="text-txt font-medium text-[11px]">
                  xAI SuperGrok{" "}
                  <span className="text-good/80 font-normal" title="OAuth stamps an xAI key that syncs into agentic workers.">Full stack</span>
                </span>
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
                <span className="text-txt font-medium text-[11px]">
                  Nous{" "}
                  <span className="text-good/80 font-normal" title="Powers chat pilot and agentic swarm/implement workers.">Full stack</span>
                </span>
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
            Optional plan sign-in above — pools are for multi-key rotate only. Cursor CLI
            is Pilot only; a single Full stack API key is enough. When every entry is
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
        {gate("advanced", "live ui vite hmr hot reload self dev restart relaunch") && _selfDevIpc && (<>
        {/* Live UI Section (Vite HMR). The backend always runs from source. */}
        <div className="border-t border-edge pt-3 space-y-2">
          <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Live UI (Vite HMR)
          </span>
          <p className="text-[10px] text-muted">
            Marionette always runs its backend from the source checkout, so
            harness/** edits are the running code after a full relaunch. The
            conversation comes back from the persisted transcript. Turn this on
            to also serve the React UI from a Vite dev server, so edits to
            webapp/src hot-reload instantly instead of needing a rebuild.
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
            onClick={relaunchMarionette}
            disabled={restarting || selfDevBusy}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded border border-edge bg-panel2 text-[11px] text-muted hover:text-txt hover:border-accent/30 transition disabled:opacity-50"
          >
            {restarting ? "Relaunching..." : "Relaunch Marionette"}
          </button>
          <p className="text-[10px] text-muted">
            Quits and reopens so the backend and UI boot together.
          </p>
        </div>
        </>)}
        {gate("advanced", "schedules cron timezone daemon autonomy") && (<>
        {/* Schedules Section */}
        <div className="border-t border-edge pt-3 space-y-2">
          <button
            onClick={() => setSchedulesOpen(!schedulesOpen)}
            className="w-full flex items-center justify-between text-left focus:outline-none"
          >
            <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Schedules
            </span>
            <span className="text-muted">
              {schedulesOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </span>
          </button>
          {schedulesOpen && (
            <div className="space-y-3 bg-panel2/40 border border-edge/50 rounded p-2.5 mt-1">
              <SchedulesPane />
            </div>
          )}
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
        {gate("advanced", "chat archive ingest backup prune compact") && (<>
        <div className="border-t border-edge pt-3 space-y-2">
          <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Chat archive
          </span>
          <p className="text-[10px] text-muted">
            Archive hides a session. Ingest copies it into a local vault and markdown
            backup. Search reads the vault. Compact removes ingested transcripts from
            the live store; Unarchive restores them.
          </p>
          <p className="text-[10px] text-faint">
            {archiveStatus
              ? `${archiveStatus.chats} chat${archiveStatus.chats === 1 ? "" : "s"} in the vault${archiveStatus.vault_present ? "" : " (empty)"}.`
              : "Archive status loading…"}
          </p>
          <button
            onClick={async () => {
              setArchiveBusy(true);
              setArchiveNotice("");
              try {
                const report = await api.ingestChatArchive();
                setArchiveNotice(`Ingested ${Number(report.ingested || 0)} archived session${Number(report.ingested || 0) === 1 ? "" : "s"}.`);
                const st = await api.archiveStatus();
                setArchiveStatus({
                  vault_present: !!st.vault_present,
                  chats: Number(st.chats) || 0,
                  backup_dir: String(st.backup_dir || ""),
                });
              } catch (err) {
                setArchiveNotice(String(err || "Ingest failed"));
              } finally {
                setArchiveBusy(false);
              }
            }}
            disabled={archiveBusy}
            className="w-full flex items-center justify-between px-3 py-2 rounded border bg-panel2 border-edge text-muted transition text-left disabled:opacity-50"
          >
            <span className="font-medium text-[11px]">Ingest archived sessions</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {archiveBusy ? "working" : "ingest"}
            </span>
          </button>
          <button
            onClick={async () => {
              setArchiveBusy(true);
              setArchiveNotice("");
              try {
                const report = await api.pruneChatArchive();
                setArchiveNotice(`Compacted ${Number(report.pruned || 0)} ingested transcript${Number(report.pruned || 0) === 1 ? "" : "s"}.`);
                const st = await api.archiveStatus();
                setArchiveStatus({
                  vault_present: !!st.vault_present,
                  chats: Number(st.chats) || 0,
                  backup_dir: String(st.backup_dir || ""),
                });
              } catch (err) {
                setArchiveNotice(String(err || "Compact failed"));
              } finally {
                setArchiveBusy(false);
              }
            }}
            disabled={archiveBusy}
            className="w-full flex items-center justify-between px-3 py-2 rounded border bg-panel2 border-edge text-muted transition text-left disabled:opacity-50"
          >
            <span className="font-medium text-[11px]">Compact ingested transcripts</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {archiveBusy ? "working" : "compact"}
            </span>
          </button>
          {archiveNotice ? <p className="text-[10px] text-muted">{archiveNotice}</p> : null}
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
                  <span className="text-faint">This app run tokens:</span>
                  <span className="text-txt font-mono font-medium">{usage.session.tokens_used.toLocaleString()}</span>
                </div>
                <div className="flex items-center justify-between text-[11px]">
                  <span className="text-faint">
                    {usage.session.cost_source === "provider" && usage.session.estimated !== true
                      ? "This app run (provider-billed):"
                      : usage.session.cost_source === "mixed"
                        ? "This app run (mixed):"
                        : usage.session.cost_source === "plan_estimated"
                          ? "This app run (plan):"
                          : "This app run (estimated):"}
                  </span>
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
            This app run resets on full quit — not Swarm pane repo-session spend or conversation lifetime.
            Spend basis may be provider-billed, mixed, estimated from catalog rates, or a plan-credit estimate.
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
