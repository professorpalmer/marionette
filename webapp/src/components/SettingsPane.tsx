import { useEffect, useState } from "react";
import { ChevronRight, ChevronDown, Plus, Trash2, ExternalLink, Search, X } from "lucide-react";
import {
  api,
  type Settings,
  type UsageData,
  hookCommandText,
} from "../lib/api";
import SkillsPane from "./SkillsPane";
import DeviceAccess from "./DeviceAccess";
import EncryptedSessions from "./EncryptedSessions";
import MemoryPane from "./MemoryPane";
import SchedulesPane from "./SchedulesPane";
import { takePendingExpandMemory, takePendingExpandSkills } from "../lib/memoryDeepLink";
import {
  readSettingsSnapshot,
  writeSettingsSnapshot,
} from "./settingsSnapshot";
import { usePanelNotice } from "../lib/useOperationalDiagnostic";
import { SettingsCollapse } from "./SettingsCollapse";
import WindowGlassSettings from "./WindowGlassSettings";
import SettingsOptIns from "./SettingsOptIns";
import { REASONING_LEVELS } from "../lib/reasoningSupport";

export type SettingsSection = "general" | "safety" | "notifications" | "plugins" | "advanced";

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
    const ok = active(s) && matches(keywords) && settings !== null;
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
  
  // Feature states
  const [hooks, setHooks] = useState<any[]>([]);
  const [allowedEvents, setAllowedEvents] = useState<string[]>([]);

  // Expand/collapse states
  const [hooksOpen, setHooksOpen] = useState(false);
  const [schedulesOpen, setSchedulesOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(() => takePendingExpandSkills());
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

  // Deep-link: the self-learning footer opens Skills & Rules.
  useEffect(() => {
    const onExpandSkills = () => {
      takePendingExpandSkills();
      setSkillsOpen(true);
      window.dispatchEvent(new CustomEvent("harness-settings-page", { detail: "advanced" }));
    };
    window.addEventListener("harness-expand-skills", onExpandSkills);
    return () => window.removeEventListener("harness-expand-skills", onExpandSkills);
  }, []);

  const searchActive = filter.trim().length > 0;
  useEffect(() => {
    const wantNotify = section === "notifications" || searchActive;
    const wantAdvanced = section === "advanced" || searchActive;

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
  }, [section, searchActive]);

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

  const canRenderWithoutSettings = searchActive;
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
        {gate("general", "provider model setup wizard api keys routing") && settings && (<>
        {/* Wizard Button */}
        <div className="space-y-1.5 border-b border-edge/65 pb-3">
          <button
            onClick={onOpenWizard}
            className="w-full bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded py-2 font-bold transition-colors text-ui-11"
          >
            Connect a provider
          </button>
          <p className="text-ui-10 text-muted">
            Pick a provider, paste a key, start chatting. One API key runs chat and swarms.
          </p>
        </div>

        </>)}
        {gate("general", "transparent background glass vibrancy acrylic mica frost window") && (
          <WindowGlassSettings />
        )}
        {gate("general", "budget steps per run") && settings && (<>
        {/* Budget Stepper / Number */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-ui-10 text-faint font-semibold">
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
            <span className="text-ui-10 text-muted">steps per run (1-50)</span>
          </div>
          <p className="text-ui-10 text-muted">
            Maximum Orchestration steps/budget allocated per task execution.
          </p>
        </div>

        </>)}
        {gate("general", "opt-ins optin auto-distill distillation toggle hash edit hash-anchored experimental review edits diff review toggle auto-verify edits typecheck syntax check self-correct diagnostics jev typesafe decisions skill retrieve openrouter") && settings && (
          <SettingsOptIns settings={settings} onUpdate={(partial) => { void update(partial); }} saving={saving} />
        )}
        {gate("general", "compaction residual hybrid summary catalog vault compact handle index") && settings && (<>
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-ui-10 text-faint font-semibold">
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
            <span className="font-medium text-ui-11">
              {settings.compactionResidual === "hybrid"
                ? "Pin handle index after compact"
                : settings.compactionResidual === "summary"
                  ? "LLM snapshot after compact"
                  : "Handle catalog; vault retrieve"}
            </span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {settings.compactionResidual === "hybrid"
                ? "hybrid"
                : settings.compactionResidual === "summary"
                  ? "summary"
                  : "catalog"}
            </span>
          </button>
          <p className="text-ui-10 text-muted">
            Default is catalog: keep files, decisions, and the last-wins
            story after compact, then retrieve matching slices later.
            The live pilot never writes the residual, including a local
            hosted model. Hybrid and summary use a cheaper summarizer only
            when one is configured; otherwise they stay extractive.
          </p>
        </div>

        </>)}
        {gate("general", "output style ste simplified technical english asd-ste100 controlled language prose") && settings && (
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-ui-10 text-faint font-semibold">
            Output Style
          </label>
          <button
            onClick={() => update({ outputStyle: settings.outputStyle === "ste" ? "off" : "ste" })}
            disabled={saving}
            aria-pressed={settings.outputStyle === "ste"}
            className={`w-full flex items-center justify-between px-3 py-2 rounded border transition text-left ${
              settings.outputStyle === "ste"
                ? "bg-accent/10 border-accent/30 text-accent"
                : "bg-panel2 border-edge text-muted"
            } disabled:opacity-50`}
          >
            <span className="font-medium text-ui-11">Simplified Technical English (ASD-STE100)</span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {settings.outputStyle === "ste" ? "on" : "off"}
            </span>
          </button>
          <p className="text-ui-10 text-muted">
            The pilot and new workers write short, controlled sentences: one
            thought per sentence, active voice, and only can, must, or will as
            helping verbs. Code, paths, and quoted text stay exact. The change
            applies from the next turn.
          </p>
        </div>
        )}
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
            <span className="font-medium text-ui-11">Use my Chrome login</span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {(settings.browserRealProfile ?? false) ? "on" : "off"}
            </span>
          </button>
          <p className="text-ui-10 text-muted">
            Copies cookies into a Marionette-owned profile so the agent browser is already
            signed in. Off by default. Closing Chrome may be required on Windows if copy
            fails.
          </p>
        </div>
        )}
        {gate("safety", "full-auto safety command guard timeout max investigation steps per-turn tool-call cap iteration budget guard") && settings && (<>
        {/* Full-Auto Safety: command guard + timeout */}
        <div className="space-y-1.5">
          <label className="block uppercase tracking-wider text-ui-10 text-faint font-semibold">
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
            <span className="font-medium text-ui-11">Guard dangerous commands in full-auto</span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {(settings.autoCommandGuard ?? true) ? "on" : "off"}
            </span>
          </button>
          <p className="text-ui-10 text-muted">
            In unattended (full-auto) mode, irreversible/remote/escalating shell commands
            (rm -rf, ssh, curl pipe-to-shell, force-push, sudo, disk writes) are blocked
            and reported instead of running. Interactive co-working is unaffected.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Command timeout (s)</label>
            <input
              type="text"
              defaultValue={settings.commandTimeout || "120"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.commandTimeout || "120")) update({ commandTimeout: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
              placeholder="120"
            />
          </div>
          <p className="text-ui-10 text-muted">
            Per-command shell timeout. Use 0 or "off" for unbounded (needed for long SSH
            sessions or builds). Even when off, a 15m safety ceiling still applies unless
            HARNESS_COMMAND_HARD_CEILING is set to off — hung shells otherwise pin the turn
            until Stop. Unbounded plus full-auto is why the guard above matters.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Max investigation steps</label>
            <input
              type="text"
              defaultValue={settings.maxPilotSteps || "40"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.maxPilotSteps || "40")) update({ maxPilotSteps: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
              placeholder="40"
            />
          </div>
          <p className="text-ui-10 text-muted">
            Send-loop step ceiling per user message (model rounds through the tool loop).
            Use 0 or "unlimited" for unbounded autopilot until the pilot finishes, the budget
            governor halts, or you stop it. Distinct from Budget (Steps) and Per-turn tool-call
            cap below. Applies on the next turn — no restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Reply output cap</label>
            <input
              type="text"
              defaultValue={settings.maxOutputTokens || "unlimited"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.maxOutputTokens || "unlimited")) update({ maxOutputTokens: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
              placeholder="unlimited"
            />
          </div>
          <p className="text-ui-10 text-muted">
            Completion-token ceiling for the next pilot request (HARNESS_MAX_TOKENS).
            The factory default lets the provider decide. APIs that require a ceiling use
            32000. Enter a positive number to set a custom cap, or use 0, off, or unlimited
            to omit the field where supported. Hitting a custom cap is a length stop, not a
            context overflow — Compact will not raise it. Continue finishes the same reply.
            Applies on the next turn — no restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Per-turn tool-call cap</label>
            <input
              type="text"
              defaultValue={settings.pilotToolBudget || "25"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.pilotToolBudget || "25")) update({ pilotToolBudget: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
              placeholder="25"
            />
          </div>
          <p className="text-ui-10 text-muted">
            Iteration-budget guard: hard cap on native tool calls within one pilot turn.
            Loop breaker, swarm gate, and delegate gate stay active when set to 0 or "unlimited"
            (only this cap is disabled). Applies on the next turn — no restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Full-auto token ceiling</label>
            <input
              type="text"
              defaultValue={settings.autoMaxTokens || "500000"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.autoMaxTokens || "500000")) update({ autoMaxTokens: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
              placeholder="500000"
            />
          </div>
          <p className="text-ui-10 text-muted">
            Tree-wide token ceiling for a full-auto run (HARNESS_AUTO_MAX_TOKENS, default
            500k). Use 0 or "unlimited" so tokens do not stop the run. Time, swarm, idle,
            and killswitch still apply. Takes effect on the next full-auto start — no
            restart needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Worker run token ceiling</label>
            <input
              type="text"
              defaultValue={settings.workerTokenBudget || "250000"}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v !== (settings.workerTokenBudget || "250000")) update({ workerTokenBudget: v });
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
              placeholder="250000"
            />
          </div>
          <p className="text-ui-10 text-muted">
            Default token ceiling for a single native worker run when no ambient AutoBudget
            is governing the tree (default 250k). Use 0 or "unlimited" for no per-worker
            token cap. Values from 1 through 39999 reset to 250k so a typo cannot
            force-submit every agentic worker on turn 2. Swarm/implement payloads stamp
            the same value as token_budget. Applies on the next worker spawn -- no restart
            needed.
          </p>
          <div className="flex items-center gap-2 pt-1">
            <label className="text-ui-11 text-muted shrink-0">Worker reasoning</label>
            <select
              value={settings.swarm_reasoning_effort || "medium"}
              onChange={(e) => {
                const next = e.target.value as Settings["swarm_reasoning_effort"];
                if (next && next !== settings.swarm_reasoning_effort) {
                  update({ swarm_reasoning_effort: next });
                }
              }}
              disabled={saving}
              className="flex-1 px-2 py-1 rounded border border-edge bg-panel2 text-ui-11 text-txt disabled:opacity-50"
            >
              {REASONING_LEVELS.map((level) => (
                <option key={level.value} value={level.value}>
                  {level.label}
                </option>
              ))}
            </select>
          </div>
          <p className="text-ui-10 text-muted">
            Blanket reasoning for swarm, implement, and parallel workers. Factory
            default is medium. The composer Workers chip writes this same setting;
            the pilot picker stays chat-only. Omit the tool argument to use this
            setting; ask the pilot to pass reasoning_effort to pin one run.
          </p>
        </div>

        </>)}
        {gate("safety", "encrypted sessions encryption at rest privacy recovery code") && (
          <SettingsCollapse
            id="encrypted-sessions"
            title="Encrypted sessions"
            defaultOpen={false}
            className="space-y-2 border-t border-edge pt-3"
          >
            <EncryptedSessions />
          </SettingsCollapse>
        )}
        {gate("safety", "device access read only grants enrollment revoke sessions") && (
          <SettingsCollapse
            id="device-access"
            title="Device access"
            defaultOpen={false}
            className="space-y-2 border-t border-edge pt-3"
          >
            <DeviceAccess />
          </SettingsCollapse>
        )}
        {gate("notifications", "observability queue notifications sound desktop messages") && (<>
        {/* Observability & Queue Prefs */}
        <div className="space-y-3 border-t border-edge pt-3">
          <label className="block uppercase tracking-wider text-ui-10 text-faint font-semibold">
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
              <span className="font-medium text-ui-11">Desktop notifications</span>
              <span className="text-ui-10 uppercase font-bold tracking-wider">
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
              <span className="font-medium text-ui-11">Completion sound</span>
              <span className="text-ui-10 uppercase font-bold tracking-wider">
                {sound ? "on" : "off"}
              </span>
            </button>
          </div>
        </div>

        </>)}
        {gate("advanced", "live ui vite hmr hot reload self dev restart relaunch") && _selfDevIpc && (<>
        {/* Live UI Section (Vite HMR). The backend always runs from source. */}
        <div className="border-t border-edge pt-3 space-y-2">
          <span className="uppercase tracking-wider text-ui-10 text-faint font-semibold flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Live UI (Vite HMR)
          </span>
          <p className="text-ui-10 text-muted">
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
            <span className="font-medium text-ui-11">Serve UI from Vite dev server (HMR)</span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {selfDev && selfDev.enabled ? "on" : "off"}
            </span>
          </button>
          {selfDev && !selfDev.viable && (
            <p className="text-ui-10 text-warn">
              Vite dev server not available (needs webapp/node_modules + webapp/src).
              The UI is served from the prebuilt dist/ until node deps are installed.
            </p>
          )}
          <button
            onClick={relaunchMarionette}
            disabled={restarting || selfDevBusy}
            className="w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded border border-edge bg-panel2 text-ui-11 text-muted hover:text-txt hover:border-accent/30 transition disabled:opacity-50"
          >
            {restarting ? "Relaunching..." : "Relaunch Marionette"}
          </button>
          <p className="text-ui-10 text-muted">
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
            <span className="uppercase tracking-wider text-ui-10 text-faint font-semibold flex items-center gap-1">
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
            <span className="uppercase tracking-wider text-ui-10 text-faint font-semibold flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-good inline-block"></span> Lifecycle Hooks
            </span>
            <span className="text-muted">
              {hooksOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            </span>
          </button>

          {hooksOpen && (
            <div className="space-y-3 bg-panel2/40 border border-edge/50 rounded p-2.5 mt-1">
              {hookError && <div className="text-risk text-ui-10 font-medium">{hookError}</div>}
              {hookStatus && <div className="text-good text-ui-10 font-medium">{hookStatus}</div>}

              {/* Hooks List */}
              <div className="space-y-2 max-h-40 overflow-y-auto pr-1">
                {hooks.length === 0 ? (
                  <div className="text-muted text-ui-10">No configured lifecycle hooks.</div>
                ) : (
                  hooks.map((hk) => (
                    <div key={hk.id} className="flex flex-col p-1.5 bg-panel2/65 border border-edge/30 rounded text-ui-11">
                      <div className="flex items-center justify-between">
                        <span className="bg-edge text-muted text-ui-9 px-1.5 py-0.5 rounded font-mono font-semibold uppercase tracking-wider">
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
                      <div className="text-txt font-mono text-ui-10 bg-panel/70 p-1.5 rounded border border-edge/20 mt-1 select-all break-all" title={hookCommandText(hk.command)}>
                        {hookCommandText(hk.command).length > 50
                          ? hookCommandText(hk.command).slice(0, 50) + "..."
                          : hookCommandText(hk.command)}
                      </div>
                    </div>
                  ))
                )}
              </div>

              {/* Add Hook Form */}
              <div className="border-t border-edge/30 pt-2.5 mt-2 space-y-1.5">
                <div className="text-ui-10 uppercase tracking-wider text-faint font-semibold">
                  Add Lifecycle Hook
                </div>
                <div className="space-y-1.5">
                  <select
                    value={newHookEvent}
                    onChange={(e) => setNewHookEvent(e.target.value)}
                    className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt text-ui-11 focus:outline-none focus:border-accent"
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
                    className="w-full bg-panel2 border border-edge rounded px-2 py-1 text-txt placeholder:text-faint text-ui-11 focus:outline-none focus:border-accent font-mono"
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
                    className="w-full bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded py-1 font-semibold text-ui-11 transition-colors flex items-center justify-center gap-1"
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
          <span className="uppercase tracking-wider text-ui-10 text-faint font-semibold flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-accent inline-block"></span> Chat archive
          </span>
          <p className="text-ui-10 text-muted">
            Archive hides a session. Ingest saves its native messages and compaction
            history in the local vault and backups. Compact replaces verified archived
            transcripts with small stubs; Unarchive restores their full content.
          </p>
          <p className="text-ui-10 text-faint">
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
                setArchiveNotice([
                  report.ingested ? `Ingested ${report.ingested} archived session${report.ingested === 1 ? "" : "s"}.` : "",
                  report.skipped_unchanged ? `${report.skipped_unchanged} already up to date.` : "",
                  report.errors ? `${report.errors} failed to ingest.` : "",
                ].filter(Boolean).join(" ") || "No archived sessions to ingest.");
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
            <span className="font-medium text-ui-11">Ingest archived sessions</span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {archiveBusy ? "working" : "ingest"}
            </span>
          </button>
          <button
            onClick={async () => {
              setArchiveBusy(true);
              setArchiveNotice("");
              try {
                const report = await api.pruneChatArchive();
                setArchiveNotice([
                  `Compacted ${report.pruned} ingested transcript${report.pruned === 1 ? "" : "s"}.`,
                  report.skipped ? `${report.skipped} skipped; already compacted or not verified for removal.` : "",
                ].filter(Boolean).join(" "));
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
            <span className="font-medium text-ui-11">Compact ingested transcripts</span>
            <span className="text-ui-10 uppercase font-bold tracking-wider">
              {archiveBusy ? "working" : "compact"}
            </span>
          </button>
          {archiveNotice ? <p role="status" className="text-ui-10 text-muted">{archiveNotice}</p> : null}
        </div>
        </>)}
        {gate("advanced", "agent memory durable facts preferences") && (<>
        {/* Agent Memory Section */}
        <div className="border-t border-edge pt-3 space-y-2">
          <button
            onClick={() => setMemoryOpen(!memoryOpen)}
            className="w-full flex items-center justify-between text-left focus:outline-none"
          >
            <span className="uppercase tracking-wider text-ui-10 text-faint font-semibold flex items-center gap-1">
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
            <span className="uppercase tracking-wider text-ui-10 text-faint font-semibold flex items-center gap-1">
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
            <label className="block uppercase tracking-wider text-ui-10 text-faint font-semibold">
              Token & Cost Usage
            </label>
            <button
              onClick={() => {
                api.getUsage()
                  .then(setUsage)
                  .catch((err) => console.error("Failed to refresh usage", err));
              }}
              className="text-ui-9 uppercase font-bold tracking-wider text-accent hover:underline bg-transparent border-0 p-0"
            >
              Refresh
            </button>
          </div>

          {usage ? (
            <div className="space-y-2.5 bg-panel2 border border-edge/50 rounded p-2.5">
              <div className="space-y-1">
                <div className="flex items-center justify-between text-ui-11">
                  <span className="text-faint">This app run tokens:</span>
                  <span className="text-txt font-mono font-medium">{usage.session.tokens_used.toLocaleString()}</span>
                </div>
                <div className="flex items-center justify-between text-ui-11">
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
                <div className="flex flex-wrap items-center justify-between gap-1 text-ui-11 border-t border-edge/30 pt-1 mt-1">
                  <span className="text-faint">Active Driver:</span>
                  <span className="text-txt font-mono font-medium truncate max-w-full" title={usage.session.driver}>{usage.session.driver}</span>
                </div>
                <div className="flex items-center justify-between text-ui-11">
                  <span className="text-faint">Price in/out (per Mtok):</span>
                  <span className="text-muted font-mono font-medium">${usage.session.price_in}/${usage.session.price_out}</span>
                </div>
              </div>

              {usage.jobs && usage.jobs.length > 0 && (
                <div className="space-y-1 border-t border-edge/40 pt-1.5 mt-1.5">
                  <div className="text-ui-9 uppercase tracking-wider text-faint font-semibold mb-1">
                    PM Job Costs (estimated)
                  </div>
                  <div className="max-h-24 overflow-y-auto space-y-1 pr-1">
                    {usage.jobs.map((job: any) => (
                      <div key={job.job_id} className="flex items-center justify-between gap-x-1.5 text-ui-10 font-mono">
                        <span className="text-muted truncate flex-1 min-w-0" title={job.job_id}>{job.job_id}</span>
                        <span className="text-faint text-ui-9 flex-shrink-0">{job.tokens.toLocaleString()} tok</span>
                        <span className="text-txt font-medium flex-shrink-0">${job.est_cost_usd.toFixed(6)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <p className="text-ui-10 text-muted">Loading usage statistics...</p>
          )}
          <p className="text-ui-9 text-muted font-mono">
            This app run resets on full quit — not Swarm pane repo-session spend or conversation lifetime.
            Spend basis may be provider-billed, mixed, estimated from catalog rates, or a plan-credit estimate.
          </p>
        </div>

        </>)}
        {gate("general", "system info version read-only") && settings && (<>
        {/* Read-Only Info */}
        <div className="border-t border-edge pt-3 space-y-2.5">
          <div className="uppercase tracking-wider text-ui-10 text-faint font-semibold">
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
            <div className="text-muted font-mono select-all break-all bg-panel2 p-1.5 rounded border border-edge/30 text-ui-11">
              {settings.state_dir || "Temporary (per-session)"}
            </div>
          </div>

          <div className="space-y-0.5">
            <div className="text-faint">Repository:</div>
            <div className="text-muted font-mono select-all break-all bg-panel2 p-1.5 rounded border border-edge/30 text-ui-11">
              {settings.repo || "None"}
            </div>
          </div>
        </div>

        </>)}
        {gate("advanced", "wiki graph portable-llm-wiki api base token") && (<>
        {/* WIKI GRAPH (portable-llm-wiki gated owner surface) */}
        <div className="border-t border-edge pt-3 space-y-2">
          <div className="uppercase tracking-wider text-ui-10 text-faint font-semibold">
            Wiki Graph
          </div>
          <div className="text-ui-10 text-muted leading-relaxed">
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
            className="w-full bg-bg border border-edge rounded px-2 py-1 text-ui-11 font-mono text-txt focus:outline-none focus:border-accent"
          />
          <input
            type="password"
            value={wikiToken}
            onChange={(e) => setWikiToken(e.target.value)}
            placeholder={wikiCfg?.has_token ? "Owner token (leave blank to keep)" : "Owner token (optional if URL includes ?t=)"}
            className="w-full bg-bg border border-edge rounded px-2 py-1 text-ui-11 font-mono text-txt focus:outline-none focus:border-accent"
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
            className="bg-accent/15 hover:bg-accent/25 text-accent text-ui-11 font-semibold px-2 py-1 rounded transition disabled:opacity-50"
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
              className="ml-2 bg-edge hover:bg-risk/20 text-muted hover:text-risk text-ui-11 font-semibold px-2 py-1 rounded transition disabled:opacity-50 border border-edge2"
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
            className="inline-flex items-center gap-1 text-ui-10 text-faint hover:text-accent transition-colors"
          >
            New here? Learn what portable-llm-wiki is at portablellm.wiki
            <ExternalLink size={10} />
          </a>
        </div>
        </>)}
        {q && !anyShown && (
          <p className="text-ui-11 text-muted">No settings match "{filter.trim()}".</p>
        )}
      </div>
    </div>
  );
}
