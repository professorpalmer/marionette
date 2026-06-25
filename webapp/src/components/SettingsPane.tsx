import { useEffect, useState } from "react";
import { Settings as SettingsIcon, ChevronRight, ChevronDown, Plus, Trash2 } from "lucide-react";
import { api, type Settings, type UsageData } from "../lib/api";

export default function SettingsPane({ onOpenWizard }: { onOpenWizard: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [keyInput, setKeyInput] = useState("");
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

    loadHooks();
  }, []);

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
    <div className="flex flex-col h-full text-[12px]">
      <div className="flex items-center justify-between px-3 py-2 border-b border-edge">
        <span className="uppercase tracking-wider text-[10px] text-faint font-medium flex items-center gap-1.5">
          <SettingsIcon size={11} /> Settings
        </span>
        <div className="flex items-center gap-2">
          {status && <span className="text-good text-[10px] font-medium">{status}</span>}
          {error && <span className="text-risk text-[10px] font-medium">{error}</span>}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-4">
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

        {/* API Key Section */}
        <div className="space-y-1.5 border-t border-edge pt-3">
          <label className="block uppercase tracking-wider text-[10px] text-faint font-semibold">
            API Key Setup
          </label>
          <div className="text-[10px] text-muted mb-2">
            Set the provider API key for <span className="font-semibold">{settings.reach} ({settings.key_env_var})</span>.
          </div>
          
          <div className="flex gap-2">
            <input
              type="password"
              placeholder="Enter API key..."
              value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              disabled={saving}
              className="flex-1 bg-panel2 border border-edge rounded px-2.5 py-1 text-txt focus:outline-none focus:border-accent disabled:opacity-50 font-mono"
            />
            <button
              onClick={() => {
                if (keyInput.trim()) {
                  update({ api_key: keyInput.trim() }).then(() => setKeyInput(""));
                }
              }}
              disabled={saving || !keyInput.trim()}
              className="bg-accent/15 hover:bg-accent/25 text-accent border border-accent/30 hover:border-accent/50 rounded px-3 py-1 font-medium text-[11px] disabled:opacity-30 disabled:hover:bg-accent/15 disabled:hover:border-accent/30 transition-colors"
            >
              Save key
            </button>
          </div>

          <div className="flex items-center justify-between text-[11px] mt-2 bg-panel2 border border-edge/50 rounded p-2">
            <div className="space-y-1">
              <div className="flex items-center gap-1.5">
                <span className="text-faint">Status:</span>
                <span className={settings.has_api_key ? "text-good font-medium" : "text-risk font-medium"}>
                  {settings.has_api_key ? `Key set: ${settings.api_key_masked}` : "No key set"}
                </span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-faint">Preflight:</span>
                <span className={settings.preflight_ok ? "text-good font-medium" : "text-risk font-medium"}>
                  {settings.preflight_ok ? "Provider ready" : "Key needed or invalid"}
                </span>
              </div>
            </div>
            {settings.has_api_key && (
              <button
                onClick={() => {
                  update({ clear_api_key: true });
                  setKeyInput("");
                }}
                disabled={saving}
                className="bg-risk/10 hover:bg-risk/20 text-risk border border-risk/30 hover:border-risk/50 rounded px-2.5 py-1 font-medium text-[11px] disabled:opacity-30 transition-colors"
              >
                Clear
              </button>
            )}
          </div>
        </div>

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
      </div>
    </div>
  );
}
