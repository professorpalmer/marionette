import { useEffect, useState } from "react";
import { api, type Settings } from "../lib/api";
import { REASONING_LEVELS } from "../lib/reasoningSupport";
import { readSettingsSnapshot, writeSettingsSnapshot } from "./settingsSnapshot";
import { SettingsGroup, SettingsPage, SettingsRow } from "./SettingsLayout";
import Switch from "./SettingsSwitch";
import EncryptedSessions from "./EncryptedSessions";
import DeviceAccess from "./DeviceAccess";

/** Text settings that take a number, or 0 for no limit. Each one is an env value on the backend. */
type LimitKey =
  | "commandTimeout"
  | "maxPilotSteps"
  | "pilotToolBudget"
  | "maxOutputTokens"
  | "autoMaxTokens"
  | "workerTokenBudget";

type Limit = { key: LimitKey; label: string; help: string; fallback: string };

const FULL_AUTO_LIMIT: Limit = {
  key: "autoMaxTokens",
  label: "Full-auto token limit",
  help: "A full-auto run stops at this number of tokens. Enter 0 for no limit.",
  fallback: "500000",
};

const TURN_LIMITS: Limit[] = [
  {
    key: "commandTimeout",
    label: "Command timeout (seconds)",
    help: "Stops a shell command after this time. Enter 0 for no limit. A 15-minute ceiling still applies.",
    fallback: "120",
  },
  {
    key: "maxPilotSteps",
    label: "Model steps per message",
    help: "The pilot stops after this number of model rounds. Enter 0 for no limit.",
    fallback: "40",
  },
  {
    key: "pilotToolBudget",
    label: "Tool calls per turn",
    help: "The pilot stops after this number of tool calls in one turn. Enter 0 for no limit.",
    fallback: "25",
  },
  {
    key: "maxOutputTokens",
    label: "Reply length limit",
    help: "The maximum output tokens for one reply. Enter unlimited to let the provider decide.",
    fallback: "unlimited",
  },
];

const WORKER_LIMIT: Limit = {
  key: "workerTokenBudget",
  label: "Worker token limit",
  help: "One worker run stops at this number of tokens. Enter 0 for no limit. A value below 40000 changes to 250000.",
  fallback: "250000",
};

function errorText(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "object" && err !== null && "error" in err && typeof err.error === "string") return err.error;
  return "The setting did not save.";
}

/**
 * Settings > Safety: limits and guards for a run, the agent browser, and the
 * data on this computer. Each row reads and writes one key of /api/settings.
 */
export default function SafetySettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(() => readSettingsSnapshot());
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  // The row that last saved, and its result: the state slot shows it.
  const [result, setResult] = useState<{ key: string; ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let live = true;
    api.settings()
      .then((next) => {
        if (!live) return;
        setSettings(next);
        writeSettingsSnapshot(next);
        setLoadError("");
      })
      .catch(() => {
        if (live && !readSettingsSnapshot()) setLoadError("Settings did not load.");
      });
    return () => { live = false; };
  }, []);

  useEffect(() => {
    if (!result?.ok) return;
    const timer = setTimeout(() => setResult(null), 2000);
    return () => clearTimeout(timer);
  }, [result]);

  const save = async (key: keyof Settings, partial: Partial<Settings>) => {
    setSaving(true);
    try {
      const next = await api.updateSettings(partial);
      setSettings(next);
      writeSettingsSnapshot(next);
      setResult({ key, ok: true, text: "Saved" });
      window.dispatchEvent(new Event("harness-config-changed"));
    } catch (err: unknown) {
      setResult({ key, ok: false, text: errorText(err) });
    } finally {
      setSaving(false);
    }
  };

  const savedState = (key: string) => (result?.key === key && result.ok ? result.text : undefined);
  const errorLine = (key: string) => (result?.key === key && !result.ok
    ? <p role="alert" className="text-ui-10 text-risk">{result.text}</p>
    : undefined);

  if (!settings) {
    return (
      <SettingsPage title="Safety" testId="safety-settings">
        <p className="text-ui-12 text-faint">{loadError || "Loading settings..."}</p>
      </SettingsPage>
    );
  }

  const limitRow = (limit: Limit) => {
    const current = settings[limit.key] || limit.fallback;
    return (
      <SettingsRow
        key={limit.key}
        label={limit.label}
        description={limit.help}
        state={savedState(limit.key)}
        testId={`safety-${limit.key}`}
        control={(
          <input
            type="text"
            aria-label={limit.label}
            defaultValue={current}
            placeholder={limit.fallback}
            disabled={saving}
            onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }}
            onBlur={(e) => {
              const value = e.target.value.trim();
              if (value && value !== current) void save(limit.key, { [limit.key]: value });
            }}
            className="w-24 bg-panel border border-edge rounded px-2 py-0.5 text-ui-11 text-txt text-right font-mono focus:outline-none focus:border-accent disabled:opacity-50"
          />
        )}
      >
        {errorLine(limit.key)}
      </SettingsRow>
    );
  };

  const guardOn = settings.autoCommandGuard ?? true;
  const chromeOn = settings.browserRealProfile ?? false;
  const workerEffort = settings.swarm_reasoning_effort || "medium";

  return (
    <SettingsPage
      title="Safety"
      description="Limits and guards for a run, the agent browser, and the data on this computer. Changes apply on the next turn."
      testId="safety-settings"
    >
      <SettingsGroup title="Full-auto">
        <SettingsRow
          label="Block dangerous commands"
          description="In full-auto, Marionette blocks rm -rf, ssh, sudo, force-push, and pipe-to-shell commands. Interactive chat does not change."
          state={savedState("autoCommandGuard") ?? (guardOn ? "On" : "Off")}
          testId="safety-autoCommandGuard"
          control={(
            <Switch
              on={guardOn}
              label="Block dangerous commands"
              disabled={saving}
              onChange={(on) => { void save("autoCommandGuard", { autoCommandGuard: on }); }}
            />
          )}
        >
          {errorLine("autoCommandGuard")}
        </SettingsRow>
        {limitRow(FULL_AUTO_LIMIT)}
      </SettingsGroup>

      <SettingsGroup title="Limits for each message">
        {TURN_LIMITS.map(limitRow)}
      </SettingsGroup>

      <SettingsGroup title="Workers">
        {limitRow(WORKER_LIMIT)}
        <SettingsRow
          label="Worker reasoning"
          description="The default reasoning effort for swarm and implement workers. The Workers chip in the composer changes it for one session."
          state={savedState("swarm_reasoning_effort")}
          testId="safety-swarm_reasoning_effort"
          control={(
            <select
              aria-label="Worker reasoning"
              value={workerEffort}
              disabled={saving}
              onChange={(e) => {
                const next = REASONING_LEVELS.find((level) => level.value === e.target.value)?.value;
                if (next && next !== workerEffort) void save("swarm_reasoning_effort", { swarm_reasoning_effort: next });
              }}
              className="bg-panel border border-edge rounded px-1.5 py-0.5 text-ui-11 text-txt focus:outline-none focus:border-accent disabled:opacity-50"
            >
              {REASONING_LEVELS.map((level) => (
                <option key={level.value} value={level.value}>{level.label}</option>
              ))}
            </select>
          )}
        >
          {errorLine("swarm_reasoning_effort")}
        </SettingsRow>
      </SettingsGroup>

      <SettingsGroup title="Browser">
        <SettingsRow
          label="Use my Chrome sign-ins"
          description="Copies your Chrome cookies into the agent browser, so that it is signed in. On Windows, close Chrome if the copy fails."
          state={savedState("browserRealProfile") ?? (chromeOn ? "On" : "Off")}
          testId="safety-browserRealProfile"
          control={(
            <Switch
              on={chromeOn}
              label="Use my Chrome sign-ins"
              disabled={saving}
              onChange={(on) => { void save("browserRealProfile", { browserRealProfile: on }); }}
            />
          )}
        >
          {errorLine("browserRealProfile")}
        </SettingsRow>
      </SettingsGroup>

      <EncryptedSessions />
      <DeviceAccess />
    </SettingsPage>
  );
}
