import { useEffect, useState, type ReactNode } from "react";
import { api, hookCommandText, type Hook } from "../lib/api";
import { getHarnessIpc } from "../lib/transport";
import { takePendingExpandMemory, takePendingExpandSkills } from "../lib/memoryDeepLink";
import {
  SETTINGS_PRIMARY_BUTTON,
  SETTINGS_QUIET_BUTTON,
  SettingsGroup,
  SettingsPage,
  SettingsRow,
} from "./SettingsLayout";
import Switch from "./SettingsSwitch";
import MemoryPane from "./MemoryPane";
import SkillsPane from "./SkillsPane";
import SchedulesPane from "./SchedulesPane";

const FIELD = "bg-panel border border-edge rounded px-2 py-0.5 text-ui-11 text-txt font-mono focus:outline-none focus:border-accent";

function errorText(err: unknown, fallback: string): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "object" && err !== null && "error" in err && typeof err.error === "string") return err.error;
  return fallback;
}

type SelfDevState = { enabled: boolean; viable: boolean };
type SelfDevBridge = { get: () => Promise<SelfDevState>; set: (on: boolean) => Promise<unknown> };
type RestartBridge = () => Promise<unknown>;

function isSelfDevBridge(value: unknown): value is SelfDevBridge {
  if (typeof value !== "object" || value === null) return false;
  const fields = value as Record<string, unknown>;
  return typeof fields.get === "function" && typeof fields.set === "function";
}

/** Live UI and relaunch exist only in the desktop app. */
function desktopBridges(): { selfDev: SelfDevBridge | null; restart: RestartBridge | null } {
  const ipc = getHarnessIpc();
  const selfDev: unknown = ipc?.selfDev;
  const restart: unknown = ipc?.restart;
  return {
    selfDev: isSelfDevBridge(selfDev) ? selfDev : null,
    restart: typeof restart === "function" ? (restart as RestartBridge) : null,
  };
}

/** A row whose content opens under it: a list too long to show at all times. */
function OpenRow({
  label,
  description,
  open,
  onToggle,
  testId,
  children,
}: {
  label: string;
  description: string;
  open: boolean;
  onToggle: () => void;
  testId: string;
  children: ReactNode;
}) {
  return (
    <SettingsRow
      label={label}
      description={description}
      testId={testId}
      control={(
        <button type="button" className={SETTINGS_QUIET_BUTTON} aria-expanded={open} onClick={onToggle}>
          {open ? "Hide" : "Show"}
        </button>
      )}
    >
      {open ? children : null}
    </SettingsRow>
  );
}

function HooksGroup() {
  const [hooks, setHooks] = useState<Hook[]>([]);
  const [events, setEvents] = useState<string[]>([]);
  const [event, setEvent] = useState("");
  const [command, setCommand] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let live = true;
    api.getHooks()
      .then((data) => {
        if (!live) return;
        setHooks(data.hooks || []);
        setEvents(data.events || []);
        setEvent((prev) => prev || data.events?.[0] || "");
      })
      .catch(() => { if (live) setResult({ ok: false, text: "Hooks did not load." }); });
    return () => { live = false; };
  }, []);

  const toggle = async (hook: Hook) => {
    setResult(null);
    try {
      const updated = await api.updateHook(hook.id, { enabled: !hook.enabled });
      setHooks((prev) => prev.map((h) => (h.id === hook.id ? updated : h)));
    } catch (err: unknown) {
      setResult({ ok: false, text: errorText(err, "The hook did not change.") });
    }
  };

  const remove = async (hook: Hook) => {
    setResult(null);
    try {
      const res = await api.removeHook(hook.id);
      if (res.ok) setHooks((prev) => prev.filter((h) => h.id !== hook.id));
      else setResult({ ok: false, text: "The hook was not removed." });
    } catch (err: unknown) {
      setResult({ ok: false, text: errorText(err, "The hook was not removed.") });
    }
  };

  const add = async () => {
    const text = command.trim();
    if (!text) {
      setResult({ ok: false, text: "Enter a command." });
      return;
    }
    setResult(null);
    try {
      const added = await api.addHook(event, text);
      setHooks((prev) => [...prev, added]);
      setCommand("");
      setResult({ ok: true, text: "Hook added." });
    } catch (err: unknown) {
      setResult({ ok: false, text: errorText(err, "The hook was not added.") });
    }
  };

  return (
    <SettingsGroup
      title="Lifecycle hooks"
      description="Run a shell command when an event occurs, for example when a session starts."
      testId="advanced-hooks"
    >
      {hooks.map((hook) => {
        const text = hookCommandText(hook.command);
        return (
          <SettingsRow
            key={hook.id}
            testId="hook-row"
            label={<span className="font-mono">{hook.event}</span>}
            description={<span className="font-mono break-all" title={text}>{text.length > 80 ? `${text.slice(0, 80)}...` : text}</span>}
            control={(
              <>
                <Switch on={hook.enabled} label={`Run hook ${hook.event}`} onChange={() => { void toggle(hook); }} />
                <button type="button" className={SETTINGS_QUIET_BUTTON} onClick={() => { void remove(hook); }} aria-label={`Remove hook ${hook.event}`}>
                  Remove
                </button>
              </>
            )}
          />
        );
      })}
      <SettingsRow label="Add a hook" description={hooks.length ? undefined : "No hooks are set."}>
        <div className="flex items-center gap-2">
          <select
            aria-label="Hook event"
            value={event}
            onChange={(e) => setEvent(e.target.value)}
            className={FIELD}
          >
            {events.map((evt) => <option key={evt} value={evt}>{evt}</option>)}
          </select>
          <input
            type="text"
            aria-label="Hook command"
            placeholder="Shell command, for example echo start"
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void add(); }}
            className={`flex-1 ${FIELD}`}
          />
          <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={!event} onClick={() => { void add(); }}>
            Add hook
          </button>
        </div>
        {result ? (
          <p role={result.ok ? "status" : "alert"} className={`mt-1 text-ui-10 ${result.ok ? "text-muted" : "text-risk"}`}>{result.text}</p>
        ) : null}
      </SettingsRow>
    </SettingsGroup>
  );
}

function WikiGroup() {
  const [config, setConfig] = useState<{ api_base: string; has_token: boolean } | null>(null);
  const [base, setBase] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    api.getWikiConfig()
      .then((w) => { if (live) { setConfig(w); setBase(w.api_base || ""); } })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  const apply = async (work: () => Promise<{ api_base: string; has_token: boolean }>, clearBase: boolean) => {
    setBusy(true);
    setError("");
    try {
      const res = await work();
      setConfig(res);
      setToken("");
      if (clearBase) setBase("");
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (err: unknown) {
      setError(errorText(err, "The wiki setting did not save."));
    } finally {
      setBusy(false);
    }
  };

  const connected = !!(config?.api_base || config?.has_token);
  const state = !config ? undefined : config.has_token ? "Token set" : config.api_base ? "No token" : "Not connected";

  return (
    <SettingsGroup title="Wiki" testId="advanced-wiki">
      <SettingsRow
        label="Wiki connection"
        description={(
          <>
            Connect from the State pane, or paste your wiki URL and owner token here.{" "}
            <a href="https://portablellm.wiki" target="_blank" rel="noopener noreferrer" className="hover:text-accent underline-offset-2 hover:underline">
              What is portable-llm-wiki?
            </a>
          </>
        )}
        state={state}
        control={connected ? (
          <button type="button" className={SETTINGS_QUIET_BUTTON} disabled={busy} onClick={() => { void apply(() => api.disconnectWiki(), true); }}>
            Disconnect
          </button>
        ) : undefined}
      >
        <div className="flex items-center gap-2">
          <input
            type="text"
            aria-label="Wiki URL"
            value={base}
            onChange={(e) => setBase(e.target.value)}
            placeholder="https://api.portablellm.wiki/t/your-tenant"
            className={`flex-1 ${FIELD}`}
          />
          <input
            type="password"
            aria-label="Owner token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={config?.has_token ? "Keep the current token" : "Owner token"}
            className={`w-40 ${FIELD}`}
          />
          <button
            type="button"
            className={SETTINGS_PRIMARY_BUTTON}
            disabled={busy}
            onClick={() => { void apply(() => api.setWikiConfig(base, token || undefined), false); }}
          >
            {busy ? "Saving..." : "Save"}
          </button>
        </div>
        {error ? <p role="alert" className="mt-1 text-ui-10 text-risk">{error}</p> : null}
      </SettingsRow>
    </SettingsGroup>
  );
}

function ArchiveGroup() {
  const [chats, setChats] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");

  const loadStatus = async () => {
    const st = await api.archiveStatus();
    setChats(Number(st.chats) || 0);
  };

  useEffect(() => { loadStatus().catch(() => {}); }, []);

  // The same work as an archive click and the start of the app: copy each
  // archived chat into the vault, then swap its transcript for a small stub.
  const moveNow = async () => {
    setBusy(true);
    setNotice("");
    try {
      const ingest = await api.ingestChatArchive();
      const prune = await api.pruneChatArchive();
      const parts = [
        ingest.ingested ? `Saved ${ingest.ingested} chat${ingest.ingested === 1 ? "" : "s"} to the vault.` : "",
        prune.pruned ? `Replaced ${prune.pruned} transcript${prune.pruned === 1 ? "" : "s"} with a stub.` : "",
        ingest.errors ? `${ingest.errors} failed.` : "",
      ].filter(Boolean);
      setNotice(parts.join(" ") || "All archived chats are already in the vault.");
      await loadStatus();
    } catch (err: unknown) {
      setNotice(errorText(err, "The move failed."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsGroup title="Chat archive" testId="advanced-archive">
      <SettingsRow
        label="Archived chats in the vault"
        description="Archive moves a chat into the vault at once. Marionette tries again at each start. Use Move now if a move failed."
        state={chats === null ? undefined : `${chats} chat${chats === 1 ? "" : "s"}`}
        control={(
          <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={busy} onClick={() => { void moveNow(); }}>
            {busy ? "Moving..." : "Move now"}
          </button>
        )}
      >
        {notice ? <p role="status" className="text-ui-10 text-muted">{notice}</p> : null}
      </SettingsRow>
    </SettingsGroup>
  );
}

function AppGroup({ selfDev, restart }: { selfDev: SelfDevBridge; restart: RestartBridge | null }) {
  const [state, setState] = useState<SelfDevState | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    selfDev.get().then(setState).catch(() => {});
  }, [selfDev]);

  const relaunch = async () => {
    if (!restart) return;
    setBusy(true);
    try { await restart(); } finally { setBusy(false); }
  };

  const toggle = async () => {
    if (!state) return;
    setBusy(true);
    try {
      const res = await selfDev.set(!state.enabled);
      setState(await selfDev.get());
      // The Vite choice is read at process start, so relaunch to apply it.
      if (res && restart) await restart();
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsGroup title="App" testId="advanced-app">
      <SettingsRow
        label="Serve the UI from Vite"
        description="Edits to webapp/src load at once, with no rebuild. The app relaunches to apply the change."
        state={state ? (state.enabled ? "On" : "Off") : undefined}
        control={(
          <Switch
            on={!!state?.enabled}
            label="Serve the UI from Vite"
            disabled={busy || !state?.viable}
            onChange={() => { void toggle(); }}
          />
        )}
      >
        {state && !state.viable ? (
          <p className="text-ui-10 text-warn">
            The Vite dev server is not available. It needs webapp/node_modules and webapp/src.
          </p>
        ) : null}
      </SettingsRow>
      {restart ? (
        <SettingsRow
          label="Relaunch Marionette"
          description="Quits and opens the app again, so the backend and the UI start together."
          control={(
            <button type="button" className={SETTINGS_PRIMARY_BUTTON} disabled={busy} onClick={() => { void relaunch(); }}>
              {busy ? "Relaunching..." : "Relaunch"}
            </button>
          )}
        />
      ) : null}
    </SettingsGroup>
  );
}

/**
 * Settings > Advanced: agent memory, skills, automation, the wiki, the chat
 * archive, and app controls. Cmd-K, /memory, and the self-learning footer
 * open Agent memory or Skills and rules here (lib/memoryDeepLink).
 */
export default function AdvancedSettingsPage() {
  const [memoryOpen, setMemoryOpen] = useState(() => takePendingExpandMemory());
  const [skillsOpen, setSkillsOpen] = useState(() => takePendingExpandSkills());
  const [schedulesOpen, setSchedulesOpen] = useState(false);
  const [bridges] = useState(desktopBridges);

  useEffect(() => {
    const onMemory = () => { takePendingExpandMemory(); setMemoryOpen(true); };
    const onSkills = () => { takePendingExpandSkills(); setSkillsOpen(true); };
    window.addEventListener("harness-expand-memory", onMemory);
    window.addEventListener("harness-expand-skills", onSkills);
    return () => {
      window.removeEventListener("harness-expand-memory", onMemory);
      window.removeEventListener("harness-expand-skills", onSkills);
    };
  }, []);

  return (
    <SettingsPage
      title="Advanced"
      description="Agent memory, automation, the wiki, the chat archive, and app controls."
      testId="advanced-settings"
    >
      <SettingsGroup title="Memory and skills">
        <OpenRow
          label="Agent memory"
          description="Facts and preferences that the agent keeps from one session to the next."
          open={memoryOpen}
          onToggle={() => setMemoryOpen((v) => !v)}
          testId="advanced-memory"
        >
          <MemoryPane embedded />
        </OpenRow>
        <OpenRow
          label="Skills and rules"
          description="Approve or reject the skills and rules that the agent proposes. Approved items load in the next session."
          open={skillsOpen}
          onToggle={() => setSkillsOpen((v) => !v)}
          testId="advanced-skills"
        >
          <SkillsPane embedded />
        </OpenRow>
      </SettingsGroup>

      <SettingsGroup title="Automation">
        <OpenRow
          label="Schedules"
          description="Send a prompt to the agent on a timer."
          open={schedulesOpen}
          onToggle={() => setSchedulesOpen((v) => !v)}
          testId="advanced-schedules"
        >
          <SchedulesPane />
        </OpenRow>
      </SettingsGroup>

      <HooksGroup />
      <WikiGroup />
      <ArchiveGroup />
      {bridges.selfDev ? <AppGroup selfDev={bridges.selfDev} restart={bridges.restart} /> : null}
    </SettingsPage>
  );
}
