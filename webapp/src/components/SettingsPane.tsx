import { useEffect, useState } from "react";
import { ChevronRight, ChevronDown, Plus, Trash2, ExternalLink } from "lucide-react";
import { api, type Settings, type UsageData, type PlatformAdapter, type GitStatus, type ProviderInfo } from "../lib/api";
import SkillsPane from "./SkillsPane";
import MemoryPane from "./MemoryPane";

export type SettingsSection = "general" | "safety" | "providers" | "notifications" | "advanced";

export default function SettingsPane({ onOpenWizard, section = "general" }: { onOpenWizard: () => void; section?: SettingsSection }) {
  const show = (s: SettingsSection) => section === s;
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
  const [showAdvancedAdapters, setShowAdvancedAdapters] = useState(false);
  const [platformError, setPlatformError] = useState("");

  // Per-provider key management states
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [provKeyInput, setProvKeyInput] = useState<Record<string, string>>({});
  const [provBusy, setProvBusy] = useState<string>("");

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

  // Self-development (Hermes-style live self-editing): run the backend from the
  // editable source checkout so edits to ~/pm-harness go live on restart. Only
  // available in the desktop app (needs Electron to swap the backend process).
  const _selfDevIpc = (typeof window !== "undefined" && (window as any).harnessIPC?.selfDev) || null;
  const _restartIpc = (typeof window !== "undefined" && (window as any).harnessIPC?.restart) || null;
  const [selfDev, setSelfDev] = useState<{ enabled: boolean; viable: boolean; packaged: boolean } | null>(null);
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
    api.settings()
      .then(setSettings)
      .catch((err) => {
        setError("Failed to load settings");
        console.error(err);
      });

    api.getWikiConfig()
      .then((w) => { setWikiCfg(w); setWikiBase(w.api_base || ""); })
      .catch(() => {});
    api.getUsage()
      .then(setUsage)
      .catch((err) => {
        console.error("Failed to load usage statistics", err);
      });

    api.getPlatform()
      .then((res) => setPlatformAdapters(res.adapters))
      .catch((err) => {
        setPlatformError("platform settings unavailable");
        console.error("Failed to load platform adapters", err);
      });

    api.providers()
      .then(setProviders)
      .catch((err) => console.error("Failed to load providers", err));

    api.getGitStatus()
      .then(setGitStatus)
      .catch((err) => {
        console.error("Failed to load Git status", err);
      });

    loadHooks();
  }, []);

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
    try { setProviders(await api.providers()); } catch (e) { console.error(e); }
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
      const timer = setTimeout(() => setStatus(""), 2000);
      return () => clearTimeout(timer);
    } catch (err: any) {
      setError(err?.error || "Failed to update settings");
    } finally {
      setSaving(false);
    }
  };

  if (!settings) {
    return (
      <div className="flex flex-col h-full text-[12px] p-4 text-faint">
        {error ? error : "Loading settings..."}
      </div>
    );
  }

  return (
    <div className="text-[12px] max-w-3xl">
      {(status || error) && (
        <div className="flex items-center gap-2 mb-3">
          {status && <span className="text-good text-[10px] font-medium">{status}</span>}
          {error && <span className="text-risk text-[10px] font-medium">{error}</span>}
        </div>
      )}

      <div className="space-y-4">
        {show("general") && (<>
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
        {show("general") && (<>
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
        {show("general") && (<>
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
        {show("general") && (<>
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
        {show("general") && (<>
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
        {show("safety") && (<>
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
        {show("providers") && (<>
        {/* Per-provider key management: connect/disconnect each provider independently */}
        <div className="space-y-2">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            Providers
          </label>
          <div className="text-[10px] text-muted mb-1">
            Connect or disconnect each provider independently. Keys imported from your environment get an on/off toggle -- flip one off to stop using it without losing the key, for easy swapping (e.g. work vs. personal).
          </div>
          <div className="space-y-1.5">
            {providers.map((p) => {
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
        </div>

        </>)}
        {show("providers") && (<>
        {/* Platform Adapters Control (ADVANCED -- optional) */}
        <div className="space-y-3 border-t border-edge pt-3">
          <button
            onClick={() => setShowAdvancedAdapters((v) => !v)}
            className="flex items-center gap-1.5 w-full text-left"
          >
            <ChevronRight size={12} className={`text-faint transition-transform ${showAdvancedAdapters ? "rotate-90" : ""}`} />
            <span className="uppercase tracking-wider text-[10px] text-faint font-semibold">External Worker Platforms</span>
            <span className="text-[9px] text-muted normal-case tracking-normal">(advanced / optional)</span>
          </button>
          {showAdvancedAdapters && (<>
          <p className="text-[10px] text-muted leading-normal pl-4">
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
          </>)}
        </div>

        </>)}
        {show("notifications") && (<>
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
        {show("advanced") && _selfDevIpc && (<>
        {/* Self-Development Section (live self-editing, Hermes-style) */}
        <div className="border-t border-edge pt-3 space-y-2">
          <span className="uppercase tracking-wider text-[10px] text-faint font-semibold flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Live Self-Editing
          </span>
          <p className="text-[10px] text-muted">
            Run the backend from the editable source checkout so edits to the repo go
            live -- Marionette can rewrite its own code and apply it without a full app
            reinstall. React UI edits (webapp/src) hot-reload instantly via the dev
            server; backend edits (harness/**) apply on restart, and the conversation
            resumes across it.
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
            <span className="font-medium text-[11px]">Run backend from editable source</span>
            <span className="text-[10px] uppercase font-bold tracking-wider">
              {selfDev && selfDev.enabled ? "on" : "off"}
            </span>
          </button>
          {selfDev && !selfDev.viable && (
            <p className="text-[10px] text-warn">
              Editable checkout not found at ~/pm-harness (needs .venv + harness/). Self-editing
              runs the frozen bundled backend until the source checkout is present.
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
        {show("advanced") && (<>
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
        {show("advanced") && (<>
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
        {show("advanced") && (<>
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
        {show("general") && (<>
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
        {show("general") && (<>
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
        {show("providers") && (<>
        {/* GitHub & Wiki Repo Provisioning */}
        <div className="border-t border-edge pt-3 space-y-2">
          <div className="uppercase tracking-wider text-[10px] text-faint font-semibold">
            GitHub / Wiki Repo
          </div>
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
        </div>

        </>)}
        {show("advanced") && (<>
        {/* WIKI GRAPH (portable-llm-wiki gated owner surface) */}
        <div className="border-t border-edge pt-3 space-y-2">
          <div className="uppercase tracking-wider text-[10px] text-faint font-semibold">
            Wiki Graph
          </div>
          <div className="text-[10px] text-muted leading-relaxed">
            Connect the portable-llm-wiki owner surface (same as the wiki MCP) to populate the Wiki graph tab.
            {wikiCfg ? <span className={wikiCfg.has_token ? "text-good" : "text-faint"}> {wikiCfg.has_token ? "Token set." : "No token."}</span> : null}
          </div>
          <input
            type="text"
            value={wikiBase}
            onChange={(e) => setWikiBase(e.target.value)}
            placeholder="WIKI_API_BASE (e.g. http://localhost:8000)"
            className="w-full bg-bg border border-edge rounded px-2 py-1 text-[11px] font-mono text-txt focus:outline-none focus:border-accent"
          />
          <input
            type="password"
            value={wikiToken}
            onChange={(e) => setWikiToken(e.target.value)}
            placeholder={wikiCfg?.has_token ? "Owner token (leave blank to keep)" : "WIKI_OWNER_TOKEN"}
            className="w-full bg-bg border border-edge rounded px-2 py-1 text-[11px] font-mono text-txt focus:outline-none focus:border-accent"
          />
          <button
            disabled={wikiSaving}
            onClick={async () => {
              setWikiSaving(true);
              try {
                const res = await api.setWikiConfig(wikiBase, wikiToken || undefined);
                setWikiCfg(res); setWikiToken("");
              } catch { /* ignore */ }
              finally { setWikiSaving(false); }
            }}
            className="bg-accent/15 hover:bg-accent/25 text-accent text-[11px] font-semibold px-2 py-1 rounded transition disabled:opacity-50"
          >
            {wikiSaving ? "Saving..." : "Save Wiki Config"}
          </button>
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
      </div>
    </div>
  );
}
